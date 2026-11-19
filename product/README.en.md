# xdev Windows Pair

**English** | [简体中文](README.md)

Send a file to a second Windows PC, run a program there, and have a specific AI chat send the result back. xdev provides the same operations through its command line and MCP tools. Either computer can initiate an operation. Each chat has its own address, even when several share a device.

This guide covers `windows-pair` 1.0.3, documentation revision 5. It also applies to existing 1.0.3 installations. [Changelog](CHANGELOG.en.md)

## Choose one installation method

Get the release ZIP and its matching `.zip.sha256` checksum file, then choose one of these methods.

- **[Install manually](#installation-and-pairing).** Follow the steps on each Windows PC. To try xdev on one computer first, skip SSH pairing.
- **[Ask an AI assistant to install xdev](AI-DEPLOY.en.md).** Give the two files and the AI deployment guide to an assistant with access to the target computers. It will perform the installation and verification.

## Requirements

| What you want to do | What you need |
| --- | --- |
| Work with local files, commands and mail | Windows x64, 64-bit CPython 3.14 and Windows PowerShell. Install the interpreter in an ASCII-only path such as `C:\Python314\python.exe`. Working directories and device names can contain Chinese characters. The base CLI uses the Python standard library. |
| Transfer files and run commands between devices | A LAN or private network that connects the two devices, an OpenSSH client on the initiating device, and OpenSSH Server with SFTP on the receiving device. For two-way operation, configure both roles on each PC. |
| Call local tools from an AI client | Clients that can run local commands can use the CLI directly. Clients that support local stdio MCP can load six tools. The default installer uses pip to install the pinned third-party Python dependencies bundled in the ZIP and can run offline. |
| Use optional features | Windows background jobs use Task Scheduler and `pythonw.exe`. WSL commands need a distribution registered on the target PC. WSL background jobs also need `python3` and `wslpath` inside that distribution. To use the optional JetStream message broker, supply a NATS server. |

The xdev team's software and scripts are licensed under MPL-2.0. The package root includes the full license in `LICENSE` and the recommended authors and version in `CITATION.cff`. Bundled dependencies retain their own licenses. [Third-party components](AI-DEPLOY.en.md#third-party-components)

## Installation and pairing

### 1. Choose device names and verify SSH

Run `hostname` to see the Windows computer name. xdev uses it by default. Use `--name` to choose a different name or `--alias` to add an alias. Use `local` to address the current device. **The device's primary name is its xdev address. The SSH alias identifies the account and network address to connect to.** For example, you can name a device “Lab” in xdev and connect to it through `lab-ssh` in your local SSH configuration.

On each PC, set `Host`, `HostName` and `User` for the other computer in `~/.ssh/config` and configure public-key login for that account. On the first connection, compare the displayed host-key fingerprint with the one obtained from the other PC. Windows offers OpenSSH as an optional feature. Microsoft documents how to enable the service and configure user keys. [Install OpenSSH](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse) · [Configure key authentication](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement)

On **each PC**, run these tests in PowerShell under the account that will use xdev. Replace `lab-ssh` with the SSH alias for the other PC. The first command should report Python 3.14.x on the other PC. The second should display its SFTP working directory. Both should run without prompting.

```powershell
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes lab-ssh "python --version"
"pwd" | sftp -b - -o BatchMode=yes -o StrictHostKeyChecking=yes lab-ssh
```

### 2. Verify and extract the release

Download `xdev-windows-pair-1.0.3-docs.5.zip` and its matching `.zip.sha256` checksum file from the same release. Run the following in PowerShell from the downloads directory on each PC. If the `py` launcher is unavailable, replace `py -3.14` with `& 'full path to the interpreter'`.

```powershell
$zip = (Resolve-Path '.\xdev-windows-pair-1.0.3-docs.5.zip').Path
$expected = ((Get-Content -LiteralPath ($zip + '.sha256') -TotalCount 1) -split '\s+')[0]
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ine $expected) { throw 'Release archive verification failed' }
$kit = Join-Path (Split-Path $zip) ([IO.Path]::GetFileNameWithoutExtension($zip))
if (Test-Path -LiteralPath $kit) { throw 'Choose a new extraction directory' }
Expand-Archive -LiteralPath $zip -DestinationPath $kit
Set-Location -LiteralPath $kit
py -3.14 -I -B -X utf8 verify.py .
if ($LASTEXITCODE -ne 0) { throw 'Package file verification failed' }
```

### 3. Install under each user's account

This fresh install uses the primary names “Study” and “Lab”. Replace those names and SSH aliases with yours. To keep the Windows name, omit `--name` on that PC and use its Windows name as the value of `--peer` on the other PC.

```powershell
# Run on the study PC
py -3.14 -I -B -X utf8 install.py install --expect-current none --name Study --peer Lab --peer-ssh lab-ssh
# Run on the lab PC
py -3.14 -I -B -X utf8 install.py install --expect-current none --name Lab --peer Study --peer-ssh study-ssh
```

For a local CLI trial, run `py -3.14 -I -B -X utf8 install.py install --expect-current none --cli-only` and select `local` as the target device. If xdev is already installed, follow [Upgrade, rollback and migration](#upgrade-rollback-and-migration). To bring over an existing mailbox, restore its backup before receiving messages.

### 4. Verify both installations

Run the following on each PC with `$peer` set to the other device's primary name. `--version` should report `xdev 1.0.3 (windows-pair)`, the two `who` calls should return the local and remote identities respectively, and `status` should report `active_verified: true`. Use the stable `xdev.cmd` entry point for daily work. Keep the extracted directory for upgrades and maintenance.

```powershell
$xdev = Join-Path $env:LOCALAPPDATA 'CrossDeviceCollab\xdev.cmd'
$peer = 'Lab'   # Use 'Study' on the lab PC
& $xdev --version
& $xdev who
& $xdev who --host $peer
py -3.14 -I -B -X utf8 install.py status
```

## Files and jobs

These examples use `$xdev` and `$peer` from the previous step. Replace `D:\Shared` with an existing directory on the target PC that the account can write to.

| Task | PowerShell example |
| --- | --- |
| List a directory | `& $xdev file ls --host $peer --path 'D:\Shared'` |
| Send a file | `& $xdev file push --host $peer --local '.\input.csv' --remote 'D:\Shared\input.csv' --no-overwrite` |
| Verify contents | Run `& $xdev file stat --host $peer --path 'D:\Shared\input.csv' --with-sha256` and compare the result with `Get-FileHash .\input.csv -Algorithm SHA256` on the source PC. |
| Retrieve a file | `& $xdev file pull --host $peer --remote 'D:\Shared\result.csv' --local '.\result.csv' --no-overwrite` |
| Update a file conditionally | Read the remote digest, then use `file push --base-sha256 original-sha256` to write the edited file back. The write stops if someone else changed the file. |
| Run a command | Use `& $xdev exec --host $peer -- hostname`. If the other PC has Ubuntu installed, try `& $xdev exec --host $peer --wsl Ubuntu -- uname -s`. |
| Start a background job | `& $xdev job start --host $peer -- python --version` returns a job ID. Check its status with `job status --host $peer job-id` and its output with `job log --host $peer job-id`. |

To create a multiline file, save its contents locally as UTF-8, then run `& $xdev file create --host $peer --path 'D:\Shared\notes.txt' --content-file '.\notes.txt'`. Through MCP, you can also pass the full multiline text to `xdev_file` as a single `--content` argument. Add `--help` to a subcommand to see all its options.

## Send mail to a specific chat

A recipient address combines **the device's primary name, the client identifier and the existing chat ID**. For clients with separate configuration directories, the address can also specify the profile. A device mailbox can hold messages for other programs. For an AI to act on a message immediately, address an existing chat that supports receiving it. Run `& $xdev ai --capabilities` to see which delivery modes each receiver supports in this release.

Run this example in a sending chat whose identity has been bound. `opencode` identifies the receiving client. Set `$chat` to the actual chat ID in that client. Give the message a short subject. Add `--wait-reply 60` to the initial message to wait for a reply during the same call.

```powershell
& $xdev msg whoami
$chat = '<target-chat-id>'
& $xdev msg send --to $peer --client opencode --session $chat --subject 'Check export row count' --text 'How many data rows does the exported file contain?'
```

In the **receiving chat**, use the ID from the notification to read the message and reply. The reply follows the return address on the original message.

```powershell
& $xdev msg read '<message-id>'
& $xdev msg reply '<message-id>' --text 'The export contains 12 data rows.'
```

- **Forward and quote.** Use `msg forward message-id --to device --client client --session chat-id --text 'Additional comments'` to include the complete original. The new recipient can use `msg reply` to reply to the forwarder or add `--audience original-author` to send a separate reply to the original author.
- **Send to several chats.** Add recipients to the initial message with `--also device client chat-id`. xdev tracks each recipient's read status and replies separately. Use `--audience all` to reply to everyone in the discussion. Transfer external files referenced in the body separately with `file push`.
- **Check delivery.** In a bound chat, search by subject with `msg inbox --subject 'export'`. A device inbox entry confirms storage. `current_turn_accepted` confirms that the client accepted the message into its current turn. Confirm that the receiving chat processed the message by checking its read receipt and linked reply. If delivery is queued, times out or has an unknown outcome, look up the original message ID.

## Connect AI tools

Assistants that can run local commands can use `xdev.cmd`. In an app that supports local stdio MCP, add a server named `xdev`, enter the values below and reconnect the server.

| Field | Value |
| --- | --- |
| Command | The full path to the Python 3.14 interpreter. Find it with `py -3.14 -c "import sys; print(sys.executable)"`. |
| Arguments | The six arguments, in order, are `-I`, `-B`, `-X`, `utf8`, the **expanded absolute path** to `%LOCALAPPDATA%\CrossDeviceCollab\launch.py` on this PC, and `mcp`. |

Call `xdev_who` from the client. The returned `_mcp_runtime.pinned_release` is the digest of the runtime this connection actually loaded. It should match `active_release`. If `reload_required` is true, reconnect the xdev server. For hosts that support skills, you can also copy `integrations/skills/xdev-local` from the ZIP to the host's skills directory. [Batch configuration and chat receivers](AI-DEPLOY.en.md#connect-clients)

Pass `arguments` to `xdev_msg` as an array of strings, one entry per argument. The receiving chat reads the message with `{"action":"read","arguments":["<message-id>"]}` and replies with `{"action":"reply","arguments":["<message-id>","--text","The export contains 12 data rows."]}`. If the host reports an argument type error, check the format once with `{"action":"whoami","arguments":[]}`. If the host still cannot pass an array, use the CLI in the same chat. Look up any previously submitted operation by its original ID before switching.

### Which clients can notify an existing chat

| Receiver | Requirements and delivery behavior |
| --- | --- |
| `codex` | Run `xdev ai --client codex --register` in the target chat to register it. Supports delivery into the current turn and explicit queuing. |
| `dsh`, `opencode` | DeepSeek Harness needs an active root chat with its receiver plugin loaded. OpenCode needs a host that can locate the chat by its original ID. Both submit to the existing chat by default, with options to queue or send only while idle. A running tool may finish its current step before the notification is processed. |
| `claude` | The limited native receiver targets Windows Desktop's cached engine 2.1.325 and requires an active registration. Verify acceptance, a read receipt and a linked reply from that particular chat. |
| `workbuddy`, `antigravity`, `claude-channel`, `codebuddy-channel` | Enable the appropriate plugin or channel, bind the specific chat and explicitly select queued delivery. A plugin reading the notification, text appearing in the input box and a successful channel write each confirm only that individual step. |

Hosts that only support tool calls can wait for a reply within the sending call. To relay to a web chat, use an assistant authorized to operate that browser. Confirm the site, browser profile and permanent chat URL. Submit the authorized contents and message ID, reopen the same chat, and verify that it displays both the original message and the corresponding answer. The relaying assistant then runs `msg reply message-id` under its own identity and names the source. Automatic receipt and delivery during a busy turn each require support from the host.

## Upgrade, rollback and migration

- **Upgrade or roll back.** In the extracted directory, run `py -3.14 -I -B -X utf8 install.py status` and note `active`. Use `install.py upgrade --expect-current current-digest` to upgrade or `install.py rollback --expect-current current-digest` to roll back, using the same Python command prefix for both. Verify the version on each PC and reconnect xdev in the affected clients. A rollback can also select a locally installed version with `--version` or `--release`.
- **Move a mailbox to a new PC.** Finish in-flight jobs and pause writes to the old mailbox. On the old PC, export it with `py -3.14 -I -B -X utf8 migrate.py export --archive 'D:\Backup\xdev-mail.zip'`. On the new PC, register the original identity key and aliases and pair SSH again. On a **fresh installation that has not yet received mail**, restore the export with `migrate.py restore --archive 'D:\Backup\xdev-mail.zip' --sha256 export-sha256`. Handle file attachments, SSH credentials, AI accounts and existing chat registrations separately. [Migration and compatibility](AI-DEPLOY.en.md#migration-and-compatibility)
