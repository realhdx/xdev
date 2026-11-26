# xdev Linux Mail

**English** · [简体中文](README.md)

Linux Mail preview `1.1.1-preview.1` runs on Linux with its own Python interpreter and uses the public core and letter format from Windows Pair 1.1.1. It can send, read and reply to mail locally and on paired devices. Mailbox state and pairing live outside the application directory and carry over between versions.

## Install

Use Python 3 (tested with 3.12) under your own Linux account. The local mailbox uses the standard library. Place the archive and its matching checksum file in an empty directory and run:

```sh
sha256sum -c xdev-linux-mail-1.1.1-preview.1.tar.gz.sha256
tar -xzf xdev-linux-mail-1.1.1-preview.1.tar.gz
cd xdev-linux-mail-1.1.1-preview.1
./xdev --version
./xdev who
```

Keep the extracted directory wherever you install user applications. Run its `xdev` by absolute path from other directories. The default state directory is `${XDG_STATE_HOME:-$HOME/.local/state}/xdev`. For a first test, `export XDEV_HOME="$(mktemp -d)"` selects a fresh private mailbox. For everyday use, keep the same state directory across processes and extract each new version to a separate application directory.

## Try a letter and a reply

Run these commands from an ordinary terminal as a device program. The first letter keeps a return address with a zero-second wait. Each command starts a fresh process.

```sh
export XDEV_HOME="$(mktemp -d)"
./xdev msg send --to local --as-device --id hello --subject 'Check the export' --text 'Please check the row count.' --wait-reply 0
./xdev msg read hello
./xdev msg reply hello --id answer --text 'Confirmed: 12 rows.'
./xdev msg read answer
```

Check that the last letter has `reply_to` set to `hello`. Reaching an AI chat also requires a receiver supported by that app. Verify a read and a linked reply inside the intended chat separately.

## Check delivery by the original letter ID

From the sending chat, run `./xdev msg status <original-letter-id>`. The result combines the mailbox receipt, host admission, a read by the addressed chat and a linked reply. Check `delivery.state` and `delivery.retry`. For example, when another route delivers the letter while the native receiver is unavailable, the state is `pending_wake`. It becomes `read` when the addressed chat reads it and `replied` when a reply arrives on the original letter. Device-wide reads are recorded separately.

With `check_same_id`, query the original ID again. With `never`, no resend is needed. Retry using that same ID only when xdev confirms the letter was never submitted and returns `retry_same_id`. A peer with limited reporting exposes only the delivery stages it can confirm.

## Pair with Windows or another Linux device

Use an existing SSH connection with verified peer identities for direct mail. Configure each direction separately and give each peer a mail-only SSH entry on Linux. Follow the [core 1.1.1 Linux pairing guide](https://github.com/realhdx/xdev/blob/v1.1.1/design/session-relay.md#linux-ssh-mailbox). Replace the final two lines of its receiver script with `exec /absolute/install/path/xdev _rpc_mail alpha`, replacing `alpha` with the paired sender’s actual device name. Keep the script and runtime writable only by trusted users. Send a letter from each side and check the reads and replies by their original IDs.

This preview exposes `who`, `msg` and the peer-bound SSH mailbox receiver. Cross-network relays, AI clients and idle wake-ups each have their own integration requirements.

The core comes from [public xdev 1.1.1](https://github.com/realhdx/xdev/tree/023b50882d464a2cbf6e74b6b6b83577ab2120b9). The distribution changes the core's series and preview version and adds the Linux launcher. A copy of the license and citation information is included at the top level of the archive. [LICENSE](https://github.com/realhdx/xdev/blob/v1.1.1/LICENSE) · [CITATION.cff](https://github.com/realhdx/xdev/blob/v1.1.1/CITATION.cff)
