# xdev — A channel for collaboration between AI chats

**English** · [简体中文](README.md)

Generative AI has lowered the barriers to research and creative work. More people can put their talents to work and turn an idea into something real. Twice the computing power can mean twice the output. Yet you have no extra time to review it. That imbalance turns time saved in creation into pressure at review. Early factories in the Industrial Revolution faced a similar bottleneck: machines sped up, but someone still had to move each piece to the next station. Today, AI tools work in parallel, then queue up for someone to pass a message. The promise of artificial intelligence has become intelligence plus manual labor. The creator is back on the assembly line, tightening one screw as the next comes within reach.

**Damn it, AI can already use a computer. Why do you still have to switch windows every time one AI hands work to another?**

xdev lets messages flow freely between AI chats, across apps and devices. You can put the copying and pasting aside. One AI hands its work directly to another. Messages flow both ways without a scramble for the channel, and the line keeps moving on its own.

For cross-device work, Windows PCs connect over SSH and SFTP. Through the command line or a local Model Context Protocol (MCP) connection, an AI assistant can browse, edit and transfer files, run commands on the other PC in Windows or WSL, and start background jobs, check their logs and retrieve the results. If the other client keeps readable transcripts or can export a chat, the assistant taking over can consult the original conversation with permission. It can check what was decided and why, making the handoff more reliable.

Now there’s time to linger over what you’ve made, and room to wonder where you might go next.

## Getting started

Choose one installation method.

| Install it yourself | Have an AI install it |
| --- | --- |
| Download the Windows Pair ZIP and its matching `.sha256` file from the [latest release](https://github.com/realhdx/xdev/releases/latest). Follow the [installation guide](product/README.en.md). For multiple devices, use the same ZIP on each. | Paste the prompt below into an AI chat that can operate your PCs. The assistant downloads the package, installs xdev and verifies the setup. [AI deployment guide](product/AI-DEPLOY.en.md) |

```text
Get the latest release of xdev from https://github.com/realhdx/xdev and follow the included AI deployment guide to install and verify it on the Windows devices I specify.
```

For builds and tests, see the [build guide](BUILDING.en.md). For versions and upgrade notes, see the [changelog](product/CHANGELOG.en.md).

### What does the 1.1 preview connect?

**Work can start from an open page or an application window.** In a browser tab you authorize, an AI can read the page, fill in a form, press a button and check the result. It can also read and operate the controls a Windows app exposes. A passage from a page or a notice in a window can travel with its source and a question to a particular chat, which can reply to the original letter. Browser tabs use OpenCode Browse and its paired extension. Windows apps use the operating system’s accessibility interface. [Working with pages and windows](product/README.en.md#read-web-pages-and-application-windows)

**The chat you need can be on another network.** Windows and Linux messaging endpoints can exchange letters over a paired SSH connection or use a private GitHub repository to hold mail encrypted at the endpoints. Once connected, a supported app can find the original chat by address and include the sender’s return address. A host that handles incoming mail events can wake an idle chat. [Cross-network routes](design/session-relay.md) · [Connecting chat hosts](design/host-discovery.md) · [Connecting cloud chats](design/paired-folder-mail.md)

**Is the other chat available?** The preview can check a particular chat before you send. Follow the same letter to see what was read, whether work has started and whether to expect a reply. If the task changes, recall an unread letter or send a linked correction. [Current preview](https://github.com/realhdx/xdev/releases/tag/v1.1.0-preview.13-docs.1) · [Mail status and retention](design/mail-lifecycle.md)

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

Each address identifies a **device, AI client and chat ID**, so even chats in the same app on the same PC can exchange letters. A chat with a registered identity can send a letter with a subject to one or more recipients, or forward it with the complete original attached. Recipients can reply to the sender, write back to the original author, or reply to everyone. Run `xdev ai --capabilities` to see which delivery modes a client supports. xdev records arrival in the inbox, acceptance into the current turn, a read receipt and a reply separately.

To look up what was said in a particular chat, use `xdev file read` to read a range of lines from an existing transcript on the other PC, or use `xdev exec` to run the client’s own export command. For example, OpenCode’s `opencode export <chat-id>` returns the chat as JSON.

## Requirements

- **Core commands** require Windows x64, 64-bit CPython 3.14 and Windows PowerShell. Each PC that receives cross-device operations needs a reachable OpenSSH/SFTP service and must accept the other PC’s verified SSH credentials.
- **Local MCP** uses the offline Python dependencies included in the installer archive. Receiving an incoming letter in an existing chat also requires a delivery mode supported by that client. [Third-party components and licenses](product/AI-DEPLOY.en.md#third-party-components)
- **Optional features** have their own requirements. Windows background jobs use Task Scheduler. Linux commands run in a WSL distribution installed on the target PC. NATS JetStream is required only if you choose broker-based message delivery.

## Contributing

In tests with Codex, Claude Desktop, DeepSeek Harness and OpenCode, xdev delivered messages to specific chats and routed replies back to the sender, with each reply linked to the original message. We also tested round trips in WorkBuddy with help in the app, and in the Gemini and Doubao web apps through a browser relay.

If you receive your first xdev letter in a new AI client, a pull request with the client version, setup steps and verification method will help others get connected. If a message gets stuck in a queue, open an issue and describe when you expected it to reach the chat and how far it actually got. After changing the code, follow the [build guide](BUILDING.en.md) to run the tests.

If xdev has saved you a round of passing messages, consider giving the project a star.

☕ If xdev has saved you a few trips between chat windows, feel free to [buy me a coffee](https://ko-fi.com/dixihan).

## License and citation

Dixi Han (韩迪熹) at China Agricultural University designed and developed xdev. Qiang Zhong (钟强) at the same university provided supervision and funding.

The source code, scripts and adapters written for xdev are licensed under MPL-2.0, which permits commercial use and redistribution. When distributing a modified version, make the corresponding source for the covered files available to recipients. Bundled third-party components retain their own licenses. [Project license](LICENSE) · [Third-party components](product/AI-DEPLOY.en.md#third-party-components)

Code contributions are welcome under MPL-2.0. Contributors retain copyright in their contributions. If you refer to xdev in a paper or in product documentation, please cite the version you used. [How to cite xdev](CITATION.cff)
