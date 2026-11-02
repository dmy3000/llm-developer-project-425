"""Закрытая учебная Cloud Function для создания и чтения заявок в YDB."""

import base64
import binascii
import json
import os
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import ydb


MAX_TEXT_BYTES = 64 * 1024
CATEGORIES = frozenset({"bug", "docs", "feature", "access"})
MAX_TICKETS = 100
MAX_REQUEST_BYTES = MAX_TEXT_BYTES + 4096
MAX_RESPONSE_BYTES = 1024 * 1024
EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[\w-]+\.)+[\w-]+")
PHONE_PATTERN = re.compile(r"(?<!\w)\+?[78](?:[\s().-]*[0-9]){10}(?!\w)")

CREATE_TICKET_QUERY = """
DECLARE $id AS Utf8;
DECLARE $user_id AS Utf8;
DECLARE $category AS Utf8;
DECLARE $text AS Utf8;
DECLARE $created_at AS Timestamp;

UPSERT INTO tickets (id, user_id, category, status, text, created_at, updated_at)
VALUES ($id, $user_id, $category, "open", $text, $created_at, $created_at);
"""

LIST_TICKETS_QUERY = """
DECLARE $user_id AS Utf8;
SELECT id, status, category, text, created_at
FROM tickets VIEW tickets_by_user
WHERE user_id = $user_id
ORDER BY created_at DESC, id DESC
LIMIT 101;
"""


def _required_text(value, name, max_bytes):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Поле {name} должно быть непустой строкой")
    value = value.strip()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"Поле {name} содержит некорректный Unicode") from None
    if size > max_bytes or "\x00" in value:
        raise ValueError(f"Поле {name} превышает лимит или содержит нулевой символ")
    return value


def mask_pii(text):
    """Маскирует обычные email и телефоны РФ с префиксом 7/8; не все виды PII."""
    text = _required_text(text, "text", MAX_TEXT_BYTES)
    return PHONE_PATTERN.sub("[PHONE]", EMAIL_PATTERN.sub("[EMAIL]", text))


def _validated_user_id(user_id):
    user_id = _required_text(user_id, "user_id", 254).casefold()
    if any(char.isspace() or ord(char) < 32 for char in user_id):
        raise ValueError("Поле user_id не должно содержать пробелы и управляющие символы")
    return user_id


def create_ticket(pool, user_id, category, text):
    """Вызывающий код передаёт user_id отправителя, полученный вне модели."""
    user_id = _validated_user_id(user_id)
    category = _required_text(category, "category", 32)
    if category not in CATEGORIES:
        raise ValueError("Допустимые категории: bug, docs, feature, access")
    text = mask_pii(text)

    # Один UUID и одна дата на все повторы SDK внутри этого вызова.
    ticket_id = str(uuid4())
    created_at = datetime.now(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = created_at - epoch
    timestamp_us = (
        (elapsed.days * 86400 + elapsed.seconds) * 1_000_000
        + elapsed.microseconds
    )
    parameters = {
        "$id": ticket_id,
        "$user_id": user_id,
        "$category": category,
        "$text": text,
        "$created_at": timestamp_us,
    }

    def write(session):
        settings = ydb.BaseRequestSettings().with_timeout(10)
        prepared = session.prepare(
            CREATE_TICKET_QUERY, settings=settings,
        )
        session.transaction(ydb.SerializableReadWrite()).execute(
            prepared, parameters, commit_tx=True, settings=settings,
        )

    pool.retry_operation_sync(
        write, retry_settings=ydb.RetrySettings(max_retries=2, idempotent=True),
    )
    return {
        "ticket_id": ticket_id,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
    }


def list_my_tickets(pool, user_id):
    """Читает заявки идентификатора, который передал доверенный вызывающий код."""
    user_id = _validated_user_id(user_id)

    def read(session):
        settings = ydb.BaseRequestSettings().with_timeout(10)
        prepared = session.prepare(LIST_TICKETS_QUERY, settings=settings)
        return session.transaction(ydb.SerializableReadWrite()).execute(
            prepared, {"$user_id": user_id}, commit_tx=True, settings=settings,
        )[0].rows

    rows = pool.retry_operation_sync(
        read, retry_settings=ydb.RetrySettings(max_retries=2, idempotent=True),
    )
    # ponytail: 100 tickets per user; add pagination before raising this ceiling.
    if len(rows) > MAX_TICKETS:
        raise ValueError("Больше 100 заявок: требуется постраничная выдача")

    tickets = []
    for row in rows:
        timestamp = row["created_at"]
        if type(timestamp) is not int or timestamp < 0:
            raise RuntimeError("В заявке некорректное поле created_at")
        ticket = {name: row[name] for name in ("id", "status", "category", "text")}
        for name, value in ticket.items():
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(f"В заявке отсутствует строковое поле {name}")
        created_at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=timestamp,
        )
        ticket["created_at"] = created_at.isoformat().replace("+00:00", "Z")
        tickets.append(ticket)
    return tickets


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON содержит повторяющиеся поля")
        result[key] = value
    return result


