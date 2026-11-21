# 私有 GitHub 信箱 · Private GitHub mailbox

[English](#english) · [其他路线 / Other routes](session-relay.md)

## 中文

两台设备都能出站访问 GitHub 时，可以用自己控制的私有仓库临时代收 xdev 信件。GitHub 运行中继，你管理仓库、访问人员和设备凭据，配对设备负责加密和解密。选择 `--transport direct` 的信件全程走两端的 SSH 连接。

1. 在已登录 GitHub CLI 的设备运行 `python deploy/github-mail.py init OWNER/REPO` 创建专用私有信箱。已有信箱用 `python deploy/github-mail.py doctor OWNER/REPO` 只读核查。也可给这两条命令加 `--token-file ABSOLUTE_PATH`。记录输出的仓库名、仓库数字 ID 和 Release 数字 ID。初始化只建一次 `xdev-mailbox-v1` 预发布；之后寄信只操作附件，正文不进入 Git 历史、Issue 或 Release 说明。
2. 为每台设备准备仅限这个仓库的凭据，允许读取、上传和删除 Release 附件。使用细粒度访问令牌时，仓库权限需要 **Contents: Read and write**，这项权限也能修改该仓库的代码，专用仓库可以隔开产品源码。用 `python deploy/github-mail.py save-token --token-file ABSOLUTE_PATH` 在本机交互保存，输入时不回显。另一个设备独立保存自己的凭据。两端另用可信方式交换一份该设备对专用的随机 32 字节密钥，以 64 个十六进制字符存入各自文件。将凭据和配对密钥放在不同文件中。[GitHub Release 附件权限](https://docs.github.com/en/rest/releases/assets)
3. 在两端各自的 xdev 配置中合并下方示例，对端交换 `alpha` 与 `beta`，填写本机的绝对路径，并选定允许收信的本机应用。两台设备可共用同一个私有仓库和 Release。
4. 在两端分别启动 `xdev msg listen --transport github --follow`。后台运行时从各端的 `token_file` 读取权限，独立于安装时的 GitHub CLI 登录。普通 `msg send`、`msg reply` 和 `msg status` 使用同一组会话地址。持续监听从约 20 秒的检查间隔开始，信箱持续空闲时逐步延长至约 5 分钟。离线或 GitHub 限速时可能更久。在 Windows 可用 `py -3.14 deploy/mail-listener.py install` 随当前用户登录启动，`status` 检查，`remove` 停用。发件方收到 `queue_acknowledged` 表示 GitHub 已收下密文；接下来查看收端回报和关联原信的实质回复。

GitHub 和获准访问仓库的人仍能看到附件大小、操作时间、有效期和上传账号等元数据。外层不包含设备名、会话名、主题或正文。监听器收到附件并按需发出回报后清理附件，恢复上线时再清理过期信件。默认有效期一小时，可设 60 到 86,400 秒。GitHub Release 没有按每封邮件设定的自动删除定时器。API 删除也无法撤回已经下载的副本或控制平台备份。

**聊天怎样知道收到了信？** GitHub 来信进入指定聊天的本地邮箱。对端只需获得一个应用的收件许可，每封信分别写明聊天地址。在工作的聊天可调用 `xdev msg inbox --wait 30` 等待来信。在这段等待期间，本机收件器会按活跃节奏检查 GitHub，同时遵守服务端要求的退避时间。返回的摘要带有主题、原信 ID 和续等用的 `cursor`。下次加 `--after-seq CURSOR` 即可接续。等待由工具完成，收到摘要后由该聊天决定是否调用 `msg read ID` 和 `msg reply ID`。装有后台到信插件的宿主可以在聊天空闲时主动通知原聊天。其他聊天下次打开邮箱时也能看到来信。

设备上的应用还可用 `xdev msg events --transport github --after 0` 查看已提交的本地到信事件及收件序号。这个入口适合显示角标或到信提示。正文和主题通过有地址范围的会话邮箱查阅。发信时带 `--delivery-report`，收到 `delivery_scope: mailbox`、`model_woken: false` 表示对端已入箱。读信与实质回复各有独立记录。

Windows 收件服务已安装后，运行 `py -3.14 deploy/mail-listener.py restart` 可重新载入当前版本并保留配对和登录启动设置。返回 `restarted: true` 表示新进程已经开始收信。聊天应用的通知接入可另用一封新信核验。

## English

If both devices can reach GitHub over HTTPS, a private repository you control can hold sealed mail until the receiving device checks in. GitHub operates the service. You choose who can access the repository and which devices hold the pair key. To keep a letter on an available end-to-end SSH path, select `--transport direct`.

1. From a device signed in with the GitHub CLI, run `python deploy/github-mail.py init OWNER/REPO` to create the dedicated private mailbox. Use `python deploy/github-mail.py doctor OWNER/REPO` for a read-only check of an existing one. Both commands also accept `--token-file ABSOLUTE_PATH`. Record the returned repository name and numeric repository and release IDs. Setup creates the `xdev-mailbox-v1` prerelease once. Subsequent letters are release assets and never enter Git history, issues or release notes.
2. On each device, use a credential scoped to that repository with permission to read, upload and delete release assets. For a fine-grained access token, grant **Contents: Read and write**. This also permits code changes in that repository, which is why the mailbox belongs in a dedicated repository. On each device, use `python deploy/github-mail.py save-token --token-file ABSOLUTE_PATH` to save its own credential with a hidden input prompt. Separately exchange one random 32-byte pair key through a trusted channel and save it as 64 hexadecimal characters on each device. [GitHub release asset permissions](https://docs.github.com/en/rest/releases/assets)
3. Merge the example below into each device’s local configuration. Swap `alpha` and `beta` at the other end and choose the applications allowed to receive mail locally. Both devices may share one repository and release. Replace both paths with private absolute paths on the corresponding device.
4. Run `xdev msg listen --transport github --follow` at each end. The runtime reads the configured token file on each device independently of the GitHub CLI used during setup. Use normal send, reply and status commands. Checks start about 20 seconds apart and back off to about five minutes while idle. Connectivity and GitHub rate limits may increase the delay. On Windows, use `py -3.14 deploy/mail-listener.py install` to start the receiver at sign-in, `status` to check it and `remove` to stop it. A `queue_acknowledged` result confirms storage of the sealed letter at GitHub. Check the recipient’s report and a substantive reply linked to the original letter for stronger evidence.

GitHub and repository collaborators can still observe upload times, account identity, sizes and expiry. The outer packet omits device names, chat IDs, subjects and text. Listeners clean up received or expired mail, posting requested reports before deleting received items. Offline listeners can delay deletion beyond the configured interval. The default lifetime is one hour and can be set to 60–86,400 seconds. Release assets do not have a per-letter server-side expiry timer. API deletion cannot recall downloaded copies or control GitHub’s backups.

**How does a chat learn about incoming mail?** Each paired device can be granted access to an application once. Every letter still names its recipient chat. A running chat can call `xdev msg inbox --wait 30`. During that wait the local receiver checks GitHub at its active cadence and honors any provider backoff. The tool returns the subject, original letter ID and a `cursor`. Resume with `--after-seq CURSOR`. The chat chooses when to call `msg read ID` or `msg reply ID`. An application with the host integration below can notify the idle original chat. Other chats can check their mailbox when next opened.

A local application can use `xdev msg events --transport github --after 0` to display an arrival indicator from the committed inbox sequence. The addressed mailbox provides each letter’s subject and content. A requested delivery report with `delivery_scope: mailbox` and `model_woken: false` confirms arrival at that mailbox. Explicit reading and a substantive reply are recorded separately.

On Windows, run `py -3.14 deploy/mail-listener.py restart` to reload the installed receiver while keeping its pairings and sign-in schedule. A `restarted: true` result confirms that a new process has started receiving. Send a fresh letter to verify the chat application's notification integration as well.

GitHub Free 可创建私有仓库。这一选项适用于经配对的设备偶尔交换短消息，受账号 API 限速和平台合理使用条款约束。大文件优先走直连；高频通信可选择自有的消息服务。

GitHub Free supports private repositories. This option is intended for occasional short messages between paired devices, subject to the account’s API limits and GitHub’s acceptable use policy. Use a direct route for large files or a dedicated messaging service for sustained traffic. [GitHub plans](https://docs.github.com/en/get-started/learning-about-github/githubs-plans) · [Acceptable use](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies) · [API best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)

## Configuration / 配置

### 空闲原聊天的后台通知 · Notify an idle chat

在 AI 应用的插件启动时，为允许自动收信的已配对设备和现有聊天注册一次。应用可以从自己的会话表取得聊天 ID。注册发生在宿主后台，模型空闲时没有查信回合。Windows 使用当前用户的收件服务，Linux 需要在聊天回合结束后仍运行收件服务与插件。GitHub 的检查间隔和限流仍会影响到信时间。

A plugin registers a paired device and an existing local chat at application startup. The app can obtain the ID from its own chat registry. The model can remain idle. On Linux both the carrier listener and the host integration must outlive the chat turn. Cloud polling and rate limits still apply.

Python 插件可以使用 `src.adapters.mail_arrival.Subscription`，在后台线程中调用 `sub.run(dispatch, stop_event)`。其他语言的插件可以启动以下子进程，以私有标准输入和输出交换逐行 JSON。插件应保存本机选定的目标聊天，按需要自动重连。协议只向宿主提供本地收件序号和去重标识。首次注册从此刻开始，之前的信件仍在原邮箱。

Python hosts can run `Subscription(root, peer=peer, client=client, session=session).run(dispatch, stop_event)` in a background thread. Other hosts can launch the following subprocess with private input and output pipes. Both interfaces deliver a local inbox sequence and a deduplication ID. The plugin owns the selected chat. First registration starts at the current inbox boundary.

重新连接时，插件接续同一聊天的订阅；默认仅提醒最近 15 分钟内入箱的未读信。更早的信仍可查收。移除插件时，可从 Python 宿主调用 `sub.disable()` 注销。宿主返回“接纳”只表示原聊天已接收通知。已读和关联回复各有独立记录。

On reconnection, the same chat resumes its subscription. The default wake window is 15 minutes from local receipt. Older mail remains available. Python hosts can call `sub.disable()` on uninstall. Host admission, explicit reading and a linked reply have separate records.

```text
xdev msg host-events --peer beta --client reviewer --session review-3
```

```json
{"protocol":"xdev.mail-arrival.v1","event":"ready"}
{"protocol":"xdev.mail-arrival.v1","event":"mail_available","inbox_seq":42,"notice_id":"…"}
```

宿主将事件交给自己的原聊天接纳接口；该接口应按 `notice_id` 去重。收到接纳凭据后，向子进程标准输入回写一行，原样带回标识。明确尚未提交时可返回 `not_submitted`；提交结果未知时返回 `unknown`，避免重复启动模型。宿主通过自己可信的事件处理器读信、沿原信回复。读信前，用当前聊天身份执行 `msg inbox --after-seq 41 --limit 1`，确认返回序号为 42，再用所得消息 ID 调用 `msg read`。数字 41 是通知序号减一。需要配置应用分身时，同时传入本机选择的 `--profile`。纯回执保持静默。

Pass the event to the original chat through the host's own admission API. Deduplicate by `notice_id` at that boundary. After the chat accepts, echo the ID in one acknowledgement line. Use `not_submitted` only for a definite non-submission, or `unknown` for an uncertain outcome. In the addressed chat, look up inbox sequence 42 with `msg inbox --after-seq 41 --limit 1`, confirm the sequence matches, then read and reply by the returned message ID. Add the local `--profile` for a profile-specific chat. Delivery receipts stay silent.

```json
{"protocol":"xdev.mail-arrival.v1","notice_id":"…","status":"accepted"}
```

```json
{
  "device": "alpha",
  "peers": {"beta": {"ssh": "beta-lan", "mail_routes": ["direct", "github"]}},
  "github_mailbox": {
    "delivery_scope": "mailbox",
    "repository": "example-owner/private-xdev-mail",
    "repository_id": 123456789,
    "release_id": 987654321,
    "token_file": "/private/xdev/github.token",
    "retention_seconds": 3600,
    "peers": {
      "beta": {
        "key_file": "/private/xdev/alpha-beta.key",
        "receiver_apps": ["reviewer"]
      }
    }
  }
}
```

从 1.1.0-preview.7 迁移时，将旧配置项 `github_mail` 改名为 `github_mailbox`，两端各重启一次监听器。仅保留新配置名。回退到旧版后它会忽略新配置，GitHub 收信由此停用，需要直连时可明确指定 `--transport direct`。

When migrating from 1.1.0-preview.7, rename `github_mail` to `github_mailbox` and restart each listener. Keep only the new name. Rolling back to preview.7 then disables GitHub reception because that version cannot read the new profile. Select `--transport direct` to use a reachable SSH peer.

`mail_routes` 指定日常自动选路的顺序。若只开放 GitHub，将本例的设备对端记录换成 `"beta": {"mail_transport": "github"}`。正文可能已提交但回执缺失时，程序保留原 ID 和既有路线，先查状态。授权、仓库身份或可见性不符时，修正配置后再寄信。

`mail_routes` gives the daily preference order. For a GitHub-only peer, the device record can be `"beta": {"mail_transport": "github"}`. If submission may have occurred, inspect the original ID on the recorded carrier. Check authorization, repository identity and visibility if dispatch is rejected.

固定同一信件 ID 和相同内容重新执行不确定的 GitHub 投递时，程序只查原上传记录。接收端可能已经处理并删除附件；查不到附件时仍保留未知。发送时也可用 `--wait-reply SECONDS` 等待关联答复，监听器会把答复交给原来的调用。

Repeating an uncertain GitHub send with the same ID and letter checks the original upload without posting again. If the recipient has already removed the asset and no confirmation is available, the result remains unknown. Add `--wait-reply SECONDS` when the sending call should wait for a correlated reply. Keep the listeners running at both ends.

```text
xdev msg send --transport github --to beta --client reviewer --session review-3 --from-client editor --from-session draft-7 --id review-42 --subject "Review draft" --text "Please review draft K72." --delivery-report
xdev msg status review-42 --as-client editor --as-session draft-7
xdev msg read review-42 --as-client reviewer --as-session review-3
xdev msg reply review-42 --transport github --from-client reviewer --from-session review-3 --text "I reviewed draft K72."
```

已有设备连接能读写专用目录的应用，可使用[共享目录收发与原聊天唤醒 · Paired-folder mail and chat notifications](paired-folder-mail.md)。
