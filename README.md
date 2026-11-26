# xdev —— AI 跨会话协作通道

**简体中文** · [English](README.en.md)　｜　[功能概览](#功能概览) · [我的应用怎么接？](#我的-ai-应用能接吗) · [Windows 安装](#windows-pair) · [Linux 安装](#linux-mail)

生成式 AI 的爆发降低了知识生产和艺术创作的技术门槛，让更多人得以施展才华，把想法迅速变成作品。产出能随算力翻番，人的注意力却不能。这种结构性的不对称把创作端提速的红利变成了审阅端的重压。一如工业革命初兴时的机器生产受制于手工操作和交接，今天的 AI 并行开工，却要排队等人传话。“人工智能”的美好许诺，硬生生被逼成了“人工 + 智能”的荒诞现实。创作者被拴在这条本该自动运转的流水线上，刚拧完一颗螺丝，下一颗又到了手边。

**见鬼，AI 都已经会用电脑了，为什么它们之间交接工作，还得你来切窗口？**

xdev 让消息在 AI 聊天之间自由往来，无论它们是否同机、是否使用同一款应用。你不必再来回切窗口、复制粘贴。一个 AI 直接把工作交给另一个，双方随时传信，不用抢通道，让流水线自己转起来。

Linux 上的程序也能用 Linux Mail（预览版）和配对的 Windows 电脑互寄信件，沿原信回复。Windows Pair 通过 SSH 和 SFTP 连接 Windows 电脑。AI 助手通过终端或本地 MCP 就能浏览、编辑和传输对端文件，在对端的 Windows 或 WSL 环境中执行命令，也能把耗时任务放到后台，查日志、取结果。对方客户端若保存了可读的聊天记录，或提供会话导出命令，接手的助手就能在授权范围内直接查阅指定聊天的原对话，核对先前的结论，追溯当时的判断依据，让交接更可靠。

这下，你有空打量自己的作品，也能匀出些精力琢磨下一步往哪里走。

## 功能概览

比如，笔记本上的助手从网页取数，把文件送到工作站跑分析，再把结果和数据来源交给另一个应用里的审阅聊天。对方问“这次算上空行了吗？”，问题沿原信回到发起分析的助手，它能翻出当时的命令和日志，把答案带回来。

| 想做什么 | xdev 能做什么 |
| --- | --- |
| 从网页取材或操作页面 | 借出一个已打开的标签页，或让 xdev 另开一页。助手能读标题和正文、点击链接、填写表单，再读回提交结果。[网页使用方法](product/README.md#读取网页与应用窗口) |
| 读取和操作 Windows 界面 | 通过辅助功能读出窗口公开的提示和控件状态。例如，先看“导出”按钮是否可用，按控件标识触发，再读取反馈，无需从截图里找坐标。[窗口使用方法](product/README.md#读取网页与应用窗口) |
| 把工作交给另一台电脑 | 浏览、编辑和双向传输文件，在配对的 Windows 设备或其 WSL 环境执行命令。耗时任务可以放到后台，稍后查日志、取结果。[文件和任务](product/README.md#文件和任务) |
| 找到该接手的聊天 | 按设备、应用和聊天 ID 双向传信。同机多个聊天也能分别通信。群发、带原件转寄、沿原信回复或另回原作者，都保留来处。[会话通信](product/README.md#给指定聊天寄信) · [查阅原对话](#查阅原对话) |
| 把信送到另一个网络 | 已配对的设备可通过 SSH 直寄。无法直连时，可另行配置自己的 GitHub 私有信箱来暂存端到端加密的信件。云端聊天也可经配对网关接入。[跨网选路](design/session-relay.md) · [云端接入](design/paired-folder-mail.md) |
| 知道对方接手了没有 | 寄前查最近报告的状态，寄后看入箱、排队、已读、处理进展和回信。计划有变可申请撤回未读信、发更正，重要原信可在本地归档。[信件状态](design/mail-lifecycle.md#中文) · [保留与归档](design/mail-retention.md#中文) |

## 我的 AI 应用能接吗？

支持本地 MCP 或本机命令的 AI 工具可以主动使用 xdev。对于尚无内置适配的应用，获准的助手可以先通过可读写的聊天界面完成交接。如果应用还提供按 ID 查找和通知原聊天的接口，助手可以把这些接口配成模板，让后续来信按地址直达。

- **内置适配已覆盖部分主流 AI 客户端。** Codex、DeepSeek Harness、OpenCode 和受支持的 Claude Desktop 引擎都已实测，指定聊天能收信并沿原信回复。能否唤醒空闲聊天、在忙时插话或排到下一轮，取决于具体接收方式。[各客户端的收信方式](product/README.md#哪些客户端能接收原聊天通知)
- **自建模板可适配提供会话接口的新应用和新版本。** 助手可以从获准读取的网页查阅接口文档，把查找和投递工具及其字段填进模板。若服务已声明这两项能力，也可授权 xdev 自动匹配。用 `xdev ai --discover --client <应用> --session <聊天ID>` 核对原聊天，再试寄一封信并核对回信。这份配置可供同一应用的其他聊天复用。[自建模板与验证示例](design/host-discovery.md#中文)
- **界面转交支持没有专用适配的聊天应用。** 获准的助手只要能认准目标聊天、提交消息并读回回答，就可以代为转交。它将问题连同原信编号递进去，核对答复后，以自己的身份沿原信回复并注明来源。Gemini 和豆包网页版已这样完成往返。[怎样转交](product/README.md#哪些客户端能接收原聊天通知) · [客户端实测](product/CLIENTS.md#工具显示已连接聊天还是用不了)

## 开始使用

### Linux Mail

Linux 消息端预览版使用 Python 3，已在 WSL 的 Ubuntu 和云端 Debian 13 上实测。下载 `.tar.gz` 和同名的 `.sha256` 校验文件，按指南解压并试寄第一封信。要与 Windows Pair 或另一台 Linux 主机通信，可接入已有的 SSH 配对。[下载预览版](https://github.com/realhdx/xdev/releases/tag/linux-mail-v1.1.0-preview.1) · [Linux 安装与配对](linux-mail/README.md)

### Windows Pair

正式版需要 Windows x64、64 位 CPython 3.14 和 Windows PowerShell。安装方式二选一。

| 自己安装 | 交给 AI 安装 |
| --- | --- |
| 下载 [Windows Pair 1.1.1](https://github.com/realhdx/xdev/releases/tag/v1.1.1) 的 ZIP 安装包和同名的 `.sha256` 校验文件，按[安装手册](product/README.md)操作。如需在多台设备上安装，各设备使用同一份安装包。 | 把下面这句话发给能操作目标电脑的 AI。它会自行下载、安装并验证。[AI 部署指南](product/AI-DEPLOY.md) |

```text
请从 https://github.com/realhdx/xdev/releases 下载最新正式版 xdev Windows Pair，按包内 AI 部署指南在我指定的 Windows 设备上安装并验证。
```

#### 运行要求

跨设备传文件或执行命令，需要接收端开放已配对的 SSH/SFTP 连接。网页操作需要 OpenCode Browse 和已配对的扩展。Windows 界面需要可访问的登录桌面。接收端可分别为网页和窗口开放只读或操作权限。安装包已附本地 MCP 的离线依赖。[完整运行条件及可选组件](product/README.md#运行条件)

## 安装后使用

### 给指定聊天发信

在已登记发件身份的聊天里运行这段 PowerShell。把 `实验室` 换成已配对设备的名称，填入收件方的客户端和聊天 ID。下面以 OpenCode 为例。

```powershell
$xdev = Join-Path $env:LOCALAPPDATA 'CrossDeviceCollab\xdev.cmd'
$peer = '实验室'
& $xdev msg send --to $peer --client opencode --session '<聊天ID>' --subject '核对结果' --text '请核对导出文件的行数，并沿原信回复。'
```

在收件聊天读信并回复。发件聊天看到关联回信，就验证了双向交接。若只看到排队，可沿原信编号查进度。[读信与回信命令](product/README.md#给指定聊天寄信)

### 传回一份结果文件

要独立核对行数，可以从同一台设备取回文件。沿用上例变量，把远端路径换成实际文件的位置。

```powershell
& $xdev file pull --host $peer --remote 'D:\共享\结果.csv' --local '.\结果.csv' --no-overwrite
```

### 查阅原对话

用 `xdev file read` 按行读取对端已有的文本记录，或用 `xdev exec` 调用客户端自己的导出命令。例如，OpenCode 的 `opencode export <聊天ID>` 会以 JSON 输出该聊天的记录。

## 版本更新

[所有发行版及下载](https://github.com/realhdx/xdev/releases) · [Windows Pair 更新说明](product/RELEASE-NOTES.md) · [历次版本记录](product/CHANGELOG.md)

## 参与改进

从 Codex、Claude Desktop 的原聊天直寄，到 WorkBuddy 的前台协助和网页聊天的界面转交，每种接法都有自己的验证记录。[已实测的客户端](product/CLIENTS.md)

如果你在一款新的 AI 客户端里收到了第一封 xdev 来信，欢迎提一个 Pull Request，补上客户端版本、接入步骤和验证方法。如果消息停在了等待队列，可以开一个 Issue，告诉大家你希望它怎样抵达、实际又停在了哪一步。修改代码后可按[构建指南](BUILDING.md)运行测试。

如果 xdev 帮你省过一次来回传话，欢迎给项目一颗 Star。

☕ If xdev has saved you a few trips between chat windows, feel free to [buy me a coffee](https://ko-fi.com/dixihan).

## 许可与引用

xdev 由中国农业大学的韩迪熹（Dixi Han）设计和开发，钟强（Qiang Zhong）提供指导与经费支持。

xdev 项目编写的源码、脚本和适配器采用 MPL-2.0，允许商业使用和再分发。对外分发修改版时，接收者应能取得受该许可覆盖文件的对应源码。随包提供的第三方组件各自沿用上游许可。[项目许可证](LICENSE) · [第三方组件](product/AI-DEPLOY.md#第三方组件)

欢迎按 MPL-2.0 提交代码改进，贡献者保留自己贡献的著作权。在论文或产品文档中提到 xdev 时，欢迎引用所用的软件版本。[推荐引用](CITATION.cff)
