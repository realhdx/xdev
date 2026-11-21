# 用共享目录接入原聊天 · Connect a chat through a paired folder

一个 AI 应用只要能通过已有设备连接读写专用目录，就能用它给另一个指定聊天发信、读回信。此适配器随 Windows Pair 预览包提供，也可从源码运行。Windows 网关负责收发，应用自己的事件订阅负责唤醒空闲的原聊天。首次用两封实信核对该应用是否支持这一步。

An app with an existing connection to a private folder can use that folder to exchange mail with a specific chat. The Windows Pair preview includes this optional component. You can also run it from source. A Windows gateway handles mail. The app's own event subscription brings an idle chat back to work. Verify the original chat with two fresh letters when enrolling a new app.

## 一次登记 · Enroll once

1. 在接收端 Windows 选一个专用空目录，通过已有设备连接只向指定应用开放它。另选一个仅本机用户可写的状态目录。两者与 xdev 安装目录分开。记下远端设备的 xdev 主名、客户端和双方原会话 ID。

   On the Windows gateway, expose one dedicated empty folder to the selected app through its existing connection. Keep enrollment state in a separate, private local directory. Record the peer's canonical xdev device name, its client name and both original chat IDs.

2. 在源码根目录运行下例。变量填入这次配对的值。三个目录变量使用本机绝对路径，父目录须已存在。服务运行期间保留源码中的 `deploy` 目录。

   From the source root, substitute the values for your pair. Use absolute local paths for the three directories, with existing parent directories. Keep the source deployment scripts in place while the receiver runs.

   ```powershell
   py -3.14 deploy/paired-folder-mail.py init --state $state --folder $shared --xdev-root $install --local-session $folderChat --peer-device $peerDevice --peer-client $peerClient --peer-session $peerChat
   ```

3. 若用 GitHub 提醒原聊天，先配置[专用私仓](github-mail.md)，在该仓库保留一个已打开的 PR，其源分支须为 `xdev-mailbox-notifications`，目标为其他分支。用已有 GitHub 连接独立核对仓库数字 ID、PR 编号、同编号 issue 的数字 `id`、源分支当前完整的提交 SHA，以及写入通知的账号登录名。issue 的数据库 ID 与 PR 编号是两个字段。下列 `prepare` 只读核对已有锚点，输出中的 `github_pr_notice` 对象需合并到安装目录的 `config.json` 顶层，保留现有配置；然后运行 `check`。

   For GitHub notices, configure the [private mailbox](github-mail.md) and keep a dedicated PR open from `xdev-mailbox-notifications` into another branch. Independently check the numeric repository ID, PR number, the numeric `id` of the corresponding issue, the current full head commit SHA and the GitHub login that will publish notices. Use the existing PR head as the anchor. Merge the returned `github_pr_notice` object into the installation's top-level configuration, then run the read-only check. Neither command creates the PR or tests a wake.

   ```powershell
   py -3.14 deploy/github-pr-notice.py prepare --root $install --repository-id $repoId --pr-number $prNumber --pr-issue-id $issueId --actor $actor --anchor $headSha
   py -3.14 deploy/github-pr-notice.py check --root $install
   ```

4. 从目标原聊天保存一次事件订阅，只监控这个私仓、这个 PR 的**新提交**。事件动作是通过既有连接查看专用目录中的未读信，读后登记，需要时沿原信回复。确认服务商把事件送到同一聊天。同一批来信可能合并提醒。每次提醒只向该分支追加一条文件内容不变、说明固定为 `xdev: mailbox updated` 的提交。GitHub 会保留提交历史、作者与时间；信件 ID、主题和正文留在配对信箱。

   In the original chat, save one subscription for **new commits** on this repository and PR. Have it check the private folder, acknowledge each letter it reads and reply where appropriate. Confirm that the provider delivers the event in that same chat. A batch may share one notice. Each notice advances the branch with an unchanged file tree and a fixed commit message. GitHub retains commit history, author and timing. Letter IDs, subjects and bodies stay in the paired mailbox.

### 原聊天怎样找对信 · Match a notice to its letters

