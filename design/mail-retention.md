# Mail retention · 信件留存

## 中文

收件箱保存正文、引用的原件、主题、收发地址和处理记录。私有 GitHub 中转使用配对加密的临时 release 附件。用于提示聊天“有新信”的空提交只有固定说明及 Git 元信息，会留在提交历史中。两类载体有各自的生命周期。

**保留与归档。** 在信件所在设备运行 `python deploy/mail-retention.py --root <数据目录> <操作>`。私有归档选在安装包、源码、运行状态目录之外，使用本机绝对文件路径。归档和导出的 JSON 含可读正文，存放在仅预期读者能访问的目录。多台设备可各自设定策略。命令行中的尖括号表示要替换的值。

| 目的 | 操作 |
| --- | --- |
| 只复制到归档，保留收件箱原件 | `export <信件ID> --archive <归档文件>` |
| 单独保护或解除保护 | `protect <信件ID>` 或 `unprotect <信件ID>` |
| 明确归档一封已读信 | `archive <信件ID> --archive <归档文件>` |
| 在原设备核验、恢复 | `verify <信件ID> --archive <归档文件>` 或 `restore <信件ID> --archive <归档文件>` |
| 从携带的归档提取原件 | `extract <信件ID> --source-device <归档时设备名> --archive <归档文件> --output <新JSON文件>` |
| 查看空间 | `usage` 查本机收件库，`folder-usage --directory <配对目录>` 查共享目录各槽 |

归档先提交并回读校验正文与内嵌原件的 SHA-256，再从收件箱移开二者。收件箱保留地址、短主题或原文摘要、已读、关联和去重信息；原 ID 可继续查账。相同正文与内嵌原件在同一归档文件里只保存一份。配对网关下次运行时会把自己先前投递的该信副本替换为带原 ID 的归档标记。信里指向的外部文件仍由其所有者保管。

**按量或按时间。** `plan --archive <归档文件> --keep-days 30 --max-live-bytes 67108864` 只列候选；相同参数用 `sweep` 才执行。两个阈值可单独使用，默认每轮最多 25 封，`--limit` 上限 100。候选必须已有收件方已读记录，并且通过 `xdev msg report <ID> --work-state completed` 报告处理完毕，或由旧宿主在本机 `mark-processed <ID>`。受保护件、未读件和未完成件保留，因而字节目标有时达不到。按 ID 的 `archive` 则表示操作者明确选择这封已读信。

`usage` 分别报告活跃正文与内嵌原件的字节、数据库文件字节、WAL 字节、可复用空页和已归档件数。`compact` 独立执行 SQLite 整理并实测前后大小，可能需要临时磁盘空间和写锁。归档本身仍占存储，同一磁盘上总占用可能增加。`folder-usage` 按槽统计 JSON 文件，达到扫描上限时返回不完整标记。收件箱归档独立于其他聊天已下载的副本、文件系统备份和云盘版本历史。

**GitHub 载体。** `github_mailbox` 下的 `retention_seconds` 默认 3600 秒，允许 60～86400 秒。`max_pending_assets` 默认 500 件，上限 1000；`max_pending_bytes` 默认 64 MiB，上限 1 GiB。发送前按该 release 中可见附件和本网关尚未到期的本地占额计数，以件数或字节数先到者为准。已确认上传的附件从后续完整列表消失后会腾出占额，上传结果不明的件保留占额至到期。满额时在上传前返回原因，保留原 ID 供重试。不同设备同时写同一 release 时没有跨设备原子配额；为各收件端单设 release 可减少竞争。

接收端按协议确认入箱后可移除临时密文；发送端的接收服务在线时也会按本地账本巡查到期件。单独运行 `github-sweep` 可巡查一批，`github-status` 只显示本地观测总数。确认私仓、release、派生名称、内容摘要及已知附件 ID 后才请求删除；同一 release 中其他文件保留。两端同时离线时要等其中一端恢复，GitHub 不执行 xdev 的到期计时。远端附件消失只记为结果未知；确认删除过期件与“收件方原聊天已读”分别记账。

## English

Run `python deploy/mail-retention.py --root <data-directory> <action>` on the device holding the inbox. Use an absolute private archive path outside the source, product and live state directories. Archives and extracted JSON contain readable material.

- **Keep or move a letter.** `export ID --archive FILE` makes a verified copy. `archive ID --archive FILE` moves the body and its embedded attachment after an explicit read. The inbox keeps addressing, subject or short preview, read evidence, reply links and a replay digest. Identical material shares one archive blob. At its next pass the paired gateway replaces its own exported copy with an archive marker carrying the original ID. Protect individual letters with `protect ID`; release that hold with `unprotect ID`. Referenced external files retain their own storage lifecycle.
- **Verify or recover.** Use `verify ID --archive FILE` or `restore ID --archive FILE` at the original inbox. On a different machine, `extract ID --source-device NAME --archive FILE --output NEWFILE` verifies and writes a new JSON document, including the embedded original. Use the source device name recorded at export.
- **Apply a policy.** `plan --archive FILE --keep-days 30 --max-live-bytes 67108864` previews the oldest eligible letters. Replace `plan` with `sweep` to act. Either threshold is sufficient. Each batch handles up to 25 letters by default or 100 with `--limit`. Only read, unprotected letters reported as completed by the recipient, or explicitly marked with `mark-processed ID`, are eligible. The live inbox can stay above the requested capacity.
- **Measure space.** `usage` reports live material, database and WAL bytes, reusable pages and archive count. `compact` runs SQLite VACUUM and reports measured size changes. Moving material to an archive on the same disk can increase total disk use. `folder-usage --directory DIRECTORY` counts JSON bytes by paired mailbox slot and reports when its scan limit was reached. Downloads, independent copies and backups have their own retention policies.
- **Bound GitHub storage.** Under `github_mailbox`, `retention_seconds` defaults to 3600 and accepts 60–86400. `max_pending_assets` defaults to 500 (1–1000); `max_pending_bytes` defaults to 64 MiB (1 byte–1 GiB). Admission counts provider-visible assets and outstanding local claims once per name. An acknowledged upload frees its slot when absent from a subsequent complete listing. An uncertain upload keeps its reservation until expiry. Capacity pressure rejects the upload before submission. Writers on different devices can observe the same free space. A dedicated release per receiver reduces contention.
- **Check expiry.** The receiver can remove sealed assets after durable inbox acceptance. The online sender's listener also checks its overdue uploads. Use `github-sweep` for a bounded sender pass and `github-status` for local outcome counts. Deletion requires verified identity and matching content. If both endpoints are offline, cleanup resumes when an endpoint returns. An absent asset has an unknown disposition. Only a chat-level receipt establishes that the addressee read the letter. Fixed-message, content-free notification commits remain in Git history.
