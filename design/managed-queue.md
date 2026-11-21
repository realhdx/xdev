# 私有 SQS 备用邮箱 · Private SQS fallback

[中文](#中文) · [English](#english)

## 中文

双方只能向外连接、接收方又可能离线时，可以用自己的 Amazon SQS FIFO 队列短暂保管密文。每台设备一个收件队列。xdev 在发件设备上加密整封信和会话通知，在收件设备上验明发件方并解密。此线路只承载会话邮件和回执。已配对设备能够直连时，直接投递可省去托管方。

1. 管理员在同一区域、同一 AWS 账户内为每台设备建立独立私有 FIFO 队列。设置 `MessageRetentionPeriod` 为 60 至 86400 秒，例如 3600 秒。保留时间应覆盖预计离线时长。禁用死信转移。设备只使用下方 IAM 权限，不需要管理队列。队列名称宜选用无个人信息的标识。
2. 在运行 xdev 的 Python 环境安装 `boto3` 和 `cryptography>=37`。每台设备通过 AWS 标准凭证链取得自己的身份，例如 IAM 角色或已登录的 AWS 配置档。每对设备在线下或已有可信通道交换独立的 32 字节随机密钥，以 64 个十六进制字符保存在仅本机使用账户可读的文件中。
3. 把下方字段合入各自的 xdev 配置。示例是设备 A 的视角。设备 B 对调队列、设备名，并选择 B 上允许 A 联系的应用。之后由宿主定位每封信指定的聊天。首次验收可显式使用 `--transport sqs`。希望指定对端默认走该队列时，再在普通的 `peers` 设备条目中设置 `mail_transport` 为 `sqs`。
4. 两端运行 `xdev msg listen --transport sqs --follow`。用已有聊天寄一封带主题、指定收件人且带 `--delivery-report` 的信。服务返回 `queue_acknowledged` 表示托管队列已接收密文。本机 `xdev msg status <ID>` 查看对端回执。收件聊天用 `msg read <ID>` 取原文，用 `msg reply <ID> --text ...` 实质回信。纯投递回执无需唤醒模型。

接收端持久入箱、处理预定的会话通知并成功发出所请求的回执后，从队列删除原密文。若回执暂时发不出，该信可重新投递；本地记录抑制重复唤醒。若接收端在保存期限内始终离线，信件会过期。发件方可检查原 ID 的状态再决定是否重投。队列自带的保留期限也无法为服务商备份提供可验证的删除证明。

## English

Use an inbound private Amazon SQS FIFO queue per device when both endpoints can make outbound connections and a receiver may go offline. The sender encrypts the complete letter and wake notice locally. The receiver verifies the paired sender before opening the message. This route carries mail and receipts. Direct delivery removes the hosting party when reachable.

1. Create a private FIFO queue per device in one AWS account and region. Set retention to 60–86400 seconds, for example one hour. Size that window for the expected offline interval and leave dead-letter redrive disabled. Use opaque queue names and the device permissions below.
2. Install `boto3` and `cryptography>=37` in the Python environment that runs xdev. Give each device its own role or profile through the standard AWS credential chain. Exchange a separate random 32-byte key for each pair over a trusted channel. Store its 64-character hex encoding in a locally protected file.
3. Merge the example below into device A's configuration. On B, reverse the devices and queue URLs and select B's local applications that A may contact. The host resolves the exact chat named on each letter. Select `--transport sqs` on individual sends, or set `peers.<device>.mail_transport` to `sqs` for that destination.
4. Run `xdev msg listen --transport sqs --follow` on both endpoints. From a bound chat send a letter with a subject, a named recipient and `--delivery-report`. The initial `queue_acknowledged` confirms ciphertext storage at the broker. Inspect `xdev msg status <ID>` for the recipient's report. The recipient reads the letter with `msg read <ID>` and replies with `msg reply <ID> --text ...`. Delivery receipts are silent.

The listener deletes a valid queued letter after local persistence, attempted host delivery when requested, and successful transmission of any requested report. A transient failure returning the report leaves the letter eligible for redelivery. Local records suppress duplicate host calls. Mail expires if a receiver stays offline past the configured retention. Check the original ID before retrying. Provider backup deletion cannot be independently verified by xdev.

### Configuration example

```json
{
  "managed_queue": {
    "region": "us-east-1",
    "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/in-a.fifo",
    "peers": {
      "device-b": {
        "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/in-b.fifo",
        "key_file": "/private/xdev/device-b.key",
        "receiver_apps": ["opencode"]
      }
    }
  }
}
```

`key_file` 填本机绝对路径。私有兼容服务可显式设置 `managed_queue.endpoint_url`，两端所有队列 URL 必须匹配该服务地址，并支持 FIFO、标准权限和相关队列属性。远程服务使用 HTTPS。  
Use an absolute key path on the device. A private compatible service can set `managed_queue.endpoint_url` explicitly. All queue URLs must match that service. It must support FIFO semantics, standard access control and the required queue attributes. Remote endpoints require HTTPS.

### IAM example · Device A

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sqs:GetQueueAttributes",
      "Resource": [
        "arn:aws:sqs:us-east-1:123456789012:in-a.fifo",
        "arn:aws:sqs:us-east-1:123456789012:in-b.fifo"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility"],
      "Resource": "arn:aws:sqs:us-east-1:123456789012:in-a.fifo"
    },
    {
      "Effect": "Allow",
      "Action": "sqs:SendMessage",
      "Resource": "arn:aws:sqs:us-east-1:123456789012:in-b.fifo"
    }
  ]
}
```

B 使用镜像权限。队列采用同账户的私有默认策略即可。示例适用于 SQS 托管的服务器端加密。自选客户管理的 KMS 密钥时还须按 AWS 说明授权。  
Reverse the queue permissions on B. Default private, same-account queue policies suffice. This example works with SQS-managed server-side encryption. Customer-managed KMS keys require the corresponding KMS permissions.

### Privacy and cost · 隐私与费用

队列服务仍可观察账户、设备访问地址、时间、密文长度、同一方向的分组和完全相同封包的重投。对端持有配对密钥，能读到信件。已入箱的端侧副本依本机策略保留。托管服务可能产生请求和相关费用。  
The operator can observe the account, connection addresses, timing, encoded sizes, traffic groups and repeated identical packets. Paired endpoints can read the letter and keep local copies according to their own policies. Hosted requests and related services can incur charges.

[AWS queue attributes](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SetQueueAttributes.html) · [FIFO sends](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SendMessage.html) · [SQS IAM permissions](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-api-permissions-reference.html) · [AWS SDK credentials](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html) · [Amazon SQS pricing](https://aws.amazon.com/sqs/pricing/)
