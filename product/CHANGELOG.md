# 版本记录

**简体中文** · [English](CHANGELOG.en.md)

## Windows Pair 1.1.0-preview.13 · 资料修订 1 · 2026-11-22

解压即可离线阅读[本版功能总览](RELEASE-NOTES.md)，从网页和 Windows 窗口的读取与操作，查到跨网投递、原聊天唤醒、并行收发及归档指南。另附 WorkBuddy 插件的会话收信指引及可选的[独立浏览器试验](../examples/isolated-browser/README.md)。运行时与离线依赖沿用 preview.13；已安装该版的用户可直接使用新资料。

## Windows Pair 1.1.0-preview.13 · 2026-11-21

多组会话可同时寄信、按原信独立查进度；群发会分别返回每位收件人的投递和状态证据。两端时钟不一致时，预检使用发件端的查询时间判断信息是否新鲜。[会话协作](../design/mail-lifecycle.md)

## Windows Pair 1.1.0-preview.12 · 2026-11-21

发信前可查询指定聊天的状态与观测时间；发信后可区分入箱、排队、已读、处理及回信意向。新增未读召回、版本关联、临时附件配额与可校验的本地归档。私仓支持静默状态与撤回请求，配对目录支持按已登记的原会话唤醒。[状态与空间管理](../design/mail-lifecycle.md) · [配对目录接入](../design/paired-folder-mail.md)

## Windows Pair 1.1.0-preview.11 · 2026-11-21

应用插件现在能在后台收到指定原聊天的新信事件。有本机宿主接入时，聊天可以保持空闲，原信和关联回信抵达后由应用唤醒原聊天。[GitHub 信箱与宿主接入](../design/github-mail.md)

## Windows Pair 1.1.0-preview.10 · 2026-11-21

原聊天主动等信时，本机收件器会从空闲节奏切回活跃检查，仍遵守 GitHub 的限流等待。短暂无法完整下载的附件会在原 ID 下复查，发送方不必补寄。

## Windows Pair 1.1.0-preview.9 · 2026-11-21

Windows 后台收件器会在无终端的登录任务中核对启动状态。GitHub 监听器在信箱持续空闲时逐步延长检查间隔，减少对用户账号的 API 请求。[使用与限额说明](../design/github-mail.md)

## Windows Pair 1.1.0-preview.8 · 2026-11-21

GitHub 信件到达后，xdev 会保存在指定聊天的本地信箱。聊天可用 `xdev msg inbox --wait 30` 在当前调用里等新信，并用返回的游标接续。Windows 可运行 `py -3.14 deploy/mail-listener.py install` 随登录启动收信，`status` 查看运行状态，`remove` 停用。

## Windows Pair 1.1.0-preview.7 · 2026-11-21

用自己的 GitHub 账号建一个专用私仓，设备就多了一条跨网收信路线。信件在端点加密，用临时 Release 附件投递，收端持久入箱后清理。私仓、密钥和接收端由各用户自行管理。 [GitHub 私有信箱](../design/github-mail.md)

## Windows Pair 1.1.0-preview.6 · 2026-11-21

接入原聊天时，可使用应用内的常驻接收器或机器可读的工具目录。匹配目录会核对宿主、原会话和投递位置。同一封信若已开始提交而结果未知，保留原记录。跨设备邮件支持在双方已配置的路线中自动选择；明确未提交时才可切换备用路线。[会话接入](../design/host-discovery.md) · [网络路线](../design/session-relay.md)

## Windows Pair 1.1.0-preview.5 · 2026-11-21

DeepSeek Harness 接入一次后，发件聊天的身份随工具调用自动取得。收件宿主按地址找到原聊天，回信沿原信返回。升级前已登记、后来休眠的聊天也使用同一套恢复机制。已接纳或结果未明的信件保留原投递记录，避免重复唤醒。

## Windows Pair 1.1.0-preview.4 · 2026-11-21

启用 `direct` 可通过 SSH 直接寄信。Linux 收件端可为每个对端配置仅接收邮件的专用 SSH 入口。使用中继时，可在两端为该设备对启用端到端加密，连同通知、转寄原件和回执一起加密。已要求加密的设备拒收明文。也可选用 Amazon SQS FIFO 暂存密文，按需另装 `boto3`。[托管队列指南](../design/managed-queue.md) · [部署与验收指南](../design/session-relay.md)

旧版有时仅凭回信地址就返回 `bidirectional_ready: true`。新版用 `return_address_known` 标明是否已知地址，`return_session_status` 说明回程状态。无法核实回信入口时，`bidirectional_ready` 返回 `null`。

