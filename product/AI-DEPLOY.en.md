# Install xdev with an AI assistant

**English** | [简体中文](AI-DEPLOY.md)

This guide covers xdev Windows Pair 1.0.3. The assistant needs to be able to run PowerShell under the designated user account on each target Windows PC. Use the release ZIP and SHA-256 file from the same release. The archive for this documentation revision is `xdev-windows-pair-1.0.3-docs.5.zip`. [Manual installation and everyday use](README.en.md)

## A task to give your assistant

> Deploy xdev on the two Windows PCs I selected. Use their Windows computer names as addresses by default and retain any aliases I already chose. First verify the direction of each private-network SSH connection, the login accounts and the host-key fingerprints. Choose a fresh install or an upgrade based on the state of each PC. Connect only the AI clients I selected. Verify file transfers and command execution in both directions. If I need chat messaging, send a test message with a subject in each direction between the two specified chats. Verify that each original chat marked the message as read and returned a linked reply. Tell me where to run xdev on each PC, which features you verified and what still needs attention.

## Install or upgrade

1. On each target PC, check `hostname`, the target account's `%LOCALAPPDATA%` and the path to 64-bit CPython 3.14. Record the local primary name, the peer's primary name and the peer's SSH alias. From each initiating account, run `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes peer-ssh-alias "python --version"` against the other PC, then run the SFTP `pwd` command in batch mode. Verify the host-key fingerprint before the first connection. Resolve the Python path and the current user's directory on each PC separately. [Pairing and extraction](README.en.md#installation-and-pairing)
2. Compute the SHA-256 digest of the whole ZIP before opening it. Compare it with the verified release checksum, then extract the ZIP to a new directory. Run all `install.py`, `migrate.py` and `verify.py` commands below from the extracted root using `py -3.14 -I -B -X utf8`. Run `verify.py .` first. If it succeeds, inspect the current state with `install.py status`.
3. **For a fresh installation**, run `install.py install --expect-current none --peer peer-name --peer-ssh peer-ssh-alias`. Add `--name local-name` to choose a custom local name. Swap the local and peer settings on the other PC. **For an upgrade**, run `install.py upgrade --expect-current current-runtime-digest` with the digest from that PC's own `status`. The default installation enables MCP using the bundled offline dependencies. Add `--cli-only` if you only need the base commands.
4. On each PC, call `%LOCALAPPDATA%\CrossDeviceCollab\xdev.cmd` with `--version`, `who` and `who --host peer-name`. In both directions, verify the Windows name, the local primary name, the peer identity key and the `active_verified` field returned by `install.py status`. For a single-device rehearsal, put `--root new-empty-directory` before the installer subcommand.

Run everyday commands under the user's account. The examples use `$xdev`, set with `$xdev = Join-Path $env:LOCALAPPDATA 'CrossDeviceCollab\xdev.cmd'`.

## Connect clients

**Configure tool calls.** In an app that supports local stdio MCP, register the [launch command from the manual](README.en.md#connect-ai-tools). The bundled script can update existing configurations for `vscode`, `copilot`, `cursor`, `codebuddy`, `gemini`, `kimi-code`, `lmstudio`, `antigravity`, `dsh` and `workbuddy`. Select the settings file the client actually uses. For example, VS Code's editor settings and the standalone Copilot settings require separate registrations. Hosts that allow comments in their settings files can also be configured through their own settings interface.

The following example uses an existing VS Code configuration. The first command reports the target path and its current SHA-256 digest. Use `apply` only after confirming the path, profile and intended change. Save the `receipt_path` it returns. If restoration reports a conflict, check what the user has changed since.

```powershell
py -3.14 -B deploy/clients.py inspect --product vscode
py -3.14 -B deploy/clients.py apply --product vscode --expect-sha256 '<sha256-from-inspect>'
# To undo this change
py -3.14 -B deploy/clients.py restore --receipt '<receipt_path>'
```

Reconnect the xdev server in the target client. List its tools and call `xdev_who` from that client. Check the device and compare `_mcp_runtime.pinned_release` with `active_release`. If the client shows that the server is stopped, start xdev in the server list. Run `deploy/clients.py verify --product product-id` to test the registered launch command independently. Verify inside the host that the open chat has loaded the configuration.

**Enable receipt in a specific chat.** Run `& $xdev ai --capabilities` on the device and follow the client instructions to bind the intended existing chat. For Codex, run `& $xdev ai --client codex --register` in the target chat. For DeepSeek Harness, first run `py -3.14 -B deploy/dsh_roots.py` to inspect the target root chat. After confirming it, configure it with `--apply` and use `--refresh-mcp` if MCP needs a reconnect. Use the original session ID for OpenCode. For other adapters, choose a supported delivery mode, such as immediate delivery or `--queue`. [Chat receiver list](README.en.md#which-clients-can-notify-an-existing-chat)

## Verify and hand over

| Check | How to verify it |
| --- | --- |
| Two-way transfer and execution | Send a small file with a unique name to an existing writable directory and compare the SHA-256 on both PCs. Repeat in the other direction. Run `& $xdev exec --host peer-name -- hostname` on each PC and check that it executed on the intended peer. |
| AI tool loading | Have each selected client call `xdev_who`. Record the device and loaded runtime. If a login prompt, quota limit or disabled plugin gets in the way, record the last step you verified. |
| Chat messaging | In each original chat, run `msg whoami`. From A, send B a test message with a short subject and a unique `--id`. In B, use `msg read message-id` to read a test word included only in the body, then reply with `msg reply message-id --text reply-text`. In A, read back the reply with the matching `reply_to`. Repeat with B as the sender. |
| Delivery during a busy turn | If the workflow requires immediate acceptance, test while the target chat is busy. Record device inbox storage, queued or current-turn acceptance, a read receipt from the original chat and its linked reply. A reply from an idle chat leaves busy-turn behavior untested. |

**Sender identity and return address.** A host that can identify the current chat supplies its sender identity. If `msg whoami` cannot identify it, verify that this chat is bound to the correct identity. On the first message, use `--from-client` and `--from-session` to specify the sender. Use `--return-client` and `--return-session` to identify a chat that can receive replies, or `--wait-reply 60` to wait within the tool call. When reading requires an explicit identity, use `--as-client` and `--as-session`. When replying, use `--from-client` and `--from-session`. For chats with separate configuration directories, also specify the corresponding profile. A receipt-only message links to the original without triggering a new model turn.

**Handle timeouts and format errors.** In the original sending chat, use `msg wait --reply-to message-id --substantive-only --timeout 0` to look for a substantive reply. To check whether the receiving device stored the message, use `msg wait --host peer-name --device-inbox --id message-id --timeout 0`. Keep the original ID when the submission outcome is unknown. For an important reply, you can also assign its own `--id` so that you can check for duplicates. For `xdev_msg`, pass `arguments` as a string array, for example `{"action":"read","arguments":["<message-id>"]}`. If the host reports an array type error, test once with `{"action":"whoami","arguments":[]}` before deciding whether to use the CLI from the same chat.

**Relay through a browser.** Use the browser profile and existing chat the user selected. Relay only content authorized for that site. After sending, reopen the chat at its permanent URL. Find the user message containing the original message ID and the corresponding model answer that follows it. Record drafts, temporary URLs and queued messages separately. The relaying assistant runs `msg reply message-id` from its own address and identifies the source in its reply. Test automatic receipt and delivery during a busy turn separately before claiming either capability.

For each device or chat, list the available features, the evidence for each and any outstanding conditions. Give the user the everyday command entry point, the actual device names, versions and any original message IDs they will need. The deployer keeps the checksums, user settings and chat test results.

## Migration and compatibility

- **Move to a new PC.** On the old device, save the primary names, identity keys and aliases for the local device and all its peers from `install.py status`. Wait for in-flight jobs to finish and pause mailbox writes. Run `migrate.py export --archive backup-path` and save the receipt and SHA-256. On the new PC, install using `--identity-key original-identity-key` to retain the local identity. Use `--alias` to register the old Windows name and historical aliases. On the peers, update the primary name, SSH target and host-key fingerprint as appropriate. Restore into the empty mailbox of the fresh installation with `migrate.py restore --archive backup-path --sha256 export-sha256`. After restoring, find past messages by subject, verify their receipts or group delivery records, and bind the target chats again.
- **Restore under a new device name.** For a fresh install, use `--name new-name --alias old-name --identity-key original-identity-key`. For any peers that also changed names, set `--peer-identity-key` and `--peer-alias` from the export records. When restoring, add `--source-device old-name`. For an older mailbox stored under a role name without device settings, use `--device original-device-role` at export time. The snapshot includes mail, receipts, group delivery records and forwarded originals. Transfer external files referenced in message bodies separately.
- **Keep legacy role addresses.** When upgrading from a role-addressed release with legacy clients still connected, keep each PC's former role and identity key. Add `--keep-legacy-clients --device old-local-role --identity-key old-local-role --peer peer-name --peer-alias old-peer-role --peer-identity-key old-peer-role --peer-ssh peer-ssh-alias` to the upgrade command. On both PCs, verify delivery of original messages and the return path for replies.
- **Handle changes in an existing dependency directory.** The default upgrade checks previously installed dependencies. If it reports `The existing deps directory has been changed`, compare the dependency digest in `release.json` with the verified release package. Use the original `deps/.xdev-deps.json` to check managed files, their contents and resolved paths, and identify who owns any additional files. If the managed files are intact, the extra files are confirmed to belong to another application, and the old MCP dependencies match the new release, you may add `--cli-only` to the original upgrade options to preserve the shared directory. Reconnect and verify in the host. Resolve discrepancies in content or provenance before proceeding.
- **Roll back.** Use `install.py rollback --expect-current current-digest` to restore the previous installed runtime. Reconnect and verify the version in the client. To restore client settings, use the separate change receipt generated by that `apply`.

## Third-party components

xdev implements device pairing, file and job operations, chat addressing, message delivery and receipts. It also provides the CLI, MCP tools and client adapters. The base commands use the Python standard library. Cross-device connections use the system's OpenSSH and SFTP. The release bundles offline copies of the Python dependencies used by the default installation.

### Offline dependencies

xdev's own source uses MPL-2.0. The package root includes the full license in `LICENSE` and the recommended citation in `CITATION.cff`. The 31 upstream Python wheels supplied with Windows Pair 1.0.3 retain their respective licenses. Extract `assets/optional-deps.zip` to find `wheelhouse/`, the per-file SHA-256 manifest and `requirements.lock.txt`. Each wheel is also a ZIP. Inside, `*.dist-info/METADATA` records the version and release details. `*.dist-info/licenses/` and, for some packages, individual source directories contain upstream licenses and notices.

| License declared upstream | Package and version |
| --- | --- |
| MIT | annotated-types 0.8.0, anyio 4.16.0, attrs 26.1.0, h11 0.16.0, httpx-sse 0.4.3, jsonschema 4.26.0, jsonschema-specifications 2025.9.1, mcp 1.27.1, pydantic 2.14.3, pydantic_core 2.50.4, pydantic-settings 2.16.3, PyJWT 2.15.1, referencing 0.37.0, rpds-py 2026.9.1, typing-inspection 0.4.5 |
| BSD-3-Clause | click 8.5.1, httpcore 1.0.9, httpx 0.28.1, idna 3.20, pycparser 3.12, python-dotenv 1.2.4, sse-starlette 3.7.0, starlette 1.10.0, uvicorn 0.54.0 |
| Apache-2.0 | nats-py 2.14.0, python-multipart 0.0.34 |
| Apache-2.0 or BSD-3-Clause | cryptography 50.0.2 |
| MIT-0 | cffi 2.1.1 |
| MPL-2.0 | certifi 2026.10.23 |
| PSF-2.0 | typing_extensions 4.16.0 |
| PSF, with separate licenses for some components | pywin32 312 |

The Model Context Protocol (MCP) Python SDK provides the local tool interface. The NATS Python client provides optional JetStream message transport. Both are distributed under their respective project licenses. [MCP Python SDK](https://pypi.org/project/mcp/1.27.1/) · [NATS Python client](https://pypi.org/project/nats-py/2.14.0/)

### License texts and corresponding source

- **nats-py and OpenSSL.** The nats-py Python source files retain the copyright notice of The NATS Authors. Its 2.14.0 wheel declares Apache-2.0. The complete text of the same license is available offline at `python_multipart-0.0.34.dist-info/licenses/LICENSE.txt` inside the python-multipart wheel. The cryptography Windows wheel statically links OpenSSL 4.0.3. The two cryptography license options and OpenSSL's Apache-2.0 license apply to their respective components. [nats-py 2.14.0 license](https://github.com/nats-io/nats.py/blob/v2.14.0/LICENSE) · [OpenSSL 4.0.3 license](https://github.com/openssl/openssl/blob/openssl-4.0.3/LICENSE.txt) · [OpenSSL contributors](https://github.com/openssl/openssl/blob/openssl-4.0.3/AUTHORS.md)
- **certifi.** The wheel's `certifi/cacert.pem` contains Mozilla's root certificate data. `*.dist-info/licenses/LICENSE` identifies its source and includes the MPL-2.0 notice. The complete Python and certificate bundle sources for this version are available to download. [certifi 2026.10.23 corresponding source](https://files.pythonhosted.org/packages/fd/11/b524cbd9bbdaca58608e4cf4053fd463417549e1959fa8d3aa18319f9555/certifi-2026.10.23.tar.gz) · [MPL-2.0](https://www.mozilla.org/MPL/2.0/)
- **pywin32.** Its package metadata declares PSF. The wheel includes `adodbapi/` as Python source under LGPL-2.1-or-later, with the full license in `adodbapi/license.txt`. Find the MAPI Stub Library's MIT notice in `win32comext/mapi/NOTICE.md` and in `MAPIStubLibrary-License.txt` in the same directory. Scintilla, PythonWin, COM and other components retain their bundled notices. [pywin32 build 312 corresponding source](https://github.com/mhammond/pywin32/tree/b312)
- **cffi.** cffi declares MIT-0. Its Windows x64 build also uses libffi code from `src/c/libffi_x86_x64/` in the upstream source package. The notice for that code follows. [cffi 2.1.1 corresponding source](https://files.pythonhosted.org/packages/9e/ef/008a1939e372c06329a3fce4279c02f328488f3526744906eeec3da7ad5f/cffi-2.1.1.tar.gz)

<details>
<summary>libffi x86/x64 copyright and license notice</summary>

```text
ffi.c - Copyright (c) 1996, 1998, 1999, 2001  Red Hat, Inc.
Copyright (c) 2002  Ranjit Mathew
Copyright (c) 2002  Bo Thorsen
Copyright (c) 2002  Roger Sayle

libffi 2.00-beta - Copyright (c) 1996-2003  Red Hat, Inc.

ffi_common.h - Copyright (c) 1996  Red Hat, Inc.

ffitarget.h - Copyright (c) 1996-2003  Red Hat, Inc.

prep_cif.c - Copyright (c) 1996, 1998  Red Hat, Inc.

types.c - Copyright (c) 1996, 1998  Red Hat, Inc.

win32.S - Copyright (c) 1996, 1998, 2001, 2002  Red Hat, Inc.
Copyright (c) 2001  John Beniton
Copyright (c) 2002  Ranjit Mathew

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
``Software''), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED ``AS IS'', WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL CYGNUS SOLUTIONS BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.
```

</details>
