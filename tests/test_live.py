import struct
import unittest
from unittest.mock import patch

from astero.audio.capture import AudioFrame
from astero.live import CodexConversation, mono_frames, paused_echo_mixer


class LiveTests(unittest.TestCase):
    def test_capture_frames_are_converted_and_gain_saturates(self):
        class Capture:
            def frames(self):
                sample = bytes([0, 0, 64])  # +2^22 in signed 24-bit
                yield AudioFrame(1_000_000_000, 16000, 9, 's24_3le', 1, sample * 9)
        out = list(mono_frames(Capture(), 6, 4))[0]
        self.assertEqual(out.timestamp, 1.0)
        self.assertEqual(struct.unpack('<h', out.data)[0], 32767)

    def test_codex_failure_is_not_a_reply(self):
        with patch('astero.live.subprocess.run') as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ''
            with self.assertRaises(RuntimeError):
                CodexConversation().respond('hello', 'local')

    def test_mixer_stays_stopped_between_turns_and_restores_on_error(self):
        state = {'value': 'running', 'changes': []}
        def run(args, **kwargs):
            if 'getprop' in args:
                output = state['value']
            else:
                state['value'] = 'stopped' if 'ctl.stop' in args else 'running'
                state['changes'].append(state['value'])
                output = ''
            return type('Result', (), {'stdout': output})()
        with patch('astero.live.subprocess.run', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                with paused_echo_mixer():
                    self.assertEqual(state['value'], 'stopped')
                    self.assertEqual(state['changes'], ['stopped'])
                    raise ValueError('interrupted')
        self.assertEqual(state['value'], 'running')
        self.assertEqual(state['changes'], ['stopped', 'running'])


if __name__ == '__main__':
    unittest.main()
