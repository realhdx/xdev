# Changelog

[简体中文](CHANGELOG.md) · **English**

## Windows Pair 1.1.0-preview.13 · Documentation revision 1 · 2026-11-22

Open the [release overview](RELEASE-NOTES.en.md) offline after extracting the ZIP. It links to guides for working with pages and Windows controls, delivering mail across networks, waking an original chat, following concurrent exchanges and archiving mail. The kit also includes the WorkBuddy plugin’s chat-mail instructions and an optional [isolated browser experiment](../examples/isolated-browser/README.md). It uses the same runtime and offline dependencies as preview.13. Existing installations can use the new guides immediately.

## Windows Pair 1.1.0-preview.13 · 2026-11-21

Concurrent exchanges can track progress by original letter ID. Group mail returns delivery and availability evidence for each addressee. Freshness uses the sender’s own inquiry time to accommodate different device clocks. [Working across chats](../design/mail-lifecycle.md)

## Windows Pair 1.1.0-preview.12 · 2026-11-21

Check a chat’s availability before sending, then follow the same letter through intake, admission, reading, work updates and reply intent. Recall unread mail, link revisions, limit temporary relay storage and archive completed work. The private GitHub carrier supports silent status checks and recall. The paired-folder adapter can notify an enrolled original chat. [Mail status and retention](../design/mail-lifecycle.md) · [Paired-folder setup](../design/paired-folder-mail.md)

## Windows Pair 1.1.0-preview.11 · 2026-11-21

A host plugin can receive new-mail events for an existing chat while the model is idle. The application can wake the addressed chat for a letter or correlated reply. [GitHub mailbox and host integration](../design/github-mail.md)

## Windows Pair 1.1.0-preview.10 · 2026-11-21

When an addressed chat waits for mail, its local receiver resumes active checks while honoring GitHub’s rate limits. Incomplete downloads are checked again under the original asset ID. The sender can leave the original letter in place.

## Windows Pair 1.1.0-preview.9 · 2026-11-21

The Windows sign-in task verifies that its background receiver started without a console. The GitHub receiver checks an idle inbox less often to conserve the owner’s API quota. [Mailbox setup and limits](../design/github-mail.md)

## Windows Pair 1.1.0-preview.8 · 2026-11-21

GitHub mail arrives in the addressed chat’s local inbox. A chat can call `xdev msg inbox --wait 30` to await mail in its current call and resume with the returned cursor. On Windows, run `py -3.14 deploy/mail-listener.py install` to receive mail at sign-in, `status` to inspect the receiver, or `remove` to turn it off.

## Windows Pair 1.1.0-preview.7 · 2026-11-21

Each user can set up a dedicated private GitHub repository for cross-network delivery. Paired devices encrypt mail locally and exchange it as temporary release assets. The receiver removes each asset after durable local intake. [Private GitHub mailbox](../design/github-mail.md)

## Windows Pair 1.1.0-preview.6 · 2026-11-21

An app can receive notices in its own process or expose a tool catalog. Discovery checks the host, original chat and supported placement. A send with an uncertain outcome keeps its original record. Paired devices can choose among configured mail routes and switch only after a confirmed pre-submission failure. [Host discovery](../design/host-discovery.md) · [Network routes](../design/session-relay.md)

## Windows Pair 1.1.0-preview.5 · 2026-11-21

DeepSeek Harness supplies the sending chat’s identity with each tool call. Once the app is connected, the host resolves the recipient by its original chat ID and replies follow the original letter. Chats registered before an upgrade can also be restored after going idle. Existing or uncertain admissions retain their delivery record to prevent duplicate wakes.

## Windows Pair 1.1.0-preview.4 · 2026-11-21

Select `direct` for mail over paired SSH. Linux receivers can restrict a peer’s dedicated SSH key to mailbox access. Endpoints that select relay delivery can require end-to-end encryption for each pairing, including notifications, forwarded originals and receipts. Peers configured to require encryption reject plaintext. Amazon SQS FIFO is an optional store for encrypted mail and requires a separate `boto3` installation. [Managed queue guide](../design/managed-queue.md) · [Deployment and verification guide](../design/session-relay.md)

Earlier versions could return `bidirectional_ready: true` based on a reply address alone. Use `return_address_known` to check for an address and `return_session_status` to inspect the return route. When xdev cannot verify the receiving chat for replies, `bidirectional_ready` is `null`.

