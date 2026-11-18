# Echo Dot 2 USB 音频采集适配器

这是 AST-01 的实现说明。支持目标为 root ADB 可访问的 Echo Dot 2 / Fire OS 6，ALSA card 0、capture device 24，16 kHz、9 通道、交错 `s24_3le`。USB ADB 是传输通道。输出按固定长度切成 `AudioFrame`，附主机单调时钟时间戳和从 1 开始的序号；帧字节数为 `样本数 × 9 × 3`。时间戳表示主机收到该帧的时刻，不是设备 ADC 的精确采样时间。

## 设备端 helper

`astero/audio/capture_helper.c` 是一个最小 tinyalsa PCM 读取器，仅向 stdout 写原始 PCM，不生成 WAV 或录音文件。它固定请求设备已确认的 S24_3LE 格式，并报告打开、读取和写出错误。使用 NDK 在 Fedora ARM64 主机编译，链接从当前 Dot 临时读取的 `/system/lib/libtinyalsa.so`，以匹配目标 tinyalsa ABI：

```sh
astero/audio/build_capture_helper.sh /path/to/android-ndk /tmp/astero-capture-helper
sha256sum /tmp/astero-capture-helper
```

脚本核实 ADB root、设备 product 和 `armeabi-v7a`，拉取设备自带 tinyalsa 库并打印库与 helper 的 SHA-256。helper 不提交仓库；构建临时目录自动删除。运行时必须把 helper 的实际 SHA-256 显式传入 `AdbCapture`，不匹配会在碰设备前失败。设备端临时程序以 `/data/local/tmp` 随机路径复制，权限为 `0700`，停止时删除。

## 使用方式

```python
from astero.audio.capture import AdbCapture

with AdbCapture(
    "/tmp/astero-capture-helper",
    expected_sha256="由 sha256sum 输出的 64 位摘要",
    serial="ADB设备序列号",
) as capture:
    for frame in capture.frames():
        consume(frame)  # 不在这里保存录音
```

短时采集可在循环中按时长或业务条件 `break`；随后退出 `with`。调用 `close()`、上下文异常或 Ctrl-C 会终止 adb 流、移除设备端 helper，并将 `mixer` 恢复到采集前状态。仅当开始时 `init.svc.mixer` 为 `running` 才会在结束时启动它；开始时为 `stopped` 则保留该状态。恢复状态不可确认时会抛出 `CaptureError`。ADB 断开、helper 非零退出及不完整 PCM 帧分别作为错误上报，不会补造帧或静默报告成功。

开始采集前会先检查本机 helper 哈希、设备 root、`mixer` 状态，再上传 helper。`mixer` 为运行态时，使用 Android init 属性暂时停止它以取得独占 PCM。此操作不改系统分区或持久配置。请在运行前确保 Dot 已完成启动且无升级/刷写过程。

## 可用设备检查

只读检查当前 ADB 设备及捕获能力：

```sh
adb devices -l
adb shell 'id; getprop ro.product.device; getprop ro.product.cpu.abi; getprop init.svc.mixer'
adb shell 'cat /proc/asound/cards; cat /proc/asound/pcm; tinypcminfo -D 0 -d 24 -c'
```

预期身份为 `biscuit_puffin`、ABI `armeabi-v7a`；root ADB 的 `id` 应显示 uid 0；设备 24 应报告 16 kHz、9 通道、S24_3LE。采集结束再运行 `adb shell getprop init.svc.mixer`，确认是采集前的状态。任何不匹配均应停止，不要尝试其他设备编号或格式。

## 替身与验证

`SyntheticCapture` 与可注入二进制流用于不连 Echo 的下游集成；`tests/test_audio_capture.py` 检查固定帧边界、格式元数据、顺序、坏长度和断连错误。它们不能验证真实 Dot 的人声质量。采集默认不写文件，`AudioFrame.pcm` 只在进程内存中短暂存在；上游若自行保存，应由上游负责清理。

**真实硬件/听感验收状态：未完成。** 目前只确认 ADB 设备在线以及先前文档记载的 PCM 设备格式和非零样本。本实现的 Android NDK helper 尚未在当前环境编译运行，未由用户说已知短句进行静音/说话对比，也未确认可听辨人声。因此不能据此宣称真实采集成功或语音清晰。首次硬件验证需用户参与短句测试，记录开始/结束 `mixer` 状态，并清除任何外部调试录音。

正常上下文退出会向 helper 的 stdin 发送停止字符，helper 在一个 10 ms 周期内退出；随后清理 adb 流、删临时程序并恢复 `mixer`。若主机进程被 `SIGKILL`、断电或 adb server/USB 故障导致清理命令无法送达，则主机无法保证 init 服务恢复；重新连接后应先检查状态，并在确认 Dot 状态安全后执行 `adb shell 'setprop ctl.start mixer'`，再核实 `running`。持续采集时建议由调用方设置看门狗，收到 `CaptureError` 后结束本轮并检查服务状态。
