# AST-03 本机唤醒模型

## 当前状态

本机检测适配器位于 `astero/wake/detector.py`。输入为带时间戳和递增序号的 16 kHz 单声道 PCM16 帧；按 80 ms 默认窗口打分，阈值和连续命中数可配置。事件包括检测时间、源序号、分数和有界预录 PCM。预录仅保存在内存，调用方需在一次捕获会话结束后释放/重置检测器。

真实模型状态：**不可用，真实唤醒验证未完成**。检查环境为 aarch64，Python 3.15.2；没有安装 `openwakeword` 或 `sherpa_onnx`，仓库及本机配置中也未发现 Astero 模型。实现不会选取或重新命名 Alexa、Jarvis 等现成模型。

## 选型

先采用显式路径加载的 openWakeWord 自定义模型作为候选：其项目文档说明默认 TFLite 推理面向常见 x86/ARM64 平台，并提供自定义唤醒词训练流程。自定义训练配置直接使用目标短语，可提供易混淆负短语；此工作仍需生成/收集合法训练数据、训练并评估，不能视作已有可用 Astero 权重。[openWakeWord 项目](https://github.com/dscripka/openWakeWord) · [自定义模型配置](https://github.com/dscripka/openWakeWord/blob/main/examples/custom_model.yml)

sherpa-onnx KWS 是可替代路线，但其常规 KWS 文档说明需准备所选关键词的音频-文本对并训练对应模型。当前未找到或验证适用于单词 Astero 的预训练模型，因此它也不能直接启用。[sherpa-onnx KWS 文档](https://k2-fsa.github.io/sherpa/onnx/kws/index.html)

运行时包、推理运行库、训练脚本、语音合成语料/声音和最终权重各自应核对其许可证与来源。仓库不包含模型权重，也不自动下载权重或用户语音。当前机器的 Python 3.15 环境没有验证上述运行时的安装兼容性；部署时应使用项目明确支持且有 ARM64 构建的 Python/运行库组合，并在目标机实测。

## 准备流程

1. 由用户确认 Astero 的预期发音（例如英语 `uh-STEH-roh` 或其他读法）及目标说话人/距离。不要自行将目标短语改成 “Hey Astero”。
2. 收集经用户授权的多说话人正样本，以及相近负样本（例如 “asterisk”、人名/相似音节、普通对话中近似发音）。保存到仓库外的受限目录；用户语音不上传到未知服务。
3. 按 openWakeWord 自定义模型流程准备合成正例、背景音和混响数据。确认 TTS 声音、背景录音、预训练资源及生成数据的许可证和使用权限。训练配置的 `target_phrase` 必须只包含 “Astero”，并加入现场发现的 `custom_negative_phrases`。
4. 训练并留存模型、数据清单、工具版本和许可记录在仓库外。使用未参与训练的 Astero 说话样本与相近负样本独立选阈值；不足以通过负样本测试时不要部署。
5. 安装经验证的 ARM64 运行时，然后将模型路径写入本机配置，例如：

   ```json
   {
     "wake": {
       "engine": "openwakeword",
       "model_path": "/home/zuohang/.local/share/astero/astero.tflite",
       "threshold": 0.5,
       "consecutive_frames": 2,
       "refractory_seconds": 1.5,
       "pre_roll_seconds": 0.5
     }
   }
   ```

6. 由用户参与真实测试，记录目标词、相近负样本的命中/误触发、说话距离和延迟，并在测试后删除临时录音。当前离线测试只验证流处理和替身分数，不构成声学性能证据。

`OpenWakeWordModel(path)` 只加载给定路径；文件不存在或可选依赖未安装会明确抛出 `ModelUnavailableError`。离线验证可使用 `astero.wake.SequenceWakeModel` 按序返回可控分数。检测事件不调用 LLM、TTS 或其他服务。
