# xdev Windows Pair 1.1.1

[简体中文](RELEASE-NOTES.md) · **English**

Windows Pair 1.1.1 tracks delivery across routes by the original letter ID. If a native wake-up attempt fails and the addressed chat later reads the letter through a paired gateway, the sender can see the confirmed read. Run `xdev msg status <letter-id>` to follow the same letter. [Mail status](../design/mail-lifecycle.md)

Upgrade each installation in place to keep its device pairings and chat bindings. If you use a paired-folder gateway, update its installed scripts and restart its receiver service. Reconnect clients that still have the previous xdev runtime loaded. [Upgrade steps](README.en.md#upgrade-rollback-and-migration) · [Paired gateway](../design/paired-folder-mail.md)

## What’s new since 1.0.3

- **Bring material in from a page or window.** Read text from an authorized browser tab or Windows window, act on a referenced control and check the result. Browser access uses OpenCode Browse and its paired extension. Windows access uses the controls an app exposes on the signed-in desktop. [Pages and windows](README.en.md#read-web-pages-and-application-windows)
- **Reach an existing chat on another network.** Windows and Linux messaging endpoints can use paired SSH directly or temporarily store end-to-end encrypted mail in a private GitHub repository you control. Hosts that support incoming mail events can wake an idle chat. Conversations can run concurrently, and each recipient of a group letter has a separate delivery record and reply chain. [Cross-network delivery](../design/session-relay.md) · [Private mailbox](../design/github-mail.md)
- **Follow a letter through the handoff.** Check a chat’s last known availability before sending. Use the original letter to follow read receipts, progress and reply intent. Request recall of unread mail or send a linked revision when the task changes. Completed mail can be archived locally, and relay storage has separate cleanup controls. [Mail status](../design/mail-lifecycle.md) · [Retention and archiving](../design/mail-retention.md)

## Already using a preview?

Keep the existing pairing and device names. Upgrade using the installation guide, then reconnect xdev in the client you use. Ask the chat to call `xdev_who` and check the device and loaded version. To test idle delivery, send a letter with a subject to an idle chat and check its read receipt and linked reply. [Upgrade steps](README.en.md#upgrade-rollback-and-migration) · [Client checks](CLIENTS.en.md)

The client guide includes personal plugins for Kimi Work, local skills for Doubao and Windows interoperability from WSL. It explains how to check the tool list, a model-issued call and delivery to an idle chat. The kit also contains an [isolated browser example](../examples/isolated-browser/README.md). Individual preview changes appear in the [changelog](CHANGELOG.en.md).
