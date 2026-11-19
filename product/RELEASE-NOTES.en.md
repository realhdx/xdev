# xdev Windows Pair 1.0.3 (documentation revision 5)

[简体中文](RELEASE-NOTES.md) · **English**

xdev delivers messages and replies between specific AI chats. On paired Windows computers, it also transfers files and runs commands and background jobs. This release adds Chinese and English editions of the user documentation. Existing 1.0.3 installations can use the new guides immediately.

**First installation.** Under Assets, download `xdev-windows-pair-1.0.3-docs.5.zip` and its matching `.sha256` file. Verify the checksum, then extract the ZIP. Use the same ZIP on both computers. Each needs Windows x64 and 64-bit CPython 3.14. Cross-device operations require a reachable SSH/SFTP connection.

| Install it yourself | Have an AI install it |
| --- | --- |
| Follow the [installation and user manual](README.en.md). | Give the [AI deployment guide](AI-DEPLOY.en.md) to an AI with permission to operate the computer. |

Choose either path. The language links in the bundled guides also work offline. [Full changelog](CHANGELOG.en.md)

**Custom installation paths.** If Windows long-path support is disabled, deeply nested installation directories can prevent offline dependencies from installing. Use the default location, or pass `--root` before `install` to choose a short path the current user can write to. Use that same location for maintenance commands and MCP configuration.
