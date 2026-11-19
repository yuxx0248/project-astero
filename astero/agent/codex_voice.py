"""Short spoken answers through the existing signed-in Codex provider.

Voice turns do not need app/plugin discovery, MCP processes, shell snapshots,
or shell tools. Disable those per invocation, while preserving the user's
configured model, reasoning effort, provider, credentials, and environment.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import subprocess
import tomllib


@dataclass(frozen=True, slots=True)
class CodexVoiceConfig:
    timeout_seconds: float = 120.0
    model: str | None = None
    reasoning_effort: str | None = None

    def __post_init__(self):
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("model must be nonempty when supplied")
        if self.reasoning_effort not in {None, "low", "high"}:
            raise ValueError("reasoning_effort must be None, low, or high")


class CodexConversation:
    """Conversation-only Codex invocation; durable context is owned by Astero."""

    instruction = (
        "你是语音助手 Astero。使用用户的语言回答，适合语音播报。"
        "直接回答；简单问题只说必要答案，确认记住时简短确认即可，无需重复整句。"
        "复杂问题或用户要求展开时，保留必要解释，不要为了简短省略关键内容。"
        "只回答问题，不运行命令、不访问文件或调用工具。用户说：\n"
    )

    def __init__(self, config: CodexVoiceConfig | None = None, *,
                 mcp_server_names: tuple[str, ...] | None = None):
        self.config = config or CodexVoiceConfig()
        self._mcp_names = (_configured_mcp_names() if mcp_server_names is None
                           else tuple(mcp_server_names))
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in self._mcp_names):
            raise ValueError("MCP server names must support dotted CLI config overrides")

    def command(self) -> list[str]:
        args = ["codex", "exec", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "-C", "/tmp"]
        for feature in ("apps", "plugins", "shell_snapshot", "shell_tool"):
            args.extend(["--disable", feature])
        for name in self._mcp_names:
            args.extend(["-c", f"mcp_servers.{name}.enabled=false"])
        if self.config.model is not None:
            args.extend(["--model", self.config.model])
        if self.config.reasoning_effort is not None:
            args.extend(["-c", f'model_reasoning_effort="{self.config.reasoning_effort}"'])
        return args + ["-"]

    def respond(self, text: str, session_id: str) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be nonempty")
        try:
            result = subprocess.run(
                self.command(), input=self.instruction + text, text=True,
                capture_output=True, timeout=self.config.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("GPT agent 无法启动或超时") from exc
        if result.returncode or not result.stdout.strip():
            # Do not copy stderr, prompts or credentials into service errors.
            raise RuntimeError(f"GPT agent 失败（退出码 {result.returncode}）")
        return result.stdout.strip()


def _configured_mcp_names() -> tuple[str, ...]:
    codex_dir = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    config = codex_dir / "config.toml"
    if not config.exists():
        return ()
    # Inspect only server names; never log configuration values or credentials.
    try:
        with config.open("rb") as source:
            value = tomllib.load(source)
        return tuple(value.get("mcp_servers", {}))
    except (OSError, ValueError) as exc:
        raise RuntimeError("cannot read Codex voice configuration") from exc
