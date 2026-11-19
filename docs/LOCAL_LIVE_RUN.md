# 本机 Echo 语音回路

## AST-10 常驻服务

日常唤醒使用服务的 `--listen` 模式：启动后自动开启语音轮次，回答完成、无语音或等待唤醒超时后重新进入监听。固定 session 保存多轮上下文。以下命令用于本机已验收的通道 6：

```sh
/home/zuohang/.local/share/astero/venv/bin/python -m astero.service --port 8766 --channel 6 --wake-secondary-channel 5 --end-silence 0.8 --listen --session-id local-voice
```

本机用户服务模板是 `systemd/astero-local.service`，使用本仓库与已有 venv；安装至 `~/.config/systemd/user/` 后可通过 `systemctl --user enable --now astero-local.service` 管理。不要与手工启动的同端口进程或旧 `astero.live` 同时运行，以免争用 Echo 采集。

当前关键词检测使用主通道 6 与辅助通道 5，共享同一模型并维护独立的重叠窗口；ASR 仍使用主通道。`--wake-secondary-channel` 可调整辅助通道，设为与主通道相同可恢复单通道。`--end-silence` 默认 0.8 秒，较长句中停顿可按需调高。调试证据见 [唤醒与延迟调试](AST-10_WAKE_LATENCY_TUNING_2026-11-18.md)。

`GET http://127.0.0.1:8766/health` 的 `ready` 只表示依赖就绪；实际是否等待唤醒要看 listener 状态。省略 `--listen` 时仅提供按请求启动的单轮 API，必须 `POST /v1/turns` 才会采音：之前声学验收完成后无法直接喊醒的原因就在这里。常驻模式会串行开启新轮次，轮次之间存在设备重新打开的短暂间隔；播放回答时不接受新唤醒，尚不支持打断。

按用户当前调试要求，AST-10 保存每次采集收到的完整多通道音频、派生音频和关联日志，位置为 `~/.local/share/astero/archives/`，不设 TTL、不自动清理。SQLite 保存会话、任务、记忆、知识和耗时事件。日志可通过 `journalctl --user -u astero-local.service` 查询。说单独的 Astero，稍停再说问题；真人发音识别率仍需校准。

## 旧版独立诊断入口

下文描述 `astero.live` 的早期运行方式；其中内存处理、不落盘和无持久会话的行为不适用于上面的 AST-10 服务。

实验路径：Echo Dot 2 经 USB ADB 输出 9 通道 PCM → 本机 sherpa-onnx 关键词检测、Silero VAD 和 SenseVoice ASR → 已登录的 Codex GPT agent → 本机 Voice Bridge → 电脑默认扬声器。转录文本会发送给已登录的 GPT 服务；麦克风音频在本机内存中处理，不默认写文件。默认执行一次只读、无持久会话的 Codex 命令，提示模型只回答而不运行工具；并非通用系统操作代理。

## 本机运行

2026-11-18 在 Fedora ARM64 上配置了 Python 3.12 环境 `/home/zuohang/.local/share/astero/venv`，安装 `sherpa-onnx==2.0.1`、NumPy；Voice Bridge 需在本机运行且健康。Echo 已 root 并通过 USB ADB 连接。首次运行前先查 `/home/zuohang/agents/SERVICES.md`、检查 `adb devices`、`adb shell getprop init.svc.mixer`、`codex login status` 和 `curl -fsS http://127.0.0.1:8765/health`。不要将 ADB 密钥、转录或录音放进仓库。

```sh
/home/zuohang/.local/share/astero/venv/bin/python -m astero.live
```

等待输出“正在监听 Astero”；说单独的 Astero，稍停后说问题。`Ctrl-C` 停止。`--once` 完成或失败一轮后退出；仅排错时可加 `--show-transcript` 在终端显示当轮识别文字和回复。`--manual --once` 可跳过唤醒词，仅诊断后半段，不能当作唤醒成功。`--channel` 调整麦克风通道（0–6，当前机器默认 3），`--gain` 调整数字增益（默认 16）。这些是实测初始值，未完成多房间、远场或误唤醒校准。

