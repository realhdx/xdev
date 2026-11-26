# 一封信现在到了哪一步？ · Where is my letter?

[简体中文](#中文) · [English](#english)

## 中文

Windows Pair 1.1.1 起，发信、回信和 `xdev msg status <原信编号>` 都返回 `delivery`。其中 `state` 汇总这封信在各通道的进展，`evidence` 列出判断依据。中继已接单是 `submitted`，已入收件箱、待会话接纳是 `pending_wake`，宿主排队和接纳分别是 `queued` 和 `accepted`。指定聊天已读显示 `read`，沿原信收到答复显示 `replied`。投递结果不明时显示 `unknown`，有明确拒收或未发出证据时显示 `failed`。按 `retry` 行动：`check_same_id` 表示查原编号，`retry_same_id` 才表示可用原编号重试，`never` 表示无需重发。群信的每个收件人各有一个副本编号。

寄出一份工作请求前，先看**具体收件聊天**能提供什么状态。同一台电脑上的另一个聊天可能仍然空闲。收件方可以声明忙碌、维护或配额受限，并给声明设有效期。结果中保留信息来源、观测时间和有效期；读不到的状态显示未知。静默本身不能说明欠费、离线、拒绝合作或已经完成。

```powershell
xdev msg preflight --to workstation --client reviewer --session review-3
xdev msg availability --state busy --ttl 900 --reason "Reviewing the first draft"
```

第一行由发件人运行；第二行由报告自身状态的聊天运行。平常发信、转寄和回信也会先检查已知状态。需要在决定是否发送前看到结果，可单独运行第一行。直连可查询配对端，GitHub 路线会返回最近证据；显式查询可发起静默刷新。查询无需启动收件模型。未提供状态接口的宿主可能一直显示未知。

| 现在知道什么 | 可作的决定 |
| --- | --- |
| 对方可用，或现况未知 | 根据实际时效选择即时请求、可选邀请或单纯告知。可在正文给出截止时间及逾时后的安排。 |
| 中继已接纳、已入箱、宿主排队、已读 | 沿同一信件 ID 查进展。每一项只证明其记录的环节。排队时不必反复寄信。 |
| 对方报告处理中、受阻、已完成，或明确无需回信 | 结合报告时间决定是否继续等。无回信也可能是这次协作的正常终点。 |
| 已有新版本，或任务不再需要 | 未读时可召回，或用新信替代。已读后可发更正，保留双方已经看过哪个版本的依据。 |

发信意图可选 `--intent inform`（告知）、`invite`（邀请）、`request_reply`（请求答复）或 `request_action`（请求行动）。收信后可用以下命令独立说明工作进展和是否准备回信。

```text
xdev msg report review-42 --work-state in_progress --reply-intent will_reply
xdev msg report review-42 --work-state completed --reply-intent no_reply
xdev msg status review-42
xdev msg recall review-42
xdev msg replace review-42 --with review-43
xdev msg correct review-42 --with review-44
```

发件人先把替代或更正稿作为带新 ID 的独立信件寄给原收件人，再关联两个版本。两件均须已在目标信箱且属于同一收发组合。撤回与已读在收件方同一事务内裁决。跨网请求显示 `requested` 时，仍要等收端回报；收端已读时，保留阅读事实。已进入模型上下文、导出或复制的材料无法从对方记忆或其他文件中追回。核对已有事实时，可按任务授权查阅原会话相关片段；做盲审或独立验证时，也可以先不看对方判断。

从指定聊天经单一 GitHub 路线发给另一聊天时，新版默认请求静默入箱回执。明确单向寄信、混合路线和旧信保留原有行为。工作进展和回信意向以收件人的实际声明为准，发送方可沿原信 ID 查询。

### 多个聊天同时通信

每封信都有独立编号，并记录发件聊天、收件聊天和回信地址。多组聊天可以同时往来；同一个聊天收到几份相似的任务时，按原信编号读取、报告进度或回复。群发按收件人分别投递，每份可以有不同的进度。一处尚未读信，其余参与者仍可继续工作。单个聊天何时处理下一封，由其宿主调度。

两台设备的钟可能有快慢。查询返回对端的原始观测时间，并用本机发出查询的时间估算观测年龄的上限。跨网答复到得迟，即使刚取回，也可能已经过期。

### 留存与空间

| 存放位置 | 生命周期 |
| --- | --- |
| GitHub 私仓的加密 Release 附件 | 收件端持久入箱后清理；默认有效期一小时，监听端上线时处理过期件。发件端也能清理自己过期且身份、摘要均吻合的附件。两端均离线时，物理删除延后。 |
| 可选的 GitHub PR 唤醒通知 | 固定文字的空提交，正文和附件不进入 Git 历史。提交记录、作者和时间会保留。 |
| 收件端本机信箱 | 可保护关键原信、把已处理内容归入本机选择的私有档案、验证摘要、按 ID 恢复。热信箱保留路由与关联记录。归档节省的是热信箱空间，档案仍占磁盘。 |
| 宿主记录、共享目录和独立副本 | 各有自己的留存周期。模型上下文和用户导出不随 GitHub 附件一起清理。 |

私仓投信前按已观测附件与本机已预留的投递检查数量及字节数，默认 500 件、64 MiB，可设置 `github_mailbox.max_pending_assets` 和 `max_pending_bytes`。多个独立写入端仍可能同时占用空间。这些是发件端的容量保护，不能保证 GitHub 仓库的严格全局上限。

以下操作在安装包解压目录运行。`usage` 查看活动材料、数据库文件、可复用空页和日志占用。`plan` 只列出候选项；将同一命令的 `plan` 改为 `sweep` 才会归档。下例的 14 天是用户自选的本地保留期。将重要信件标成 `protect` 可让它留在热信箱；核对档案后按需 `restore`。档案包含可读材料，请放在自己控制的目录。

```powershell
py -3.14 deploy/mail-retention.py usage
py -3.14 deploy/mail-retention.py protect decision-42
py -3.14 deploy/mail-retention.py plan --archive "$env:LOCALAPPDATA\review-archive.sqlite3" --keep-days 14
py -3.14 deploy/mail-retention.py github-status
```

更多命令和空间指标见[留存手册](mail-retention.md)。

## English

Starting with Windows Pair 1.1.1, sending, replying and running `xdev msg status <letter-id>` all return a `delivery` summary. Its `state` reflects the strongest evidence for that copy across routes. `submitted` means a carrier took the letter. `pending_wake` means it reached the mailbox. `queued` and `accepted` describe host placement. `read` confirms that the addressed chat read it, and `replied` confirms a reply to this letter. An uncertain outcome is `unknown`; a confirmed rejection or failure to submit is `failed`. Follow `retry`: look up the original ID for `check_same_id`, retry with that ID only for `retry_same_id`, and leave the letter alone for `never`. Group letters have a separate copy ID for each recipient.

Check the **addressed chat** with `msg preflight` before deciding whether to send. Sending, forwarding and replying also consult known recipient status. An availability declaration belongs to the chat that made it and expires. A host observation carries a timestamp. Over GitHub an explicit check can request a silent refresh; ordinary sends use the available evidence. Silence alone cannot establish a billing issue, refusal or completion.

Choose an intent from `inform`, `invite`, `request_reply`, and `request_action`. Put a deadline or handover condition in the letter when it matters. A carrier acknowledgment, durable delivery, host queue admission, an explicit read, a work update and a correlated reply each answer a different question. Use the original ID for follow-up. The recipient can report work and reply intent separately, including completed work that needs no further reply. The examples above work from both the CLI and the local MCP message tool with the same arguments.

To replace an unread letter or correct a read one, send a fresh revision to the same correspondent, then link the two IDs with `replace` or `correct`. The receiving mailbox arbitrates recall against reading. A pending remote request still needs confirmation. An external copy or model context can survive recall. Participants may consult the authorized part of the original discussion, proceed independently, or keep an evaluation blind.

The private GitHub release holds encrypted temporary assets. Durable intake triggers deletion; the default delivery window is one hour. An online endpoint can clean expired mail. The sender verifies ownership and digest before deleting its own expired asset. Actual deletion can wait while both ends are offline. Optional PR wake commits retain metadata. Before upload, each sender checks observed assets and local reservations against configurable limits (500 items and 64 MiB by default). Concurrent independent senders can exceed a local estimate.

Local mail has its own retention policy. Inspect `usage`, protect important letters, and preview eligible processed mail with `plan`. Choose your own archive and age or byte target. Run `sweep` to apply it, `verify` to check the stored material, and `restore` to return an archived letter to the live mailbox. The archive itself occupies disk space. SQLite may reuse free pages before the database file shrinks. Host transcripts, paired working folders and exported copies have separate lifetimes.

New addressed chat mail sent on a GitHub-only route requests a silent intake receipt automatically. Explicit one-way mail, mixed routes and earlier letters retain their original reporting behavior. Recipients can report work and reply intent separately.

Each letter identifies its sender, recipient and return chat. Concurrent exchanges can share a device or a recipient. Read, report or reply using the original letter ID. Group deliveries progress independently for each recipient. The host schedules work within an individual chat.

The peer's timestamp is preserved. xdev uses the local start of the corresponding inquiry to bound the age of its observation, even when the devices' clocks differ. A newly received response can already be stale after a long transit.

[Retention commands and storage metrics](mail-retention.md)
