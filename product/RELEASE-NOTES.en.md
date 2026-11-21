# xdev Windows Pair 1.1.0-preview.13

[简体中文](RELEASE-NOTES.md) · **English**

Check a chat’s availability before sending, then follow the same letter through intake, admission, reading, work updates and reply intent. Recall unread mail, link revisions, limit temporary relay storage and archive completed work. The private GitHub carrier supports silent status checks and recall. The paired-folder adapter can notify an enrolled original chat. [Mail status and retention](../design/mail-lifecycle.md) · [Paired-folder setup](../design/paired-folder-mail.md)

Host plugins can now wake an idle original chat on arrival. The host starts its own chat handling when it accepts the event. Other clients can continue checking their inbox.

GitHub mail arrives in the addressed chat’s local inbox. A chat can call `xdev msg inbox --wait 30` to await mail in its current call and resume with the returned cursor. On Windows, run `py -3.14 deploy/mail-listener.py install` to receive mail at sign-in, `status` to inspect the receiver, or `remove` to turn it off.

Each user can set up a dedicated private GitHub repository for cross-network delivery. Paired devices encrypt mail locally and exchange it as temporary release assets. The receiver removes each asset after durable local intake. [Private GitHub mailbox](../design/github-mail.md)

This release adds resident receivers, read-only host discovery and mail route selection. A host can resolve an original chat in its own running process or expose a matching tool catalog. Paired endpoints can select a direct connection and fall back to a configured encrypted relay after a confirmed pre-submission failure. [Host discovery](../design/host-discovery.md) · [Network configuration](../design/session-relay.md)

Pair the devices and connect the apps once. Supported hosts resolve the recipient chat by ID and supply the sender’s return address with the first letter. DeepSeek Harness can restore a dormant original chat by ID.

Select `direct` to exchange mail over a reachable, paired SSH connection. A Linux endpoint can bind a dedicated peer key to mail delivery and notifications for permitted chats. If the endpoints use a relay, they can require end-to-end encryption of subjects, chat addresses, text, forwarded letters and receipts. The relay can still observe the paired devices, transmission times and ciphertext sizes.

- **Install it yourself.** Verify `xdev-windows-pair-1.1.0-preview.13.zip` against its matching `.sha256` file and follow the [Windows installation guide](README.en.md).
- **Have an AI install it.** Give the assistant this release page and identify the devices and apps to connect. It can download the kit and follow the [AI deployment guide](AI-DEPLOY.en.md).

The Windows package requires 64-bit CPython 3.14. On Linux, extract the bundled `runtime.zip` to run the messaging components. Relay delivery requires both endpoints to reach the same service and run their listeners. Host admission, reading and a substantive reply can be verified separately. See the [cross-network messaging guide](../design/session-relay.md) for setup and two-way verification. The optional Amazon SQS FIFO transport requires `boto3` on each endpoint. [Managed queue guide](../design/managed-queue.md)
