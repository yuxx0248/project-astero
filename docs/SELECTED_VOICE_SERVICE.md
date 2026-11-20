# 固定音色的本地播报服务

`voice_bridge.clone_service` 使用 Voice Bridge 共享包中的 `cassandra` 参考音频，在启动时加载一次官方 Qwen3-TTS Base 模型、提取一次参考提示，随后串行复用。它在独立进程运行，不修改现有 CustomVoice 服务或模型。

## 部署

在 Astero 仓库根目录执行；模型约 4.54 GB，下载和运行数据保存在仓库外。

```sh
uv venv --python 3.12 ~/.local/share/astero/voice-design-venv
uv pip install --python ~/.local/share/astero/voice-design-venv/bin/python \
  'voice-bridge-local @ git+https://github.com/yuxx0248/voice-bridge.git@214cacd071e100b3a039c262a153dd79ed087d32'
uv pip install --python ~/.local/share/astero/voice-design-venv/bin/python --torch-backend cpu -r requirements-voice.txt
~/.local/share/astero/voice-design-venv/bin/python scripts/fetch_voice_profile_model.py
~/.local/share/astero/voice-design-venv/bin/python -m voice_bridge.clone_service \
  --model-dir ~/.local/share/astero/models/voice-base \
  --data-root ~/.local/share/astero
```

已存在独立环境时直接使用，无需重新创建。下载脚本从 Voice Bridge 共享 profile 读取固定的官方 model ID/revision，验证模型和 tokenizer 的 SHA-256 后生成 `astero-provenance.json`。服务启动核对 Base 类型、profile 对应的 model ID/revision；参考 WAV 由共享 profile loader 校验 SHA-256。

常驻部署可将 `systemd/astero-voice.service` 放入 `~/.config/systemd/user/`，执行 `systemctl --user daemon-reload` 和 `systemctl --user enable --now astero-voice.service`。先查看共享服务清单并确认端口没有冲突，启用后将实际状态登记到清单。默认仅监听 `127.0.0.1:8767`。

## 接口与边界

- `GET /health` 报告 `tts_ready`、`voice_profile`、`reference_sha256` 和模型版本。加载失败时是 `not_ready`，不回退旧音色。
- `POST /announce` 接受 `text` 和可选 `voice`。`voice` 如提供必须等于 profile ID；非空 `style` 不支持，会返回 400。
- 返回 202 只表示入队。`GET /announcements/{id}` 报告 `queued → generating → playing → completed/failed`。
- `paplay` 成功退出才报告 completed；这仍不是麦克风测得的物理扬声器验收。
- 文本上限 500 字符，等候队列默认 4 个任务，状态内存保留最近 256 个终态任务；请求体上限 16 KiB，HTTP 并发线程上限 8。
- 每次生成最多 256 codec token，talker 生成时间目标上限 120 秒。未得到 EOS 的音频会保存用于调试，但不会播放，也不会标记完成。该截止是在生成步骤检查，不能中断底层挂死的单次运算；systemd 有最终停止超时。
- 当前 CPU float32 非流式推理在试听中约每句 26–33 秒，不能视为已完成低延迟优化。长回答可能因 token 上限失败，需要后续分句/流式方案。

每个任务在 `~/.local/share/astero/tts/<id>/` 保存 `request.json`、`audio.wav`（若已生成）和 `result.json`，目录 0700、文件 0600，无 TTL。元数据包含选中音色、参考校验和、随机种子、生成完整性及排队/合成/播放/总耗时；普通日志不打印文本内容。内存状态被裁剪或进程重启不删除磁盘档案。

服务只支持固定参考音色，不接受上传参考音频、任意文件路径或临时风格改写。更换声音应建立新的版本化 profile，并经过试听验收。
