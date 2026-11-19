"""Local USB Echo voice loop: sherpa-onnx, Codex and Voice Bridge."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import contextmanager
from itertools import chain

from astero.audio.capture import AdbCapture
from astero.audio.pcm import PCMFrame, convert_frame
from astero.orchestrator import Orchestrator, OrchestratorConfig, TurnStatus
from astero.speech.asr import SherpaOnnxASR, ZipformerConfig
from astero.speech.endpoint import EndpointCollector, EndpointConfig
from astero.speech.voice_bridge import VoiceBridgeClient
from astero.agent.codex_voice import CodexConversation
from astero.speech.live_wake import Detected, SherpaKeyword
from astero.speech.live_vad import SileroVAD

ROOT = Path.home() / '.local/share/astero'


class SenseVoice:
    """Local multilingual command recognition; no audio leaves the host."""
    def __init__(self, model_dir: Path):
        import sherpa_onnx as s
        self.recognizer = s.OfflineRecognizer.from_sense_voice(
            model=str(model_dir / 'model.int8.onnx'), tokens=str(model_dir / 'tokens.txt'),
            num_threads=2, sample_rate=16000, provider='cpu', language='auto', use_itn=True,
        )

    def recognize(self, samples, sample_rate):
        stream = self.recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self.recognizer.decode_stream(stream)
        return stream.result.text


class ManualWake:
    """Explicit diagnostic mode; does not pretend to detect a keyword."""
    def feed(self, frame):
        return [Detected(frame.timestamp)]

    def reset(self):
        pass


def mono_frames(capture, channel, gain):
    import numpy as np
    for frame in capture.frames():
        raw = PCMFrame(frame.timestamp_ns / 1e9, frame.sequence, frame.sample_rate,
                       frame.channels, frame.sample_format, frame.pcm)
        mono = convert_frame(raw, channel=channel)
        if gain != 1:
            samples = np.frombuffer(mono.data, dtype='<i2').astype(np.float32) * gain
            data = np.clip(samples, -32768, 32767).astype('<i2').tobytes()
            from dataclasses import replace
            mono = replace(mono, data=data)
        yield mono


@contextmanager
def quiet_echo_speaker():
    """Temporarily disable the Dot's own amplifier during USB capture."""
    def command(value=None):
        args = ['adb', 'shell', 'tinymix', 'Ext_Speaker_Amp_Switch']
        if value is not None:
            args.append(value)
        return subprocess.run(args, capture_output=True, text=True, timeout=5, check=True).stdout
    state = command()
    if '>On' not in state and '>Off' not in state:
        raise RuntimeError('无法确认 Echo 扬声器功放状态')
    was_on = '>On' in state
    if was_on:
        command('Off')
    try:
        yield
    finally:
        if was_on:
            command('On')


@contextmanager
def paused_echo_mixer():
    """Keep the stock mixer stopped across turns, then restore its prior state."""
    def command(*args):
        return subprocess.run(['adb', 'shell', *args], capture_output=True,
                              text=True, timeout=5, check=True).stdout.strip()

    def wait_for(expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if command('getprop', 'init.svc.mixer') == expected:
                return
            time.sleep(0.1)
        raise RuntimeError(f'Echo mixer 未进入 {expected} 状态')

    state = command('getprop', 'init.svc.mixer')
    if state not in ('running', 'stopped'):
        raise RuntimeError(f'无法确认 Echo mixer 状态：{state!r}')
    try:
        if state == 'running':
            command('setprop', 'ctl.stop', 'mixer')
            wait_for('stopped')
        yield
    finally:
        if state == 'running':
            command('setprop', 'ctl.start', 'mixer')
            wait_for('running')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Astero 本机语音助手')
    parser.add_argument('--channel', type=int, default=3, choices=range(7))
    parser.add_argument('--gain', type=float, default=16.0, help='输入数字增益，默认 16')
    parser.add_argument('--show-transcript', action='store_true', help='测试时在终端显示识别文本')
    parser.add_argument('--once', action='store_true', help='处理一轮后退出')
    parser.add_argument('--manual', action='store_true', help='诊断：不等唤醒词，立即等待语音命令')
    args = parser.parse_args(argv)
    if not 0 < args.gain <= 100:
        parser.error('--gain must be greater than 0 and at most 100')
    kws = ROOT / 'models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'
    asr = ROOT / 'models/asr'
    helper = ROOT / 'bin/capture-bionic'
    try:
        wake = ManualWake() if args.manual else SherpaKeyword(kws, ROOT / 'models/astero-keywords.txt')
        vad_path = ROOT / 'models/silero_vad.onnx'
        cfg = ZipformerConfig(encoder=asr / 'encoder-epoch-99-avg-1.int8.onnx',
                              decoder=asr / 'decoder-epoch-99-avg-1.int8.onnx',
                              joiner=asr / 'joiner-epoch-99-avg-1.int8.onnx',
                              tokens=asr / 'tokens.txt')
        digest = hashlib.sha256(helper.read_bytes()).hexdigest()
        bridge = VoiceBridgeClient()
        bridge.health()
        with (SherpaOnnxASR(cfg, recognizer=SenseVoice(ROOT / 'models/sensevoice')) as recognizer,
              quiet_echo_speaker(), paused_echo_mixer(), quiet_echo_speaker()):
            while True:
                vad = SileroVAD(vad_path)
                endpoint = EndpointCollector(vad, EndpointConfig(pre_roll_seconds=0,
                                                                 pre_roll_buffer_seconds=0))
                controller = Orchestrator(wake, endpoint, recognizer, CodexConversation(), bridge,
                                          config=OrchestratorConfig(max_listen_seconds=86400,
                                                                    playback_timeout_seconds=180))
                with AdbCapture(helper, digest) as capture:
                    frames = mono_frames(capture, args.channel, args.gain)
                    first = next(frames)
                    print('正在等待语音命令；Ctrl-C 停止。' if args.manual else
                          '正在监听 Astero；Ctrl-C 停止。', flush=True)
                    result = controller.run_once(chain((first,), frames))
                if result.status is TurnStatus.COMPLETED:
                    print('一轮语音交互完成。', flush=True)
                else:
                    print(f'语音交互状态：{result.status.value} {result.error or ""}', file=sys.stderr, flush=True)
                if args.show_transcript and result.transcript is not None:
                    print('识别文本：' + result.transcript, flush=True)
                if args.show_transcript and result.reply is not None:
                    print('GPT 回复：' + result.reply, flush=True)
                if args.once or result.status is TurnStatus.CANCELLED:
                    return 0 if result.status is TurnStatus.COMPLETED else 1
                if result.bridge_may_still_play:
                    print('播放状态未知，已停止监听以避免自触发。', file=sys.stderr)
                    return 1
                time.sleep(0.2)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'启动失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
