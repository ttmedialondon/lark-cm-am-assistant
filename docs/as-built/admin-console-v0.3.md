# Admin Console：当前实现说明

入口：`https://larkbot.ttapp.help/admin`。必须先完成正常 Lark OAuth 登录。

## 访问控制

每个请求都会把 Secure + HttpOnly Web Session 映射回 Lark 授权记录，再查询 `admin_roles`：

- `owner`：查看运营统计、人员/群目录、同步任务、`/admin/messages` 的正文、`/admin/contacts` 的外部联系人目录、`/admin/accounts` 的账号出现记录和 `/admin/creators` 的人工达人主档；
- `operator` / `viewer`：查看管理台统计，但不能进入正文页；
- 无活动角色：403。

当前部署只写入一个 `owner`；其他已授权 CM/AM 不会自动获得管理台权限。Token、刷新 Token、App Secret 永不由管理台查询或展示。

`/admin/messages` 仅展示选定 CM/AM 在新版授权后重新观察到的会话消息。每次访问会写一条 `admin_message_content_viewed` 审计记录，记录查看者、目标授权用户和可选会话 ID，不保存正文。

## 数据模型

```text
authorizations (一个已 OAuth 的 CM / AM；含正文同意时间)
       │ 1:n
authorization_chats (该用户可见会话 + 私有同步游标/状态)
       │ n:1                         │ 1:n
       ▼                             ▼
chats (公司级唯一群聊或私聊)     authorization_message_observations
       │ 1:n                         (该用户见过该消息；是否由该用户发送)
       ▼
chat_messages (公司级唯一消息 ID；加密正文与必要元数据)
       │
       ├── lark_identities（发送者 / 成员 / @ 提及的稳定 Lark 身份）──> contacts
       └── social_account_observations（账号出现在该消息中的授权范围事实）──> social_accounts

chats ──< chat_members（最新群成员快照）
contacts ──< contact_roles / contact_identifiers / contact_social_accounts（人工确认事实）
```

同一个 Lark 群被多位 CM/AM 授权时，群元数据和正文只保留一份；每位 CM/AM 的可见性、游标、同步状态和“本人发送”判断仍独立保存。因此，公司级计数不会重复，人员视图也不会误显示未重新授权的正文。

## 页面

| 路径 | 内容 |
|---|---|
| `/admin` | 有效授权人数、唯一群/私聊、去重消息、已加密正文，以及“接入进度与需要处理”。未来 5 分钟的定时 job 不计为活动任务。 |
| `/admin/users` | 每位已授权 CM/AM 的单一接入阶段、群/私聊范围、首次回填进度和消息索引；不把私聊目录发现误当正文完成。 |
| `/admin/users/{authorization_id}` | 某一授权用户的发现/可同步/已完成/等待进度、群/私聊分别的回填比例、当前可见会话与近期任务。 |
| `/admin/chats` | 公司级唯一群和专题群、已授权可见人数、去重消息数、同步覆盖与成员人数状态。 |
| `/admin/messages` | **仅 owner**：按 CM/AM、群/私聊、具体会话筛选，最近 100 条正文分页展示。 |
| `/admin/contacts` | **仅 owner**：按稳定 Lark 身份查看外部联系人、会话统计及其发出的账号出现记录；不推断角色或账号归属。 |
| `/admin/accounts` | **仅 owner**：查看平台账号的原始消息出现证据和人工确认归属；不把出现记录当作达人候选。 |
| `/admin/creators` | **仅 owner**：人工维护已确认达人及稳定身份标识。 |
| `/admin/jobs` | 最近同步任务及不含敏感内容的运行统计。 |

## 当前同步参数

- 首次回填：Lark API 可返回的全部历史；
- 增量同步：每 5 分钟；
- 去重回看窗口：10 分钟；
- 每个授权用户：同一时刻最多一个 worker 任务；
- 正文：新版授权后 AES-GCM 加密存储，不进日志；
- 私聊：以 `types=p2p` 尝试发现，状态会在用户记录上可见，失败不会影响群聊。
- 群成员：每次成员刷新分页更新最新快照；Lark 未返回的成员不会被删除或标为离群。
- 首次历史回填：安全分页续跑属于同一个 job，不记为错误重试；`attempts` 只在临时错误、租约过期或退避重试时增加。
- 已归档/解散会话：保留发现事实但不计划回填，并从“可同步会话”分母中排除。
- 专题群：计入群聊总数，在目录与人员页单独标为“专题群”。
- 账号规则 V1：从所有已同意正文同步的消息中提取明确的 TikTok、Instagram、YouTube、Twitch 主页链接或带平台标签的账号。记录只表示账号出现在某条消息中，不因发言人、昵称、群名、普通 @ 提及或模型猜测自动确认达人、经纪人或账号归属。
