# xdev — A channel for collaboration between AI chats

**English** · [简体中文](README.md)

Generative AI has lowered the barriers to research and creative work. More people can put their talents to work and turn an idea into something real. Twice the computing power can mean twice the output. Yet you have no extra time to review it. That imbalance turns time saved in creation into pressure at review. Early factories in the Industrial Revolution faced a similar bottleneck: machines sped up, but someone still had to move each piece to the next station. Today, AI tools work in parallel, then queue up for someone to pass a message. The promise of artificial intelligence has become intelligence plus manual labor. The creator is back on the assembly line, tightening one screw as the next comes within reach.

**Damn it, AI can already use a computer. Why do you still have to switch windows every time one AI hands work to another?**

xdev lets messages flow freely between AI chats, across apps and devices. You can put the copying and pasting aside. One AI hands its work directly to another. Messages flow both ways without a scramble for the channel, and the line keeps moving on its own.

For cross-device work, Windows PCs connect over SSH and SFTP. Through the command line or a local Model Context Protocol (MCP) connection, an AI assistant can browse, edit and transfer files, run commands on the other PC in Windows or WSL, and start background jobs, check their logs and retrieve the results. If the other client keeps readable transcripts or can export a chat, the assistant taking over can consult the original conversation with permission. It can check what was decided and why, making the handoff more reliable.

Now there’s time to linger over what you’ve made, and room to wonder where you might go next.

## What you can do with xdev

