# Changelog

[简体中文](CHANGELOG.md) · **English**

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
