# ChatLab 微信文件约定 v1

这是知域接受的**离线微信聊天文件约定**，用于让用户已经合法获得的导出结果接入统一知识库。它不是聊天数据库读取器、密钥提取器或登录模拟器；知域只接收一个已经生成的 JSON 文件。

## 最小结构

```json
{
  "chatlab": {
    "version": "1",
    "generator": "YourExporter"
  },
  "meta": {
    "platform": "wechat",
    "name": "示例会话",
    "type": "private",
    "ownerId": "wxid_owner"
  },
  "messages": [
    {
      "sender": "wxid_contact",
      "accountName": "联系人显示名",
      "timestamp": 1738713600,
      "type": 0,
      "content": "示例文本",
      "platformMessageId": "stable-message-id"
    }
  ]
}
```

知域当前检查的最低条件是：`chatlab`、`meta` 和 `messages` 必须存在；`chatlab.generator` 非空；`meta.platform` 必须为 `wechat`。其余字段可逐步增加，但导出器应尽可能使用下面的稳定字段，才能获得可靠的归属、排序和增量去重。

## 字段约定

| 位置 | 字段 | 建议 | 用途 |
| --- | --- | --- | --- |
| `chatlab` | `version` | 推荐字符串版本号 | 让导出器以后可演进格式；当前兼容 v1 和 WeFlow 的历史版本。 |
| `chatlab` | `generator` | 必填、非空 | 仅记录导出器标识，方便排查格式兼容性。 |
| `meta` | `platform` | 必填，固定 `wechat` | 防止把其他平台文件误导入微信会话区。 |
| `meta` | `name` | 推荐 | 会话显示名。 |
| `meta` | `type` | 推荐 `private` 或 `group` | 会话类型。 |
| `meta` | `ownerId` | 推荐 | 本人平台 ID，用于判断消息是否由本人发送。 |
| `messages[]` | `sender` | 强烈建议 | 发送者平台 ID。兼容 `senderUsername`。 |
| `messages[]` | `timestamp` | 强烈建议，Unix 秒或毫秒 | 时间线排序。兼容 `createTime`。 |
| `messages[]` | `content` | 推荐 | 文本内容。兼容 `parsedContent`、`rawContent`；没有文字时会保留为空消息记录。 |
| `messages[]` | `type` | 推荐 | 消息类型。兼容 `localType`。 |
| `messages[]` | `platformMessageId` | 强烈建议 | 稳定消息 ID；兼容 `serverId`，用于跨次导入更可靠地去重。 |
| `messages[]` | `localId` / `dedupKey` | 可选 | 没有平台消息 ID 时的辅助去重依据。 |
| `messages[]` | `replyToMessageId` / `quote` | 可选 | 回复关系。 |
| `messages[]` | `mediaPath`、`mediaType`、`mediaFileName`、`mediaLocalPath` | 可选 | 只保存导出文件已经提供的媒体定位信息；不会主动读取图片或媒体内容。 |

`members` 和 `sync` 都是可选对象：前者可提供参与人信息，后者可提供导出端自己的水位信息。知域仍会根据每条消息的稳定标识或内容身份做去重，因此同一个文件重复导入不会重复写入已有消息。

## 导出器接入要求

1. 导出器必须由用户自行获得并在其自己的电脑、账号和授权范围内运行；知域不要求、也不接收数据库密钥、Cookie、登录态或受保护数据库。
2. 每个导出文件应只包含用户明确选择的会话和时间范围；导出器不要把密钥、访问令牌、绝对系统路径或无关应用配置写入 JSON。
3. 用户在知域中先执行只读检查。检查会生成与文件内容绑定的令牌；文件被改动后，必须重新检查才能导入。
4. 导入后的聊天默认标记为 `restricted`。它不会默认进入普通文件检索，也不会因导出器名称而改变隐私范围。

兼容接口为 `POST /api/chat-imports/chatlab/inspect` 和 `POST /api/chat-imports/chatlab/import`。原有 `/api/weflow/chatlab/*` 路径仅供历史客户端兼容，新接入方应使用通用路径。
