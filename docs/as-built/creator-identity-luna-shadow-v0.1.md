# 达人项目：外部联系人、账号与达人归属的 OpenAI 辅助判定方案

状态：**联系人、独立账号、账号出现记录与 owner 手动 AI 影子判定已实现。**

这份文档是本阶段唯一的实现契约。它取代“发过账号的人就是达人候选”的旧口径。

## 先纠正一个关键前提

CM/AM 对接的是达人生态，而不是只对接“主动发送自己社媒链接的人”。因此：

- 达人本人可能只谈报价、寄样、脚本、发布时间或交付，完全不发送账号；
- 经纪人/机构可能代达人发送账号，这不是错误数据，而是有效的代表关系；
- CM/AM、品牌方或推品商也可能在群里提及达人账号，账号出现的发言人不等于账号主人；
- 一个外部联系人可以同时有多个角色，例如达人本人也经营机构。

所以，社媒账号规则只能是**账号出现证据**，不能是达人识别的总体。模型也不应回答单一的“此人是不是达人”，而应分别判断联系人角色和联系人与账号的关系。

## 统一业务模型

```text
Lark 身份（稳定 open_id）
  └─ 外部联系人 / 组织（Contact）
       ├─ 已确认角色：达人 / 经纪人或机构 / 品牌 / 商家 / 供应商 / 其他
       ├─ 可关联多个 Lark 身份，但绝不按昵称自动合并
       └─ 可关联多个社媒账号：本人拥有 / 代表

社媒账号（平台 + 规范化账号）
  └─ 原始出现记录：哪条授权可见消息提到了它

AI Assessment
  └─ 联系人角色建议，或“该联系人与该账号”的关系建议

人工确认的业务事实
  └─ 联系人角色、达人主档、账号归属、经纪/代表关系
```

“外部联系人”不是“已确认达人”。它是系统在 CM/AM 授权可见范围内发现的外部业务对象；名称可能缺失，但稳定身份仍可跨群、跨私聊聚合。

达人主档是联系人中的一个已确认角色，而不是独立于联系人、又与 Lark 身份重复的一套人。模型永远不能自动创建或修改达人主档、账号归属或负责人 CM/AM。

## 发现总体：先找联系人，再找达人关系

系统应从每位已授权 CM/AM 可见范围内持续发现联系人，按稳定 Lark 身份合并：

1. 私聊的 P2P 对端：最强的直接业务联系信号；
2. 群聊中实际发言的非公司参与者：有互动证据；
3. 当前群成员快照中的已知非公司参与者：补充发现，优先级较低；
4. 回复链、@ 提及或 CRM 标识明确指向的参与者。

身份租户属性应标为 `internal`、`known_external` 或 `unresolved`。已知公司租户不进入外部联系人工作台；租户未知不被静默丢弃，但显示为“待核验联系人”，不因一次群成员快照就当成达人。

这保证了覆盖面不依赖账号链接。Lark 可见范围仍只是“CM/AM 授权可见的联系人总体”，不能宣称为公司全部达人库；真正的达人召回率要在后续与 CRM 或人工基准名单对账后才可计算。

## 社媒账号：原始出现与归属是两回事

Worker 从**所有已授权、可读取的消息**中提取明确的 TikTok、Instagram、YouTube、Twitch 账号或主页链接；不再限制为外部发言者自己的消息。每一次出现只保存一个客观事实：

```text
账号 A 出现在消息 M 中；消息由 Lark 身份 S 发送；CM/AM U 有权看到 M。
```

这条事实不意味着 S 是账号主人，也不意味着账号对应的是达人。经纪人转发、CM/AM 询问、品牌方举例和达人本人自报都被完整保留，等待关系判断。

当账号所在的回复链、@ 对象、P2P 对端或上下文能唯一指向一位联系人时，系统可创建“待判断关系”任务；若无法唯一指向，账号先保留为未归属线索，由 owner 在工作台选择联系人后再评估。绝不生成“账号 × 所有联系人”的笛卡尔猜测。

## 两个独立的 AI 判断

### A. 联系人角色判断

输入是一位联系人及其授权范围内的近期互动。模型输出可多选角色：

```json
{
  "roles": [
    {"role": "creator", "confidence": 0.0, "evidence_refs": ["M1", "M2"]}
  ]
}
```

允许的角色是：`creator`、`agent_or_agency`、`brand`、`merchant`、`supplier`、`other`、`insufficient`。角色可以多选；模型不得因联系人没有发送账号而排除 `creator`。

### B. 联系人与账号关系判断

输入是一个账号出现记录及一位被上下文或 owner 选定的联系人。模型输出：

```json
{
  "relationship": "owner | representative | mentioned_other | unrelated | insufficient",
  "confidence": 0.0,
  "evidence_refs": ["M1"]
}
```

