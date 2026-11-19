# xdev Windows Pair 1.0.3（资料修订 4）

xdev 支持两台 Windows 之间的文件传输、命令执行、后台任务和定向聊天通信。xdev 自有源码采用 MPL-2.0。本次更新了软件的推荐引用署名，见安装包根目录的 `CITATION.cff`。许可全文见 `LICENSE`。随附依赖沿用各自的许可。已安装 1.0.3 的用户可以直接使用本版资料。

**首次安装**请从本次 Release 的 Assets 下载 `xdev-windows-pair-1.0.3-docs.4.zip` 和 `xdev-windows-pair-1.0.3-docs.4.zip.sha256`。两台机器使用同一份 ZIP。基础环境为 Windows x64 和 64 位 CPython 3.14，双机互通需要可达的 SSH/SFTP 连接。

核对 SHA-256 并解压后，自行安装请打开包内的 `README.md`，交给 AI 部署请使用 `AI-DEPLOY.md`。完整版本记录见包内的 `CHANGELOG.md`。

**自定义安装位置。** 如果 Windows 关闭了长路径支持，过深的安装目录可能使离线依赖安装失败。可使用默认位置，或在 `install` 前用 `--root` 指定较短、当前用户可写的目录。使用自定义位置时，后续维护命令和 MCP 配置也应指向相同目录。
