# macOS 移植功能矩阵

| 功能 | macOS 状态 | 实现 |
|---|---|---|
| 局域网主机 TCP 协议 | 已保留 | Python socket |
| 文件投递 | 已保留 | 原协议，无平台 API |
| MobiDrop 手机互传 | 已保留 | ThreadingHTTPServer |
| 黑洞悬浮面板 | 已保留 | Tk / tkinterdnd2 |
| 全屏截图 | 已适配 | Pillow ImageGrab + macOS 屏幕录制权限 |
| 选区截图 | 已适配 | Tk overlay + Retina 坐标比例换算 |
| 全局热键 | 已适配 | pynput |
| KVM 鼠标监听/吞键 | 已适配 | Quartz Event Tap |
| KVM 鼠标/键盘注入 | 已适配 | Quartz CGEvent |
| KVM 与 Windows 扫描码协议 | 已适配常用键 | Mac keycode ↔ Windows Set-1 |
| 开机自启 | 已适配 | LaunchAgent |
| Finder 打开路径 | 已适配 | `open` |
| 本机关机 | 已适配 | AppleScript System Events |
| 高风险应用预检 | 已适配基础版 | `ps` + 常见应用白名单 |
| Windows 防火墙 netsh | 不适用 | 交给 macOS 应用防火墙 |
| WMI 内屏亮度 | 不适用 | 明确降级 |
| Windows DDC/CI | 暂未实现 | 明确降级 |
| Spacedesk | 不适用 | Mac UI 改为 Sidecar/显示器设置提示 |

## 需要真机重点验证

1. macOS 14/15/26 上 Quartz Event Tap 权限弹窗与重新授权流程。
2. Mac ↔ Windows KVM：中文输入法切换、组合键、Command/Windows 键映射。
3. 多显示器 + Retina 下选区截图坐标。
4. PyInstaller bundle 中 tkinterdnd2/TkDND 动态库加载。
5. pystray 在菜单栏中的退出/恢复窗口行为。

当前环境不是 macOS，因此能完成源码静态检查、协议映射测试和打包配置生成，但不能在这里实际启动 Cocoa/Tk App 或生成可验证的 macOS `.app`。