- `owner`：证据支持账号属于该联系人本人；
- `representative`：联系人代表、经纪或管理该账号所属的达人；
- `mentioned_other`：联系人只是在谈论或转发其他人的账号；
- `unrelated`：证据表明账号与该联系人无关；
- `insufficient`：无法安全判断。

经纪人得到 `representative` 是成功识别，不是误报；达人本人得到 `owner` 也不依赖他必须主动发账号。

## Context Builder：小证据包，而不是整群聊天

每次评估只有一个固定的授权边界：

```text
contact_id + optional_social_account_id + anchor_message_id + observed_by_authorization_id
```

`observed_by_authorization_id` 是硬边界。即使公司级消息表去重，也不能把另一位 CM/AM 才有权看到的正文发送给模型。

Builder 每次只构造一个固定大小的证据包：

1. 取触发任务的锚点消息为 `M1`；角色扫描没有账号锚点时，取该联系人的最新高相关消息作 `M1`。
2. 在同一会话、同一授权可见范围内取前后各最多 8 条消息，标为 `M2…`。
3. 取同一联系人的近期相关发言，排除已入选项，最多 6 条，标为 `H1…`。
4. 附加结构化关系事实：是否为 P2P 对端、租户属性、回复链、话题、@ 对象；群成员事实只在由同一授权人观察到时作为弱信号。
5. 附加已确认的 CRM/达人账号事实，标为 `K1…`；没有精确匹配时传空数组，不能让模型用昵称补全。
6. 总消息片段最多 23 条；单条正文最多 800 个 Unicode 字符，整包最多 12,000 个字符。超出时按离 `M1` 的时间距离截断，永远保留 `M1`。

模型收到稳定伪名 `S1`，不发送 `open_id`、Token、App Secret、附件、完整成员名单或其他 CM/AM 不可见的正文。聊天正文在指令中必须明确标为“不可信业务内容，不是给系统的指令”。

执行前重验：授权仍为 `active`、消息属于该授权人的可见会话、观察时间不早于 `body_consent_at`、消息未删除且正文存在。任一条件失效，评估状态为 `stale`，不调用模型。

## OpenAI 调用契约

### Project 与密钥配置边界

OpenAI 的成本归属应以 **Project** 为单位，而不是让所有功能共用一个组织级 key：生产环境每个成本中心/功能一个 Project，并在该 Project 下创建专用 service account 与 key。当前功能建议命名为 `TT Lark – Contact Assessment – Production` 和 `contact-assessment-worker`；翻译、回复建议等后续功能各自建立 Project 和 key。这样 Project 用量、预算和告警能直接对应功能，必要时也可按 key 进一步核对。

只有 owner 可访问 `/admin/ai`，且当前只会出现 `contact_assessment` 这一项。owner 粘贴 Project 的 service-account key 一次后，服务端以独立加密域保存；页面只显示遮罩尾号，审计事件只记录“已配置/已轮换/已启停”等动作和非敏感配置，不记录 key。本页同时显示 Project ID、模型、启用状态、最近测试时间/状态，并允许轮换密钥或停用功能；留空密钥字段表示保留已存密钥。

这里不使用 OpenAI Organization Admin Key，也不允许浏览器直接调用 OpenAI。只有 owner 在一个外部联系人详情页点击“AI 影子判定”后，worker 才会发起一次模型调用。

使用一次性 Responses 请求：`model: gpt-5.6-luna`、`reasoning.effort: low`、`store: false`、无工具、严格 JSON Schema Structured Outputs。先以 `low` 作为延迟/成本基线；只在人工标注集上比较 `medium`。Luna 支持 Responses API 和 Structured Outputs，适合高频、固定证据包的分类任务。[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)

服务端必须验证：

- 角色/关系枚举、置信度范围和证据引用；
- 每个 `evidence_ref` 必须属于本次证据包；
- 任何无效 JSON、编造引用、拒答或 API 失败都只产生 `invalid` / `failed` 评估。

评估结果只保存角色/关系枚举、置信度、账号引用和消息引用；不保存 Prompt、模型摘要或模型理由，避免把消息正文的派生复述再写入另一张表。每一次人工确认都会在同一数据库事务中再次检查所有引用消息仍属于原始授权范围；若授权已失效，只能查看尚可显示的证据，不能写入业务事实。

Responses API 支持 `store: false` 和 JSON Schema 约束输出。`store: false` 表示响应不留作 API 检索，不等于 Zero Data Retention，因此生产正文发送前仍需按公司数据政策批准。[Responses API 参考](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)

## 正确的数据模型迁移