| Goal | How xdev helps |
| --- | --- |
| Hand work to a particular chat | Address a chat by device, app and chat ID. Chats can exchange letters even inside the same app on one PC. Send to a group, forward a letter with the original attached, reply in context or write back to the original author. [Chat messaging](product/README.en.md#send-mail-to-a-specific-chat) |
| See whether someone has picked up the work | Check the recipient’s last reported status before sending. Afterwards, track arrival, queuing, reads, progress and replies separately. If plans change, request a recall of unread mail or send a correction. Archive letters you want to keep locally. [Mail status](design/mail-lifecycle.md#english) · [Retention and archiving](design/mail-retention.md#english) |
| Bring source material into a conversation | Read an authorized browser tab or Windows app window, fill in a field or press a button, then check the result. If a client offers readable transcripts or an export, look up what was said in the original chat. [Pages and windows](product/README.en.md#read-web-pages-and-application-windows) · [Reading the original chat](#read-the-original-chat) |
| Work on another computer | Browse, edit and transfer files in either direction. Run commands on a paired Windows PC or in its WSL environment. Start long jobs in the background, check their logs and retrieve the results. [Files and jobs](product/README.en.md#files-and-jobs) |
| Reach a chat on another network | Windows and Linux messaging endpoints can send mail directly over paired SSH or through your own private GitHub repository, which temporarily holds end-to-end encrypted mail. Apps that handle incoming mail events can wake an idle chat. [Cross-network routes](design/session-relay.md) · [Connecting cloud chats](design/paired-folder-mail.md) |

## Will it work with my AI app?

Apps with local MCP support can call xdev tools. To deliver mail directly to a particular chat, start here.

- **Use a built-in adapter.** Codex, DeepSeek Harness, OpenCode and some Claude Desktop engines already have adapters for incoming mail. Some clients can take a message during the current turn. Others queue it for the next one. [Client setup guide](product/CLIENTS.en.md) · [How each client receives mail](product/README.en.md#which-clients-can-notify-an-existing-chat)
- **Make a template for a new app or version.** If the app exposes tools to find and notify a particular chat, you or your AI assistant can map their fields using the example. If the service already declares those roles, you can authorize xdev to match them automatically. Run `xdev ai --discover` to see approved connections, then send a test letter and check for a read receipt and a reply from the intended chat. Waking an idle chat requires support from the app itself. [Template and verification example](design/host-discovery.md#english)

## Getting started

Choose one installation method.

| Install it yourself | Have an AI install it |
| --- | --- |
| Download the Windows Pair ZIP and its matching `.sha256` file from the [latest release](https://github.com/realhdx/xdev/releases/latest). Follow the [installation guide](product/README.en.md). For multiple devices, use the same ZIP on each. | Paste the prompt below into an AI chat that can operate your PCs. The assistant downloads the package, installs xdev and verifies the setup. [AI deployment guide](product/AI-DEPLOY.en.md) |

```text
Get the latest release of xdev from https://github.com/realhdx/xdev and follow the included AI deployment guide to install and verify it on the Windows devices I specify.
```

For builds and tests, see the [build guide](BUILDING.en.md).

## Using xdev after installation

### Retrieve a result file

After installing and pairing the PCs, run this in PowerShell. Replace `lab` with the name you registered for the other PC and use the path to an existing file on it. Either PC can initiate a transfer.

```powershell
$xdev = Join-Path $env:LOCALAPPDATA 'CrossDeviceCollab\xdev.cmd'
$peer = 'lab'
& $xdev who --host $peer
& $xdev file pull --host $peer --remote 'D:\shared\results.csv' --local '.\results.csv' --no-overwrite
```

When editing an existing remote file, use the SHA-256 digest from your first read as a condition for writing it back. This protects changes someone else saved in the meantime. For a long computation, start a background job on the target PC and use its job ID to look up its status and logs later.

### Send a letter to a specific chat

In the sending chat, use the same `$xdev` and `$peer` as above. This example addresses an OpenCode chat. Replace the placeholder with the recipient’s chat ID. [Read and reply examples](product/README.en.md#send-mail-to-a-specific-chat)

```powershell
& $xdev msg send --to $peer --client opencode --session '<chat-id>' --subject 'Check the export' --text 'Please check the row count in the exported file and reply to this letter.'
```

### Read the original chat

Use `xdev file read` to read a range of lines from an existing transcript on the other PC, or use `xdev exec` to run the client’s own export command. For example, OpenCode’s `opencode export <chat-id>` returns the chat as JSON.

## Requirements

- **Core commands** require Windows x64, 64-bit CPython 3.14 and Windows PowerShell. Each PC that receives cross-device operations needs a reachable OpenSSH/SFTP service and must accept the other PC’s verified SSH credentials.
- **Local MCP** uses the offline Python dependencies included in the installer archive. Receiving an incoming letter in an existing chat also requires a delivery mode supported by that client. [Third-party components and licenses](product/AI-DEPLOY.en.md#third-party-components)
- **Optional features** have their own requirements. Windows background jobs use Task Scheduler. Linux commands run in a WSL distribution installed on the target PC. NATS JetStream is required only if you choose broker-based message delivery.

Browser actions require OpenCode Browse and a paired browser extension. Windows app actions use the controls the app exposes through Windows accessibility.

## Version updates

[Latest release and downloads](https://github.com/realhdx/xdev/releases/latest) · [Release notes](product/RELEASE-NOTES.en.md) · [Changelog](product/CHANGELOG.en.md)

## Contributing

In tests with Codex, supported Claude Desktop engines, DeepSeek Harness and OpenCode, xdev delivered messages to specific chats and routed replies back to the sender, with each reply linked to the original message. We also tested round trips in WorkBuddy with help in the app, and in the Gemini and Doubao web apps through a browser relay.

If you receive your first xdev letter in a new AI client, a pull request with the client version, setup steps and verification method will help others get connected. If a message gets stuck in a queue, open an issue and describe when you expected it to reach the chat and how far it actually got. After changing the code, follow the [build guide](BUILDING.en.md) to run the tests.

If xdev has saved you a round of passing messages, consider giving the project a star.

☕ If xdev has saved you a few trips between chat windows, feel free to [buy me a coffee](https://ko-fi.com/dixihan).

## License and citation

Dixi Han (韩迪熹) at China Agricultural University designed and developed xdev. Qiang Zhong (钟强) at the same university provided supervision and funding.

The source code, scripts and adapters written for xdev are licensed under MPL-2.0, which permits commercial use and redistribution. When distributing a modified version, make the corresponding source for the covered files available to recipients. Bundled third-party components retain their own licenses. [Project license](LICENSE) · [Third-party components](product/AI-DEPLOY.en.md#third-party-components)

Code contributions are welcome under MPL-2.0. Contributors retain copyright in their contributions. If you refer to xdev in a paper or in product documentation, please cite the version you used. [How to cite xdev](CITATION.cff)
