# 系统技术说明

本文记录后端、人员模型、姓名机制与后台。业务以 [达人名单](../product/creator-management.md)、[消息总结](../product/message-summary.md)、[后台权限](../product/admin-access.md) 为准。2026-09-11 改为三阶段依次判断身份、CM 归属和 handle，统一完整资料入库门槛；扫描及总结保持关闭。

## 1. 实际架构

```text
浏览器 / Lark 网页入口
        │ OAuth + HTTPS
        ▼
Caddy → FastAPI + Jinja2 / HTMX / Tabler → PostgreSQL
                                             ▲
                       独立 Python worker ────┘
                            ├─ Lark 授权同步
                            └─ 已启用的 AI 会话分析
```

单体应用、顺序 SQL 迁移和 PostgreSQL 持久任务。现有页面在 `backend/app/templates/`、`static/`；不把旧 React、Alembic、Redis 或长期事件工作台设计写成已上线能力。

## 2. 数据怎么关联

| 对象 | 存什么、不能混成什么 |
|---|---|
| `authorizations` | 已接入 CM 的授权、正文同意和接入状态；不是全体 Lark 人口库 |
| `chats`、`chat_messages` | 公司级去重的会话与消息，正文加密；去重不代表所有人都能读 |
| `authorization_chats`、`authorization_message_observations` | 哪位授权人可见哪个会话、观察到哪些消息，以及各自同步状态 |
| `lark_identities`、`contacts`、`chat_members` | 稳定 Lark 身份、联系人与最新群成员快照；不能按显示姓名合并用户 |
| `contact_roles` | 业务类别及主/辅助角色；未知保持待识别，不写成“其他” |
| `organizations`、`organization_tenants`、`contact_organization_links` | 组织及可验证租户/人员关系；租户相同不等于达人、员工或负责人 |
| `social_accounts`、`contact_social_accounts` | 平台账号及关联用户；一个用户可以关联多个 handle |
| `creator_workflow_candidates`、`creator_workflow_sources`、`creator_workflow_requests` | 待确认线索、来源和请求幂等记录 |
| `creator_account_assignments` | 用户与 CM 的正式归属；有效归属的账号不可为空，是名单与总结取数依据 |
| `contact_assignments` | 旧联系人级负责人关系；不能代替账号级确认，也不能认领一个 handle 就转走同人的其他 handle |
| `creator_workflow_ai_runs`、`creator_workflow_ai_tasks` | 同人三阶段任务、每阶段冻结引用及输入指纹、初始指纹去重、阶段结果、总预算与共享重试 |
| `creator_workflow_onboarding` | 新授权同步就绪检查，不自动调用 AI；旧游标只留历史 |
| `offhours_summary_settings`、`offhours_summary_runs`、`offhours_summary_tasks` | 指定总结时段、执行版本和会话结果 |
| `admin_roles`、`admin_page_permissions`、`audit_events` | 主人身份、逐页权限、必要的查看/修改审计 |

现有人员类别为 creator、partner_contact、internal_colleague、merchant_contact、platform_contact、other_external_contact。内部同事依内部租户事实识别；外部人的角色、所属组织和负责人是独立事实。

## 3. 授权与正文边界

- Lark 使用 CM 自己的 user access token，读取 API 实际返回且其本来有权查看的范围。用户权限为 `offline_access`、`im:chat:read`、`im:message:readonly`、`im:chat.members:read`。
- 旧的仅统计授权不能静默升级。只有记录正文同意、且该来源在同意后重新观察到的消息，才可按有效范围展示正文。
- 群和私聊共享底层存储；私聊对端身份记录在授权来源的会话关系上。P2P 发现结果单独标记，不可用不阻塞群聊同步。
- 消息正文、OAuth 令牌、AI 密钥及派生总结使用各自加密域；正文、完整 prompt、密钥和令牌不进入普通日志或 Git。不保存附件二进制。
- 最新群成员快照包含稳定 ID、姓名和可用租户信息，不保存成员历史版本；API 未返回某人不等于已离群。
- 后台逐页权限、前台名单编辑、测试编辑和正文读取分别校验。每请求查询当前权限，不将权限缓存到登录会话。写操作有 CSRF、版本/冲突校验、事务和审计。
- 正文读取同时核对授权、同意、会话可见且启用、有效消息观察和删除状态；选用户或建立关联不新增权限。详细入口范围见后台权限文档。

## 4. 同步与并发

- OAuth 后回填可读取历史；常规每 300 秒增量，重看前 600 秒避免边界遗漏。重复消息按稳定消息标识去重。
- 会话目录分页按授权人顺序完成；消息分页用数据库 claim、租约、游标和 generation 保证同会话串行、不同会话可并行。消息及观察关系与游标在同一事务保存，重启可续跑。
- 每位授权人最多一个 coordinator；默认全局最多 3 个在途 Lark 请求、每秒 10 个，每人优先最多占 2 个会话槽位，其他人无等待时可借用剩余槽位。
- 群成员刷新是低优先级任务，默认最多 1 个在途请求；消息任务优先。单会话临时失败单独退避，429 或 Lark 限流响应触发共享暂停，不紧密重试。
- 归档/解散会话保留发现事实，但不回填且不计入可同步分母；专题群按群聊展示。未来已排程的任务不等于故障或正在处理。
- 生产保持一个 worker 进程，内部线程池执行并发；不能单凭加进程就认为吞吐提高。

