# xdev — A channel for collaboration between AI chats

**English** · [简体中文](README.md)　|　[Features](#what-you-can-do-with-xdev) · [Will my app work?](#will-it-work-with-my-ai-app) · [Install](#getting-started)

Generative AI has lowered the barriers to research and creative work. More people can put their talents to work and turn an idea into something real. Twice the computing power can mean twice the output. Yet you have no extra time to review it. That imbalance turns time saved in creation into pressure at review. Early factories in the Industrial Revolution faced a similar bottleneck: machines sped up, but someone still had to move each piece to the next station. Today, AI tools work in parallel, then queue up for someone to pass a message. The promise of artificial intelligence has become intelligence plus manual labor. The creator is back on the assembly line, tightening one screw as the next comes within reach.

**Damn it, AI can already use a computer. Why do you still have to switch windows every time one AI hands work to another?**

xdev lets messages flow freely between AI chats, across apps and devices. You can put the copying and pasting aside. One AI hands its work directly to another. Messages flow both ways without a scramble for the channel, and the line keeps moving on its own.

For cross-device work, Windows PCs connect over SSH and SFTP. Through the command line or a local Model Context Protocol (MCP) connection, an AI assistant can browse, edit and transfer files, run commands on the other PC in Windows or WSL, and start background jobs, check their logs and retrieve the results. If the other client keeps readable transcripts or can export a chat, the assistant taking over can consult the original conversation with permission. It can check what was decided and why, making the handoff more reliable.

Now there’s time to linger over what you’ve made, and room to wonder where you might go next.

## What you can do with xdev

Say an assistant on your laptop reads data from a web page, sends the file to a workstation for analysis, then passes the results and their source to a review chat in another app. When the reviewer asks “Did that count include blank rows?”, the question goes back to the assistant that ran the analysis. It can check the command and the job log before it answers.

| Goal | How xdev helps |
| --- | --- |
| Read and use a web page | Share an open tab or let xdev open one. An assistant can read headings and text, follow a link, fill in a form and check the result after submission. [Web pages](product/README.en.md#read-web-pages-and-application-windows) |
| Read and operate a Windows app | Read the text and control states a window exposes through Windows accessibility. An assistant can tell whether **Export** is enabled, activate it by its identifier and read the response without locating it in a screenshot. [App windows](product/README.en.md#read-web-pages-and-application-windows) |
| Put another computer to work | Browse, edit and transfer files in either direction. Run commands on a paired Windows PC or in WSL. Leave long jobs running in the background, then check their logs and retrieve the results. [Files and jobs](product/README.en.md#files-and-jobs) |
| Hand work to the right chat | Address a chat by device, app and chat ID, even within one app on one PC. Send to a group, forward the original letter, reply to the sender or write to the original author. [Chat messaging](product/README.en.md#send-mail-to-a-specific-chat) · [Read the original chat](#read-the-original-chat) |
| Reach a chat on another network | Windows and Linux messaging endpoints can send mail over paired SSH or through your own private GitHub repository, which temporarily holds end-to-end encrypted mail. [Cross-network routes](design/session-relay.md) · [Cloud chats](design/paired-folder-mail.md) |
| See whether the work has been picked up | Check the recipient’s last reported status before sending. Track arrival, queuing, reads, progress and replies. If plans change, request a recall of unread mail or send a correction. Archive important letters locally. [Mail status](design/mail-lifecycle.md#english) · [Retention and archiving](design/mail-retention.md#english) |

## Will it work with my AI app?

AI tools with local MCP support or permission to run local commands can use xdev. An app without a built-in adapter can take part in a first handoff through its interface. If it also exposes tools to find and notify an existing chat by ID, a reusable template can deliver future letters by address.

- **Built-in adapters cover several widely used AI clients.** In tests with Codex, DeepSeek Harness, OpenCode and supported Claude Desktop engines, the addressed chat received a letter and replied to it. The receiving app determines whether a letter can wake an idle chat, reach it during a turn or queue for the next one. [How each client receives mail](product/README.en.md#which-clients-can-notify-an-existing-chat)
- **Custom templates extend support to new apps and versions that expose a chat API.** An assistant can read the app’s API documentation in an authorized tab, then map the lookup and delivery tools and their fields. If the service declares those roles, you can authorize xdev to match them automatically. Check the target with `xdev ai --discover --client <app> --session <chat-id>`, then exchange a test letter. Other chats in the same app can reuse that configuration. [Template and verification example](design/host-discovery.md#english)
- **Interface relays can reach chats without a dedicated adapter.** An authorized assistant needs to identify the chat, submit the question with its letter ID and read back the answer in that same chat. It then replies to the original letter under its own name and credits the source chat. Gemini and Doubao web chats have completed round trips this way. [How to relay a letter](product/README.en.md#which-clients-can-notify-an-existing-chat) · [Tested clients](product/CLIENTS.en.md#the-client-shows-a-connection-but-the-chat-cannot-use-it)

## Getting started

The Windows package requires Windows x64, 64-bit CPython 3.14 and Windows PowerShell. Choose either installation method.

| Install it yourself | Have an AI install it |
| --- | --- |
| Download the Windows Pair ZIP and its matching `.sha256` file from the [latest release](https://github.com/realhdx/xdev/releases/latest). Follow the [installation guide](product/README.en.md). For multiple devices, use the same ZIP on each. | Paste the prompt below into an AI chat that can operate your PCs. The assistant downloads the package, installs xdev and verifies the setup. [AI deployment guide](product/AI-DEPLOY.en.md) |

```text
Get the latest release of xdev from https://github.com/realhdx/xdev and follow the included AI deployment guide to install and verify it on the Windows devices I specify.
```

### Requirements

Remote file transfers and commands use the receiving PC’s paired SSH/SFTP connection. Browser access requires OpenCode Browse and a paired extension. Windows apps must run on an accessible signed-in desktop. The receiver can grant remote read or interaction permissions separately for tabs and windows. The package includes offline dependencies for local MCP. [Full requirements and optional components](product/README.en.md#requirements)

## Using xdev after installation

### Send a letter to a specific chat

Run this PowerShell example in a chat with a registered sender identity. Replace `lab` with the paired device name and supply the recipient’s client and chat ID. This example sends to OpenCode.

```powershell
$xdev = Join-Path $env:LOCALAPPDATA 'CrossDeviceCollab\xdev.cmd'
$peer = 'lab'
& $xdev msg send --to $peer --client opencode --session '<chat-id>' --subject 'Check the export' --text 'Please check the row count in the exported file and reply to this letter.'
```

Read the letter in the receiving chat and reply to it. A linked reply back in the sending chat confirms the round trip. If the letter is queued, use its original ID to check progress. [Read and reply commands](product/README.en.md#send-mail-to-a-specific-chat)

### Retrieve a result file

To check the count independently, retrieve the file from the same device. Reuse the variables above and replace the remote path with the file’s actual location.

```powershell
& $xdev file pull --host $peer --remote 'D:\shared\results.csv' --local '.\results.csv' --no-overwrite
```

### Read the original chat

Use `xdev file read` to read a range of lines from an existing transcript on the other PC, or use `xdev exec` to run the client’s own export command. For example, OpenCode’s `opencode export <chat-id>` returns the chat as JSON.

## Version updates

[Latest release and downloads](https://github.com/realhdx/xdev/releases/latest) · [Release notes](product/RELEASE-NOTES.en.md) · [Changelog](product/CHANGELOG.en.md)

## Contributing

The client guide records round trips through several routes, from direct delivery in Codex and supported Claude Desktop engines to assisted delivery in WorkBuddy and relays through web chats. [Tested clients and methods](product/CLIENTS.en.md)

If you receive your first xdev letter in a new AI client, a pull request with the client version, setup steps and verification method will help others get connected. If a message gets stuck in a queue, open an issue and describe when you expected it to reach the chat and how far it actually got. After changing the code, follow the [build guide](BUILDING.en.md) to run the tests.

If xdev has saved you a round of passing messages, consider giving the project a star.

☕ If xdev has saved you a few trips between chat windows, feel free to [buy me a coffee](https://ko-fi.com/dixihan).

## License and citation

Dixi Han (韩迪熹) at China Agricultural University designed and developed xdev. Qiang Zhong (钟强) at the same university provided supervision and funding.

The source code, scripts and adapters written for xdev are licensed under MPL-2.0, which permits commercial use and redistribution. When distributing a modified version, make the corresponding source for the covered files available to recipients. Bundled third-party components retain their own licenses. [Project license](LICENSE) · [Third-party components](product/AI-DEPLOY.en.md#third-party-components)

Code contributions are welcome under MPL-2.0. Contributors retain copyright in their contributions. If you refer to xdev in a paper or in product documentation, please cite the version you used. [How to cite xdev](CITATION.cff)
