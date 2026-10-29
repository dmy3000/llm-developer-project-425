import os
import imaplib
import json
import smtplib
import ssl
from contextlib import contextmanager
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid, parseaddr
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MAX_MESSAGE_BYTES = 64 * 1024

def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Не задана переменная окружения: {name}")
    return value

def is_test_message(sender, subject):
    sender = parseaddr(sender)[1]
    allowed_sender = required_env("ALLOWED_SENDER")
    test_subject = required_env("TEST_SUBJECT")

    return (
        sender.strip().casefold() == allowed_sender.casefold()
        and subject.strip() == test_subject
    )

@contextmanager
def _open_mailbox(password, *, readonly):
    host = required_env("IMAP_HOST")
    user = required_env("IMAP_USER")
    allowed_sender = required_env("ALLOWED_SENDER")
    test_subject = required_env("TEST_SUBJECT")
    if not password:
        raise ValueError("Не задан пароль приложения")

    for value in (allowed_sender, test_subject):
        if not value.isascii() or any(char in value for char in '\r\n\x00"\\'):
            raise ValueError("Для тестового поиска нужны ASCII-значения без кавычек, обратных слешей и переносов строк")

    with imaplib.IMAP4_SSL(
        host, 993, ssl_context=ssl.create_default_context(), timeout=15
    ) as mailbox:
        mailbox.login(user, password)
        status, data = mailbox.select("INBOX", readonly=readonly)
        if status != "OK":
            raise RuntimeError("Не удалось открыть INBOX")
        yield mailbox


def _find_test_messages(mailbox):
    allowed_sender = required_env("ALLOWED_SENDER")
    test_subject = required_env("TEST_SUBJECT")
    status, data = mailbox.uid(
        "search", None, "UNSEEN",
        "FROM", f'"{allowed_sender}"',
        "SUBJECT", f'"{test_subject}"',
    )
    if status != "OK":
        raise RuntimeError("Не удалось найти тестовые письма")

    matched = []
    for uid in data[0].split():
        if not uid.isdigit():
            raise RuntimeError("Сервер вернул некорректный UID письма")
        status, parts = mailbox.uid(
            "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])"
        )
        if status != "OK":
            raise RuntimeError("Не удалось получить заголовки письма")

        headers = [part[1] for part in parts if isinstance(part, tuple)]
        if len(headers) != 1:
            raise RuntimeError("Сервер вернул неожиданный ответ с заголовками")
        message = BytesParser(policy=policy.default).parsebytes(
            headers[0], headersonly=True
        )
        if len(message.get_all("From", [])) != 1 or len(message.get_all("Subject", [])) != 1:
            raise ValueError("У письма отсутствует или повторяется From либо Subject")

        if is_test_message(str(message["From"]), str(message["Subject"])):
            # Дополнительный байт позволяет обнаружить превышение лимита.
            status, parts = mailbox.uid(
                "fetch", uid, f"(BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)"
            )
            if status != "OK":
                raise RuntimeError("Не удалось получить тестовое письмо")
            contents = [part[1] for part in parts if isinstance(part, tuple)]
            if len(contents) != 1 or not isinstance(contents[0], bytes):
                raise RuntimeError("Сервер вернул неожиданный ответ с письмом")
            if len(contents[0]) > MAX_MESSAGE_BYTES:
                raise ValueError("Тестовое письмо превышает лимит 64 КиБ")

            full_message = BytesParser(policy=policy.default).parsebytes(contents[0])
            matched.append((uid, extract_plain_text(full_message)))

    return matched


def read_test_messages(password):
    with _open_mailbox(password, readonly=True) as mailbox:
        return [text for uid, text in _find_test_messages(mailbox)]


def check_imap(password):
    return len(read_test_messages(password))


class _HTMLTextParser(HTMLParser):
    BLOCK_TAGS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "blockquote", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored_tag = None

    def handle_starttag(self, tag, attrs):
        if self.ignored_tag is not None:
            return
        if tag in {"head", "script", "style"}:
            self.ignored_tag = tag
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.ignored_tag is not None:
            if tag == self.ignored_tag:
                self.ignored_tag = None
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.ignored_tag is None:
            self.parts.append(data)


def extract_plain_text(message):
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        raise ValueError("В письме нет текстовой части text/plain или text/html")

    text = body.get_content().strip()
    if body.get_content_type() == "text/html":
        parser = _HTMLTextParser()
        parser.feed(text)
        parser.close()
        lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
        text = "\n".join(line for line in lines if line)
    if not text:
        raise ValueError("Текст письма пустой")

    return text