## Windows Pair 1.1.0-preview.3 · 2026-11-21

Chats on separate networks can exchange letters through a trusted NATS JetStream server. Each endpoint connects outbound, and each letter names a device, client and chat. A third-party app can register a local receiver for an existing chat and report acceptance into the current turn, the next turn, or the original conversation. xdev deduplicates by message ID and retains uncertain results to prevent repeated wakes. Read receipts and linked replies can be checked separately. [Deployment and verification guide](../design/session-relay.md)

## Windows Pair 1.1.0-preview.2

CLI and MCP guidance now covers pages and apps that are still loading or processing an action. Surface results describe the current state and suggest a read-only follow-up for the same device and target. When an action’s outcome is uncertain, inspect the target before trying the action again.

## Windows Pair 1.1.0-preview.1

Adds a semantic interface for browser tabs and Windows controls through the CLI and MCP. Remote access is opt-in on each receiver.

## Windows Pair 1.0.3 · Documentation revision 6 · 2026-11-20

The project overview and installation guides now give each installation path its own starting point. Download the release for a manual installation, or give an AI one prompt to fetch, install and verify xdev on the selected devices. One PC can run independently. Additional devices use the same package and need pairing only for cross-device work.

## Windows Pair 1.0.3 · Documentation revision 5 · 2026-11-20

The project overview, installation and user manual, AI deployment guide, build guide and release documentation now have Chinese and English editions. The same ZIP supports both installation paths. The project overview also lists the clients tested in real chats and how each handoff worked. Existing 1.0.3 installations can use the new guides immediately.

## Windows Pair 1.0.3 · Documentation revision 4 · 2026-11-19

Updated the recommended author attribution in the package’s `CITATION.cff`. The full package is `xdev-windows-pair-1.0.3-docs.4.zip`. The citation applies to existing 1.0.3 installations.

## Windows Pair 1.0.3 · Documentation revision 3 · 2026-11-19

xdev’s own source is licensed under MPL-2.0. The full package, `xdev-windows-pair-1.0.3-docs.3.zip`, includes the license text in `LICENSE` and the recommended citation in `CITATION.cff`. Third-party components retain their upstream licenses. This revision uses the 1.0.3 runtime and offline dependencies.

## Windows Pair 1.0.3 · Documentation revision 2 · 2026-11-19

Introduced separate starting points for manual and AI-assisted installation. The guides list dependencies by feature and identify the source and license of each bundled third-party component. The full package is `xdev-windows-pair-1.0.3-docs.2.zip`. Existing installations can use these docs with the 1.0.3 runtime and receiver plugins.

## Windows Pair 1.0.3 · Documentation revision 1 · 2026-11-19

Added examples of MCP arguments, command-line fallback, delivery checks by message ID and forwarding to an existing browser chat.

## Windows Pair 1.0.3 · 2026-11-19

The installer retries a limited number of times when Windows temporarily locks a client configuration file. Before each retry, it verifies the original digest to preserve changes made by the user or another application.

## Windows Pair 1.0.2 · 2026-11-19

Added configuration support for Copilot, CodeBuddy, Gemini CLI, Kimi Code, VS Code and Cursor. Updates preserve other entries, and a change receipt supports rollback. The optional `xdev-local` skill helps an AI find the installed commands.

## Windows Pair 1.0.1 · Documentation revision 1 · 2026-11-19

Added guidance on client reconnection and message receipt, writing multiline files on Windows, and upgrading a shared Python dependency directory.

## Windows Pair 1.0.1 · 2026-11-19

New installations use the Windows device name by default. Users can choose a primary name and aliases. Devices using older addresses can upgrade one at a time. Existing messages, references and replies retain their original addresses.

## Windows Pair 1.0.0 · 2026-11-19

First release for private collaboration between two Windows computers. It supports file transfers with digest verification, remote commands, background jobs and WSL execution. Chat mailboxes support subjects, directed replies, forwarding complete originals and group messages. The installer supports upgrades, rollback and private mailbox migration.

## Identifying your version

`xdev --version` shows the product series and runtime version. `install.py status` shows the active runtime digest on the local computer. A documentation-only package has a `-docs.N` suffix and its own SHA-256 file. A client’s receiver plugin may use a version number specific to its host.

[Installation and user manual](README.en.md) · [AI deployment guide](AI-DEPLOY.en.md)
