# Cassandra 音色

用户在试听英文 B 和其中文复用版本后，选择“姑且使用这个声音”。仓库将这一版固化为 `cassandra`：成熟、厚实、中性的女低音；英文设计目标为标准南部英式发音，中文通过同一参考音频跨语种复用。没有金属音效、变调或其它电子处理。

这次接受的是具体试听效果，状态为 `accepted: true`、`provisional: true`。它不表示任意新句子的音色、口音、语气都能完全一致，也不代表所有新句子已通过人工验收。

## Voice Bridge 共享包中的音色资产

- `voice_bridge/profiles/cassandra/reference.wav`：共享 Voice Bridge 包内的英文参考音频，5.6 秒，单声道 PCM16 / 24 kHz。
- `voice_bridge/profiles/cassandra/preview-zh.wav`：用户接受的三句中文试听，包含句间 0.7 秒静音，总长 15.08 秒。
- `voice_bridge/profiles/cassandra/profile.json`：共享包内的身份、暂定接受状态、参考转录、SHA-256、设计描述和种子、固定模型版本以及试听信息。

参考音频和中文试听都是合成音频，不含用户录音，由 Voice Bridge 共享包维护。模型权重、真实用户录音和运行输出继续保存在仓库外。

参考文件 SHA-256：`50f35d5ac743544fe79ff3b08bc995f5157eb770f8a4af92f2fb0993147fab7a`。

## 读取与复用

```python
from voice_bridge.voice_profile import load_voice_profile

profile = load_voice_profile("cassandra")
prompt = model.create_voice_clone_prompt(
    ref_audio=str(profile.reference_audio),
    ref_text=profile.reference_text,
    x_vector_only_mode=False,
)
wavs, rate = model.generate_voice_clone(
    text="你好，我在。", language="Chinese", voice_clone_prompt=prompt,
    non_streaming_mode=True, max_new_tokens=256,
)
```

这里的 `model` 必须是已加载的官方 `Qwen/Qwen3-TTS-12Hz-1.7B-Base`，固定 revision 为 `fd4b254389122332181a7c3db7f27e918eec64e3`。生产调用应复用已提取的 prompt，并检查模型的 EOS 结束标志，避免把 token 上限截断的音频当作完整回答。

`voice_bridge.voice_profile.load_voice_profile()` 不依赖当前工作目录，也不会加载 PyTorch。它检查允许的 profile ID、元数据类型、参考 SHA-256、单声道 PCM16 / 24 kHz 和录音完整性。损坏、未知或不匹配时明确失败，不静默切换到 Serena 或其它声音。默认 ID 常量为 `DEFAULT_VOICE_PROFILE = "cassandra"`；具体运行配置与切换状态仍以服务配置为准。

`cassandra` 的参考声音与来源描述视为不可变资产。如果重新设计音色，创建新版本 ID，重新试听、接受和记录来源；不要覆盖已有资产让现有配置悄悄变声。

## 在新机器上准备运行环境

Voice Bridge 包分发音色参考，Base 模型权重在首次安装时单独下载。以下命令在 Astero 仓库根目录执行，需要 Python 3.12 和 `uv`；安装到独立环境，不改现有 Astero 的 Python 环境。

```sh
uv venv --python 3.12 "$HOME/.local/share/astero/voice-design-venv"
uv pip install --python "$HOME/.local/share/astero/voice-design-venv/bin/python" \
  'voice-bridge-local @ git+https://github.com/yuxx0248/voice-bridge.git@214cacd071e100b3a039c262a153dd79ed087d32'
uv pip install --python "$HOME/.local/share/astero/voice-design-venv/bin/python" \
  --torch-backend cpu 'qwen-tts==0.1.1'
"$HOME/.local/share/astero/voice-design-venv/bin/python" \
  scripts/fetch_voice_profile_model.py \
  --profile cassandra \
  --model-dir "$HOME/.local/share/astero/models/voice-base"
```

下载脚本从 Voice Bridge 共享 profile 读取模型 ID 和固定 revision，只获取 JSON、文本词表和 safetensors 文件，逐个校验全部 safetensors 的文件大小和上游 LFS SHA-256，最后写入 `astero-provenance.json`。它只依赖 `huggingface_hub` 和 Python 标准库，不加载模型；可重复运行，保留已有目录及其中其它文件，不将权重写入仓库。校验失败会退出，不会生成新的成功验证记录。

下载完成后的常驻服务、systemd 配置及验证步骤见 [音色运行说明](VOICE_PROFILE_RUNTIME.md)。先确认独立音色服务健康，再让 Astero 使用对应 endpoint；不要把这个 profile ID 传给只认识九个预设音色的旧 CustomVoice 服务。

## 来源、许可与性能边界

音色由官方 [Qwen3-TTS VoiceDesign](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign) 生成，固定 revision `5ecdb67327fd37bb2e042aab12ff7391903235d3`、seed `241119`，再使用官方 [Base 模型](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base) 的语音复刻能力复用。官方模型标注的许可证为 Apache-2.0；模型权重本身不随本仓库音色资产分发。官方流程见 [VoiceDesign then Clone](https://github.com/QwenLM/Qwen3-TTS#voice-design-then-clone)。

当前 Python CPU float32 的中文三句合成实测分别约 30.12、26.33、32.84 秒；音频本身分别为 4.40、4.40、4.88 秒。这个版本固化了音色选择，但未解决低延迟问题。不能将合成成功等同于实时响应验收通过。生成环境和进一步的复现命令见 [VoiceDesign 试听说明](VOICE_DESIGN_PREVIEW.md)。
