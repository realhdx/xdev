# xdev Linux Mail · Linux 消息端

**简体中文** · [English](README.en.md)

Linux 消息端预览版 `1.1.1-preview.1`。它使用与 Windows Pair 1.1.1 相同的信件格式和公开核心，直接在 Linux 的 Python 环境中运行，可向本机或已配对的设备发信、读信并沿原信回复。配对和信箱保存在程序目录之外，可随升级沿用。

## 安装

在自己的 Linux 用户账户下，准备 Python 3（本次使用 3.12）和一个空的解压目录。本地信箱使用 Python 标准库。将压缩包和同名的 `.sha256` 文件放在一起，执行：

```sh
sha256sum -c xdev-linux-mail-1.1.1-preview.1.tar.gz.sha256
tar -xzf xdev-linux-mail-1.1.1-preview.1.tar.gz
cd xdev-linux-mail-1.1.1-preview.1
./xdev --version
./xdev who
```

包可放在自己的软件目录中，从任何目录调用其中 `xdev` 的绝对路径。默认状态目录为 `${XDG_STATE_HOME:-$HOME/.local/state}/xdev`。首次试用时可用 `export XDEV_HOME="$(mktemp -d)"` 选择新的私有信箱。正常使用同一状态目录可以跨进程保留信件，升级时将另一个版本解压到单独目录即可。

## 试寄和回复

以下示例供终端中的设备程序使用。先选新的试用信箱，逐条运行；首信选择保留回信地址，等待时间为零。每次调用均启动一个新的进程。

```sh
export XDEV_HOME="$(mktemp -d)"
./xdev msg send --to local --as-device --id hello --subject '核对结果' --text '请确认行数' --wait-reply 0
./xdev msg read hello
./xdev msg reply hello --id answer --text '已核对，共 12 行。'
./xdev msg read answer
```

最后一条消息的 `reply_to` 应为 `hello`。要投递到 AI 原聊天，需为对端选择其实际支持的接收方式，并另外检验原聊天已读与关联回复；设备程序的回信只验证邮箱。

## 按原信编号查投递结果

在发信聊天中运行 `./xdev msg status <原信编号>`。程序汇总该信的入箱回执、宿主接纳、指定聊天已读和关联回信。重点看 `delivery.state` 和 `delivery.retry`。例如，备用通道已经送达、原生收件器尚未就绪时显示 `pending_wake`。收件聊天读信后变为 `read`，沿原信回复后变为 `replied`。设备级读取单独记录，避免被计作指定聊天已读。

`check_same_id` 表示继续按原编号查询，`never` 表示无需重寄。只有确定尚未提交且返回 `retry_same_id` 时才用原编号重试。若对端仅能提供部分证据，查询显示已能确认的阶段。

## 与 Windows 或另一台 Linux 互通

现有 SSH 配对可用于跨机直寄。每个方向分别固定对端身份，Linux 收端使用仅限信件的 SSH 入口。按[核心 1.1.1 的 Linux 配对说明](https://github.com/realhdx/xdev/blob/v1.1.1/design/session-relay.md#linux-ssh-mailbox)配置时，将示例收件脚本最后两行换成 `exec /绝对安装路径/xdev _rpc_mail alpha`，以实际登记的发件设备主名替代 `alpha`。收件脚本和包目录仅允许受信用户写入。两端交换角色分别试寄，核对同一原信 ID 的读取与回复。

本预览版提供 `who`、`msg` 和限制为单个配对方的 SSH 收件入口。跨网中继、AI 客户端加载和空闲唤醒各有独立的接入条件。

核心来源为[公开的 xdev 1.1.1](https://github.com/realhdx/xdev/tree/023b50882d464a2cbf6e74b6b6b83577ab2120b9)。包内核心仅替换产品系列及预览版本号，并另附 Linux 启动文件。压缩包根目录附有许可证和引用信息。[LICENSE](https://github.com/realhdx/xdev/blob/v1.1.1/LICENSE) · [CITATION.cff](https://github.com/realhdx/xdev/blob/v1.1.1/CITATION.cff)
