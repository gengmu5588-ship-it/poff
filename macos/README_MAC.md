# POFF V2.2 — macOS 移植版

这是基于原 POFF V2.2 Python 源码整理的 macOS 工程。

## 已适配

- 主窗口 / Tkinter UI
- 局域网主机发现与 TCP 控制协议
- 文件投递（TCP + SHA-256）
- 黑洞悬浮投递
- MobiDrop 手机扫码双向传输（17891）
- 全屏截图、选区截图、预览、发送
- 开机自启：Windows Registry → `~/Library/LaunchAgents/com.poff.app.plist`
- 打开文件/目录：Explorer → Finder `open`
- ICMP 探测：Windows ping 参数 → macOS ping 参数
- 本机关机：Windows `shutdown` → `osascript` / System Events
- KVM：Win32 Low-Level Hook / SendInput → macOS Quartz Event Tap / CGEvent
- 全局热键：RegisterHotKey → pynput

## Mac 版明确降级

### 亮度控制

原版依赖 Windows WMI 和 DDC/CI。macOS 没有稳定公开、适合直接打包分发的百分比亮度 API，因此当前 UI 会显示“不支持”，不会尝试私有 API。

### Spacedesk 平板副屏

Spacedesk Driver 是 Windows 方案。Mac 版对应页面改成提示使用 Sidecar（随航）/ macOS 显示器设置，不再显示假的服务启停状态。

## KVM 兼容策略

网络协议继续发送 Windows Set-1 扫描码：

- Mac 本地 Quartz keycode → Set-1 扫描码 → 网络
- Mac 接收 Set-1 扫描码 → Quartz keycode → CGEvent 注入

因此 Mac → 现有 Windows POFF 的常用键盘按键可以继续沿用原协议；鼠标协议没有变化。

目前映射覆盖常用 ANSI 键盘：字母、数字、符号、Shift/Ctrl/Alt/Command、方向键、Home/End/Page、Delete、F1-F12 等。特殊媒体键/非标准键盘可能需要继续补映射。

## 第一次运行需要的 macOS 权限

1. **辅助功能**：KVM 注入键鼠。
2. **输入监控**：KVM 全局监听（具体是否单独要求取决于 macOS 版本）。
3. **屏幕与系统录音**：截图。
4. **自动化 / System Events**：仅在你主动执行“本机关机”时可能弹出。
5. **传入网络连接**：macOS 防火墙开启时，首次监听 17890/17891 可能弹出允许连接提示。

权限变更后，macOS 有时要求退出并重新打开 POFF。

## 开发运行

```bash
chmod +x run_dev_mac.sh
./run_dev_mac.sh
```

## 构建 .app

推荐 Python 3.12（python.org 官方安装包，通常自带兼容的 Tk）：

```bash
chmod +x build_mac.sh
./build_mac.sh
```

输出：

```text
dist/POFF.app
```

## 制作 DMG

```bash
chmod +x make_dmg.sh
./make_dmg.sh
```

输出：

```text
dist/POFF-2.2-macOS.dmg
```

## Gatekeeper / 分发

`build_mac.sh` 默认执行 ad-hoc codesign，适合自己的 Mac 或内部测试。

如果需要发给其他用户并避免 Gatekeeper 警告，需要 Apple Developer ID：

1. `codesign --options runtime --timestamp --sign "Developer ID Application: ..." POFF.app`
2. 使用 `xcrun notarytool submit ... --wait` 公证 DMG/App
3. `xcrun stapler staple ...`

这些步骤需要你自己的 Apple Developer 证书与 Apple ID/App Store Connect 凭据，因此没有硬编码进脚本。

## 重要限制

PyInstaller **不能从 Linux/Windows 交叉编译出 macOS `.app`**。本工程已经把打包配置准备好，但最终的 `.app/.dmg` 必须在你的 Mac 上运行构建脚本生成。
