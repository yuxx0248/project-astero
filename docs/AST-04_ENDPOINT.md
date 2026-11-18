# AST-04 VAD 与语音端点

文件 astero/speech/endpoint.py 实现唤醒后的单轮命令收集。它消费 AST-02 的 MonoFrame（16 kHz、单声道、PCM16LE 或 float32LE），输出含采样率、编码、起止时间、首尾序号和 PCM 字节的 SpeechSegment。组件不保存文件，不调用 ASR、LLM 或 Voice Bridge。

## 状态与配置

持续把音频帧传给 EndpointCollector.feed()，包括空闲期间，以维护有界预录缓冲。唤醒检测器报告 WakeEvent 后调用 wake()。正常事件顺序为 wake、可选 speech_started、endpoint。终态还包括 no_speech_timeout、max_duration、cancelled 和 stream_lost。取消、无语音超时和断流丢弃不完整 PCM；达到最长时长会返回已开始语音的片段。

默认参数为 0.5 秒预录、5 秒等待开口、1.5 秒静音结束及 90 秒最长命令。pre_roll_buffer_seconds 默认 0.75 秒，限制空闲期间保留的 PCM；预录按完整 VAD 帧保留，边界最多多取一个帧。静音阈值可调到 3 秒。参数只是起点，应通过真实说话测试调整。时间戳使用音频采集层的秒单位时钟，帧应按时间顺序到达。

## VAD 依赖与模型来源

VAD.is_speech(MonoFrame) -> bool 是模型适配边界。当前仓库没有附带模型，也没有用音量阈值替代 VAD。选用 Silero VAD 的 ONNX 变体作为首选适配目标：其官方项目提供 ONNX CPU 安装选项和模型；ONNX Runtime 官方 Python 文档列出 Linux ARM64 CPU 支持。[Silero VAD 官方仓库](https://github.com/snakers4/silero-vad)；[ONNX Runtime Python 安装文档](https://onnxruntime.ai/docs/get-started/with-python.html)。

本交付仅验证接口和状态机，不安装模型、不下载权重，也没有验证 Fedora ARM64 上具体 wheel、模型版本或真实 VAD 精度。集成时应从官方项目固定版本获取 ONNX 权重，确认随该版本发布的许可及模型输入窗口要求，再实现适配器并做 ARM64 导入和真实语音验证。第三方模型不随仓库提交。

## 验证与实测边界

离线替身测试覆盖预录、唤醒前片段保留、静音端点、无语音超时、最长时长、取消、断流、帧格式及时间顺序错误。尚未用真实中文、英文短句检查截断、静音超时和 VAD 判定；该实测需要用户在麦克风前分别说出已知短句。本次没有采集或保留音频。
