# xdev Windows Pair

[English](README.en.md) | **简体中文**

xdev 让指定的 AI 聊天彼此寄信、沿原信回复，也能通过同一套 CLI 和 MCP 工具操作文件、运行任务。聊天可以在同一台 Windows 设备上，也可以位于已配对的不同设备上。

本手册对应 `windows-pair` 1.0.3，资料修订 6。已安装 1.0.3 的用户可直接使用本手册。[版本说明](CHANGELOG.md)

## 安装方式，二选一

- **[手动安装](#安装与配对)**。下载同一发行的安装包和校验文件，在所选 Windows 设备上安装。需要跨设备互通时再配置 SSH。
- **[交给 AI 安装](AI-DEPLOY.md)**。把下面一句话发给能操作目标电脑的助手，它会自行下载安装包。

```text
请从 https://github.com/realhdx/xdev 获取最新正式版，按包内 AI 部署指南在我指定的 Windows 设备上安装并验证 xdev。
```

## 运行条件

| 使用范围 | 需要准备 |
| --- | --- |
| 本机文件、命令和信箱 | Windows x64、64 位 CPython 3.14 和 Windows PowerShell。解释器须装在只含 ASCII 字符的路径中，例如 `C:\Python314\python.exe`。工作目录和设备主名可用中文。基础 CLI 使用 Python 标准库。 |
| 两台设备之间传输和执行 | 双方可达的局域网或私网、发起端的 OpenSSH 客户端、接收端的 OpenSSH Server 和 SFTP。双向操作时两端均按这两种角色配置。 |
| AI 调用本机工具 | 可运行本机命令的客户端可直接调用 CLI。支持本地 stdio MCP 的客户端可加载六个工具。默认安装用 pip 安装包内已锁定的第三方 Python 依赖，可离线完成。 |
| 可选功能 | Windows 后台作业使用任务计划程序和 `pythonw.exe`。WSL 命令需要目标机已注册的发行版，WSL 后台作业还需要发行版内的 `python3` 和 `wslpath`。选择 JetStream 消息代理时需另备 NATS 服务。 |

xdev 团队编写的软件及脚本采用 MPL-2.0。安装包根目录的 `LICENSE` 保存许可全文，推荐作者署名和版本见 `CITATION.cff`。随附依赖沿用各自的许可。[第三方组件说明](AI-DEPLOY.md#第三方组件)

## 安装与配对

### 1. 选择设备名称，按需配对

运行 `hostname` 查看 Windows 计算机名。xdev 默认采用该名称，也可用 `--name` 自行命名，用 `--alias` 增加别名。本机可写为 `local`。**设备主名是 xdev 中显示的地址，SSH 别名负责找到对端的账户与网络地址。** 例如，可以把设备称为「实验室」，在本机 SSH 配置中用 `lab-ssh` 连接它。

**仅在需要跨设备互通时配置 SSH。** 在发起端的 `~/.ssh/config` 分别为对机设置 `Host`、`HostName` 和 `User`，配置对该账户的公钥登录。首次连接时，将显示的主机密钥指纹与对方提供的指纹核对。Windows 提供 OpenSSH 可选功能，启用服务和配置用户密钥可按微软指南操作。[安装 OpenSSH](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse) · [配置密钥认证](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement)

在发起端的目标用户 PowerShell 中测试对端。如需双向操作，交换方向再测一次。将 `lab-ssh` 换成相应的 SSH 别名。第一条应返回对机的 Python 3.14.x，第二条应显示 SFTP 的远端工作目录，两条都应能免交互执行。

```powershell
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes lab-ssh "python --version"
"pwd" | sftp -b - -o BatchMode=yes -o StrictHostKeyChecking=yes lab-ssh
```

### 2. 校验并解压

从同一发行版本下载 `xdev-windows-pair-1.0.3-docs.6.zip` 和同名 `.zip.sha256` 校验文件。部署多台时使用同一份包。在每台目标电脑的下载目录运行下面的 PowerShell。若没有 `py` 启动器，以 `& '解释器的完整路径'` 替换示例中的 `py -3.14`。

```powershell
$zip = (Resolve-Path '.\xdev-windows-pair-1.0.3-docs.6.zip').Path
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

在每台已安装设备上，`--version` 应显示 `xdev 1.0.3 (windows-pair)`，`who` 应返回本机身份，`status` 应返回 `active_verified: true`。日常使用稳定入口 `xdev.cmd`，解压目录留作维护。

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

下面的例子在已绑定身份的发件聊天中运行。`opencode` 是收件客户端标识，`$chat` 填目标宿主中的真实聊天 ID。给对方一个简短主题。需要在本次调用中等待答复时，可以在首信加 `--wait-reply 60`。

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
- **查收**。在已绑定的聊天用 `msg inbox --subject '导出'` 按主题查找。设备入箱表示信已保存，`current_turn_accepted` 表示客户端已接纳当前回合。原聊天标读并返回关联答复后，才可确认对方处理了这封信。停在队列、超时或结果未知时，用原信 ID 继续查询。

## 接入 AI 工具

支持本机命令的助手可以使用 `xdev.cmd`。在支持本地 stdio MCP 的应用中添加名为 `xdev` 的服务，按下表填写后重连该服务。

| 字段 | 值 |
| --- | --- |
| 命令 | Python 3.14 解释器的完整路径。用 `py -3.14 -c "import sys; print(sys.executable)"` 查询。 |
| 参数 | 六项，依次为 `-I`、`-B`、`-X`、`utf8`、本机 `%LOCALAPPDATA%\CrossDeviceCollab\launch.py` 的**展开后绝对路径**、`mcp`。 |

在客户端调用 `xdev_who`。返回的 `_mcp_runtime.pinned_release` 是此连接实际加载的运行时摘要；应与 `active_release` 一致。若 `reload_required` 为真，重新连接这个 xdev 服务。支持技能的宿主还可将 ZIP 内的 `integrations/skills/xdev-local` 复制到它支持的技能目录。[批量配置与收件设置](AI-DEPLOY.md#接入客户端)

`xdev_msg` 的 `arguments` 是字符串数组，每个参数单独占一项。收件方调用 `{"action":"read","arguments":["<来信ID>"]}` 读信，再用 `{"action":"reply","arguments":["<来信ID>","--text","导出文件有 12 行数据。"]}` 回信。若宿主报告参数类型错误，用 `{"action":"whoami","arguments":[]}` 检查一次格式。仍不能传数组时，在同一聊天使用命令行入口。切换入口前先按原 ID 查询已经提交的操作。

### 哪些客户端能接收原聊天通知

| 接收器 | 使用条件与投递方式 |
| --- | --- |
| `codex` | 在目标聊天执行 `xdev ai --client codex --register` 完成登记。支持向当前回合提交，也支持显式排队。 |
| `dsh`、`opencode` | DeepSeek Harness 需要已加载接收插件的活动根聊天；OpenCode 需要能按原 ID 定位的宿主。默认向既有聊天提交，可选排队或仅在空闲时发送。正在运行的工具可能要到下一步才处理通知。 |
| `claude` | 受限原生入口适配 Windows Desktop 缓存引擎 2.1.325，需活跃登记。以该原聊天的接纳、已读和回信确认实际可用性。 |
| `workbuddy`、`antigravity`、`claude-channel`、`codebuddy-channel` | 需要相应插件或已启用的通道，绑定具体会话并显式选择排队。插件读取通知、输入框出现文本或通道写入成功，各自只证明相应步骤。 |

仅支持工具调用的宿主可在发件调用中等待回复。向网页聊天转交时，使用有权限操作该浏览器的助手，确认网站、浏览器配置和正式聊天地址。将获准发送的正文及信件 ID 提交后，重开同一聊天，核对已显示的原信和对应回答。由转交助手以自己的身份 `msg reply 原信ID` 并注明来源。常驻接收和忙时插话需由相应宿主另行支持。

## 升级、回退与换机

- **升级或回退**。在解压目录运行 `py -3.14 -I -B -X utf8 install.py status` 记下 `active`。升级运行 `install.py upgrade --expect-current 当前摘要`，回退运行 `install.py rollback --expect-current 当前摘要`，都使用同一 Python 前缀。在各升级或回退的设备核对版本，重连相关客户端的 xdev 服务。回退还可用 `--version` 或 `--release` 选择本机已有版本。
- **带信箱迁机**。先完成在途任务，暂停对旧信箱的写入。在旧机用 `py -3.14 -I -B -X utf8 migrate.py export --archive 'D:\备份\xdev-mail.zip'` 导出。新机登记原身份键及别名，按需重新配对 SSH，在**新装且尚未收信**的目标上，用 `migrate.py restore --archive 'D:\备份\xdev-mail.zip' --sha256 导出摘要` 恢复。文件附件、SSH 凭据、AI 账户和原聊天登记需分别处理。[迁移与兼容选项](AI-DEPLOY.md#迁移与兼容选项)
