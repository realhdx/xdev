# 在原应用内接收消息 · Resident host integration

宿主插件可以把一封信的通知直接交给运行中的原聊天。收、发双方采用本机回环连接和双向 TLS 认证。常驻端读取自己应用的会话表，一套接入可服务多个聊天。邮件正文、关联关系和阅读记录继续由 xdev 邮箱保存。

A cooperating host can receive notices in its own running process. The client and server authenticate each other on the IPv4 loopback address. The host resolves a chat using its own live session table. One listener can serve many chats. The durable inbox retains the message body, reply links and read receipts.

## 接入 · Setup

1. 在宿主可导入 xdev 的 Python 环境准备一个新的本地私有目录。在包含 `src/` 的运行时目录执行 `python -m src.adapters.resident /absolute/path/to/new-identity`。首次生成身份需要 `cryptography`。宿主运行、回环传输依赖 Python 标准库的 TLS 支持。Windows 安装包的 `deps` 内含配套依赖。每个应用配置使用独立身份，证书有效期一年。更换时使用新目录并同时更新两端。
2. 在宿主插件启动、原会话服务已就绪时运行下面的监听器。示例中的 `chat_service` 代表由宿主提供的对象；在有线程要求的应用中，把操作交给宿主自己的事件循环。请求已经提交但接纳结果不确定时，返回未知或抛出异常。
3. 在本机 xdev 配置的 `session_hosts` 中选用此应用。用户可指定本机可用端口。下例的端口和路径仅为示例。之后来信只需提供设备、应用和原聊天 ID。

Run the provisioning command above once per app profile using an interpreter with `cryptography`. Start the listener inside the application and configure its fixed local port and identity directory in xdev. The normal send path only uses the standard library. Rotate the one-year credentials by provisioning a new directory and switching both sides together.

```python
from src.adapters.resident import listen

def receive(session_id, notice):
    chat = chat_service.original_by_id(session_id)
    if chat is None:
        return None
    # Adapt this call to the host. Preserve the host's evidence of actual placement.
    result = chat.accept_mail_notice(notice["message_id"], notice["notice_id"], notice["mode"])
    return {"session": chat.id, "placement": result.placement}

with listen(identity_directory, "my-editor", receive, port=17943):
    run_application()
```

```json
{
  "session_hosts": {
    "my-editor": {
      "resident": {
        "port": 17943,
        "identity_directory": "/absolute/path/to/new-identity"
      }
    }
  }
}
```

`current_turn` 表示宿主接纳到当前轮，`next_turn` 表示进入下一轮，`original_session` 表示原聊天已接纳但宿主无法确认轮次。接纳后在该聊天调用 `msg read` 与 `msg reply` 才产生已读和关联回信。通知带原信 ID、主题和发件地址，供宿主显示。程序处理未知提交结果时会保留原 ID，防止再次启动模型。

Return the actual chat ID together with `current_turn`, `next_turn` or `original_session` based on the host's own acknowledgement. Returning `None` means the host did not submit anything. A timeout or an unverifiable reply leaves the original notice pending inspection. The cooperating chat can read and reply using the letter ID. Discovery of hosts that publish a tool catalog is described in the [host discovery guide](host-discovery.md).
