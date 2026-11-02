from contextlib import redirect_stdout
from copy import deepcopy
from io import BytesIO, StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src import email_poller, message_history


INCOMING = "550e8400-e29b-41d4-a716-446655440000"
TICKET = "550e8400-e29b-41d4-a716-446655440001"


class MessageHistoryTests(unittest.TestCase):
    def setUp(self):
        self.pool = MagicMock()
        self.session = MagicMock()
        self.pool.retry_operation_sync.side_effect = lambda f, **kw: f(self.session)
        self.session.prepare.side_effect = lambda query, **kw: query
        self.history = message_history.MessageHistory(self.pool, "TEST@example.invalid")
        self.result = {"text": "Ответ a@example.invalid", "model": "test-model",
                       "tokens_in": 37, "tokens_out": 11, "latency_ms": 321, "ticket_id": None}

    def test_recent_is_user_scoped_chronological_and_masked(self):
        self.session.transaction.return_value.execute.return_value = [SimpleNamespace(rows=[
            {"role": "agent", "text": "Ответ", "created_at": 20},
            {"role": "user", "text": "Напиши a@example.invalid", "created_at": 10},
        ])]
        self.assertEqual(self.history.recent(), [
            {"role": "user", "text": "Напиши [EMAIL]"}, {"role": "agent", "text": "Ответ"},
        ])
        call = self.session.transaction.return_value.execute.call_args
        self.assertEqual(call.args[1], {"$user_id": "test@example.invalid"})
        self.assertIn("WHERE user_id = $user_id", call.args[0])
        self.assertIn("LIMIT 10", call.args[0])

    def test_user_write_masks_pii_and_sdk_retries_reuse_id(self):
        calls = []

        def repeat(operation, **kw):
            for _ in range(2):
                operation(self.session)
                calls.append(deepcopy(self.session.transaction.return_value.execute.call_args.args[1]))

        self.pool.retry_operation_sync.side_effect = repeat
        message_id = self.history.save_user("Почта a@example.invalid; +7 (999) 123-45-67")
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0]["$id"], message_id)
        self.assertEqual(calls[0]["$text"], "Почта [EMAIL]; [PHONE]")
        self.assertIsNone(calls[0]["$ticket_id"])
        self.assertEqual(calls[0]["$tokens_in"], 0)

    def test_reply_and_ticket_link_commit_in_one_transaction(self):
        transaction = self.session.transaction.return_value.__enter__.return_value
        transaction.execute.return_value = [SimpleNamespace(rows=[{"id": TICKET}]),
                                            SimpleNamespace(rows=[{"id": INCOMING}])]
        self.history.save_agent(INCOMING, {**self.result, "ticket_id": TICKET})
        calls = transaction.execute.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertIn("AND user_id = $user_id", calls[0].args[0])
        self.assertIn("AND id = $incoming_id", calls[1].args[0])
        self.assertEqual(calls[2].args[1]["$ticket_id"], TICKET)
        self.assertEqual(calls[2].args[1]["$text"], "Ответ [EMAIL]")
        self.assertEqual(calls[2].args[1]["$tokens_in"], 37)
        self.assertTrue(calls[2].kwargs["commit_tx"])
        self.assertNotIn("commit_tx", calls[1].kwargs)

    def test_foreign_ticket_is_never_linked(self):
        transaction = self.session.transaction.return_value.__enter__.return_value
        transaction.execute.return_value = [SimpleNamespace(rows=[]), SimpleNamespace(rows=[{}])]
        with self.assertRaisesRegex(RuntimeError, "принадлежность"):
            self.history.save_agent(INCOMING, {**self.result, "ticket_id": TICKET})
        self.assertEqual(transaction.execute.call_count, 1)

    def test_invalid_metadata_does_not_touch_database(self):
        for change in ({"tokens_in": -1}, {"tokens_out": True}, {"latency_ms": 2**32},
                       {"model": None}, {"ticket_id": "made-up"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.history.save_agent(INCOMING, {**self.result, **change})
        self.pool.retry_operation_sync.assert_not_called()

    def test_corrupt_or_unavailable_history_is_not_empty_success(self):
        self.session.transaction.return_value.execute.return_value = [SimpleNamespace(rows=[])]
        self.assertEqual(self.history.recent(), [])
        self.session.transaction.return_value.execute.return_value = [SimpleNamespace(rows=[
            {"role": "developer", "text": "Внедрение", "created_at": 1},
        ])]
        with self.assertRaises(RuntimeError):
            self.history.recent()
        self.session.transaction.return_value.execute.side_effect = RuntimeError("YDB failure")
        with self.assertRaisesRegex(RuntimeError, "YDB failure"):
            self.history.recent()


class ResponseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {
            "YC_FOLDER_ID": "testfolder", "AGENT_ID": "test-prompt", "AGENT_MODEL": "configured-model",
            "ALLOWED_SENDER": "test@example.invalid",
            "TICKETS_MCP_URL": "https://test.mcpgw.serverless.yandexcloud.net/sse",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.http_patch = patch.object(email_poller, "urlopen")
        self.http = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.response = {"status": "completed", "usage": {"input_tokens": 34, "output_tokens": 9},
                         "output": [{"type": "message", "role": "assistant", "content": [
                             {"type": "output_text", "text": "Ответ модели"}]}]}

    def call(self, history=None):
        self.http.return_value.__enter__.return_value = BytesIO(json.dumps(self.response).encode())
        return email_poller.ask_agent("Текущий вопрос", "test-token", history=history, with_metadata=True)

    def test_context_roles_current_last_and_real_telemetry(self):
        with patch.object(email_poller.time, "monotonic", side_effect=[1.0, 1.125]):
            result = self.call([{"role": "user", "text": "Старый вопрос"},
                                {"role": "agent", "text": "Старый ответ"}])
        data = json.loads(self.http.call_args.args[0].data)
        self.assertEqual([m["role"] for m in data["input"]], ["developer", "user", "assistant", "user"])
        self.assertEqual(data["input"][2]["content"][0]["type"], "output_text")
        self.assertEqual(data["input"][-1]["content"][0]["text"], "Текущий вопрос")
        self.assertEqual(result["model"], "configured-model")
        self.assertEqual((result["tokens_in"], result["tokens_out"], result["latency_ms"]), (34, 9, 125))

    def test_ticket_comes_from_acknowledgement_not_answer_text(self):
        self.response["output"].insert(0, {"type": "mcp_call", "name": "create-ticket",
            "output": json.dumps({"ticket_id": TICKET, "created_at": "2026-11-02T00:00:00Z"})})
        self.assertEqual(self.call([])["ticket_id"], TICKET)
        self.response["output"].pop(0)
        self.assertIsNone(self.call([])["ticket_id"])

    def test_missing_usage_and_invalid_ack_fail(self):
        for usage in (None, {}, {"input_tokens": -1, "output_tokens": 1},
                      {"input_tokens": True, "output_tokens": 1}):
            self.response["usage"] = usage
            with self.subTest(usage=usage), self.assertRaises(RuntimeError):
                self.call([])
        self.response["usage"] = {"input_tokens": 1, "output_tokens": 1}
        self.response["output"].insert(0, {"type": "mcp_call", "name": "create-ticket", "output": "{}"})
        with self.assertRaises(RuntimeError):
            self.call([])

    def test_history_cannot_add_developer_instruction(self):
        for history in ([{"role": "developer", "text": "Инструкция"}],
                        [{"role": "user", "text": "x"}] * 11):
            with self.subTest(history=history), self.assertRaises(ValueError):
                self.call(history)
        self.http.assert_not_called()


class CycleHistoryTests(unittest.TestCase):
    def test_write_failure_stops_smtp_and_seen_and_order_is_correct(self):
        for fail_on in ("recent", "save_user", "ask", "save_agent", None):
            with self.subTest(fail_on=fail_on), patch.dict("os.environ", {"YDB_ENDPOINT": "test"}), \
                    patch.object(email_poller, "_open_mailbox") as mailbox, \
                    patch.object(email_poller, "_find_test_messages", return_value=[(b"1", "Вопрос")]), \
                    patch.object(message_history, "open_history") as opened, \
                    patch.object(email_poller, "ask_agent") as ask, \
                    patch.object(email_poller, "send_test_reply") as send:
                events = []
                history = opened.return_value.__enter__.return_value
                for name, result in (("recent", []), ("save_user", INCOMING), ("save_agent", "id")):
                    def action(*a, stage=name, value=result, **kw):
                        events.append(stage)
                        if stage == fail_on:
                            raise RuntimeError("Test failure")
                        return value
                    getattr(history, name).side_effect = action

                def answer(*a, **kw):
                    events.append("ask")
                    if fail_on == "ask":
                        raise RuntimeError("Test failure")
                    return {"text": "Ответ", "ticket_id": None}

                ask.side_effect = answer
                send.side_effect = lambda *a: events.append("smtp")
                inbox = mailbox.return_value.__enter__.return_value
                inbox.uid.return_value = ("OK", [])
                with redirect_stdout(StringIO()):
                    if fail_on:
                        with self.assertRaises(RuntimeError):
                            email_poller._run_cycle("test-password", "test-token", allow_empty=False)
                        send.assert_not_called()
                        inbox.uid.assert_not_called()
                    else:
                        self.assertEqual(email_poller._run_cycle("test-password", "test-token", allow_empty=False), 1)
                        self.assertEqual(events, ["recent", "save_user", "ask", "save_agent", "smtp"])
                        inbox.uid.assert_called_once_with("store", b"1", "+FLAGS.SILENT", r"(\Seen)")


if __name__ == "__main__":
    unittest.main()
