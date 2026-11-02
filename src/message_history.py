"""Историю и телеметрию записывает poller; модель не управляет этими полями."""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from uuid import UUID, uuid4

import ydb

# Используем те же проверки и маскирование, что и инструмент заявок.
if __package__:
    from .ydb_tickets.index import mask_pii, _validated_user_id
else:
    from ydb_tickets.index import mask_pii, _validated_user_id


RECENT_QUERY = """
DECLARE $user_id AS Utf8;
SELECT role, text, created_at FROM messages
WHERE user_id = $user_id
ORDER BY created_at DESC, id DESC LIMIT 10;
"""

SAVE_QUERY = """
DECLARE $id AS Utf8;
DECLARE $user_id AS Utf8;
DECLARE $ticket_id AS Utf8?;
DECLARE $role AS Utf8;
DECLARE $text AS Utf8;
DECLARE $model AS Utf8?;
DECLARE $tokens_in AS Uint64;
DECLARE $tokens_out AS Uint64;
DECLARE $latency_ms AS Uint32;
DECLARE $created_at AS Timestamp;
UPSERT INTO messages
(id, user_id, ticket_id, role, text, model, tokens_in, tokens_out, latency_ms, created_at)
VALUES ($id, $user_id, $ticket_id, $role, $text, $model,
        $tokens_in, $tokens_out, $latency_ms, $created_at);
"""

CHECK_LINK_QUERY = """
DECLARE $user_id AS Utf8;
DECLARE $incoming_id AS Utf8;
DECLARE $ticket_id AS Utf8;
SELECT id FROM tickets WHERE id = $ticket_id AND user_id = $user_id;
SELECT id FROM messages WHERE user_id = $user_id AND id = $incoming_id AND role = "user";
"""

LINK_QUERY = """
DECLARE $user_id AS Utf8;
DECLARE $incoming_id AS Utf8;
DECLARE $ticket_id AS Utf8;
UPDATE messages SET ticket_id = $ticket_id
WHERE user_id = $user_id AND id = $incoming_id;
"""


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError("Некорректный идентификатор сообщения или заявки")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise ValueError("Некорректный идентификатор сообщения или заявки") from None
    return value


class MessageHistory:
    def __init__(self, pool, user_id):
        self.pool = pool
        self.user_id = _validated_user_id(user_id)

    def _retry(self, operation):
        return self.pool.retry_operation_sync(
            operation, retry_settings=ydb.RetrySettings(max_retries=2, idempotent=True),
        )

    def recent(self):
        def read(session):
            settings = ydb.BaseRequestSettings().with_timeout(10)
            query = session.prepare(RECENT_QUERY, settings=settings)
            return session.transaction(ydb.SerializableReadWrite()).execute(
                query, {"$user_id": self.user_id}, commit_tx=True, settings=settings,
            )[0].rows

        rows = self._retry(read)
        if len(rows) > 10:
            raise RuntimeError("YDB вернула больше 10 реплик")
        history = []
        for row in reversed(rows):
            if (row.get("role") not in {"user", "agent"}
                    or type(row.get("created_at")) is not int or row["created_at"] < 0):
                raise RuntimeError("Некорректная реплика в YDB")
            history.append({"role": row["role"], "text": mask_pii(row.get("text"))})
        return history

    def _parameters(self, role, text, *, model=None, tokens_in=0, tokens_out=0,
                    latency_ms=0, ticket_id=None):
        for value, bits in ((tokens_in, 64), (tokens_out, 64), (latency_ms, 32)):
            if type(value) is not int or not 0 <= value < 2**bits:
                raise ValueError("Некорректная телеметрия ответа")
        if model is not None and (not isinstance(model, str) or not model.strip()
                                  or len(model) > 512 or any(char.isspace() for char in model)):
            raise ValueError("Некорректная модель в телеметрии")
        now = datetime.now(timezone.utc)
        delta = now - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return {
            "$id": str(uuid4()), "$user_id": self.user_id, "$role": role,
            "$text": mask_pii(text), "$model": model,
            "$tokens_in": tokens_in, "$tokens_out": tokens_out, "$latency_ms": latency_ms,
            "$ticket_id": _uuid(ticket_id) if ticket_id is not None else None,
            "$created_at": (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds,
        }

    def save_user(self, text):
        parameters = self._parameters("user", text)

        def write(session):
            settings = ydb.BaseRequestSettings().with_timeout(10)
            query = session.prepare(SAVE_QUERY, settings=settings)
            session.transaction(ydb.SerializableReadWrite()).execute(
                query, parameters, commit_tx=True, settings=settings,
            )

        self._retry(write)
        return parameters["$id"]

    def save_agent(self, incoming_id, result):
        incoming_id = _uuid(incoming_id)
        parameters = self._parameters(
            "agent", result["text"], model=result["model"], tokens_in=result["tokens_in"],
            tokens_out=result["tokens_out"], latency_ms=result["latency_ms"],
            ticket_id=result["ticket_id"],
        )
        if parameters["$model"] is None:
            raise ValueError("Не задана модель ответа")

        def write(session):
            settings = ydb.BaseRequestSettings().with_timeout(10)
            save = session.prepare(SAVE_QUERY, settings=settings)
            link_parameters = {"$user_id": self.user_id, "$incoming_id": incoming_id,
                               "$ticket_id": parameters["$ticket_id"]}
            check = link = None
            if parameters["$ticket_id"] is not None:
                check = session.prepare(CHECK_LINK_QUERY, settings=settings)
                link = session.prepare(LINK_QUERY, settings=settings)
            with session.transaction(ydb.SerializableReadWrite()) as transaction:
                if check is not None:
                    found = transaction.execute(check, link_parameters, settings=settings)
                    if len(found[0].rows) != 1 or len(found[1].rows) != 1:
                        raise RuntimeError("Не подтверждена принадлежность заявки или исходного сообщения")
                    transaction.execute(link, link_parameters, settings=settings)
                transaction.execute(save, parameters, commit_tx=True, settings=settings)

        self._retry(write)
        return parameters["$id"]


@contextmanager
def open_history(iam_token):
    def config(name):
        value = os.environ.get(name, "").strip()
        if not value:
            raise ValueError(f"Не задана настройка {name}")
        return value

    endpoint, database = config("YDB_ENDPOINT"), config("YDB_DATABASE")
    user_id = _validated_user_id(config("ALLOWED_SENDER"))
    if not endpoint.startswith("grpcs://") or not database.startswith("/"):
        raise ValueError("Некорректные настройки YDB")
    if not isinstance(iam_token, str) or not iam_token.strip() or any(c in iam_token for c in "\r\n"):
        raise ValueError("Некорректный IAM-токен")
    with ydb.Driver(endpoint=endpoint, database=database,
                    credentials=ydb.AccessTokenCredentials(iam_token)) as driver:
        driver.wait(timeout=10, fail_fast=True)
        with ydb.SessionPool(driver, size=1) as pool:
            yield MessageHistory(pool, user_id)
