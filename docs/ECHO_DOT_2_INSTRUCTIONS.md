# Echo Dot 2 解锁与麦克风测试操作说明

本文记录 Project Astero 使用 Echo Dot 2 作为候选网络麦克风的操作，以及 2026-11-18 在实际设备上的结果。设备是 **Echo Dot 2nd Generation，型号 RS03QR，代号 biscuit**。其他 Echo 型号不能直接套用本文的固件、按键或刷写步骤。

## 目标与选择

- 当前测试：Echo Dot 2 通过 USB 连接本机，由本机处理语音并使用本机 Voice Bridge 播放回复，详见 [本机语音通路计划](LOCAL_VOICE_PATH.md)。未来再评估经局域网连接 Xeon 和使用独立球形 Echo 播放。Pi 4 不在本次测试路径中。
- 唤醒词目标是单独的 **Astero**。当前没有训练或验证该唤醒模型。
- 选用 [Firebreak](https://github.com/bboe/firebreak) 管理解锁和恢复流程，使用其默认的 Fire OS 6 目标；不使用 `--factory-reset`。工具的默认流程还会安装 root 环境，不只是更改 bootloader 锁状态。
- 优先保留设备原有设置，但解锁和固件写入仍可能导致数据丢失或设备无法启动。Firebreak 的完整备份需要 TWRP；对于锁定设备，进入 TWRP 可能先要求解锁，不能保证在解锁前获得完整备份。
- 根访问和能够读取音频不代表原厂波束成形、AEC、网络音频传输或者远场唤醒已经可用；每项都要单独测试。

## 操作前检查

1. 核对机身标签确为 RS03QR，并准备支持 **数据传输**的 micro-USB 线和稳定供电。避免 USB 集线器或接触不良的接口；刷写过程中不要拔线、断电或让电脑休眠。
2. Firebreak 提醒，原厂设备在解锁前连接 Wi-Fi 可能自动更新至不受支持的版本。先保持离线，核实具体固件和工具兼容性。不要把某一次成功视为对所有固件的保证。
3. 在 Linux 电脑准备 `adb`、`fastboot`、`uv`/`uvx` 和 `libusb`，按照 [Firebreak 安装说明](https://github.com/bboe/firebreak#install)配置 USB 权限。不要以 root 运行 Firebreak。
4. 本次电脑为 Fedora 45、aarch64；使用发行版 `android-tools` 37.0.0p1、`uvx` 和 Firebreak **1.4.3**。工具版本或设备状态变化时，先阅读该版本的帮助与上游文档。
5. 保留电脑的 `~/.android/adbkey`，不要提交到仓库或分享私钥。本次 root ADB 配置绑定该密钥；丢失后恢复访问可能需要重新进入 TWRP，具体按 Firebreak 文档操作。

## 进入 fastboot 并识别

对已确认的二代 Dot：断开 micro-USB，按住顶部带圆点的 Action 按钮，再接入电脑；灯环变绿后松开。如果一直是蓝灯，检查数据线、连接和电脑 USB 口。[上游排障说明](https://github.com/bboe/firebreak/blob/main/docs/troubleshooting.md)提供了其他可能原因。

```sh
lsusb
fastboot devices -l
fastboot getvar product
fastboot getvar lk_build_desc
fastboot getvar unlock_status
fastboot getvar secure
```

如新加入 `plugdev` 组但当前登录会话未刷新，可用 `sg plugdev -c 'fastboot devices -l'`，或重新登录。本次观察到 USB ID `0bb4:0c01`，`product: BISCUIT`、`lk_build_desc: 63cb91b-20221007_072309`、`unlock_status: false`、`secure: yes`。不要据此推断其他设备的状态。

## 解锁并验证

在已确认设备型号、fastboot 状态和用户授权后，参阅 [Firebreak 文档](https://github.com/bboe/firebreak)并检查当前版本的帮助。此次运行的是：

```sh
sg plugdev -c 'uvx --python 3.12 firebreak@1.4.3 --no-update-check --verbose unlock'
```

不要添加 `--force` 绕过提示，也不要添加 `--factory-reset`，除非已决定清空设备。按工具提示处理；若报错，不要随意断电或反复刷写，先记录完整阶段和错误，再参考上游的对应恢复步骤。下载、刷写和首次启动可能需要数分钟。

本次工具执行 amonet-biscuit v2.0.1，进入 TWRP，安装 Fire OS 6 8349 至两个槽位，安装 boot-root 并重启。最终验证：

```sh
adb devices -l
adb shell 'id; getprop ro.product.device; getprop ro.build.version.name; getprop sys.boot_completed; getprop ro.adb.secure'
```

本机返回 `uid=0(root)`、`biscuit_puffin`、`Fire OS 6.5.7.5 (NS6575/8349)`、启动完成值 `1`、ADB 安全设置值 `1`。Firebreak 报告 root ADB 绑定本机密钥。不要在公开记录中附设备序列号、ADB 私钥或 Wi-Fi 凭据。

## 麦克风接口检查

```sh
adb shell 'ls -l /dev/snd; cat /proc/asound/cards; cat /proc/asound/pcm'
adb shell 'tinypcminfo -D 0 -d 24 -c'
adb shell 'cat /proc/asound/card0/pcm24c/sub0/status; getprop init.svc.mixer'
```

本机卡 0、设备 24 的采集接口为 **16 kHz、9 通道、S24_3LE（3 字节打包有符号 PCM）**。原厂 `mixer` 服务通常独占该接口。按 [EchoMuse 的设备说明](https://github.com/wilbowes/EchoMuse/blob/main/device/tools/README.md)，通道 0–6 是麦克风，7–8 是 Echo **自身扬声器**的立体声播放参考；后者不能直接作为另一台蓝牙扬声器播放的参考。

本次以 `tinycap` 直接选择 24 位时无法打开 PCM。临时停止 `mixer` 后，通过临时编译的 Android ARM `LD_PRELOAD` 适配器将 `pcm_open` 的格式改为 `PCM_FORMAT_S24_3LE`，取得 80,896 帧，约 5.06 秒。注意：该次 `tinycap` 打印了 32 bit，生成的 WAV 头不能用于判断实际格式；数据按 9 通道、每样本 3 字节解析。通道 0–6 均有非零信号，通道 7–8 均为零；测试时没有建立可控播放参考，因此此结果符合静音参考的预期，但不构成参考链路验证。

**恢复要求：**任何独占采集实验都必须确保结束或失败时重新启动 `mixer`，并检查其 `init.svc.mixer` 状态。实验录音及临时二进制应在分析后从设备与电脑删除，不提交仓库。本次已恢复 `mixer`，其状态为 `running`，采集接口重新显示 `RUNNING`，临时录音和设备侧适配器已删除。

上述结果证明能够打开 PCM 接口并读出非零数据；尚不能证明各通道录到了清晰人声或七个麦克风各自正常。不要将有非零采样当作人声识别或音质验收。

## 下一步验证

1. 与用户协调一次有声测试：采集有明确静音和说话时段的短音频，比较各通道声能并必要时试听；结束后删除录音。
2. 评估独占 PCM 与原厂服务的关系，选择可恢复、可重复的采集方式；不要直接把本次临时注入适配器当作生产实现。
3. 先在当前电脑通过 USB 完成人声采集、识别和 Voice Bridge 回复的半双工测试；步骤和验收见 [本机语音通路计划](LOCAL_VOICE_PATH.md)。
4. 后续再验证持续采集、时间戳、断线重连及有认证的局域网音频传输，记录延迟与丢帧。
5. 后续对 Xeon 到独立球形 Echo 的播放链路单独获取参考信号，测量延迟、漂移和 AEC；必要时采用暂停播放或半双工。
6. 在真实声学环境测试 **Astero** 唤醒、中文/英文识别、音乐中误触发和长时间稳定性。不要假定 Echo 原厂处理链在自定义采集路径中仍然生效。