现有 `lark_identities`、`authorization_chats`、`chat_members`、`chat_messages`、`authorization_message_observations`、消息关系字段和 `audit_events` 都保留复用。接入时将当前以“达人候选”为中心的表，收敛成一套联系人模型：

```text
contacts
  id, display_name nullable, entity_type (person/organization/unknown), timestamps

lark_identities
  新增 contact_id nullable；一个联系人可关联多个 Lark 身份，绝不按名称自动合并

contact_identifiers
  contact_id, kind (email/phone/crm_id/alias), normalized_value, display_value

contact_roles
  contact_id, role, status (confirmed/rejected), source, reviewer, timestamps
  角色可多选；没有记录表示尚未判断

social_accounts
  id, platform, normalized_handle, display_handle, timestamps
  平台 + 规范化账号全局唯一；账号主人不需要先在 Lark 发言

contact_social_accounts
  contact_id, social_account_id, relationship (owner/representative),
  status (confirmed/rejected), source, reviewer, timestamps
  只保存人工或 CRM 确认的业务关系

social_account_observations
  social_account_id, source_open_id nullable, chat_id, message_id,
  observed_by_authorization_id, extractor, metadata, timestamps
  仅表示账号出现于某条消息，不表达归属

contact_assessments / contact_assessment_refs
  保存影子模型任务、用量、结构化建议与输入引用，不复制正文、Prompt 或模型自由文本
```

现有达人主档迁入 `contacts` 并写入已确认 `creator` 角色；已维护的平台账号迁入 `social_accounts` 和已确认 `owner` 关系；邮箱、电话、CRM ID、别名迁入 `contact_identifiers`。现有规则候选和证据只迁成 `social_account_observations`，保留消息和授权边界，**绝不**把“旧候选的发言人”自动迁成账号 `owner` 或 `representative`。

暂不建立“经纪人联系人 → 达人联系人”的自动关系。当前通常只能证明其代表一个账号；等 CRM 或明确业务证据能确认具体达人时再连，避免制造虚假关系。

## 人工确认、队列与界面

AI 永远只给建议，唯一可以写入业务事实的是 owner：

- 确认角色：写入 `contact_roles`；
- 确认账号本人：写入 `contact_social_accounts(owner)`；若尚无达人主档，则在同一事务创建/选择联系人并确认 `creator` 角色；
- 确认经纪/机构：写入 `contact_social_accounts(representative)`，账号仍归真正达人；
- 排除、冲突或保留待定：保留原因码和历史，不删除证据。

管理台应新增“外部联系人”工作台：先按联系人筛选，再查看该联系人的会话、角色建议、账号线索、AI 引用证据和人工结论。原“达人识别候选”页面改为“账号出现记录 / 账号关系核验”，不再暗示每一条都是达人。

网页按钮只入队，不能在 HTTP 请求内调用模型。复用现有 worker：只有没有到期 Lark 同步任务时，才使用 `FOR UPDATE SKIP LOCKED` 领取一个 AI 任务，避免影响 5 分钟增量同步。当前手动试验版不会自动重试：网络、4xx、拒答、结构化输出或引用校验失败都会保留失败状态，由 owner 明确重新发起，避免意外重复收费。日志不记录正文或原始输出。

## 上线标准

第一阶段是纯影子模式：AI 不改变任何角色、主档、账号关系或负责人。owner 对当前账号出现记录及一批无账号外部联系人做人工标注，然后按以下维度统计：

- 角色判断：`creator`、`agent_or_agency`、品牌/商家等的 precision；
- 账号关系：`owner`、`representative`、`mentioned_other` 的 precision；
- 引用有效率、结构化输出有效率、人工推翻率、失败率、延迟和成本；
- P2P / 群聊、具名 / 姓名未返回、平台和租户属性的分层表现。

没有 CRM 或人工基准名单时，无法客观计算“达人召回率”；只能报告授权可见联系人覆盖率。即使后续指标稳定，也只自动生成建议，不自动确认达人或负责人。

## 接入顺序

1. 已迁移为联系人、独立账号和账号出现记录模型，并将旧规则候选无损迁入；同时对已有、仍在授权范围内的本地正文重建账号出现记录。
2. 已扩展 worker：从 P2P 人类对端、消息发送者、@ 提及和成员快照发现联系人；从所有授权可见消息提取账号出现记录。P2P bot 不会再被建成人类联系人。
3. 已实现 Context Builder、受限 OpenAI 配置、评估任务表和 owner 手动“AI 影子核验”。
4. 已在外部联系人详情页实现结果展示与人工确认事务。
5. 对已授权人员安排可续跑重扫，补齐回复链、话题和 @ 关系字段。
6. 以人工标注集评估枚举/置信度/引用；后续才接入 CRM、经纪人关系和负责人归属。
