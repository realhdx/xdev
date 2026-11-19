# xdev Windows Pair 1.0.3 (documentation revision 6)

[简体中文](RELEASE-NOTES.md) · **English**

xdev delivers messages and replies between specific AI chats on the same Windows PC or across paired devices. It also transfers files and runs commands and background jobs. This revision updates the project overview and installation instructions. Existing 1.0.3 installations can use the new guides immediately.

- **Install it yourself.** Download `xdev-windows-pair-1.0.3-docs.6.zip` and its matching `.sha256` file from the release assets. Verify the checksum and follow the [installation and user manual](README.en.md). Use the same ZIP on each device.
- **Have an AI install it.** Give this prompt to an assistant with permission to operate the selected devices.

  > Get the latest release of xdev from https://github.com/realhdx/xdev and follow the included AI deployment guide to install and verify it on the Windows devices I specify.

Each device needs Windows x64 and 64-bit CPython 3.14. A single installation supports local use. For cross-device work, pair the devices that need to communicate over SSH/SFTP. [AI deployment guide](AI-DEPLOY.en.md) · [Full changelog](CHANGELOG.en.md)

**Custom installation paths.** If Windows long-path support is disabled, deeply nested installation directories can prevent offline dependencies from installing. Use the default location, or pass `--root` before `install` to choose a short path the current user can write to. Use that same location for maintenance commands and MCP configuration.
