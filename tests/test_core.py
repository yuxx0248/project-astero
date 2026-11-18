import unittest
from astero.core import Portal, DryRunAgent, NullSpeaker, State, TaskReceipt


class MusicSpy:
    def __init__(self):
        self.ducks = 0
        self.restores = 0

    def duck(self): self.ducks += 1
    def restore(self): self.restores += 1


class DeferredAgent:
    def submit(self, text, session_id):
        return TaskReceipt("accepted", "Task queued", "task-123")


class FailAgent:
    def submit(self, text, session_id):
        raise OSError("agent offline")


class SpeakSpy:
    def __init__(self): self.spoken = []
    def speak(self, text): self.spoken.append(text)


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.music = MusicSpy()
        self.speaker = SpeakSpy()

    def test_completed_flow_and_restore(self):
        p = Portal(DryRunAgent(), self.speaker, self.music)
        self.assertTrue(p.wake())
        r = p.submit_transcript("hello")
        self.assertEqual(r.status, "completed")
        self.assertEqual(p.state, State.FOLLOW_UP)
        self.assertEqual(len(self.speaker.spoken), 1)
        p.stop()
        self.assertEqual(self.music.restores, 1)

    def test_queued_not_spoken_as_finished(self):
        p = Portal(DeferredAgent(), self.speaker, self.music)
        p.wake()
        r = p.submit_transcript("long job")
        self.assertEqual(r.status, "accepted")
        self.assertEqual(self.speaker.spoken, [])

    def test_error_restores_music(self):
        p = Portal(FailAgent(), self.speaker, self.music)
        p.wake()
        with self.assertRaises(OSError):
            p.submit_transcript("check")
        self.assertEqual(p.state, State.IDLE)
        self.assertEqual(self.music.restores, 1)

    def test_empty_transcript(self):
        p = Portal(DryRunAgent(), self.speaker, self.music)
        p.wake()
        r = p.submit_transcript(" ")
        self.assertEqual(r.status, "empty")
        self.assertEqual(p.state, State.IDLE)

    def test_no_wake_while_processing(self):
        p = Portal(DryRunAgent(), self.speaker, self.music)
        p.wake()
        self.assertFalse(p.wake())


if __name__ == "__main__":
    unittest.main()
