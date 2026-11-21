# Use xdev in an AI client

**English** | [简体中文](CLIENTS.md)

Install xdev under the Windows account that will run it, then connect the client you use. In your usual chat, ask “Call `xdev_who` on the local device and tell me its name.” A tool result with the device name confirms that this chat can call xdev. [Windows installation guide](README.en.md#installation-and-pairing)

## Choose a connection

| Client | How to connect and what has been verified |
| --- | --- |
| Desktop apps, editors and CLIs with local MCP support | Add a local stdio server named `xdev` in the client’s MCP settings. Use the Python command and arguments in the manual. Codex CLI, Antigravity App, IDE and CLI, Cursor, VS Code, Chatbox, Cherry Studio, GitHub Copilot CLI and Kimi Code each listed all seven tools in a client test. The three Antigravity clients share a configuration. Check the one you use. [Command and arguments](README.en.md#connect-ai-tools) · [Clients with a configuration script](AI-DEPLOY.en.md#connect-clients) |
| OpenCode, Claude Code, CodeBuddy and Gemini CLI | Use the same stdio setup. The clients confirmed the connection with `opencode mcp list`, `claude mcp get xdev`, `codebuddy mcp get xdev` and `gemini mcp list`, respectively. Gemini CLI also checks whether the workspace is trusted. |
| Kimi Work | Use the built-in Plugin Builder to create a personal plugin with the Windows stdio command and arguments above. Install it from **Plugins → Personal**. In tests, Kimi Work enabled the plugin, and a direct call using its launch configuration returned a result from `xdev_who`. Try that call in a Kimi chat as well. Each user creates their own plugin. [Kimi personal plugin guide](https://www.kimi.com/help/plugins-and-skills/create) |
| Doubao Work and Doubao desktop clients with local skills | Copy the bundled `integrations/skills/xdev-local` directory into `agent_mode/workspace/.user_skills/` in the active workspace. Look for **Xdev Local** in the skill manager. Doubao Work listed the skill in testing. It uses the Windows `xdev.cmd` launcher, so try it in a chat that can run local commands. [Bundled skill](../integrations/skills/xdev-local/SKILL.md) · [Doubao skill installation guide](https://docs.volcengine.com/docs/agent-toolkit/force-skill-installation-guide?lang=zh) |

**Assistants in WSL** can reuse the Windows installation and its device pairing through Windows interoperability. Try the following in an Ubuntu Bash shell. The Windows package leaves installation of an `xdev` command inside the distribution to the user.

```bash
powershell.exe -NoProfile -Command '& "$env:LOCALAPPDATA\CrossDeviceCollab\xdev.cmd" who'
```

## The client shows a connection, but the chat cannot use it

- **The old tools still appear after an upgrade.** Reconnect the xdev server in the client, then ask the chat to call `xdev_who`. Its `_mcp_runtime.pinned_release` and `active_release` should match. This version’s seven tools include `xdev_surface`. If the client has a tool allowlist, check whether it includes that entry.
- **The client asks for sign-in, a model or permission.** Complete the step in that client. Check whether the chat extension and MCP are enabled for the current project. LM Studio also requires its own access token when calling a configured MCP plugin through its API.
- **A window control is missing.** `SURFACE_REMOTE_DISABLED` means the receiving device has not granted that level of window access. A snapshot marked `truncated: true` contains only part of the control tree. The app’s own command or tools page may report the state you need. [Read pages and windows](README.en.md#read-web-pages-and-application-windows)
- **You want incoming mail to wake the original chat.** Send a test letter with a short subject between two named chats. Confirm that the recipient chat reads the letter and replies to the original. To test idle wake-up, let the recipient go idle first. To test delivery during a turn, check whether the message enters the current turn. Tests have confirmed addressed round trips in Codex, DeepSeek Harness, OpenCode and a supported Claude Desktop engine. WorkBuddy completed a round trip with help in the foreground. The Gemini and Doubao web apps did so through browser handoff. [Delivery modes by client](README.en.md#which-clients-can-notify-an-existing-chat)
