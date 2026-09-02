# Admin Console：当前实现说明

入口：`https://larkbot.ttapp.help/admin`。必须先完成正常 Lark OAuth 登录。

## 访问控制

每个请求都会把 Secure + HttpOnly Web Session 映射回 Lark 授权记录，再查询 `admin_roles`：

- `owner`：查看运营统计、人员/群目录、同步任务、`/admin/messages` 的正文、`/admin/contacts` 的外部联系人目录和 `/admin/creators` 的已确认达人；
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
       └── lark_identities（发送者 / 成员 / @ 提及的稳定 Lark 身份）──> contacts

chats ──< chat_members（最新群成员快照）
contacts ──< contact_roles（人工确认或自动确认的角色事实）
contacts ──< contact_assignments（单一当前负责人 + 人工交接历史）
```

同一个 Lark 群被多位 CM/AM 授权时，群元数据和正文只保留一份；每位 CM/AM 的可见性、游标、同步状态和“本人发送”判断仍独立保存。因此，公司级计数不会重复，人员视图也不会误显示未重新授权的正文。

## 页面

| 路径 | 内容 |
|---|---|
| `/admin` | 有效授权人数、唯一群/私聊、去重消息、已加密正文，以及“接入进度与需要处理”。未来 5 分钟的定时 job 不计为活动任务。 |
| `/admin/users` | 每位已授权 CM/AM 的单一接入阶段、群/私聊范围、首次回填进度和消息索引；不把私聊目录发现误当正文完成。 |
| `/admin/users/{authorization_id}` | 某一授权用户的发现/可同步/已完成/等待进度、群/私聊分别的回填比例、当前可见会话与近期任务；owner 可明确标记该账号是否可担任达人负责人。 |
| `/admin/chats` | 公司级唯一群和专题群、已授权可见人数、去重消息数、同步覆盖与成员人数状态。 |
| `/admin/messages` | **仅 owner**：按 CM/AM、群/私聊、具体会话筛选，最近 100 条正文分页展示。 |
| `/admin/contacts` | **仅 owner**：按稳定 Lark 身份查看外部联系人、会话统计、角色状态与可核验的 AI 消息证据。 |
| `/admin/creators` | **仅 owner**：查看已确认拥有 `creator` 角色的联系人及其稳定 Lark 身份。 |
| `/admin/contacts/{open_id}`（达人） | **仅 owner**：显示唯一当前负责人、最近的 AI 匹配状态与低置信建议；可人工确认或交接给另一位已接入 CM/AM。 |
| `/admin/ai` | **仅 owner**：分别配置“联系人身份识别”和“达人负责人匹配”的 OpenAI Project / service-account key。 |
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
- 联系人角色：仅基于稳定 Lark 身份及当前授权范围内、可核验的消息证据生成或确认；群名只是弱证据，不会单独确认角色。
