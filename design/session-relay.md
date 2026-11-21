# 跨网络给指定聊天寄信 · Session mail across networks

[English](#english) · [中继配置模板 / broker template](../deploy/session-relay.conf.example)

## 中文

给指定聊天寄信，地址由设备、应用和会话 ID 组成。先按网络条件选择路线。

| 路线 | 适用场景 | 谁接触传输中的材料 |
| --- | --- | --- |
| SSH 直连（`direct`） | 两端已有可达地址，要求无法直连时停下 | 两端解密。xdev 禁用 SSH 跳板、代理和已有复用连接。系统使用的 VPN 或网络路由仍由设备决定 |
| 自管 JetStream（`relay`） | 两端都只能向外连接，或一端经常离线 | 管理员选择中继主机。在两端为对方设置 `encryption: required` 后，中继收到密文。连接时间、设备路由、字节数和相同封包的重传仍可见 |
| [托管私有队列（`sqs`）](managed-queue.md) | 希望借现成平台短暂代收，两台设备都能出站联网 | 设备端先加密，收件后删除，队列保留期限定在 1 分钟到 24 小时 |
| [私有 GitHub 信箱（`github`）](github-mail.md) | 两台设备都能访问 GitHub，能接受轮询延迟 | GitHub 托管指定私有 Release 的加密附件；仓库授权由维护者管理 |

GitHub 路线只把信件送入指定聊天的本地邮箱。活动聊天用 `xdev msg inbox --wait 30` 可在等待期间获知主题和原信 ID，自行决定何时读信、回复。关闭的聊天可以稍后查阅。设备上的到信提示可订阅本地收件序号，详见[私有 GitHub 信箱](github-mail.md)。

按次指定 `--transport direct`，或把对端的 `peers.<peer>.mail_transport` 设为 `direct`。直连失败会返回错误，切换中继由发件方明确选择。如果超时且结果未知，先查原信 ID 和目标收件情况。SSH 的普通 `ssh` 路线可继续使用用户配置的跳板。Linux 的直收配置见[下方示例](#linux-ssh-mailbox)。会话中继传递信件、转附原信和回报，跨网传大文件及远程操作需要单独可达的文件或命令通道。

**准备自管中继。** xdev 没有预设公共中继。可按需在两端都能连到的主机启动 NATS JetStream。使用 TLS 和每台设备独立的账号，按[权限模板](../deploy/session-relay.conf.example)预建流和每台设备的持久消费者。下例使用工作队列，成功确认后移除封包，离线封包最多保留一天。已有流要单独核对保留策略。旧版仅签名的配置允许中继读取明文，两端都升级并为彼此启用强制加密后，再发送私密材料。配对密钥仅交给两端。长效配对密钥泄露可能影响过去被保存的密文。队列移除无法撤回运营方此前留存的副本。

**配置两端。** 在各自的 `XDEV_HOME/config.json` 合并下方示例。两端约定稳定身份，此例为 `alpha`、`beta`；各自可以另取显示名或别名。设备 B 交换两端名、换用自己的账号和路径，选择允许对方联系的本机应用。私钥文件路径均在本机，每对设备使用独立随机的 32 字节配对密钥，文件中保存 64 位十六进制文本。用已信任的方式交换密钥。Windows 使用本机绝对路径和本地可执行文件。`receiver_apps` 按设备一次授权应用。每封信仍须指明已有聊天，宿主按 ID 定位。第三方应用可在 `session_hosts` 一次接入自己的接收程序。需要只开放个别聊天时，可沿用精确的 `receivers` 名单。两种授权都为空时，信件只入箱。

**开启收信。** 按下方示例在两端各运行一个持续监听进程。Windows 安装包用 `xdev.cmd`。Linux 可解开同一发行包中的 `runtime.zip`，进入包含 `src/` 的目录，或直接进入本版本源码根目录。用 `python3 -m src.cli.main` 替换示例中的 `xdev`，在该解释器环境安装 `nats-py`；端到端加密还需要提供 AES-SIV 的 `cryptography`。配好服务后可交由系统现有的进程管理器启动。撤销聊天授权、更改监听配置或轮换密钥后，重启两端相应的监听器使更改生效。监听器空闲时等待网络事件，不调用模型。接收端再次上线后可在保留期内补收。

**确认结果。** 发信带 `--delivery-report` 时，发端用 `msg status` 查这封信的无声回报，接收设备的监听器和发件设备的监听器都要运行。`broker_acknowledged` 只表示中继已保存。`report_received` 带回收件箱结果和宿主接纳位置。`current_turn` 代表进入当前轮，`next_turn` 代表排队，`original_session` 代表宿主确认了原聊天但尚不能区分轮次。模型的阅读与处理另查显式阅读记录和携带原件 ID 的实质回信。及时回信的单次调用可用 `--wait-reply 45`；超过等待时间后仍可查原 ID。宿主报告未激活时，激活后在收端用 `xdev ai --client CLIENT --session SESSION ID` 补投既有信件。遇到结果未知先查那个聊天，避免重复启动。群发按每封副本 ID 查询回报。关联回信只唤醒曾向该收信会话寄过对应原信的聊天。要联系转寄链上的其他作者或初次联系同组成员，可给其已授权的地址另寄新信。

**一次发信，按配对配置选路。** 平常使用 `msg send` 或 `msg reply` 即可。若双方已有可用的加密中继，在各端的对端记录里可写 `"mail_routes": ["direct", "relay"]`。原有的 `mail_transport` 若固定为某一路线，先改为 `auto` 才会启用选路。此例先尝试直连，仅当本地能确认正文尚未提交时才尝试中继。已有私有 SQS 或 GitHub 配对也可加入列表。两端各自启动被选中中继和队列的监听进程。发送时只提交到一条路线，原路返回该载体的投递报告。

不填写 `mail_routes` 时，只有 SSH 配对的安装沿用原有 SSH 设置。新增合格的加密中继和队列配对后，默认按 SSH、JetStream、SQS、GitHub 的顺序选择，显式配置的 SSH 跳板仍受尊重。已有设备邮箱专用 NATS 配置可继续用于普通设备邮件。按次指定 `--transport` 优先于设备配置，固定路线的失败会立即返回。单独请求 `--delivery-report` 时，自动模式只选支持异步回报的 JetStream、SQS 或 GitHub。

结果中的 `route_attempts` 列出尝试顺序。`not_submitted` 表示该路线尚未提交正文。`submission_unconfirmed` 或 `inbox_unknown` 表示结果未知。此时保留原 ID，用发信身份查询 `msg status`，并在对端核对原信。指纹、证书、凭据或授权错误会停止投递。设备已入箱而宿主尚未接纳时，查该会话已有的通知。系统不会因排队或迟迟没有答复再换路寄一封。

## English

Address a device, application and existing conversation. Choose `--transport direct` when the peer is reachable and the letter must go straight there. To persist this choice, set `peers.<peer>.mail_transport` to `direct`. This route disables SSH jumps, proxy commands and shared connections. It stops on failure. The operating system still controls VPNs and underlying routing. After an ambiguous timeout, check the original letter ID before choosing another route. The ordinary `ssh` route honors configured jumps.

For outbound-only or intermittently connected peers, keep a private JetStream relay ready to start on a mutually reachable host, or choose a [managed private queue](managed-queue.md) or a [private GitHub mailbox](github-mail.md). xdev supplies no public relay. Relay mail carries letters, attached originals and delivery reports. Large file transfers and remote operations need their own reachable transport. With `encryption` set to `required` on both devices, the paired endpoints seal the entire letter before publication. A JetStream operator sees routing identities, timing, sizes and identical retransmissions. Pair keys belong on the endpoints. Compromise of a long-term key can expose retained ciphertext. Older signing-only settings allow the broker to read letters. Upgrade both endpoints before enabling private mail.

The work-queue example below removes acknowledged mail and expires outstanding mail after one day. Existing streams retain their configured policy. Removal cannot erase copies already made by the operator.

GitHub delivers to the addressed chat’s local mailbox. A running chat can call `xdev msg inbox --wait 30` to receive an incoming subject and original letter ID, then choose when to read or reply. A closed chat can check later. Local applications can also display an arrival indicator from the inbox sequence. See the [private GitHub mailbox guide](github-mail.md).

Use the permissions template above, one device credential per node and a separate provisioning account. Create the stream and a durable pull consumer for each identity. Choose retention and capacity for the longest expected offline period. If offline longer, messages may expire. Put the matching device pair and local receiver entries in each node’s configuration. Select permitted local applications once with `receiver_apps`. Each letter still names an existing chat, which its own host resolves. To limit a peer to individual chats, use `receivers` instead. With neither grant, letters stay in the inbox. Replace example paths, programs, device names and conversation IDs with values from your own installation.

Start a listener on each device. On Windows use the installed `xdev.cmd`. On Linux, extract the bundled `runtime.zip` and work from the directory containing `src/`, or start from this revision’s source root. Run `python3 -m src.cli.main` with `nats-py` and, for sealed mail, an AES-SIV-capable `cryptography` installed in that interpreter. Restart the relevant listeners after changing permissions, credentials or pairing keys. A process supervisor can start the listener at login or boot. Idle listeners wait on the network without calling a model. To check a letter before a human or model replies, add `--delivery-report` and query `msg status` as its sender. A broker acknowledgment confirms storage at the relay. The paired device’s report confirms inbox delivery and the placement actually acknowledged by its host. A matching substantive reply provides separate evidence of processing. If admission is unknown, inspect the original chat before retrying. A correlated reply may wake the callback for its own recorded outgoing letter. To start an exchange with another author or group member, send a new letter to that permitted address.

**Send once across paired routes.** With both endpoints paired for SSH and a sealed JetStream relay, put `"mail_routes": ["direct", "relay"]` in each peer record. Set `mail_transport` to `auto` or omit it. A fixed setting and a per-send `--transport` each select a single carrier, with the per-send choice taking precedence. Run a listener for each receiving relay or queue you select. Add `sqs` or `github` after pairing both ends for that carrier. A normal send or reply tries the next route only after the sending endpoint confirms that it has not submitted the letter. Each asynchronous report returns on its original carrier.

With no route list, an SSH-only installation retains its existing SSH settings. With sealed carriers paired, the default order is SSH, JetStream, SQS, then GitHub. An existing SSH jump configuration remains effective. Legacy mailbox-only NATS can still carry unaddressed device mail. Requesting `--delivery-report` in automatic mode selects among the paired asynchronous report carriers.

Read `route_attempts` for the selected carrier and earlier attempts. `not_submitted` records a pre-submission failure. After `submission_unconfirmed` or `inbox_unknown`, query the original ID as its sender and check the recipient's mailbox. A host-key, certificate, credentials or permission error stops dispatch. A queued or unconfirmed host notification stays with the original delivery. Reusing the ID after an uncertain outcome does not submit the letter again.

```text
xdev msg send --to beta --client reviewer --session review-3 --from-client editor --from-session draft-7 --id draft-42 --subject "Review draft" --text "Please review the revised draft."
xdev msg status draft-42 --as-client editor --as-session draft-7
```

## Provision the relay / 配置中继

Use a NATS server with JetStream and TLS-first support, the provided per-device subject permissions and a CLI context authenticated as the `provision` user. Select `--tlsfirst` for this server. If the relay uses a private CA, set its certificate in that context or add `--tlsca /path/to/relay-ca.pem`. Create `stream.json` and `alpha-consumer.json` with these contents; for the second consumer replace both occurrences of `alpha` with `beta`. Capacity shown is 256 MiB. At capacity `discard: new` rejects further publications so the sender can see failure. Monitor disk use and expired messages.

```json
{"name":"XDEV_SESSION_V1","subjects":["xdev.session.v1.*.*"],"retention":"workqueue","storage":"file","discard":"new","max_bytes":268435456,"max_age":86400000000000,"num_replicas":1}
```
```json
{"durable_name":"xdev-alpha","filter_subject":"xdev.session.v1.alpha.*","ack_policy":"explicit","deliver_policy":"all","replay_policy":"instant"}
```
```text
nats --tlsfirst stream add --config stream.json
nats --tlsfirst consumer add XDEV_SESSION_V1 --config alpha-consumer.json
nats --tlsfirst consumer add XDEV_SESSION_V1 --config beta-consumer.json
```

## Device configuration / 设备配置

Example for alpha. The callback program is supplied by the local application or its integrator. Use the existing device record when upgrading. Use an absolute path for each credential; omit `ca_file` if the server uses a certificate trusted by the operating system.

```json
{
  "device": "alpha",
  "peers": {"beta": {"mail_transport": "relay"}},
  "session_relay": {
    "url": "tls://relay.example.org:4222",
    "user": "alpha",
    "password_file": "/private/xdev/alpha.password",
    "ca_file": "/private/xdev/relay-ca.pem",
    "peers": {
      "beta": {"encryption": "required", "key_file": "/private/xdev/alpha-beta.key",
               "receiver_apps": ["editor"]}
    }
  },
  "session_hosts": {
    "editor": {"argv": ["/opt/editor/bin/xdev-receiver"]}
  }
}
```

For the beta node in this example, use `"device": "beta"`, register `alpha` as its peer, and supply beta’s own relay credentials. Under `session_relay.peers.alpha`, use the same pair key and `"receiver_apps": ["reviewer"]`. Configure the reviewer application once via `session_hosts.reviewer.argv`. Both example receiver programs stand for integrations you supply. For a built-in adapter, use its client name and actual host session ID instead.

## Send, read, reply / 端到端核验

```text
xdev msg listen --transport relay --follow
```

From alpha’s editor chat, then in beta’s reviewer chat, then back at alpha. The short challenge makes it possible to distinguish a fresh answer from an old notification.

```text
xdev msg send --to beta --client reviewer --session review-3 --from-client editor --from-session draft-7 --id handoff-01 --subject "Review draft" --text "Which draft did you receive? Reference K72." --delivery-report
xdev msg read handoff-01 --as-client reviewer --as-session review-3
xdev msg reply handoff-01 --from-client reviewer --from-session review-3 --text "I received draft K72."
xdev msg status handoff-01 --as-client editor --as-session draft-7
xdev msg wait --reply-to handoff-01 --from beta --from-client reviewer --from-session review-3 --as-client editor --as-session draft-7 --substantive-only --timeout 10
```

<a id="linux-ssh-mailbox"></a>

## Linux 直收 / Direct Linux mailbox

Linux 收件端可用一个仅限邮箱的 SSH 密钥。先用可信途径核对主机指纹并写入发端的 known_hosts。在收端（此例为 beta）安装 xdev 运行时。给每个发件设备单独创建密钥入口，固定允许的发件方和收端应用。以下路径均为示例。反向发信需要在另一设备配置它自己的接收端。

On the receiving Linux host, install the runtime in a protected directory, own the mailbox state as the receiving account, and pin one SSH key to one peer. For the sender named alpha, install this executable wrapper on beta:

```sh
#!/bin/sh
set -eu
export XDEV_HOME=/home/mail/.local/share/xdev
cd /opt/xdev
exec /usr/bin/python3 -m src.cli.main _rpc_mail alpha
```

Prefix alpha's public key in beta's `authorized_keys` with `restrict,command="/home/mail/.local/bin/xdev-mail-rpc"`. Use a dedicated key and a separate forced-command entry for each additional peer. Keep the fixed script and runtime writable only by trusted local operators. The endpoint accepts letter delivery and notifications to approved local chats. Commands and filesystem operations require a separate grant.

On beta authorize the receiving application once in the peer record:

```json
{"peers":{"alpha":{"receiver_apps":["reviewer"]}}}
```

On alpha select the SSH Host with the pinned key. The peer's name or stable identity must also resolve as local on beta:

```json
{"peers":{"beta":{"ssh":"beta-mail","rpc":"linux-mailbox","mail_transport":"direct"}}}
```

## Integrating another host / 新客户端接口

A third-party MCP host may include `{"xdev/session":{"client":"editor","session":"draft-7"}}` in its per-call `_meta`, or supply explicit sender arguments. A host with no inbound API can still initiate a bounded request with `--wait-reply`. To accept unsolicited mail into an existing conversation, supply a receiver that can actually address that conversation in the host.

xdev runs the locally configured executable with its fixed arguments. Its standard input carries one UTF-8 JSON notice with `protocol`, `client`, `session`, `device`, `message_id`, `notice_id`, `mode`, `subject` and `sender`. The host uses its own registry to resolve the supplied session, rejects missing or ambiguous sessions, and submits only to that original chat. A host-wide command must support this lookup. Put single-chat integrations in `session_receivers` instead. The receiving chat reads the letter from its inbox. Echo the protocol, client, session and both IDs on standard output, with `status: "accepted"` and the host-confirmed `placement` of `current_turn`, `next_turn` or `original_session`. Return `status: "requires_activation"` only when nothing was admitted. Use the notice ID to deduplicate. After a timeout or mismatched acknowledgment the outcome stays unknown. The adapter executes at the receiving user's privilege level. Keep executable paths and bindings locally controlled.

[TLS-first setup](https://docs.nats.io/learn/security/encryption) · [JetStream pull consumers](https://docs.nats.io/learn/jetstream/pull-consumers) · [NATS permissions](https://docs.nats.io/learn/security/authorization)
