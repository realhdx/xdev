# 发现已有聊天 · Discover existing chats

[中文](#中文) · [English](#english)

## 中文

`xdev ai --capabilities` 查看已知接入方式。`xdev ai --discover` 读取本机已授权服务公布的能力目录。`xdev ai --discover --client studio --session room-42` 向声明支持该客户端的服务核对一个原聊天。查询不会向聊天发信。真正投递时还要核对宿主返回的接纳位置，原聊天已读和关联回信仍分别记录。

### 接入已有 MCP 服务

在本机 `config.json` 的 `session_host_catalogs` 下登记服务的固定可执行路径和启动参数、预期的 MCP 实现名称、客户端地址。在运行 xdev 的 Python 环境安装可选依赖 `mcp>=1.26,<2`。冷启动时需等待本地服务响应。xdev 与本机受托服务完成 `initialize` 和 `tools/list`，校验工具输入结构。仅有工具名、描述或 `readOnlyHint` 无法证明工具会向指定原聊天插话。

- 服务已有独立的只读查找和投递 API 时，可从服务自己的文档建立下方的本地字段映射。供应方无需更名或修改工具。
- 服务已经发布了 `_meta["io.xdev/receiver"]` 语义时，可以对这一已授权服务使用 `"accept_declared_roles": true`，省去 `roles`。未选择该项又未提供本地映射时，xdev 只报告目录和未匹配的原因。
- 查找工具必须声明 `readOnlyHint: true`，返回精确聊天 ID、是否确实为原聊天、宿主实例标识。投递工具须确认同一聊天、同一通知 ID、已接纳和位置。当前模板支持顶层字符串入参和顶层结果字段，多模式接口还要映射 `mode`。请求每次发送一条含主题和原信 ID 的短通知。正文仍从 xdev 邮箱读取。

下例中的字段均为该示例服务的公开接口。将程序路径和服务名替换为自己的本机服务。

```json
{
  "session_host_catalogs": {
    "studio-tools": {
      "type": "mcp-stdio",
      "argv": ["/absolute/path/to/studio-mcp"],
      "server_name": "Studio Desk",
      "client": "studio",
      "placements": ["current_turn"],
      "roles": {
        "resolve": {
          "tool": "find_conversation",
          "inputs": {"session": "chat_id"},
          "outputs": {"session": "chat_id", "original": "is_original", "instance": "host_instance"}
        },
        "submit": {
          "tool": "deliver_notice",
          "inputs": {"session": "chat_id", "text": "prompt", "notice_id": "request_key"},
          "outputs": {"session": "chat_id", "notice_id": "request_key", "accepted": "ok", "placement": "where"}
        }
      }
    }
  }
}
```

### 结果和优先级

| 结果 | 如何理解 |
| --- | --- |
| `configured_binding` / `native_adapter_declared` | 找到了配置或内置契约。支持现场查询的内置适配器会返回实测状态。 |
| `resolved` | 只有一个服务通过只读接口确认这个原聊天。 |
| `unresolved` | 没有找到受支持且匹配的原聊天。每个目录会给出结果，可能只有读取能力、缺少角色映射、没有这个 ID。 |
| `ambiguous` / `incomplete` | 多个服务声称拥有它，或有相关目录无法核验。先核实归属。 |
| `HOST_PLACEMENT_UNAVAILABLE` | 例如宿主只支持排队，而本次要求立即插话。允许下一轮再处理时，显式使用 `--queue`。 |
| `unknown` | 通知可能已经提交，回执未通过核对。原 ID 的账本阻止自动再投。 |

内置客户端继续走自己的路径。第三方客户端先用本机选定的精确单聊绑定，其次用应用级常驻接收器；仅在连接前确定未送出时才降级到已配置命令或已授权目录。目录从不提供可执行路径。服务离线、原聊天没被选中、会话只读或宿主只支持排队，分别按实际证据呈现。

## English

Use `xdev ai --capabilities` for static contracts, `xdev ai --discover` to inspect approved local providers, and `xdev ai --discover --client studio --session room-42` for a read-only exact-chat check. Catalog access sends no wake notice. After actual delivery, host placement, an original-chat read and a correlated reply provide separate evidence.

**Existing MCP servers.** Configure a fixed executable, expected implementation name and client in the local settings as shown above. Install `mcp>=1.26,<2` in the Python environment running xdev. A cold local service may take a few seconds to start. The official client negotiates `initialize` and `tools/list`. A local mapping identifies the resolve/submit roles and maps native input and output names. Alternatively, explicitly allow the host's published semantic metadata with `accept_declared_roles: true`. Without either form of consent, discovery yields catalog information only. Tool names and descriptions alone cannot establish delivery semantics.

The resolver must be read-only and echo the exact session with a positive original-chat check and a host instance identifier. This template accepts top-level string inputs and top-level receipt fields. Dispatch verifies the instance and mapped schemas again. A successful native receipt must echo the exact session and notice ID, confirm acceptance, and report `current_turn`, `next_turn` or `original_session`. Queue-only providers require explicit queuing. A missing or mismatched receipt locks the original notice in an unknown state for reconciliation.

**Provider-published MCP mapping.** On the resolve tool, use `_meta["io.xdev/receiver"]` with `version: 1`, `client`, `role: "resolve"`, and the `inputs` and `outputs` maps illustrated above. On the delivery tool set `role: "submit"` and include its `placements`. Set `annotations.readOnlyHint: true` only on the resolver. Each role must be unique for that client. A compatible host can choose its own native tool names.

**Native lightweight catalogs.** An approved command can also implement `xdev.host-catalog.v1` without the MCP SDK. Set `session_host_catalogs.<provider>.argv` locally; optionally restrict it to a list of `clients`. Each invocation reads/writes one JSON object on standard input/output. Replies echo `protocol`, `provider`, `op`, `request_id` and any supplied `instance`, `client`, `session`. The three operations are:

| Operation | Required reply |
| --- | --- |
| `catalog` | `status: "ok"`, a stable `instance` for this running host and `clients: [{"client":"studio","template":"xdev.receiver.v1","placements":["current_turn"]}]` |
| `resolve` | `status:"ready", original:true` only for an exact original chat owned by this instance. Return `not_found` or `inactive` when appropriate. |
| `submit` | The request includes the selected template and short `notice`. Reply with `ack` containing the standard receiver receipt. Deduplicate on `notice.notice_id`. |

For example, this local provider declaration authorizes exactly one helper and limits which client identities it can represent.

```json
{"session_host_catalogs":{"editor":{"argv":["/absolute/path/to/editor-mail"],"clients":["studio"]}}}
```

The provider and executable are trusted by the local operator. A supplier can make inaccurate declarations. xdev verifies identity fields and placement, and an independently observed read or reply confirms the chat handled the message. Catalog discovery does not enumerate system ports, read another process or execute paths supplied by a catalog. [Official MCP tool protocol](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) · [Standard receiver envelope](session-relay.md)
