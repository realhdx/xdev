# xdev Windows Pair 1.0.3（资料修订 6）

**简体中文** · [English](RELEASE-NOTES.en.md)

xdev 可在指定的 AI 聊天之间传信、沿原信收回复，也能在配对设备之间传文件、运行命令和后台任务。本次更新调整了首页和安装说明。已装 1.0.3 的用户可直接使用新手册。

- **自己安装**。从本次 Release 的 Assets 下载 `xdev-windows-pair-1.0.3-docs.6.zip` 和同名的 `.sha256` 文件，核对校验值后按[安装与使用手册](README.md)操作。多台设备各用同一份安装包。
- **交给 AI 安装**。把下面这句话发给有权操作目标设备的 AI。

  > 请从 https://github.com/realhdx/xdev 获取最新正式版，按包内 AI 部署指南在我指定的 Windows 设备上安装并验证 xdev。

每台设备需要 Windows x64 和 64 位 CPython 3.14。单机安装即可使用本机功能。需要跨设备协作时，再通过 SSH/SFTP 配对要互通的设备。[AI 部署指南](AI-DEPLOY.md) · [完整版本记录](CHANGELOG.md)

**自定义安装位置。** 如果 Windows 关闭了长路径支持，过深的安装目录可能使离线依赖安装失败。可使用默认位置，或在 `install` 前用 `--root` 指定较短、当前用户可写的目录。使用自定义位置时，后续维护命令和 MCP 配置也应指向相同目录。