## Windows Pair 1.1.0-preview.3 · 2026-11-21

新增跨网络会话中继。两端主动连接同一台受信任的 NATS JetStream 服务，按设备、应用和会话地址双向收发信件。第三方应用可在本机为既有会话登记接收程序，分别报告当前轮次接纳、排入下一轮或原会话接纳。xdev 按消息 ID 去重，接纳结果未知时保留记录，已读和回信可独立核对。[部署与验收指南](../design/session-relay.md)

## Windows Pair 1.1.0-preview.2

CLI 和 MCP 的使用说明新增了页面和应用仍在加载或处理操作时的示例。界面结果会说明当前状态，并给出下一次只读调用，沿用同一设备和目标。操作结果尚不确定时，可先查看目标，避免重复执行。

## Windows Pair 1.1.0-preview.1

新增网页与 Windows 控件的结构化读取与操作，支持 CLI 和 MCP。远程访问由接收端按需开启。

## Windows Pair 1.0.3 · 资料修订 6 · 2026-11-20

首页和安装文档并列给出手动安装和 AI 安装两个入口。手动安装从发行页面下载。交给 AI 时，一条指令即可让它获取正式版并在指定设备上安装、验证。单机可独立使用，多台设备各用同一份包，需要互通时再配对。

## Windows Pair 1.0.3 · 资料修订 5 · 2026-11-20

首页、安装与使用手册、AI 部署指南、构建指南和发行资料均可切换中英文。首次安装可在同一份 ZIP 中选择手动安装或交给 AI 安装。首页新增已在真实聊天中验证过的客户端与交接方式。已安装 1.0.3 的用户可直接使用新手册。

## Windows Pair 1.0.3 · 资料修订 4 · 2026-11-19

安装包根目录的 `CITATION.cff` 更新了软件的推荐引用署名。完整安装包为 `xdev-windows-pair-1.0.3-docs.4.zip`。已有 1.0.3 安装可以直接使用这份引用资料。

## Windows Pair 1.0.3 · 资料修订 3 · 2026-11-19

xdev 自有源码采用 MPL-2.0。新安装包 `xdev-windows-pair-1.0.3-docs.3.zip` 在根目录附上许可全文（`LICENSE`）和推荐引用信息（`CITATION.cff`）。第三方组件继续适用各自的许可。运行时和离线依赖沿用 1.0.3，已有安装无需升级。

## Windows Pair 1.0.3 · 资料修订 2 · 2026-11-19

首次安装可从手动安装或 AI 部署两条路径开始。手册增加了按功能划分的依赖说明及随包第三方组件的来源和许可。完整安装包为 `xdev-windows-pair-1.0.3-docs.2.zip`。已有 1.0.3 安装可以直接使用本版文档，运行时和接收插件沿用 1.0.3。

## Windows Pair 1.0.3 · 资料修订 1 · 2026-11-19

补充 MCP 参数示例、命令行回退、按消息 ID 核对送达，以及将消息转交给已有网页聊天的操作说明。

## Windows Pair 1.0.3 · 2026-11-19

客户端配置文件被 Windows 短时占用时，安装器会有限重试。每次重试前核对原摘要，保留其他应用或用户刚写入的修改。

## Windows Pair 1.0.2 · 2026-11-19

增加 Copilot、CodeBuddy、Gemini CLI、Kimi Code、VS Code 和 Cursor 的客户端配置入口。更新已有配置时保留其他条目，可使用变更回执恢复。新增帮助 AI 发现本地命令的可选技能 `xdev-local`。

## Windows Pair 1.0.1 · 资料修订 1 · 2026-11-19

补充客户端收信与重连、Windows 多行文件写入及共享 Python 依赖目录的升级说明。

## Windows Pair 1.0.1 · 2026-11-19

新安装默认按 Windows 设备名寻址，也可自定义主名和别名。与旧地址并存的设备可滚动升级，既有消息、引用和回信沿用原地址。

## Windows Pair 1.0.0 · 2026-11-19

首次发布双 Windows 私有协作工具。支持文件传输与摘要校验、远端命令、后台任务和 WSL 执行。会话信箱支持主题、定向回复、转寄原件和群发。安装器提供升级、回退和私人信箱迁移。

## 如何识别版本

`xdev --version` 显示产品系列和运行时版本，`install.py status` 显示本机启用的运行时摘要。仅更新文档的完整安装包在文件名中增加 `-docs.N`，每份安装包各有 SHA-256 校验文件。客户端接收插件可采用其宿主所需的独立版本号。

[产品手册](README.md) · [AI 部署指南](AI-DEPLOY.md)
