# xdev Windows Pair 1.0.3（资料修订 5）

**简体中文** · [English](RELEASE-NOTES.en.md)

xdev 可在指定的 AI 聊天之间传信、沿原信收回复，也能在两台配对的 Windows 电脑之间传文件、运行命令和后台任务。这次更新提供完整的中英文用户文档。已装 1.0.3 的用户可直接使用新手册。

**首次安装**请从本次 Release 的 Assets 下载 `xdev-windows-pair-1.0.3-docs.5.zip` 和同名的 `.sha256` 文件，核对 SHA-256 后解压。两台机器使用同一份 ZIP。基础环境为 Windows x64 和 64 位 CPython 3.14，双机互通需要可达的 SSH/SFTP 连接。

| 自己安装 | 交给 AI 安装 |
| --- | --- |
| 按[安装与使用手册](README.md)操作。 | 将 [AI 部署指南](AI-DEPLOY.md)交给有本机操作权限的 AI。 |

任选一种方式即可。安装包内的手册也可以离线切换语言。[完整版本记录](CHANGELOG.md)

**自定义安装位置。** 如果 Windows 关闭了长路径支持，过深的安装目录可能使离线依赖安装失败。可使用默认位置，或在 `install` 前用 `--root` 指定较短、当前用户可写的目录。使用自定义位置时，后续维护命令和 MCP 配置也应指向相同目录。