GitHub 事件可能只提示“信箱有更新”，不保证含有每次提交的 SHA。使用支持通知清单的接收器：私仓确认接受一次通知后，它在共享目录写入 `signals/<notice-id>.json`。清单中的 `schema` 为 `xdev.paired-folder.notice.v1`，`commit_sha` 是该次实际提交，`message_ids` 列出该批信件。

收到事件时，原聊天经已有设备连接查看尚未处理的清单。用现有 GitHub 连接核对每份清单中的 SHA 属于已登记 PR 的专用分支，提交说明为 `xdev: mailbox updated`，文件树与父提交一致。然后只打开该清单列出且仍未读的信。多次事件即使合并、迟到或重复，每份清单仍对应自己的提交和信件。当前分支顶端不能替代清单中的 SHA，寄信和收信的人都无需逐封提供 SHA。

A GitHub event may only say the mailbox changed. It need not include a SHA for each commit. With a manifest-aware receiver, each notice accepted by the private repository produces `signals/<notice-id>.json` in the shared folder. Its `schema` is `xdev.paired-folder.notice.v1`; `commit_sha` identifies the actual commit and `message_ids` lists its letters.

On wake, the original chat reads outstanding manifests over the paired connection. For each, verify the recorded commit belongs to the enrolled PR branch, carries the fixed message `xdev: mailbox updated` and has the same file tree as its parent. Open only the listed letters that are still unread. This works with merged, late or duplicate events. Always use the recorded SHA. The branch may have advanced, and neither correspondent needs to supply a SHA by hand.

## Windows 登录接收器 · Run at sign-in

在网关上用配套 Python 的 `pythonw.exe` 运行后台任务。在 PowerShell 中设置 `$pythonw` 为该程序的绝对路径。按需选择动作。重启后 `startup_id` 应改变，`running` 和 `cycle_ok` 均应为真。

On the gateway, set `$pythonw` to the absolute path of the matching `pythonw.exe`. Select the action you need. After restarting the receiver, verify that `startup_id` changed and that both `running` and `cycle_ok` are true.

```powershell
$svc = @{ State = $state; XdevRoot = $install; Pythonw = $pythonw }
.\deploy\paired-folder-service.ps1 -Action install @svc
.\deploy\paired-folder-service.ps1 -Action status @svc
.\deploy\paired-folder-service.ps1 -Action restart @svc
```

手动停用时运行 `.\deploy\paired-folder-service.ps1 -Action remove @svc`，并在目标应用暂停或移除该 PR 事件订阅。移除登录任务会停止本机接收器，已有信件、配置、状态记录和 PR 的提交历史予以保留。

To stop the integration, run `.\deploy\paired-folder-service.ps1 -Action remove @svc` and pause or remove the PR subscription in the app. This stops and unregisters the local worker. Mail, enrollment state and Git history remain available.

## 日常收发 · Exchange mail

在另一端的已登记原聊天中，像平常一样发信。下例中 `$gateway` 是 Windows 网关的 xdev 主名；`paired-folder` 是网关代表该共享目录使用的固定客户端名。发信身份和回信地址由原会话及登记确定，每封信只需新的 ID、简短主题和正文。

From the enrolled peer chat, send a normal xdev letter. Address the gateway by its canonical name and the folder by its enrolled chat ID. The explicit sender and return fields below identify the peer chat and give replies a route back.

```powershell
xdev msg send --transport direct --to $gateway --client paired-folder --session $folderChat --from-client $peerClient --from-session $peerChat --return-client $peerClient --return-session $peerChat --id review-001 --subject "Review figures" --text "Which panel needs revision?"
```

走 GitHub 私仓中继时，在网关的对应设备配对项中把 `{"client":"paired-folder","session":"原聊天 ID"}` 加入 `receivers`，并重启网关的 GitHub 收件服务。此后上例使用 `--transport github` 即可。显式回信地址与 `--wait-reply` 是两种回信方式，示例采用前者。见[私仓信箱配置](github-mail.md)。

For delivery through GitHub, add the exact `paired-folder` client and chat ID to the gateway's `receivers` list for that peer, then restart its GitHub mail listener. Use `--transport github` in the example. The explicit return address lets the sender finish its turn and receive the reply later. [Private mailbox configuration](github-mail.md)

