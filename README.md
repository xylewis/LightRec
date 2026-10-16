# LightRec 使用说明

## 录屏

1. 双击 `LightRec.exe`。
2. 默认录制主显示器全屏、电脑声音和麦克风。直接点 **开始录制**。
3. 结束时点 **停止并保存**。

录制开始和停止时，屏幕边缘会短暂显示描边提示。描边的颜色、透明度、粗细、两端渐隐、显示边和时长可在 **设置… → 描边提醒** 调整，并可预览。视频为 MP4，默认保存在“视频\LightRec”。保存位置可在主窗口更改，也可点 **打开** 直接查看。录制完成后，主窗口会显示保存结果，不再弹出完成窗口。

点击窗口关闭按钮时，可选择**最小化到系统托盘**或**退出程序**；勾选“不再提示”可记住选择。程序在托盘运行时，双击托盘图标可重新打开窗口，右键菜单可退出。

默认热键显示在主窗口；按 **设置… → 快捷键 → 录入…** 可自定义。默认热键是 `Ctrl+Shift+F9` 开始、`Ctrl+Shift+F10` 停止、`Ctrl+Shift+F11` 切换窗口穿透。

## 设置

主窗口只保留录制范围、声音、保存位置和开始/停止。视频格式、帧率、码率、显示器编号、声音码率、静止画面跳过和窗口穿透选项在 **设置… → 录制设置**。描边提示样式在 **设置… → 描边提醒**。

便携包已把 Python、FFmpeg 和声音组件一起打包。正常使用不需要安装 Python 或 FFmpeg，也不需要设置“录屏组件路径”。设置页显示“FFmpeg 已包含”即为正常；只有开发版缺少 FFmpeg 时才需要手动指定路径。

声音默认选“电脑声音 + 麦克风”。软件启动时会预热编码器，开始录制时画面不必等待声音设备打开；若声音设备启动失败，录屏仍会继续，状态栏会显示提示。如果没有声音，请在 Windows 声音设置里选择可用的扬声器和麦克风；也可以在主窗口暂时关闭录音声音。

## 便携版

解压 `dist/LightRec-portable-x64-fast.zip`，运行 `LightRec-portable-x64-fast/LightRec.exe`。这是目录便携版，启动时不需要像单文件版那样先解压整个程序，因此打开更快；请保留旁边的 `_internal` 文件夹。压缩包内包含本说明和第三方许可证。

用户设置保存在当前 Windows 用户的 `%APPDATA%\LightRec\settings.json`，换电脑时复制整个 `LightRec-portable-x64-fast` 文件夹即可运行，设置需要在新电脑重新配置。

## 重新打包（开发者）

在 Windows 上安装 Python 3.14 和 FFmpeg 后，运行：

```powershell
py -3.14 -m pip install -r requirements-audio.txt -r requirements-build.txt
./build_windows.ps1
```

第三方组件信息和许可证见 `THIRD_PARTY_NOTICES.txt`。当前电脑已验证短时录制；不同 Windows 版本和编码硬件仍需在对应电脑上实测。

## 源码结构

- `lightrec.py`：启动入口。
- `lightrec_app/app.py`：主窗口与应用流程协调，收集设置后交给录制会话。
- `lightrec_app/recording.py`：视频/音频进程生命周期、硬件回退和 MP4 收尾。
- `lightrec_app/media.py`、`audio.py`：FFmpeg 命令与编码器探测、WASAPI 音频采集。
- `lightrec_app/config.py`、`hotkeys.py`：设置文件与热键值解析、系统全局热键线程。
- `lightrec_app/ui/`、`tray.py`：设置窗口、选区、描边提示和系统托盘组件。
