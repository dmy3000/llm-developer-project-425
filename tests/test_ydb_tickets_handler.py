import base64
from contextlib import redirect_stdout
from io import StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.ydb_tickets import index


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {
            "TICKETS_USER_ID": "test-user",
            "YDB_ENDPOINT": "grpcs://example.invalid:2135",
            "YDB_DATABASE": "/test-db",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.driver_patch = patch.object(index.ydb, "Driver")
        self.driver = self.driver_patch.start()
        self.addCleanup(self.driver_patch.stop)
        self.pool_patch = patch.object(index.ydb, "SessionPool")
        self.pool = self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)
        self.context = SimpleNamespace(token={"access_token": "test-token-not-real"})
        self.payload = {"user_id": "test-user", "category": "bug", "text": "Не работает принтер"}

    def http(self, body):
        return {"httpMethod": "POST", "body": json.dumps(body)}

    def test_direct_create_returns_acknowledged_result(self):
        answer = {"ticket_id": "test-id", "created_at": "2026-10-30T00:00:00Z"}
        with patch.object(index, "create_ticket", return_value=answer) as create:
            result = index.handle({"action": "create-ticket", **self.payload}, self.context)
        self.assertEqual(result, answer)
        create.assert_called_once_with(
            self.pool.return_value.__enter__.return_value, **self.payload,
        )

    def test_mcp_raw_arguments_dispatch_both_tools(self):
        with patch.object(index, "create_ticket", return_value={"ticket_id": "test"}) as create:
            self.assertEqual(index.handle(self.payload, self.context), {"ticket_id": "test"})
            create.assert_called_once()
        with patch.object(index, "list_my_tickets", return_value=[]) as read:
            self.assertEqual(index.handle({"user_id": "TEST-USER"}, self.context), [])
            self.assertEqual(read.call_args.kwargs, {"user_id": "test-user"})

    def test_http_and_base64_responses_are_json(self):
        for encoded in [False, True]:
            event = self.http({"action": "list-my-tickets", "user_id": "test-user"})
            if encoded:
                event["body"] = base64.b64encode(event["body"].encode()).decode()
                event["isBase64Encoded"] = True
            with self.subTest(encoded=encoded), patch.object(index, "list_my_tickets", return_value=[]):
                result = index.handle(event, self.context)
                self.assertEqual(result["statusCode"], 200)
                self.assertEqual(json.loads(result["body"]), [])

    def test_invalid_arguments_never_connect_to_database(self):
        invalid = [
            None, [], {}, {"action": None},
            {"action": "delete-ticket", "user_id": "test-user"},
            {**self.payload, "extra": True},
            {**self.payload, "text": ""},
            {**self.payload, "category": "unknown"},
            {**self.payload, "text": "я" * (32 * 1024 + 1)},
        ]
        for event in invalid:
            with self.subTest(event_type=type(event).__name__), self.assertRaises(ValueError):
                index.handle(event, self.context)
        self.driver.assert_not_called()

    def test_invalid_http_is_400_and_other_method_is_405(self):
        for body in ['{', '[]', '{"user_id":"test-user","user_id":"other"}', 'x' * 100_000]:
            result = index.handle({"httpMethod": "POST", "body": body}, self.context)
            self.assertEqual(result["statusCode"], 400)
        self.assertEqual(index.handle({"httpMethod": "GET"}, self.context)["statusCode"], 405)
        self.driver.assert_not_called()

    def test_invalid_base64_and_utf8_are_rejected(self):
        for body in ["***", base64.b64encode(b"\xff").decode()]:
            result = index.handle({
                "httpMethod": "POST", "body": body, "isBase64Encoded": True,
            }, self.context)
            self.assertEqual(result["statusCode"], 400)
        self.driver.assert_not_called()

    def test_foreign_user_is_rejected_before_database_access(self):
        with self.assertRaises(PermissionError):
            index.handle({**self.payload, "user_id": "other-user"}, self.context)
        result = index.handle(self.http({"user_id": "other-user"}), self.context)
        self.assertEqual(result["statusCode"], 403)
        self.driver.assert_not_called()

    def test_missing_token_fails_without_exposing_details(self):
        with redirect_stdout(StringIO()):
            result = index.handle(self.http(self.payload), SimpleNamespace(token=None))
        self.assertEqual(result["statusCode"], 500)
        self.driver.assert_not_called()

    def test_database_failure_is_not_success_and_does_not_leak_error(self):
        log = StringIO()
        with patch.object(index, "create_ticket", side_effect=RuntimeError("sensitive-error-value")):
            with redirect_stdout(log):
                result = index.handle(self.http(self.payload), self.context)
            self.assertEqual(result["statusCode"], 500)
            self.assertNotIn("sensitive-error-value", str(result) + log.getvalue())
            with redirect_stdout(log), self.assertRaisesRegex(RuntimeError, "Не удалось подтвердить"):
                index.handle(self.payload, self.context)

    def test_response_limit_is_explicit_error(self):
        with patch.object(index, "list_my_tickets", return_value=[{"text": "я" * 600_000}]):
            with redirect_stdout(StringIO()):
                result = index.handle(self.http({"user_id": "test-user"}), self.context)
        self.assertEqual(result["statusCode"], 500)


if __name__ == "__main__":
    unittest.main()
