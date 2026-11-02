-- Выполнять в help-desk-db после проверки отсутствия этих таблиц.
-- Существующие таблицы не удаляются и не заменяются.
CREATE TABLE tickets (
    id Utf8 NOT NULL,
    user_id Utf8,
    category Utf8,
    status Utf8,
    text Utf8,
    created_at Timestamp,
    updated_at Timestamp,
    PRIMARY KEY (id),
    INDEX tickets_by_user GLOBAL ON (user_id)
);

CREATE TABLE messages (
    id Utf8 NOT NULL,
    user_id Utf8 NOT NULL,
    ticket_id Utf8,
    role Utf8,
    text Utf8,
    model Utf8,
    tokens_in Uint64,
    tokens_out Uint64,
    latency_ms Uint32,
    created_at Timestamp,
    PRIMARY KEY (user_id, id)
);
