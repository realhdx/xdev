# xdev —— AI 跨会话协作通道

**简体中文** · [English](README.en.md)

生成式 AI 的爆发降低了知识生产和艺术创作的技术门槛，让更多人得以施展才华，把想法迅速变成作品。产出能随算力翻番，人的注意力却不能。这种结构性的不对称把创作端提速的红利变成了审阅端的重压。一如工业革命初兴时的机器生产受制于手工操作和交接，今天的 AI 并行开工，却要排队等人传话。“人工智能”的美好许诺，硬生生被逼成了“人工 + 智能”的荒诞现实。创作者被拴在这条本该自动运转的流水线上，刚拧完一颗螺丝，下一颗又到了手边。

**见鬼，AI 都已经会用电脑了，为什么它们之间交接工作，还得你来切窗口？**

xdev 让消息在 AI 聊天之间自由往来，无论它们是否同机、是否使用同一款应用。你不必再来回切窗口、复制粘贴。一个 AI 直接把工作交给另一个，双方随时传信，不用抢通道，让流水线自己转起来。

跨设备协作时，Windows 电脑通过 SSH 和 SFTP 互通。AI 助手通过终端或本地 MCP 就能浏览、编辑和传输对端文件，在对端的 Windows 或 WSL 环境中执行命令，也能把耗时任务放到后台，查日志、取结果。对方客户端若保存了可读的聊天记录，或提供会话导出命令，接手的助手就能在授权范围内直接查阅指定聊天的原对话，核对先前的结论，追溯当时的判断依据，让交接更可靠。

这下，你有空打量自己的作品，也能匀出些精力琢磨下一步往哪里走。

## 功能概览

