# 在 AI 客户端中使用 xdev

[English](CLIENTS.en.md) | **简体中文**

先在目标 Windows 账户安装 xdev，再选择实际要使用的客户端。验收时打开平常工作的聊天，请它「调用 `xdev_who` 查询本机，并告诉我设备名」。看到工具返回的设备名，就确认了这个聊天能调用 xdev。[Windows 安装手册](README.md#安装与配对)

## 选择接入方式

| 客户端 | 如何接入，以及已验证的能力 |
| --- | --- |
| 支持本地 MCP 的桌面应用、编辑器和 CLI | 从客户端的 MCP 设置添加名为 `xdev` 的本地 stdio 服务，按手册填写 Python 命令和参数。Codex CLI、Antigravity 的独立应用、IDE 和 CLI、Cursor、VS Code、Chatbox、Cherry Studio、GitHub Copilot CLI 和 Kimi Code 均有客户端列出七项工具的实测。Antigravity 三种入口共用配置，可分别打开要用的入口验收。[启动命令和参数](README.md#接入-ai-工具) · [支持配置脚本的客户端](AI-DEPLOY.md#接入客户端) |
| OpenCode、Claude Code、CodeBuddy、Gemini CLI | 按上行接入，使用客户端自带的状态命令查连接。实测入口分别为 `opencode mcp list`、`claude mcp get xdev`、`codebuddy mcp get xdev`、`gemini mcp list`。Gemini CLI 的工作区信任设置会影响工具是否启用。 |
| Kimi Work | 用内置 Plugin Builder 创建个人插件，向它提供上述 Windows stdio 启动命令和参数，再到「插件 → 个人」安装。实测已在宿主内启用，按插件配置调用 `xdev_who` 成功。首次使用仍请在 Kimi 聊天里试调。个人插件由使用者创建。[Kimi 个人插件指南](https://www.kimi.com/help/plugins-and-skills/create) |
| 豆包工作及有本地技能的豆包桌面端 | 将随包的 `integrations/skills/xdev-local` 整个目录复制到当前工作区的 `agent_mode/workspace/.user_skills/`，在技能管理中查找 **Xdev Local**。实测豆包工作已列出该技能，底层通过 Windows 的 `xdev.cmd` 工作。请在支持执行本机命令的聊天中试用。[随包技能](../integrations/skills/xdev-local/SKILL.md) · [豆包技能安装说明](https://docs.volcengine.com/docs/agent-toolkit/force-skill-installation-guide?lang=zh) |

**WSL 中的助手**可通过 Windows 互操作复用同一安装及设备配对。在 Ubuntu 的 Bash 里可先执行下面这一行。Windows 安装包不会在发行版内自动创建 `xdev` 命令。

```bash
powershell.exe -NoProfile -Command '& "$env:LOCALAPPDATA\CrossDeviceCollab\xdev.cmd" who'
```

## 工具显示已连接，聊天还是用不了？

- **升级后仍显示旧工具。** 在客户端中单独重连 xdev，再让聊天调用 `xdev_who`。返回的 `_mcp_runtime.pinned_release` 和 `active_release` 应相同。此版本的七项工具包括 `xdev_surface`。若客户端使用工具允许列表，也检查该项是否被过滤。
- **发起调用时要求登录、选模型或授权。** 按客户端提示完成相应步骤。检查当前项目是否启用了聊天扩展和 MCP。LM Studio 通过其 API 调用已登记的 MCP 插件时，还需要 LM Studio 的访问令牌。
- **工具已加载，但读不到某个窗口控件。** `SURFACE_REMOTE_DISABLED` 表示对端未开放相应窗口权限。`truncated: true` 表示这次快照只包含部分控件。可从目标应用自身的命令或工具页核实状态。[网页与窗口读取](README.md#读取网页与应用窗口)
- **希望来信主动唤醒原聊天。** 在一对指定聊天之间寄一封带主题的测试信，确认收件聊天自己读信，并沿原信回复。需要空闲唤醒时，让收件聊天先空闲；需要忙时插话时，检查消息是否进入当前回合。Codex、DeepSeek Harness、OpenCode 及受支持的 Claude Desktop 引擎有指定聊天往返实测。WorkBuddy 有前台协助往返记录，Gemini 和豆包网页版有浏览器转交往返记录。各入口支持的投递方式见[原聊天接收器](README.md#哪些客户端能接收原聊天通知)。
