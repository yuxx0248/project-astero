"""Pure control-plane state machine; audio model integrations are adapters."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class State(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    FOLLOW_UP = "follow_up"


class Music(Protocol):
    def duck(self) -> None: ...
    def restore(self) -> None: ...


class Speaker(Protocol):
    def speak(self, text: str) -> None: ...


@dataclass(frozen=True)
class TaskReceipt:
    status: str
    message: str
    task_id: str | None = None


class Agent(Protocol):
    def submit(self, transcript: str, session_id: str) -> TaskReceipt: ...


class NullMusic:
    def duck(self) -> None:
        pass

    def restore(self) -> None:
        pass


class NullSpeaker:
    """Dry-run only. Does not claim audible output."""
    def speak(self, text: str) -> None:
        print(f"[TTS DRY RUN] {text}")


class DryRunAgent:
    """No network or tool execution."""
    def submit(self, transcript: str, session_id: str) -> TaskReceipt:
        return TaskReceipt("completed", f"Dry-run received: {transcript}", "demo-task")


class Portal:
    def __init__(self, agent: Agent, speaker: Speaker, music: Music, session_id: str = "local"):
        self.agent, self.speaker, self.music = agent, speaker, music
        self.session_id = session_id
        self.state = State.IDLE

    def wake(self) -> bool:
        if self.state not in {State.IDLE, State.FOLLOW_UP}:
            return False
        self.music.duck()
        self.state = State.LISTENING
        return True

    def submit_transcript(self, transcript: str) -> TaskReceipt:
        if self.state != State.LISTENING:
            raise RuntimeError("Portal not listening")
        if not transcript.strip():
            self.stop()
            return TaskReceipt("empty", "No speech detected")
        self.state = State.PROCESSING
        try:
            receipt = self.agent.submit(transcript, self.session_id)
            # 'accepted' is NOT task completion. Never narrate it as success.
            if receipt.status == "completed":
                self.state = State.SPEAKING
                self.speaker.speak(receipt.message)
                self.state = State.FOLLOW_UP
            elif receipt.status in {"accepted", "running", "confirmation_required"}:
                self.state = State.FOLLOW_UP
            else:
                self.stop()
            return receipt
        except Exception:
            self.stop()
            raise

    def follow_up(self) -> bool:
        return self.wake()

    def stop(self) -> None:
        self.music.restore()
        self.state = State.IDLE
