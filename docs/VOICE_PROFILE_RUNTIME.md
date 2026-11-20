# Cassandra 音色的运行配置

Astero 常驻服务默认选择仓库音色 `cassandra`。它是用户暂定采用的 B 英式成熟低女声，以同一参考音频通过官方 Qwen3-TTS Base 模型生成中文或英文。详情见 [音色档案](VOICE_PROFILE.md)。

## 启动

独立克隆播报后端 `astero-voice.service` 使用本机环境 `~/.local/share/astero/voice-design-venv/` 和模型 `~/.local/share/astero/models/voice-base/`。模型权重在仓库外；合成参考音频由 Voice Bridge 共享包提供。后端绑定 `127.0.0.1:8767`，常驻加载模型及音色参考；原有 `127.0.0.1:8765` VoiceBridge 仍供其他用途使用。

在独立环境、Base 模型和仓库 systemd 单元已安装后：

```sh
systemctl --user start astero-voice.service
systemctl --user restart astero-local.service
```

`astero-local.service` 显式传递 `--voice-profile cassandra`，并声明对克隆后端的启动依赖。手动运行也可以明确指定：

```sh
~/.local/share/astero/venv/bin/python -m astero.service \
  --port 8766 --channel 6 --wake-secondary-channel 5 --end-silence 0.8 \
  --listen --session-id local-voice --voice-profile cassandra
```

不要同时启动两个 Astero 采集进程。使用 HTTP 服务前先核对共享服务清单和健康检查。

## 防止音色静默回退

`--voice-profile` 支持 `cassandra` 和 `bridge-default`。未提供 `--voice-bridge-url` 时，Cassandra 使用端口 8767，`bridge-default` 使用旧端口 8765。URL 可显式覆盖，但 Cassandra 模式要求后端 `/health` 的 `voice_profile` 必须是 `cassandra`，否则健康检查降级、禁止开始新一轮收音。每次提交播报前会再次验证音色，并在 `/announce` 中明确传递 `voice=cassandra`；不会因为服务重启或 URL 配错而静默使用 Vivian。

Astero `/health` 返回 `voice_profile` 和原有 `dependencies.tts` 状态。每轮录音归档 metadata 与 `tts_request` 调试数据也记录所选 profile，以便追溯。

若需明确退回旧音色，可停止已有采集进程后运行同一命令，将尾部替换为：

```sh
--voice-profile bridge-default --voice-bridge-url http://127.0.0.1:8765
```

这是操作人员明确选择旧音色，不是自动故障回退。

## 速度和验收边界

当前独立后端采用 CPU float32、非流式克隆合成。中文试听约 4–5 秒音频需要 26–33 秒生成，明显慢于旧量化 CustomVoice；固定音色不等于完成低延迟优化。常驻加载避免逐轮重新加载模型，但不能消除合成本身的成本。

本次将常驻服务播放等待上限设为 300 秒，仍可能遇到显式超时；超时不会取消后端已接收的任务。后端必须报告截断或播放失败，不能把未完整播放当作完成。运行状态和真实语音验收以部署后的检查为准，代码配置并不单独证明已部署。

## 本机部署验证 — 2026-11-18

已启用并启动 `astero-voice.service`，随后重启 `astero-local.service`。8767 的健康回执确认 `cassandra` 和仓库参考 SHA-256；8766 回执确认 `ready=true`、`voice_profile=cassandra`、`listener.listening=true`。原有 8765 / 8080 服务未改配置。

只包含本次音色相关改动的干净副本中，全量 156 项测试通过；工作区其它并行改动不纳入这次提交。

向 8767 显式提交 B 音色的“你好，我在。”，观察到 generating → playing → completed。合成 13.67 秒、播放器 1.81 秒、总计 15.47 秒；音频 1.76 秒、EOS 完整结束、无 PCM 溢出。独立本地 ASR 回读与台词一致。这是克隆播报后端与运行配置验证，不是新增一轮唤醒至回答的声学 E2E 测试，也不证明物理扬声器听感已人工验收。

运行证据：`~/.local/share/astero/benchmarks/voice-profile-b-acceptance/`；合成音频及阶段记录：`~/.local/share/astero/tts/de3159a05a9543f88036d1edc4cfc574/`。这些运行记录不入 Git。
