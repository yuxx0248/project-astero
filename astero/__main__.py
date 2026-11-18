"""CLI dry-run demonstrating event routing, not a microphone service."""
import argparse
import json
from pathlib import Path

from .core import Portal, DryRunAgent, NullSpeaker, NullMusic


def main() -> None:
    parser = argparse.ArgumentParser(description="Astero voice portal scaffold")
    parser.add_argument("--config", default="configs/astero.example.json")
    parser.add_argument("--demo", action="store_true", help="Run safe dry-run without hardware")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if not args.demo:
        parser.error("Real-time audio is not integrated yet. Use --demo.")
    portal = Portal(DryRunAgent(), NullSpeaker(), NullMusic())
    print(f"{config['assistant_name']}: demo wake (no microphone)")
    portal.wake()
    receipt = portal.submit_transcript("Check my kernel logs")
    print(f"Agent status={receipt.status} task_id={receipt.task_id}")
    portal.stop()


if __name__ == "__main__":
    main()
