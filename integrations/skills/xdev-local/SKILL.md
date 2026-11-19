---
name: xdev-local
description: 在已安装 xdev 的 Windows 上，向用户选定的已配对设备传文件、运行任务，或给指定的既有 AI 聊天寄信、收信和回信。
---

当前客户端已加载 xdev MCP 时，直接使用其文件、命令、任务或消息工具。能运行本机命令的宿主也可从 PowerShell 使用同一安装：

```powershell
$xdev = Join-Path $env:LOCALAPPDATA 'CrossDeviceCollab\xdev.cmd'
& $xdev who
& $xdev ai --capabilities
```

按用户选定的 Windows 机器名、xdev 主名或已登记的别名寻址。用 `file push` / `pull` 传文件；多行文本用 `file create --content-file`，需要验证内容时用 `file stat --with-sha256`。命令参数以本机 `--help` 为准。

给 AI 寄信用设备名、客户端和既有聊天 ID 指定收件人，用 `msg whoami` 核对本聊天的回信地址。首信带短主题；用 `msg read ID` 登记已读，实质答复用 `msg reply ID --text ...` 自动关联原信。发送方已在工具里等待答复时，使用原信 ID 继续查收。设备入箱、排队、当前回合接纳、原聊天已读和关联回信应按实际观察分别报告。结果未知时先按原 ID 查账。
