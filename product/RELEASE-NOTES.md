# xdev Windows Pair 1.1.0-preview.13 · 资料修订 1

**简体中文** · [English](RELEASE-NOTES.en.md)

这份资料修订可离线阅读。若想亲自验证两张同名网页能否独立操作，可运行随包的[独立浏览器试验](../examples/isolated-browser/README.md)。它在无窗口 Chrome 或 Edge 的临时配置中使用合成页面，需另备 Node.js 和 Playwright Core。已经安装 preview.13 的用户可以直接取用手册和示例。

相较于 1.0.3，1.1 预览系列把已授权的网页和 Windows 软件窗口接入了原有的文件、任务与会话协作。用 `xdev surface` 或 `xdev_surface` 可以读取网页正文或窗口控件，按控件引用填写、点击，再回读结果。读到的原文或提示可以附上来源，直接寄给需要它的聊天。网页入口使用 OpenCode Browse 和已配对的扩展；Windows 窗口使用系统辅助功能。[网页与窗口](README.md#读取网页与应用窗口)

本次预览还接通了跨网络的原聊天。多个互不相关的聊天可以同时交接，同一封群发信也能逐个收件人查看进展、沿各自的原信回复。[逐版更新](CHANGELOG.md) · [多会话协作](../design/mail-lifecycle.md)

发信前可查询指定聊天的状态与观测时间；发信后可区分入箱、排队、已读、处理及回信意向。新增未读召回、版本关联、临时附件配额与可校验的本地归档。私仓支持静默状态与撤回请求，配对目录支持按已登记的原会话唤醒。[状态与空间管理](../design/mail-lifecycle.md) · [配对目录接入](../design/paired-folder-mail.md)

应用插件可用本地到信事件唤醒空闲的原聊天。宿主负责启动自己的聊天处理流程。其他客户端仍可主动查收。

GitHub 信件到达后，xdev 会保存在指定聊天的本地信箱。聊天可用 `xdev msg inbox --wait 30` 在当前调用里等新信，并用返回的游标接续。Windows 可运行 `py -3.14 deploy/mail-listener.py install` 随登录启动收信，`status` 查看运行状态，`remove` 停用。

用自己的 GitHub 账号建一个专用私仓，设备就多了一条跨网收信路线。信件在端点加密，用临时 Release 附件投递，收端持久入箱后清理。私仓、密钥和接收端由各用户自行管理。 [GitHub 私有信箱](../design/github-mail.md)

本版增加常驻接收、只读目录发现和网络选路。常驻宿主可在自己的会话表里定位原聊天。陌生宿主可通过已授权目录声明接口，xdev 核对参数和原聊天后提交。网络可按对端配置优先直连，并在确认尚未投信时切换到已授权的加密中继。[接入与发现](../design/host-discovery.md) · [网络配置](../design/session-relay.md)

设备配对并接入应用后，支持自动寻址的宿主会定位收件聊天，并在首信附上原发件聊天的回信地址。DeepSeek Harness 可按 ID 恢复休眠的原聊天。

可以用 `direct` 在已配对、可达的设备之间直接寄信。Linux 收端可为配对设备绑定专用 SSH 密钥，限制该密钥只能投信和通知明确允许的聊天。当两边选择中继时，可在双方启用端到端加密，保护主题、聊天地址、正文、转附信件和回执。中继仍能观察配对的设备、传输时间和密文大小。

- **手动安装**。在目标 Windows 设备上核对 `xdev-windows-pair-1.1.0-preview.13-docs.1.zip` 和同名 `.sha256`，按[安装手册](README.md)操作。
- **交给 AI 安装**。把本发行页交给能操作目标设备的 AI，指定要接通的设备和应用。助手自行下载并按[AI 部署指南](AI-DEPLOY.md)安装验证。

Windows 安装包需要 64 位 CPython 3.14。Linux 端可解开包内的 `runtime.zip` 运行消息组件。选择中继时，两端需能连接同一服务并运行监听程序。宿主接纳、已读和实质回信可分别核对。实际配置和双向测试见[跨网络会话通信指南](../design/session-relay.md)。如选用 Amazon SQS FIFO，两端需另装 `boto3`。[托管短存队列指南](../design/managed-queue.md)
