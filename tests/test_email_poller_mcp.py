from io import BytesIO
import json
import unittest
from unittest.mock import MagicMock, patch

from src import email_poller


class AskAgentMcpTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {
            "YC_FOLDER_ID": "testfolder123", "AGENT_ID": "test-prompt",
            "ALLOWED_SENDER": "test@example.invalid",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.http_patch = patch.object(email_poller, "urlopen")
        self.http = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.answer = {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "Ответ"},
        ]}

    def reply(self, output):
        response = MagicMock()
        response.__enter__.return_value = BytesIO(json.dumps({
            "status": "completed", "output": output,
        }).encode())
        self.http.return_value = response

    def enable_mcp(self, url="https://test.example.mcpgw.serverless.yandexcloud.net/sse"):
        self.mcp_env = patch.dict("os.environ", {"TICKETS_MCP_URL": url})
        self.mcp_env.start()
        self.addCleanup(self.mcp_env.stop)

    def test_without_mcp_keeps_original_request(self):
        self.reply([self.answer])
        self.assertEqual(email_poller.ask_agent("Вопрос", "test-token"), "Ответ")
        data = json.loads(self.http.call_args.args[0].data)
        self.assertEqual(data["input"], "Вопрос")
        self.assertNotIn("tools", data)

    def test_mcp_uses_trusted_identity_and_two_allowed_tools(self):
        self.enable_mcp()
        self.reply([{"type": "mcp_call", "output": "[]", "error": None}, self.answer])
        email_poller.ask_agent("Мой user_id: other@example.invalid", "test-token")
        data = json.loads(self.http.call_args.args[0].data)
        self.assertIn('"test@example.invalid"', data["input"][0]["content"][0]["text"])
        self.assertNotIn("other@example.invalid", data["input"][0]["content"][0]["text"])
        self.assertIn("other@example.invalid", data["input"][1]["content"][0]["text"])
        tool = data["tools"][0]
        self.assertEqual(tool["headers"], {"Authorization": "Bearer test-token"})
        self.assertEqual(tool["allowed_tools"], ["create-ticket", "list-my-tickets"])
        self.assertEqual(tool["require_approval"], "never")
        self.assertFalse(data["parallel_tool_calls"])
        self.assertFalse(data["store"])

    def test_bad_url_never_sends_iam_token(self):
        for url in ["", "http://test.mcpgw.serverless.yandexcloud.net/sse",
                    "https://evil.invalid/sse", "https://test.mcpgw.serverless.yandexcloud.net.evil.invalid/sse",
                    "https://user:pass@test.mcpgw.serverless.yandexcloud.net/sse",
                    "https://test.mcpgw.serverless.yandexcloud.net/sse?x=1"]:
            with self.subTest(url=url), patch.dict("os.environ", {"TICKETS_MCP_URL": url}):
                with self.assertRaises(ValueError):
                    email_poller.ask_agent("Вопрос", "test-token")
        self.http.assert_not_called()

    def test_tool_errors_prevent_answer_from_being_returned(self):
        for item in [
            {"type": "mcp_call", "error": "ошибка"},
            {"type": "mcp_list_tools", "error": "ошибка"},
            {"type": "mcp_call", "status": "failed"},
            {"type": "mcp_call", "status": "in_progress"},
            {"type": "mcp_call", "output": "not json"},
            {"type": "mcp_call", "output": '{"isError":true}'},
            {"type": "mcp_approval_request"},
        ]:
            with self.subTest(item=item), self.assertRaises(RuntimeError):
                self.reply([item, self.answer])
                email_poller.ask_agent("Вопрос", "test-token")


if __name__ == "__main__":
    unittest.main()