def ask_agent(text, iam_token):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Не задан текст запроса к модели")
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("Текст запроса превышает лимит 64 КиБ")
    if not isinstance(iam_token, str) or not iam_token.strip() or any(char in iam_token for char in "\r\n"):
        raise ValueError("Не задан корректный IAM-токен")

    folder_id = required_env("YC_FOLDER_ID")
    agent_id = required_env("AGENT_ID")
    if not folder_id.isascii() or not folder_id.isalnum():
        raise ValueError("Некорректный идентификатор каталога")

    request = Request(
        "https://ai.api.cloud.yandex.net/v1/responses",
        data=json.dumps({
            "prompt": {"id": agent_id},
            "input": text,
            "max_output_tokens": 500,
        }, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {iam_token}",
            "OpenAI-Project": folder_id,
            "Content-Type": "application/json",
            "x-data-logging-enabled": "false",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read(1024 * 1024 + 1)
    except HTTPError as error:
        raise RuntimeError(f"Responses API отклонил запрос: HTTP {error.code}") from None
    if len(raw) > 1024 * 1024:
        raise RuntimeError("Ответ Responses API превышает лимит размера")

    result = json.loads(raw)
    if result.get("status") != "completed" or result.get("error") is not None:
        raise RuntimeError("Модель не завершила формирование ответа")
    texts = [
        part["text"]
        for item in result["output"]
        if item["type"] == "message" and item.get("role") == "assistant"
        for part in item["content"]
        if part["type"] == "output_text"
    ]
    answer = "\n".join(texts).strip()
    if not answer:
        raise RuntimeError("Модель не вернула текстовый ответ")
    return answer


def send_test_reply(text, password):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Нельзя отправить пустой ответ")
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("Ответ превышает лимит 64 КиБ")
    if not isinstance(password, str) or not password.strip():
        raise ValueError("Не задан пароль приложения")

    host = required_env("SMTP_HOST")
    port = int(required_env("SMTP_PORT"))
    user = required_env("SMTP_USER")
    mailbox = required_env("HELPDESK_MAILBOX")
    recipient = required_env("ALLOWED_SENDER")
    subject = required_env("TEST_SUBJECT")

    if host != "smtp.mail.ru" or port != 465:
        raise ValueError("Для теста разрешён только smtp.mail.ru:465 с TLS")
    if user.casefold() != mailbox.casefold():
        raise ValueError("SMTP_USER должен совпадать с HELPDESK_MAILBOX")
    for address in (user, mailbox, recipient):
        if (not address.isascii() or parseaddr(address)[1] != address
                or address.count("@") != 1 or any(char.isspace() for char in address)):
            raise ValueError("Некорректный почтовый адрес в настройках")

    message = EmailMessage()
    message["From"] = mailbox
    message["To"] = recipient
    message["Subject"] = f"Re: {subject}"
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=mailbox.rsplit("@", 1)[1])
    message.set_content(text)

    with smtplib.SMTP_SSL(
        host, port, context=ssl.create_default_context(), timeout=15
    ) as smtp:
        smtp.login(user, password)
        refused = smtp.send_message(message, from_addr=mailbox, to_addrs=[recipient])
        if refused:
            raise RuntimeError("SMTP-сервер отклонил получателя тестового ответа")

def _run_cycle(password, iam_token, *, allow_empty):
    with _open_mailbox(password, readonly=False) as mailbox:
        messages = _find_test_messages(mailbox)
        if not messages and allow_empty:
            return 0
        if len(messages) != 1:
            raise ValueError(
                "Для теста нужно ровно одно непрочитанное тестовое письмо"
            )

        uid, text = messages[0]
        answer = ask_agent(text, iam_token)
        send_test_reply(answer, password)
        status, data = mailbox.uid("store", uid, "+FLAGS.SILENT", r"(\Seen)")
        if status != "OK":
            raise RuntimeError(
                "Ответ принят SMTP-сервером, но письмо не удалось отметить прочитанным. "
                "Не повторяй отправку: сначала проверь почту и отметь исходное письмо вручную."
            )
    return 1


def run_test_cycle(password, iam_token):
    _run_cycle(password, iam_token, allow_empty=False)


def handle(event, context):
    # Cloud Functions передаёт токен прикреплённого сервисного аккаунта.
    token = getattr(context, "token", None)
    if not isinstance(token, dict):
        raise ValueError("В контексте функции нет IAM-токена сервисного аккаунта")
    iam_token = token.get("access_token")
    if (not isinstance(iam_token, str) or not iam_token.strip()
            or any(char in iam_token for char in "\r\n")):
        raise ValueError("В контексте функции нет корректного IAM-токена")

    # Значение подставляет платформа из Lockbox при запуске версии функции.
    password = required_env("IMAP_PASSWORD")
    processed = _run_cycle(password, iam_token, allow_empty=True)
    print(json.dumps({"processed": processed}))
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"processed": processed}),
    }
