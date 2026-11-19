from __future__ import annotations

import argparse
import json
import sys

from .acoustic_loop import DEFAULT_WAKE_STYLE, ValidationError, run_acoustic_loop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Astero local TTS-to-Echo acceptance loop")
    parser.add_argument("--text", required=True, help="known test command spoken by local TTS")
    parser.add_argument("--session", default="astero-acoustic-validation")
    parser.add_argument("--service-url", default="http://127.0.0.1:8766")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--wake-style", default=DEFAULT_WAKE_STYLE,
                        help="TTS guidance for the wake word only; use an empty string for plain TTS")
    args = parser.parse_args(argv)
    try:
        result = run_acoustic_loop(args.service_url, args.bridge_url, args.text,
                                   session_id=args.session, timeout_seconds=args.timeout,
                                   wake_style=args.wake_style)
    except (ValidationError, TimeoutError, ValueError) as exc:
        print(json.dumps({"completed": False, "error": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