共享目录端使用以下 UTF-8 JSON 文件。写入时先写临时文件再改为最终文件名。每次操作取新的 `op_id`，文件名与它一致。重试时保留原 ID 和原内容。

At the folder end, write each UTF-8 JSON request to a temporary file and rename it into place. Use a fresh `op_id` per operation and keep its filename and content unchanged on retry.

| 文件 / File | 用途 / Purpose |
| --- | --- |
| `in/review-001.json` | 阅读来信的 `id`、`subject`、`text`、`sender`、`recipient` 和 `reply_to`。Read the delivered letter. |
| `read/seen-1.json` | 读完后登记 / Acknowledge after reading: `{"op_id":"seen-1","message_id":"review-001"}` |
| `out/answer-1.json` | 沿原信回复 / Reply to that letter: `{"op_id":"answer-1","reply_to":"review-001","subject":"Review figures","text":"Panel B."}` |
| `out/new-2.json` | 独立发起另一封 / Start a new letter: `{"op_id":"new-2","subject":"Second review","text":"Is the legend clear?"}` |
| `control/availability-1.json` | 声明本聊天的状态 / Declare this chat's availability: `{"op_id":"availability-1","action":"availability","state":"busy","ttl":900}` |
| `control/probe-1.json` | 查固定对端的最近状态 / Check the enrolled peer: `{"op_id":"probe-1","action":"preflight"}` |
| `control/work-1.json` | 登记已读来信的处理进度及回信意向 / Report work on an acknowledged letter: `{"op_id":"work-1","action":"report","message_id":"review-001","work_state":"completed","reply_intent":"no_reply"}` |
| `results/new-2.json` | 按操作 ID 取得状态和 xdev 信件 ID。Look up the outcome and assigned message ID. |

同一登记可持续使用。网关升级后的第一次轮询会补建 `control/`。每次新查询使用新的 `op_id`，结果仍在 `results/`。重复提交同一操作只返回已保存的观测。`prepare` 查自己，`preflight` 查已登记对端；结果标明采样、缓存或未知。`status` 限本次登记的收发件；`recall`、`replace`、`correct` 限本聊天寄出的信，后两者还须写已寄出的 `replacement_id`。可在 `out/` 信件加 `intent`，取 `inform`、`invite`、`request_reply` 或 `request_action`。各状态的含义见[信件状态手册](mail-lifecycle.md)。

The gateway adds `control/` on the first poll after upgrade. Use a new `op_id` for a fresh observation. Reusing an operation returns its saved result. Use `prepare` to inspect this chat or `preflight` to check its enrolled peer. Results distinguish sampled, cached and unknown status. Use `status` for mail registered to this pair. The author can request `recall`, `replace` or `correct` on outgoing mail. The latter two take a `replacement_id` from the same outgoing exchange. Outbound letters accept the optional `intent` values listed above. [Mail status guide](mail-lifecycle.md)

网关会把已召回或已归档的入箱文件逐批替换为状态标记。已经被宿主复制的内容、聊天记录、通知清单与归档包各有自己的保留期。

On subsequent polls, the gateway replaces its recalled or archived inbox copies with status markers in batches. Copies a host previously made, chat history, notification manifests and archive files have their own retention policies.

入箱由 `mailbox_received` 记录，宿主接纳可显示为 `session_accepted`。共享目录中的信实际读完并提交阅读记录后，`read` 表示 xdev 已登记已读。状态为 `unknown` 时按原操作查询。验收时双方各自空闲，分别发起新信，在对方的原会话核对正文与 `reply_to`。

`mailbox_received` records storage and `session_accepted` records host admission. A successful `read` result records the explicit acknowledgment after reading. For `unknown`, retain the request and query the original operation. Test both directions with a fresh letter from each side and verify the body and linked reply in the original chats.

其他 AI 工具可复用同一文件格式，每对聊天使用独立登记。已有本地宿主插件的工具也可直接使用[后台来信接口](github-mail.md)，将事件交给自己的原会话入口。

Other AI tools can reuse this folder format with a separate enrollment for each pair. Applications with a local host plugin can also use the [mail-arrival interface](github-mail.md) and hand the event to their own chat.
