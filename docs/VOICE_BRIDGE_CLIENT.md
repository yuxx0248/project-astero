# Astero Voice Bridge 客户端

`astero.speech.voice_bridge.VoiceBridgeClient` 将回复文字交给本机 Voice Bridge，并等待任务进入终态。HTTP 202 和 `queued` 只表示已入队；只有 `completed` 表示 Bridge 的播放器进程成功退出。`failed` 会抛出 `VoiceBridgeTaskError`，轮询期限耗尽会抛出 `VoiceBridgeTimeoutError`。

## 配置和使用

默认服务地址是 `http://127.0.0.1:8765`。可通过 `VoiceBridgeConfig` 显式设置环回地址、整轮超时、轮询间隔、单次 HTTP 请求超时，以及可选的语音和风格：

```python
from astero.speech.voice_bridge import VoiceBridgeClient, VoiceBridgeConfig

client = VoiceBridgeClient(VoiceBridgeConfig(
    base_url="http://127.0.0.1:8765",
    timeout_seconds=120,
    poll_interval_seconds=0.25,
    request_timeout_seconds=5,
))
result = client.speak("你好，我已经准备好了。")
print(result.status, result.elapsed_seconds)
```

客户端只允许 HTTP loopback URL，因为 Bridge 没有鉴权且只应在本机使用。`health()` 验证 Bridge 返回 `status: ok` 且 `tts_ready: true`。`speak()` 提交 `/announce`，验证 202 响应和任务 ID，再查询 `/announcements/{id}` 直至 `completed` 或 `failed`。任务 ID 仅允许 ASCII 字母、数字、下划线和连字符，以防 URL 分隔符或百分号编码改变轮询路径。异常信息不包含播报文字。

Voice Bridge 没有单任务取消端点。因此请求或轮询超时、轮询期间服务中断时，已提交的播报可能仍在队列中或正在播放；异常会指出此不确定性和已知任务 ID。不要把这类异常当作 Bridge 已取消。

## 验证记录

使用前按 `/home/zuohang/agents/SERVICES.md` 检查状态。2026-11-18，本机 `GET http://127.0.0.1:8765/health` 返回 `{"status":"ok","tts_ready":true}`。替身 HTTP 服务测试覆盖 202→排队/生成/播放/完成、失败、503、无效响应、超时、轮询中断和任务 ID 路径校验；`python3 -m unittest tests.test_voice_bridge -v` 的 9 项测试全部通过。

真实服务已播报短语“Astero语音桥接测试，请听一下。”，Bridge 任务 `1b23d25c578d4fbdaea9fca3ba35e969` 最终状态为 `completed`，耗时约 7.03 秒。这证明 TTS 和播放器流程报告成功；用户是否实际听见尚待确认。
