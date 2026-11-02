import unittest
from copy import deepcopy
from unittest.mock import Mock
from uuid import UUID

from src.ydb_tickets.index import create_ticket, list_my_tickets, mask_pii


class CreateTicketTests(unittest.TestCase):
    def test_invalid_arguments_do_not_access_database(self):
        pool = Mock()
        invalid = [
            ("", "bug", "Ошибка"),
            ("user\nother", "bug", "Ошибка"),
            ("user", "unknown", "Ошибка"),
            ("user", "bug", " "),
            ("user", "bug", "я" * (32 * 1024 + 1)),
            ("user", "bug", "\ud800"),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                create_ticket(pool, *args)
        pool.retry_operation_sync.assert_not_called()

    def test_parameters_are_masked_and_stable_between_sdk_retries(self):
        pool = Mock()
        session = Mock()
        calls = []

        def repeat_write(operation, **kwargs):
            for _ in range(2):
                operation(session)
                call = session.transaction.return_value.execute.call_args
                calls.append(deepcopy(call.args[1]))
                self.assertTrue(call.kwargs["commit_tx"])

        pool.retry_operation_sync.side_effect = repeat_write
        answer = create_ticket(
            pool, "test-user", "bug",
            "Не работает вход: test+tag@example.invalid, +7 (999) 123-45-67. "
            "Текст SQL: '; DROP TABLE tickets; --",
        )
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(str(UUID(answer["ticket_id"])), calls[0]["$id"])
        self.assertTrue(answer["created_at"].endswith("Z"))
        self.assertIn("[EMAIL], [PHONE]", calls[0]["$text"])
        self.assertIn("DROP TABLE", calls[0]["$text"])
        query = session.prepare.call_args.args[0]
        self.assertNotIn("test-user", query)
        self.assertNotIn("DROP TABLE", query)

    def test_database_error_is_not_reported_as_success(self):
        pool = Mock()
        session = Mock()
        session.transaction.return_value.execute.side_effect = RuntimeError("Ошибка записи YDB")
        pool.retry_operation_sync.side_effect = lambda operation, **kwargs: operation(session)
        with self.assertRaisesRegex(RuntimeError, "Ошибка записи YDB"):
            create_ticket(pool, "test-user", "docs", "Вопрос по инструкции")

    def test_masks_common_contacts_without_hiding_ticket_reference(self):
        self.assertEqual(
            mask_pii("Написать a@example.invalid или b@example.invalid. "
                     "Телефоны: 89991234567; +7 999 765 43 21."),
            "Написать [EMAIL] или [EMAIL]. Телефоны: [PHONE]; [PHONE].",
        )
        value = "Заявка 550e8400-e29b-41d4-a716-446655440000"
        self.assertEqual(mask_pii(value), value)


class ListMyTicketsTests(unittest.TestCase):
    def setUp(self):
        self.pool = Mock()
        self.session = Mock()
        self.pool.retry_operation_sync.side_effect = lambda operation, **kwargs: operation(self.session)

    def set_rows(self, rows):
        self.session.transaction.return_value.execute.return_value = [Mock(rows=rows)]

    def test_reads_only_parameterized_user_and_formats_timestamp(self):
        self.set_rows([{
            "id": "test-ticket", "status": "open", "category": "bug",
            "text": "Сломался принтер", "created_at": 1_000_001,
        }])
        result = list_my_tickets(self.pool, "TEST-USER")
        self.assertEqual(result[0]["created_at"], "1970-01-01T00:00:01.000001Z")
        self.assertEqual(set(result[0]), {"id", "status", "category", "text", "created_at"})
        self.assertEqual(self.session.transaction.return_value.execute.call_args.args[1], {"$user_id": "test-user"})
        query = self.session.prepare.call_args.args[0]
        self.assertIn("WHERE user_id = $user_id", query)
        self.assertNotIn("test-user", query)

    def test_empty_list_is_success_but_database_error_propagates(self):
        self.set_rows([])
        self.assertEqual(list_my_tickets(self.pool, "test-user"), [])
        self.session.transaction.return_value.execute.side_effect = RuntimeError("YDB недоступна")
        with self.assertRaisesRegex(RuntimeError, "YDB недоступна"):
            list_my_tickets(self.pool, "test-user")

    def test_invalid_user_does_not_access_database(self):
        for value in [None, "", "a\nb"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                list_my_tickets(self.pool, value)
        self.pool.retry_operation_sync.assert_not_called()

    def test_overflow_and_corrupt_rows_fail_explicitly(self):
        self.set_rows([{}] * 101)
        with self.assertRaisesRegex(ValueError, "постраничная"):
            list_my_tickets(self.pool, "test-user")
        self.set_rows([{"created_at": None}])
        with self.assertRaisesRegex(RuntimeError, "created_at"):
            list_my_tickets(self.pool, "test-user")


if __name__ == "__main__":
    unittest.main()
