import subprocess
import unittest
from unittest.mock import patch

from astero.agent.codex_voice import CodexConversation, CodexVoiceConfig


class CodexVoiceTests(unittest.TestCase):
    def test_voice_disables_unneeded_tools_without_changing_model_or_reasoning(self):
        backend = CodexConversation(mcp_server_names=("node_repl", "other-server"))
        args = backend.command()
        self.assertIn('mcp_servers.node_repl.enabled=false', args)
        self.assertIn('mcp_servers.other-server.enabled=false', args)
        self.assertNotIn("--ignore-user-config", args)
        self.assertNotIn("--model", args)
        self.assertFalse(any("reasoning_effort" in arg for arg in args))
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        for feature in ("apps", "plugins", "shell_snapshot", "shell_tool"):
            self.assertEqual(args[args.index(feature) - 1], "--disable")

    @patch("astero.agent.codex_voice.subprocess.run")
    def test_context_and_long_answers_are_preserved(self, run):
        context = '历史：暗号是蓝色月亮。当前问题：详细解释为什么会下雨。'
        answer = '水蒸气凝结为小水滴。' * 100
        run.return_value = subprocess.CompletedProcess([], 0, "  " + answer + "\n", "")
        result = CodexConversation(mcp_server_names=()).respond(context, "s")
        self.assertEqual(result, answer)
        self.assertTrue(run.call_args.kwargs["input"].endswith(context))
        self.assertNotIn("env", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    @patch("astero.agent.codex_voice.subprocess.run")
    def test_failure_does_not_expose_provider_output(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "private", "secret")
        with self.assertRaises(RuntimeError) as raised:
            CodexConversation(mcp_server_names=()).respond("hello", "s")
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))
        run.side_effect = subprocess.TimeoutExpired("codex", 1)
        with self.assertRaises(RuntimeError):
            CodexConversation(mcp_server_names=()).respond("hello", "s")

    def test_configuration_rejects_bad_timeouts(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                CodexVoiceConfig(timeout_seconds=value)


if __name__ == "__main__":
    unittest.main()
