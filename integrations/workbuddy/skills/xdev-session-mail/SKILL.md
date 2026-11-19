---
name: xdev-session-mail
description: 在 WorkBuddy 收到注明来源和 xdev 信件 ID 的通知时，用本聊天身份查收和关联回信，避免重复寄信或因参数格式反复调用。
---

在用户授权的聊天中处理通知。仅查通知指向的原信。保留短主题和原信 ID。

**工具参数。** xdev_msg 的 `arguments` 是字符串数组。即使只有一个 ID，也传数组。以下 JSON 中的标记应换成真实值。

```json
{"action":"read","arguments":["MESSAGE_ID"]}
```

若宿主提示需要身份，可用本聊天的 `xdev_channel_status` 查当前绑定。确认客户端为 workbuddy、当前聊天 ID 与通知收件人一致后，使用 `["MESSAGE_ID","--as-client","workbuddy","--as-session","SESSION_ID"]`。不得借用另一聊天身份。只需确认收到时，用原信的已读登记或纯回执。

**避免空转。** 如果宿主报告 `expected array of strings`，按上面的形状更正一次。相同错误再次出现时，使用已安装的本机命令入口。保留宿主原有的单次授权机制。以下为 PowerShell，替换原信 ID、当前聊天 ID、答复正文和本次回信的稳定 ID 后执行。

```powershell
$xdev = Join-Path $env:LOCALAPPDATA 'CrossDeviceCollab\xdev.cmd'
& $xdev msg read MESSAGE_ID --as-client workbuddy --as-session SESSION_ID
& $xdev msg reply MESSAGE_ID --text '答复正文' --id REPLY_ID --from-client workbuddy --from-session SESSION_ID
```

新回信沿用原信的返回地址。一次回信选定一个稳定 ID。若上次发送结果未知，先在收件设备按此回信 ID 查询；已送达就结束，仍未知时报现状。切换 MCP、CLI 或界面前同样核对原 ID。分别记录实际入箱、该聊天已读和对应回信。
