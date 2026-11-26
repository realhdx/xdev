# xdev Windows Pair

[English](README.en.md) | **简体中文**

xdev 让指定的 AI 聊天彼此寄信、沿原信回复，也能通过同一套 CLI 和 MCP 工具操作文件、运行任务。聊天可以在同一台 Windows 设备上，也可以位于已配对的不同设备上。

本手册对应 `windows-pair` 1.1.1。[客户端接入与验证](CLIENTS.md) · [本版新功能](RELEASE-NOTES.md) · [版本记录](CHANGELOG.md)

从[网页和 Windows 窗口](#读取网页与应用窗口)读取材料后，可以把摘录、来源和问题寄给指定聊天。随包手册还涵盖[原聊天自动寻址](../design/host-discovery.md)、[空闲唤醒](../design/paired-folder-mail.md)、[多人交接](../design/mail-lifecycle.md)和[归档恢复](../design/mail-retention.md)。WorkBuddy 插件附有[原聊天查信与回信指引](../integrations/workbuddy/skills/xdev-session-mail/SKILL.md)。

选用 `direct` 时，xdev 通过已配对的 SSH 连接寄信。需要跨网暂存时，可在两端为该设备对启用端到端加密，交给自选中继保存密文。第三方 AI 应用可以登记自己的会话地址。[Windows 与 Linux 接入指南](../design/session-relay.md) · [使用 Amazon SQS 暂存密文](../design/managed-queue.md)

发信前可查询指定聊天的状态与观测时间；发信后可区分入箱、排队、已读、处理及回信意向。新增未读召回、版本关联、临时附件配额与可校验的本地归档。私仓支持静默状态与撤回请求，配对目录支持按已登记的原会话唤醒。[状态与空间管理](../design/mail-lifecycle.md) · [配对目录接入](../design/paired-folder-mail.md)

## 安装方式，二选一

- **[手动安装](#安装与配对)**。下载同一发行的安装包和校验文件，在所选 Windows 设备上安装。跨设备传文件或执行命令时再配置 SSH。
- **[交给 AI 安装](AI-DEPLOY.md)**。把下面一句话发给能操作目标电脑的助手。

```text
请从 https://github.com/realhdx/xdev/releases/tag/v1.1.1 下载 xdev 安装包和校验文件，按包内 AI 部署指南在我指定的 Windows 设备上安装并验证。
```

## 运行条件

| 使用范围 | 需要准备 |
| --- | --- |
| 本机文件、命令和信箱 | Windows x64、64 位 CPython 3.14 和 Windows PowerShell。解释器须装在只含 ASCII 字符的路径中，例如 `C:\Python314\python.exe`。工作目录和设备主名可用中文。基础 CLI 使用 Python 标准库。 |
| 两台设备之间传输和执行 | 双方可达的局域网或私网、发起端的 OpenSSH 客户端、接收端的 OpenSSH Server 和 SFTP。双向操作时两端均按这两种角色配置。 |
| AI 调用本机工具 | 可运行本机命令的客户端可直接调用 CLI。支持本地 stdio MCP 的客户端可加载七个工具。默认安装用 pip 安装包内已锁定的第三方 Python 依赖，可离线完成。 |
| 可选功能 | Windows 后台作业使用任务计划程序和 `pythonw.exe`。WSL 命令需要目标机已注册的发行版，WSL 后台作业还需要发行版内的 `python3` 和 `wslpath`。选择 JetStream 消息代理时需另备 NATS 服务。 |

xdev 团队编写的软件及脚本采用 MPL-2.0。安装包根目录的 `LICENSE` 保存许可全文，推荐作者署名和版本见 `CITATION.cff`。随附依赖沿用各自的许可。[第三方组件说明](AI-DEPLOY.md#第三方组件)

## 安装与配对

### 1. 选择设备名称，按需配对

运行 `hostname` 查看 Windows 计算机名。xdev 默认采用该名称，也可用 `--name` 自行命名，用 `--alias` 增加别名。本机可写为 `local`。**设备主名是 xdev 中显示的地址，SSH 别名负责找到对端的账户与网络地址。** 例如，可以把设备称为「实验室」，在本机 SSH 配置中用 `lab-ssh` 连接它。

**跨设备传文件和执行命令需要 SSH。** 在发起端的 `~/.ssh/config` 分别为对机设置 `Host`、`HostName` 和 `User`，配置对该账户的公钥登录。首次连接时，将显示的主机密钥指纹与对方提供的指纹核对。Windows 提供 OpenSSH 可选功能，启用服务和配置用户密钥可按微软指南操作。[安装 OpenSSH](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse) · [配置密钥认证](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement)

在发起端的目标用户 PowerShell 中测试对端。如需双向操作，交换方向再测一次。将 `lab-ssh` 换成相应的 SSH 别名。第一条应返回对机的 Python 3.14.x，第二条应显示 SFTP 的远端工作目录，两条都应能免交互执行。

```powershell
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes lab-ssh "python --version"
"pwd" | sftp -b - -o BatchMode=yes -o StrictHostKeyChecking=yes lab-ssh
```

### 2. 校验并解压

从同一发行版本下载 `xdev-windows-pair-1.1.1.zip` 和同名 `.zip.sha256` 校验文件。部署多台时使用同一份包。在每台目标电脑的下载目录运行下面的 PowerShell。若没有 `py` 启动器，以 `& '解释器的完整路径'` 替换示例中的 `py -3.14`。

```powershell
$zip = (Resolve-Path '.\xdev-windows-pair-1.1.1.zip').Path
$expected = ((Get-Content -LiteralPath ($zip + '.sha256') -TotalCount 1) -split '\s+')[0]
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ine $expected) { throw '发布包校验失败' }
$kit = Join-Path (Split-Path $zip) ([IO.Path]::GetFileNameWithoutExtension($zip))
if (Test-Path -LiteralPath $kit) { throw '请选择新的解压目录' }
Expand-Archive -LiteralPath $zip -DestinationPath $kit
Set-Location -LiteralPath $kit
py -3.14 -I -B -X utf8 verify.py .
if ($LASTEXITCODE -ne 0) { throw '包内文件校验失败' }
```

### 3. 在各自的用户账户中安装

**仅本机使用。** 在目标用户账户中运行以下命令，默认安装 CLI 和本地 MCP，目标设备可写为 `local`。仅需 CLI 时加 `--cli-only`。

```powershell
py -3.14 -I -B -X utf8 install.py install --expect-current none
```

**跨设备互通。** 下面以「书房」和「实验室」双向配对为例。将主名和 SSH 别名替换成你的设置。沿用 Windows 名时，省略本机的 `--name`，并在对机的 `--peer` 填写这个 Windows 名。

```powershell
# 在书房机运行
py -3.14 -I -B -X utf8 install.py install --expect-current none --name 书房 --peer 实验室 --peer-ssh lab-ssh
# 在实验室机运行
py -3.14 -I -B -X utf8 install.py install --expect-current none --name 实验室 --peer 书房 --peer-ssh study-ssh
```

已有 xdev 的设备按「升级、回退与换机」操作。需要迁入旧信箱时，先恢复备份，再开始收信。

### 4. 验证安装

在每台已安装设备上，`--version` 应显示 `xdev 1.1.1 (windows-pair)`，`who` 应返回本机身份，`status` 应返回 `active_verified: true`。日常使用稳定入口 `xdev.cmd`，解压目录留作维护。

```powershell
$xdev = Join-Path $env:LOCALAPPDATA 'CrossDeviceCollab\xdev.cmd'
$peer = 'local'   # 以下示例默认使用本机
& $xdev --version
& $xdev who
py -3.14 -I -B -X utf8 install.py status
```

需要验证已配置的 SSH 配对时，将 `$peer` 设为对机主名，运行 `& $xdev who --host $peer` 核对远端身份。如需双向互通，在对机反向验证。

## 文件和任务

示例使用上一步的 `$xdev` 和 `$peer`；`D:\共享` 代表目标机上已有、当前账户可写的目录。

| 任务 | PowerShell 示例 |
| --- | --- |
| 浏览目录 | `& $xdev file ls --host $peer --path 'D:\共享'` |
| 发文件 | `& $xdev file push --host $peer --local '.\input.csv' --remote 'D:\共享\input.csv' --no-overwrite` |
| 核对内容 | `& $xdev file stat --host $peer --path 'D:\共享\input.csv' --with-sha256`，将结果与本机 `Get-FileHash .\input.csv -Algorithm SHA256` 比较 |
| 取回文件 | `& $xdev file pull --host $peer --remote 'D:\共享\result.csv' --local '.\result.csv' --no-overwrite` |
| 有条件地更新 | 先读取远端摘要；用 `file push --base-sha256 原摘要` 写回修改后的文件。其他人改过该文件时，本次写入会停止。 |
| 执行命令 | `& $xdev exec --host $peer -- hostname`。若对机已安装 Ubuntu，可用 `& $xdev exec --host $peer --wsl Ubuntu -- uname -s`。 |
| 后台运行 | `& $xdev job start --host $peer -- python --version` 返回任务 ID。随后用 `job status --host $peer 任务ID` 查状态，`job log --host $peer 任务ID` 查输出。 |

多行正文先保存为本地 UTF-8 文件，再用 `& $xdev file create --host $peer --path 'D:\共享\notes.txt' --content-file '.\notes.txt'` 创建。通过 MCP 时也可将多行正文整体作为 `xdev_file` 的一个 `--content` 参数。按具体子命令加 `--help` 可查看完整选项。

## 给指定聊天寄信

收件地址由**设备主名、客户端标识和原聊天 ID**组成。独立配置目录还可限定对应的用户配置。设备邮箱适合程序间留信；希望 AI 立即处理时，须指定能接收该信的原聊天。运行 `& $xdev ai --capabilities` 查看本版本各接收器支持的模式。

在发件聊天中运行下面的例子。支持自动识别的宿主会填写发件身份和回信地址。`opencode` 是收件客户端标识，`$chat` 填目标宿主中的真实聊天 ID。给对方一个简短主题。需要在本次调用中等待答复时，可以在首信加 `--wait-reply 60`。

```powershell
& $xdev msg whoami
$chat = '<目标聊天ID>'
& $xdev msg send --to $peer --client opencode --session $chat --subject '核对导出行数' --text '请回复导出文件的数据行数。'
```

在**收件聊天**内按通知中的 ID 读信和回复，回复会沿原信地址返回。

```powershell
& $xdev msg read '<来信ID>'
& $xdev msg reply '<来信ID>' --text '导出文件有 12 行数据。'
```

- **转寄与引用**。用 `msg forward 来信ID --to 设备 --client 客户端 --session 聊天ID --text '补充意见'` 附上完整原件。新收件人用 `msg reply` 回复转寄者，加 `--audience original-author` 可另回原作者。
- **群发**。首信可用 `--also 设备 客户端 聊天ID` 追加收件人，各自保留已读和回信。用 `--audience all` 回复讨论成员。正文中引用的外部文件可用 `file push` 另行传送。
- **查收**。在收件聊天用 `msg inbox --subject '导出'` 按主题查找。设备入箱表示信已保存，`current_turn_accepted` 表示客户端已接纳当前回合。原聊天标读并返回关联答复后，才可确认对方处理了这封信。停在队列、超时或结果未知时，用原信 ID 继续查询。

## 接入 AI 工具

支持本机命令的助手可以使用 `xdev.cmd`。在支持本地 stdio MCP 的应用中添加名为 `xdev` 的服务，按下表填写后重连该服务。

| 字段 | 值 |
| --- | --- |
| 命令 | Python 3.14 解释器的完整路径。用 `py -3.14 -c "import sys; print(sys.executable)"` 查询。 |
| 参数 | 六项，依次为 `-I`、`-B`、`-X`、`utf8`、本机 `%LOCALAPPDATA%\CrossDeviceCollab\launch.py` 的**展开后绝对路径**、`mcp`。 |

在客户端调用 `xdev_who`。返回的 `_mcp_runtime.pinned_release` 是此连接实际加载的运行时摘要；应与 `active_release` 一致。若 `reload_required` 为真，重新连接这个 xdev 服务。支持技能的宿主还可将 ZIP 内的 `integrations/skills/xdev-local` 复制到它支持的技能目录。[批量配置与收件设置](AI-DEPLOY.md#接入客户端)

`xdev_msg` 的 `arguments` 是字符串数组，每个参数单独占一项。收件方调用 `{"action":"read","arguments":["<来信ID>"]}` 读信，再用 `{"action":"reply","arguments":["<来信ID>","--text","导出文件有 12 行数据。"]}` 回信。若宿主报告参数类型错误，用 `{"action":"whoami","arguments":[]}` 检查一次格式。仍不能传数组时，在同一聊天使用命令行入口。切换入口前先按原 ID 查询已经提交的操作。

### 哪些客户端能接收原聊天通知

GitHub 信件到达后，xdev 会保存在指定聊天的本地信箱。聊天可用 `xdev msg inbox --wait 30` 在当前调用里等新信，并用返回的游标接续。Windows 可运行 `py -3.14 deploy/mail-listener.py install` 随登录启动收信，`status` 查看运行状态，`remove` 停用。

用自己的 GitHub 账号建一个专用私仓，设备就多了一条跨网收信路线。信件在端点加密，用临时 Release 附件投递，收端持久入箱后清理。私仓、密钥和接收端由各用户自行管理。 [GitHub 私有信箱](../design/github-mail.md)

第三方应用可让通知进入自己的常驻进程，或公开工具目录供 xdev 按语义和参数匹配。用 `xdev ai --discover` 查看这台设备已授权的入口。选择目标时可加 `--client 应用 --session 聊天ID` 做只读核对。[常驻接收](../design/resident-host.md) · [接口发现与模板](../design/host-discovery.md)

| 接收器 | 使用条件与投递方式 |
| --- | --- |
| `codex` | 聊天第一次调用 xdev 邮箱工具时会自动登记。设备上已有唯一可用的 Codex 宿主时，也能通过宿主按 ID 核实并通知其他原聊天。支持向当前回合提交或显式排队。 |
| `dsh`、`opencode` | DeepSeek Harness 新装选 `--with-dsh`，已有安装可接入 `dsh` 客户端。宿主按原 ID 定位各自的根聊天。OpenCode 直接使用原会话 ID。两者都支持即时、排队或仅空闲时投递。 |
| `claude` | 受限原生入口适配 Windows Desktop 缓存引擎 2.1.325，需活跃登记。以该原聊天的接纳、已读和回信确认实际可用性。 |
| `workbuddy`、`claude-channel`、`codebuddy-channel` | 启用相应插件或官方通道后，宿主可在打开或接入聊天时自动登记。发送时显式选择排队。以宿主回执或原聊天已读核对进度。 |
| `antigravity` | 原生侧车可一次接入一个项目，按 ID 核对并投递给该项目的已有根聊天，也可只开放指定聊天。发送时用 `--queue`。原聊天接纳后的忙时排队位置仍需宿主确认。 |

仅支持工具调用的宿主可在发件调用中等待回复。向网页聊天转交时，使用有权限操作该浏览器的助手，确认网站、浏览器配置和正式聊天地址。将获准发送的正文及信件 ID 提交后，重开同一聊天，核对已显示的原信和对应回答。由转交助手以自己的身份 `msg reply 原信ID` 并注明来源。常驻接收和忙时插话需由相应宿主另行支持。

## 读取网页与应用窗口

`xdev surface` 与 MCP 工具 `xdev_surface` 能把页面或窗口读成文本和控件列表。同一个会话可以先定位目标、读取快照，再按快照中的控件引用操作。操作后重新读取目标，就能核对实际变化。

对浏览器中已授权的标签页，`xdev surface tools --provider browser --target 标签页ID` 可以只读列出网站公开的 WebMCP 工具。页面能打开、能输入文字、能向原会话提交消息，分别需要核实。

**第一次调用可能要等几秒。** 如果打开网页后返回 `opened_pending`，说明标签页已经创建，页面仍在加载。稍等约 2 秒，按返回的 `next_call` 读取同一设备上的同一标签页。看到预期的标题、正文或按钮就可以继续。如果第一次只读到加载提示，再稍等并读取一次；仍未出现时，报告最后看到的内容。点击或填写后也可能要等应用处理；看到“已派发”时，先找页面上的结果或确认信息，避免重复点击或提交。

```powershell
& $xdev surface targets --provider browser
& $xdev surface snapshot --provider browser --target '<标签 ID>'
& $xdev surface targets --provider windows --app notepad
& $xdev surface snapshot --provider windows --target '<窗口 ID>'
& $xdev surface invoke --provider windows --target '<窗口 ID>' --ref '<按钮引用>'
& $xdev surface snapshot --provider windows --target '<同一窗口 ID>'
```

**网页。** 需要本机已有可执行的 OpenCode Browse 和已配对的浏览器扩展。用户可通过扩展借出已打开的标签，或用 `surface open --provider browser --value https://example.com` 打开一个新标签；用 `close` 归还或关闭对应标签。引用只用于取得它的标签。页面导航后重新读取快照。[Browse 使用说明](https://opencode.ai/product/browse)

**独立浏览器实验。** 包内另附可选的[隔离浏览器自测](../examples/isolated-browser/README.md)，用于在单独的浏览器环境里试验读取和操作合成页面。这个示例可脱离 OpenCode Browse 运行，与上述日常 CLI/MCP 入口分别使用。

**Windows 应用。** 使用系统自带的 UI Automation，当前登录桌面上的程序须向辅助功能接口提供控件。`invoke` 调用按钮等控件的默认动作，`set-value` 使用 `--ref` 和 `--value` 填写可编辑控件。每个窗口支持的动作以快照为准。遇到超时先回读状态，避免重复操作。[微软 UI Automation 说明](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/ui-automation-overview)

**跨设备。** 在配对设备上使用时加 `--host 设备名`。接收端默认关闭远程界面访问。要让已有配对账户读取选定类型的界面，可在接收端 xdev 的 `config.json` 中合并 `"surface_remote": {"browser": "read"}`。`read` 允许列出目标和读取快照。`interact` 另外允许操作该类型的界面。两类入口分别设置。远端必须能连接其浏览器扩展或登录桌面；Windows SSH 会话常处于非交互桌面，此时由桌面所在的会话读取并只转交任务需要的内容。

## 升级、回退与换机

- **升级或回退**。在解压目录运行 `py -3.14 -I -B -X utf8 install.py status` 记下 `active`。升级运行 `install.py upgrade --expect-current 当前摘要`，回退运行 `install.py rollback --expect-current 当前摘要`，都使用同一 Python 前缀。在各升级或回退的设备核对版本，重连相关客户端的 xdev 服务。回退还可用 `--version` 或 `--release` 选择本机已有版本。
- **带信箱迁机**。先完成在途任务，暂停对旧信箱的写入。在旧机用 `py -3.14 -I -B -X utf8 migrate.py export --archive 'D:\备份\xdev-mail.zip'` 导出。新机登记原身份键及别名，按需重新配对 SSH，在**新装且尚未收信**的目标上，用 `migrate.py restore --archive 'D:\备份\xdev-mail.zip' --sha256 导出摘要` 恢复。文件附件、SSH 凭据、AI 账户和原聊天登记需分别处理。[迁移与兼容选项](AI-DEPLOY.md#迁移与兼容选项)