## 5. AI 与确认

`creator-stages-v1` / `person_staged_v1` 复用现有任务表，不新增队列服务。每人一个任务，`stage` 在 identity → ownership → handle 顺序推进；`stage_results` 保存受控结论、分数及冻结引用，不存模型原话。明确否定与信息不足分开，只有 pass 才在服务器本地准备下一阶段材料。

`creator_targeting.build_preflight` 遍历当前有效授权内已同步历史，只选外部用户本人的业务发言，不要求 CM 沟通。`build_stage_evidence` 在第二步才核对真实私聊对端、明确回复/@和双方片段；第三步只取本人账号线索，Excel 线索与消息引用分离，不用 X 引用证明本人身份。脱敏保留精确账号/主页链接，其他成员的聊天不整包发送。

`ai_dispatch` 共享最多 3 个有效并发槽；同人的阶段串行，不同人并行。每批最多30人，每阶段最多3组对话、30条、16,000字符，单条1,500字符。总尝试最多4次，`stage_attempts` / `retry_count` 确保仅一个跨阶段共享重试；领取后的调用前校验失败也占尝试，不将计数当作账单。`initial_fingerprint` 不随阶段改变，重新准备不能为同 CM / 同人开新预算。新证据可复用原任务补查未通过阶段，原通过项须仍有效；无新材料不重复送审。补查预检标记 `resumed` 和 `preflight_revision`，须明确启动，不因刷新调用。批次 `started_at` 记录开始或恢复时间，状态优先当前活动批次，避免旧批重试被新空预检遮住。

每次调用和保存均重验当前及所有前置已通过阶段的授权、作者、原文内容和输入指纹；配置变化或暂停后不能写入在途结果。只对当前阶段失败做有限重试；保存事务冲突最多重试3次且不重发模型请求。沿用 OpenAI Responses 结构化输出和既有配置，不换模型；明确启动才付费调用，不做 OCR / 附件读取。

`_admission_gate` 是 `_promote`、自动及人工确认的统一门槛：新版本 AI 两项有效通过、用户及 CM 一致、准确非空 handle 有 AI 本人证据或 CM 显式核实、无冲突。依据纳入三个阶段完整上下文（最多90条），revision 包括解密正文 hash；正文改变即失效，同正文重新加密不会误判。列表仅在单次读取内缓存共享引用的权限和正文 revision，写入仍重新检查，下次读取不复用旧缓存。旧 business_v2 或人工清疑点均不能绕过。CAP 允许 NULL。

`handle_verification` 只能由服务器根据明确的手工核实动作写入；客户端不能提交整段核实 JSON。换人/换 CM 更新 `ai_invalidated_at`，切回旧值也不能重用失效结论；改账号只重核账号，不打断前两步。CM 已核实账号且前两项 AI 通过时结束任务，不再发送第三步，阶段来源记为 CM 而非伪造 AI 分数。人工退回、换人/换 CM 取消旧扫描，保存前还校验冻结的候选关键字段，禁止另建行绕过退回。正式关键资料纠错先结束旧 assignment，后续重新通过才恢复。043 同时约束正式 candidate 与有效 assignment 的账号非空，并将不符合新规则的旧正式归属退出；041 的清库不会重复执行。

前端默认正式名单，待处理与不纳入分开；批次异常不当作业务否定，也不混入达人总数。OAuth 只登记本地同步就绪检查，不调用 AI。旧联系人识别/关联及当前达人扫描、消息总结均保持关闭；不回复、不发通知、不代办业务。

## 6. 姓名和 @

`name`、`en_name` 保留 Lark 来源字段，`display_name` 是派生显示。OAuth/当前有效个人资料优先，旧消息与旧提及只补缺值；英文名字段缺失保留旧值，明确返回空值才清除。

`identity_names` 统一页面显示及可核验 CM 别名；单身份联系人可同步标签，多身份联系人保留人工标签。名称变化不合并身份、不修改归属。

`message_mentions` 只按同条消息保存的明确身份映射还原 `@_user_N`，同时处理正文/富文本及中文相邻字符。未知或冲突不猜，URL、代码和邮箱中的同样字符不替换。先检查阅读权限，再解析和展示。

维护脚本 `backend/scripts/backfill_identity_names.py` 默认只检查；显式 `--apply --owner-id …` 才写入，`--refresh-members` 可在已有权限内刷新群成员。实时资料不足时保留已有姓名，不扩大权限、不重跑达人 AI。请求前后检查授权与稳定 ID，失败不阻塞消息同步。

## 7. 已移除的旧入口

后台达人目录、归属复核、专用聊天详情和整理清单已下线，旧预览与快照读取代码、旧一次性交接/恢复脚本一并移除。当前只从 `/dashboard/creators` 管理达人及必填账号，不再展示旧联系人归属工作台。

036 迁移只删除 `creators` / `creator-review` 页面权限、保存移除审计并收紧权限键约束，不删除候选、正式达人、人口库或消息表。两位明确测试者的 Remi 前台权限不再依赖旧后台权限。历史代码从 Git 恢复；Remi 此前的数据清理仍以 [历史记录](../status/2026-09-10-reset-and-docs.md) 为准，不重复执行。

运行配置、发布、备份和恢复统一见 [部署手册](../runbooks/deploy-mvp.md)，不在这里复制命令。