采集独占 PCM 设备，程序在整个监听会话内停止 Echo 的原厂 mixer 和关闭 Echo 自身功放，避免每轮结束时重复切换；退出时尝试恢复原状态。2026-11-18 使用多轮无语音超时并抽样检查，观察到 mixer 保持 stopped、功放保持 Off；用户随后确认当时没有杂音。若断电、USB 拔出或进程被强制杀死，无法保证自动恢复：重新连接后检查 `adb shell getprop init.svc.mixer` 和 `adb shell 'tinymix Ext_Speaker_Amp_Switch'`，必要时执行 `adb shell 'setprop ctl.start mixer'` 并检查结果。电脑自己的音箱用于回复，播放时不处理新麦克风帧；尚无回声消除和打断。

## 本机资源与来源

资源放在 `~/.local/share/astero/`，不提交仓库：

- `models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/`：开源候选 [sherpa-onnx 中英关键词模型](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)，用音素指定 Astero；`models/astero-keywords.txt` 包含用户发音的几种候选。监听时同时使用本地 Zipformer 识别 `ASTERO`/`ASTRO` 作为备用触发，因此可能误触发普通词 astro；尚需用实际目标词和负样本校准。
- `models/asr/`：sherpa-onnx bilingual Zipformer int8，用于备用唤醒识别；`models/sensevoice/`：本机 SenseVoice int8，用于完整命令转录。来源：[Zipformer](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20)、[SenseVoice 导出](https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17)。模型权重有各自许可，重新分发前应检查。
- `models/silero_vad.onnx`：由 [sherpa-onnx 官方发布](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)的 Silero VAD 文件。
- `bin/capture-bionic`：从仓库 `astero/audio/capture_bionic.c` 为已连接设备构建，链接从设备读取的 Android libc 与 tinyalsa。当前本机可用 `sh astero/audio/build_bionic_helper.sh ~/.local/share/astero/bin/capture-bionic` 重建；需要 `adb`、root Echo 和 Fedora `gcc-arm-linux-gnu`。该 helper 的 ABI 针对本次 Fire OS 6 / armeabi-v7a 设备，其他固件须先验证。

## 已验证与限制

- 新 helper 已在真实 Echo 读取 PCM，退出后 `mixer` 恢复。修复了 Android linker 的 stderr 被 ADB 合并进 PCM 的错位问题。
- 曾在用户参与的内存诊断中识别出“一加一等于几”，同时发现用户说的唤醒词被 Zipformer 转写为 `ASTRO`。
- 使用合成音频直接驱动控制器时，确实经历唤醒、ASR、GPT 回复和 Voice Bridge `completed`，但转录不准确。一次经电脑扬声器 → Echo 麦克风的实验也走到 `completed`，但转录不是原问题，不能视为准确的语音闭环。
- 更换 SenseVoice 后，自动声学回环取得一次准确结果：电脑扬声器播放近似 Astero 发音的合成音，Echo 采集后唤醒；电脑播放“What is two plus two?”，识别为“What is2 plus2?”，GPT 返回“Four.”，Voice Bridge 报告完成。该唤醒测试是近似音素的合成语音，不是用户本人说出 Astero 的成功记录。
- Echo 自身扬声器测试：播放了两段不同频率的测试音，`tinyplay` 退出码为 0，用户确认听到两声且当时没有杂音。此前用户报告语音完成或扬声器测试结束数秒后偶发电流声；在监听期间保持功放关闭后，用户确认目前没有杂音。每轮重复切换功放和 mixer 是与现象吻合的可能原因，尚未证明是唯一原因；退出监听恢复原状态时仍需留意。
- 真实唤醒率、用户说出 Astero 后的完整中文/英文语音命令、输出噪声和长时间运行尚未充分验证。不要把单次合成语音测试视为真实用户验收。
