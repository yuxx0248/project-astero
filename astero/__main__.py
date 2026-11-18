"""Minimal offline CLI for checking the AST-08 dependency boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from astero.agent.conversation import MockConversationBackend
from astero.orchestrator import TurnStatus, manual_trigger


class _DemoPlayback:
    def announce(self, text: str) -> str:
        self.text = text
        return "demo-task"

    def get_status(self, task_id: object) -> str:
        return "completed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Astero offline checks")
    parser.add_argument("--config", default="configs/astero.example.json")
    parser.add_argument("--demo", action="store_true", help="run the original core scaffold demo")
    parser.add_argument("--manual-text", help="send text through the mock LLM and mock playback")
    args = parser.parse_args(argv)
    if args.manual_text is not None:
        playback = _DemoPlayback()
        result = manual_trigger(args.manual_text, MockConversationBackend(), playback)
        print(json.dumps({
            "mode": "offline_mock",
            "status": result.status.value,
            "transcript": result.transcript,
            "reply": result.reply,
            "error": result.error,
        }, ensure_ascii=False))
        return 0 if result.status is TurnStatus.COMPLETED else 1
    if args.demo:
        try:
            from .core import Portal, DryRunAgent, NullSpeaker, NullMusic
        except ImportError:
            parser.error("the original core scaffold is not available in this checkout")
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        portal = Portal(DryRunAgent(), NullSpeaker(), NullMusic())
        print(f"{config['assistant_name']}: demo wake (no microphone)")
        portal.wake()
        receipt = portal.submit_transcript("Check my kernel logs")
        print(f"Agent status={receipt.status} task_id={receipt.task_id}")
        portal.stop()
        return 0
    parser.error("provide --demo or --manual-text; live voice components are not composed by this CLI")
    return 2


if __name__ == "__main__":
    sys.exit(main())