| 想做什么 | xdev 能做什么 |
| --- | --- |
| 让指定聊天接手工作 | 按设备、应用和聊天 ID 双向传信。同机的多个聊天也能各自交接。可以群发、转寄并附上原件，沿原信回复，或另回原作者。[会话通信](product/README.md#给指定聊天寄信) |
| 知道工作交接到了哪一步 | 发信前查对方最近报告的状态，寄出后分别查入箱、排队、已读、处理进展和回信。计划变了，可以申请撤回未读信、发送更正，也可以把需要留存的信归档到本地。[信件状态](design/mail-lifecycle.md#中文) · [保留与归档](design/mail-retention.md#中文) |
| 把原始材料带进讨论 | 读取已授权的网页标签或 Windows 窗口，填写表单、点击按钮，再回读结果。客户端提供文本记录或导出功能时，也能查阅指定聊天的原对话。[网页与窗口](product/README.md#读取网页与应用窗口) · [查阅原对话](#查阅原对话) |
| 在另一台电脑上动手 | 浏览、编辑和双向传输文件，在配对的 Windows 设备或其 WSL 环境中执行命令，把耗时任务放到后台，查日志、取结果。[文件和任务](product/README.md#文件和任务) |
| 把信送到另一个网络 | Windows 与 Linux 消息端可以通过配对的 SSH 连接直寄，也可以用自己的 GitHub 私仓暂存端到端加密的信件。接入来信事件的应用还能唤醒空闲的原聊天。[跨网选路](design/session-relay.md) · [云端接入](design/paired-folder-mail.md) |

## 我的 AI 应用能接吗？

支持本地 MCP 的应用可以调用 xdev 工具。要让来信直接抵达指定聊天，可以从这里入手。

- **用内置适配。** Codex、DeepSeek Harness、OpenCode 和部分 Claude Desktop 引擎已有原聊天收信适配。不同客户端可能立即接收，也可能要等下一轮。[客户端接入指南](product/CLIENTS.md) · [各客户端的收信方式](product/README.md#哪些客户端能接收原聊天通知)
- **为新应用或新版本自建模板。** 如果应用提供查找和通知指定聊天的接口，你或 AI 可以照示例把接口字段填进模板。服务已声明这两项能力时，也可以授权 xdev 自动匹配。用 `xdev ai --discover` 查看已授权的入口，再发一封测试信，核对原聊天已读并沿原信回复。能否唤醒空闲聊天取决于应用自身支持。[自建模板与验证示例](design/host-discovery.md#中文)

## 开始使用

安装方式，二选一。

| 自己安装 | 交给 AI 安装 |
| --- | --- |
| 从[最新正式版](https://github.com/realhdx/xdev/releases/latest)下载 Windows Pair 安装包和同名的 `.sha256` 文件，按[安装手册](product/README.md)操作。如需在多台设备上安装，各设备使用同一份安装包。 | 把下面这句话发给能操作目标电脑的 AI。它会自行下载、安装并验证。[AI 部署指南](product/AI-DEPLOY.md) |

```text
请从 https://github.com/realhdx/xdev 获取最新正式版，按包内 AI 部署指南在我指定的 Windows 设备上安装并验证 xdev。
```

从源码构建或运行测试见[构建指南](BUILDING.md)。

## 安装后使用

### 传回一份结果文件

安装和配对后，在 PowerShell 中运行下例。把 `实验室` 换成配对时登记的对端名称，把远端路径换成已有文件。两台电脑都可以发起传输。

```powershell
$xdev = Join-Path $env:LOCALAPPDATA 'CrossDeviceCollab\xdev.cmd'
$peer = '实验室'
& $xdev who --host $peer
& $xdev file pull --host $peer --remote 'D:\共享\结果.csv' --local '.\结果.csv' --no-overwrite
```

编辑远端已有文件时，可以把最初读取的 SHA-256 作为写回条件，防止覆盖他人刚保存的改动。耗时的计算可以放到目标机后台运行，之后按任务 ID 查状态和日志。

### 给指定聊天发信

在发件聊天中沿用上例的 `$xdev` 和 `$peer`。这里以 OpenCode 为例，把占位符换成收件方的聊天 ID。[读信和回复示例](product/README.md#给指定聊天寄信)

```powershell
& $xdev msg send --to $peer --client opencode --session '<聊天ID>' --subject '核对结果' --text '请核对导出文件的行数，并沿原信回复。'
```

### 查阅原对话

用 `xdev file read` 按行读取对端已有的文本记录，或用 `xdev exec` 调用客户端自己的导出命令。例如，OpenCode 的 `opencode export <聊天ID>` 会以 JSON 输出该聊天的记录。

## 运行要求

- **基础命令**使用 Windows x64、64 位 CPython 3.14 和 Windows PowerShell。每个接收跨机操作的设备需开放可达的 OpenSSH/SFTP 服务，并允许对端使用已验证的 SSH 身份登录。
- **本地 MCP**使用随安装包提供的离线 Python 依赖。接收已有 AI 聊天的来信还需要该客户端支持的接收方式。第三方库及其许可见[第三方组件](product/AI-DEPLOY.md#第三方组件)。
- **可选功能**按需启用。Windows 后台任务使用任务计划程序，Linux 命令在目标机已安装的 WSL 发行版内运行。选择消息代理投递时才需要 NATS JetStream。

网页操作需要 OpenCode Browse 和已配对的浏览器扩展。Windows 窗口操作使用系统辅助功能，支持的控件由应用决定。

## 版本更新

[最新正式版及下载](https://github.com/realhdx/xdev/releases/latest) · [本版更新说明](product/RELEASE-NOTES.md) · [历次版本记录](product/CHANGELOG.md)

## 参与改进

xdev 的跨会话通信已在 Codex、受支持的 Claude Desktop 引擎、DeepSeek Harness 和 OpenCode 中通过实测，支持向指定聊天发信，并沿原信收到答复。WorkBuddy 通过前台协助，Gemini 和豆包网页版通过浏览器转交，也完成了消息往返。

如果你在一款新的 AI 客户端里收到了第一封 xdev 来信，欢迎提一个 Pull Request，补上客户端版本、接入步骤和验证方法。如果消息停在了等待队列，可以开一个 Issue，告诉大家你希望它怎样抵达、实际又停在了哪一步。修改代码后可按[构建指南](BUILDING.md)运行测试。

如果 xdev 帮你省过一次来回传话，欢迎给项目一颗 Star。

☕ If xdev has saved you a few trips between chat windows, feel free to [buy me a coffee](https://ko-fi.com/dixihan).

## 许可与引用

xdev 由中国农业大学的韩迪熹（Dixi Han）设计和开发，钟强（Qiang Zhong）提供指导与经费支持。

xdev 项目编写的源码、脚本和适配器采用 MPL-2.0，允许商业使用和再分发。对外分发修改版时，接收者应能取得受该许可覆盖文件的对应源码。随包提供的第三方组件各自沿用上游许可。[项目许可证](LICENSE) · [第三方组件](product/AI-DEPLOY.md#第三方组件)

欢迎按 MPL-2.0 提交代码改进，贡献者保留自己贡献的著作权。在论文或产品文档中提到 xdev 时，欢迎引用所用的软件版本。[推荐引用](CITATION.cff)
