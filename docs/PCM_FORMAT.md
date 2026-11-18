# PCM 解码接口

`astero.audio.pcm` 是无状态的纯 Python 转换层，不访问声卡、Echo 或文件系统，也不会保存音频。

## 输入和输出

- `PCMFrame`：`timestamp`（秒）、`sequence`、`sample_rate`、`channels`、`sample_format` 和原始 `data` 字节。
- `convert_frame(frame, channel=0, output_format="pcm_s16le")`：接受 16,000 Hz、9 通道、交错 `s24_3le`。一个采样时刻占 9 × 3 = 27 字节。帧字节数必须是 27 的整数倍。
- `MonoFrame`：保留输入时间戳、序号和采样率，带有 `sample_count`、输出格式及单声道数据字节。PCM16LE 每样本 2 字节；float32LE 每样本 4 字节。
- `decode_s24_3le` 和 `split_interleaved` 可独立用于解码和检查通道排列。

## 通道与量化

通道 0–6 是麦克风，7–8 是 Echo 自身播放参考。`convert_frame` 默认只选麦克风通道 0，并允许将 `channel` 设为 0–6；它不做未经验证的多通道混合，也不允许选择参考通道。

24 位值先进行有符号小端解码，范围为 −8,388,608 到 8,388,607。PCM16LE 转换丢弃最低 8 位（算术右移），并钳制到 −32,768 到 32,767；float32LE 使用原始值除以 8,388,608，范围为 [−1.0, 1.0)。因此正满量程不会溢出为负值，负满量程精确映射到 −1.0 / −32,768。转换不会改变采样率。

不完整的 24 位样本、交错采样组或帧长度会抛出 `ValueError`；采样率、通道数、格式及通道选择不匹配同样会明确报错。模块不导出 WAV，因此不会产生与编码不符的 WAV 头部。