def _parse_request(event):
    if not isinstance(event, dict):
        raise ValueError("Запрос должен быть JSON-объектом")
    payload = event
    if "httpMethod" in event:
        body = event.get("body")
        encoded = event.get("isBase64Encoded", False)
        if not isinstance(body, str) or type(encoded) is not bool:
            raise ValueError("Некорректное тело HTTP-запроса")
        if len(body) > (MAX_REQUEST_BYTES * 4 // 3 + 4):
            raise ValueError("Тело HTTP-запроса превышает лимит")
        try:
            raw = base64.b64decode(body, validate=True) if encoded else body.encode("utf-8")
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("Тело HTTP-запроса превышает лимит")
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
        except (UnicodeError, binascii.Error, json.JSONDecodeError, RecursionError):
            raise ValueError("Ожидался корректный JSON в UTF-8") from None
    if not isinstance(payload, dict):
        raise ValueError("Аргументы должны быть JSON-объектом")

    if "action" in payload:
        action = payload["action"]
    else:
        # MCP Hub передаёт непосредственно аргументы выбранного инструмента.
        action = "create-ticket" if "category" in payload or "text" in payload else "list-my-tickets"
    if action == "create-ticket":
        required = {"user_id", "category", "text"}
    elif action == "list-my-tickets":
        required = {"user_id"}
    else:
        raise ValueError("Неизвестное действие")
    if not required <= payload.keys() or payload.keys() - required - {"action"}:
        raise ValueError("Отсутствуют обязательные поля или переданы лишние поля")
    arguments = {"user_id": _validated_user_id(payload["user_id"])}
    if action == "create-ticket":
        category = _required_text(payload["category"], "category", 32)
        if category not in CATEGORIES:
            raise ValueError("Допустимые категории: bug, docs, feature, access")
        arguments.update(category=category, text=_required_text(payload["text"], "text", MAX_TEXT_BYTES))
    return action, arguments


def _required_env(name):
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Не задана настройка {name}")
    return value.strip()


def _response(value, is_http, status=200):
    body = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Ответ превышает 1 МиБ: требуется постраничная выдача")
    if is_http:
        return {
            "statusCode": status,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "isBase64Encoded": False,
            "body": body,
        }
    return value


def handle(event, context):
    """Прямой вызов/MCP возвращает JSON; HTTP получает стандартную обёртку."""
    is_http = isinstance(event, dict) and "httpMethod" in event
    if is_http and event["httpMethod"] != "POST":
        return _response({"error": "Разрешён только POST"}, True, 405)
    try:
        action, arguments = _parse_request(event)
    except ValueError as error:
        if is_http:
            return _response({"error": str(error)}, True, 400)
        raise

    try:
        # Однопользовательский учебный стенд: идентификатор задаётся вне LLM.
        allowed_user = _validated_user_id(_required_env("TICKETS_USER_ID"))
        if arguments["user_id"] != allowed_user:
            raise PermissionError("Доступ к заявкам этого пользователя запрещён")
        token = getattr(context, "token", None)
        access_token = token.get("access_token") if isinstance(token, dict) else None
        if (not isinstance(access_token, str) or not access_token.strip()
                or any(char in access_token for char in "\r\n")):
            raise RuntimeError("В контексте нет корректного IAM-токена")
        endpoint = _required_env("YDB_ENDPOINT")
        database = _required_env("YDB_DATABASE")
        if not endpoint.startswith("grpcs://") or not database.startswith("/"):
            raise RuntimeError("Некорректные настройки подключения к YDB")
        with ydb.Driver(
            endpoint=endpoint, database=database,
            credentials=ydb.AccessTokenCredentials(access_token),
        ) as driver:
            driver.wait(timeout=10, fail_fast=True)
            with ydb.SessionPool(driver, size=1) as pool:
                if action == "create-ticket":
                    result = create_ticket(pool, **arguments)
                else:
                    result = list_my_tickets(pool, **arguments)
        return _response(result, is_http)
    except PermissionError:
        if is_http:
            return _response({"error": "Доступ к заявкам этого пользователя запрещён"}, True, 403)
        raise
    except Exception as error:
        # Не выводим аргументы, токены, письма или диагностический текст SDK.
        print(json.dumps({"event": "ydb_tickets_failed", "error_type": type(error).__name__}))
        message = "Не удалось подтвердить результат операции с заявками"
        if is_http:
            return _response({"error": message}, True, 500)
        raise RuntimeError(message) from None
