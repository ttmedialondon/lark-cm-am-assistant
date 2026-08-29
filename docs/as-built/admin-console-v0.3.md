# Admin Console v0.3：已部署结构说明

状态：内部测试版。入口为 `https://larkbot.ttapp.help/admin`；必须先完成正常的 Lark OAuth 登录。

## 访问控制

管理台不依赖前端隐藏按钮。每个请求都会把当前安全 Web Session 映射回 Lark 授权记录，再查询 `admin_roles`：

- `owner`：查看全量运营统计；后续可管理管理员与同步操作；
- `operator`：预留给运营管理员；
- `viewer`：预留给只读管理员；
- 没有活动角色：返回 403。

本次发布后只会把当前已验证的部署者授权写入一个 `owner` 角色；其他已授权 CM/AM 不会自动拥有 `/admin` 权限。令牌、刷新令牌、应用密钥和消息正文不会被管理台查询或展示。

## 数据模型

```text
authorizations (一个已 OAuth 的 CM / AM)
       │ 1:n
authorization_chats (该用户当前可见群 + 该用户自己的同步游标/状态)
       │ n:1                         │ 1:n
       ▼                             ▼
chats (公司级唯一群)            authorization_message_observations
       │ 1:n                         (该用户见过该消息；是否由该用户发送)
       ▼
chat_messages (公司级唯一消息 ID；不含消息正文)
```

同一个 Lark 群被多位 CM/AM 授权时：

- 群元数据和每个消息 ID 都只保留一份；
- 每位 CM/AM 的可见性、分页游标、同步状态和“本人发送”判断仍单独保存；
- 公司统计使用 `chats` / `chat_messages`，不会重复计数；
- 个人仪表盘仍按该用户的 `authorization_chats` / observation 统计。

这避免了将不同用户可读取的历史错误地混在一起，同时为后续 AI 分析提供公司级去重的输入。

## 管理台页面

| 路径 | 内容 |
|---|---|
| `/admin` | 有效授权人数、唯一群、唯一消息、待处理关系、活动任务与近期异常。 |
| `/admin/users` | 所有已授权 CM/AM 的当前群数、可见消息数、本人消息数、同步状态。 |
| `/admin/users/{authorization_id}` | 某一授权用户的当前群关系和最近任务。 |
| `/admin/chats` | 公司级唯一群、已授权可见人数、去重消息数、同步覆盖与成员人数状态。 |
| `/admin/jobs` | 最近同步任务及无敏感数据的运行统计。 |

“成员人数”与“已授权可见人数”不是同一个指标：前者是 Lark 返回的群成员总数，后者是已经向本服务 OAuth 的 CM/AM 中、当前可看到该群的人数。

## 当前同步参数

- 首次回填：Lark API 可返回的全部历史；
- 增量同步：每 5 分钟；
- 去重回看窗口：10 分钟；
- 每个授权用户：同一时刻最多一个 worker 任务；
- 消息正文、附件、成员名单：不入库。

## 下一步所需的 Lark 能力

- 群成员总数：`im:chat.members:read`，启用后用户需要重新 OAuth；
- P2P：`im:message.p2p_msg:get_as_user`，但公开群列表 API 不会枚举 P2P，因此不能仅凭该权限完成“全量私聊扫描”；
- 公司通讯录：应用身份 `contact:contact:readonly_as_app`，并在 Lark 后台将通讯录可访问范围设为全员。
