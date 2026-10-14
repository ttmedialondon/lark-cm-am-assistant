# 系统技术说明

更新：2026-10-13。本文记录现行后端、数据关系与运行边界。业务以 [人员身份与达人名单](../product/creator-management.md)、[消息总结](../product/message-summary.md)、[后台权限](../product/admin-access.md) 为准；功能启用状态由线上配置决定，不由历史文档或发布脚本决定。

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
| `contact_assessments`、`contact_assessment_refs` | 旧身份判断及引用历史；仍用于核对已有类别来源，不再泛扫或确认旧候选 |
| `organizations`、`organization_tenants`、`contact_organization_links` | 组织及可验证租户/人员关系；租户相同不等于达人、员工或负责人 |
| `social_accounts`、`contact_social_accounts` | 平台账号及关联用户；一个用户可以关联多个 handle |
| `creator_workflow_candidates`、`creator_workflow_sources`、`creator_workflow_requests` | 保留的旧线索、来源和请求历史；无现行写入入口 |
| `creator_account_assignments` | 已退役的账号级归属历史；不参与当前名单、冲突或调度，仅保留已结束关系供解除历史核对及审计 |
| `contact_assignments` | 人级负责人；当前人工入库事务与账号绑定同时完成，引用 `import_row_id`；旧 AI 任务引用保留为历史 |
| `creator_ownership_runs`、`creator_ownership_tasks`、`creator_ownership_refs` | 历史负责人扫描批次、人×CM任务/受控结论（已退役）、授权绑定且冻结的消息引用；输入与派生说明加密，原文仍来自原存储 |
| `creator_handle_runs`、`creator_handle_tasks` | 旧反向 handle 扫描历史；无现行扫描或确认入口 |
| `creator_import_batches`、`creator_import_rows` | 单列 Excel 批次及账号行，记录上传人、所选 CM、找群找人结果与核对状态；确认导入后排队，不创建空壳联系人 |
| `creator_handle_jobs`、`creator_responsibility_jobs` | 已确认达人的账号及多负责人自动匹配，独立证据与版本 |
| `chat_group_classifications`、`creator_group_jobs` | 群用途及独立分类任务，人工结果优先 |
| `creator_importance_profiles`、`creator_importance_jobs` | 达人 × CM 的聊天事实、人工调整及动态优先级 |
| 各功能 `*_requests` | 实际请求、用量、安全错误及未知结果回执，不是预算限额 |
| `creator_broadcast_lists`、`creator_broadcast_templates` | 本人的收件名单与消息正文模板 |
| `creator_broadcast_previews`、`creator_broadcast_batches`、`creator_broadcast_targets` | 发送快照、独立批次及逐群回执 |
| `desktop_auth_codes`、`desktop_reply_requests` | 一次性桌面授权及按本人/请求 ID 去重的回复回执 |
| `creator_workflow_ai_runs`、`creator_workflow_ai_tasks`、`creator_workflow_onboarding` | 旧扫描结果、引用与接入记录，仅保留历史，未完成项已取消 |
| `offhours_summary_settings`、`offhours_summary_runs`、`offhours_summary_tasks` | 指定总结时段、执行版本和会话结果 |
| `admin_roles`、`admin_page_permissions`、`audit_events` | 主人身份、逐页权限、必要的查看/修改审计 |

人员类别复用 creator、partner_contact、internal_colleague、merchant_contact、platform_contact、other_external_contact 六个代码；不区分 CAP/非 CAP。人工已确认身份和可信内部同事事实保留，角色、组织、账号及负责人是独立关系。一个达人可有多个账号、多位 CM；关系不授予聊天读取权限，不凭显示名合并身份。

## 3. 授权与正文边界

- Lark 使用 CM 自己的 user access token，读取 API 实际返回且其本来有权查看的范围。用户权限为 `offline_access`、`im:chat:read`、`im:message:readonly`、`im:chat.members:read`。
- 旧的仅统计授权不能静默升级。只有记录正文同意、且该来源在同意后重新观察到的消息，才可按有效范围展示正文。
- 群和私聊共享底层存储；私聊对端身份记录在授权来源的会话关系上。P2P 发现结果单独标记，不可用不阻塞群聊同步。
- 消息正文、OAuth 令牌、AI 密钥及派生总结使用各自加密域；正文、完整 prompt、密钥和令牌不进入普通日志或 Git。不保存附件二进制。
- 最新群成员快照包含稳定 ID、姓名和可用租户信息，不保存成员历史版本；API 未返回某人不等于已离群。
- 后台逐页权限、主人修改权限和正文读取分别校验。每请求查询当前权限，不将权限缓存到登录会话。写操作有 CSRF、版本/冲突校验、事务和审计；旧前台名单编辑入口已删除。
- 正文读取同时核对授权、同意、会话可见且启用、有效消息观察和删除状态；选用户或建立关联不新增权限。详细入口范围见后台权限文档。

## 4. 同步与并发

- OAuth 后回填可读取历史；上轮完成后等待 300 秒再做增量，重看前 600 秒避免边界遗漏。300 秒不是全量同步完成时限。重复消息按稳定消息标识去重。
- 会话目录分页按授权人顺序完成；消息分页用数据库 claim、租约、游标和 generation 保证同会话串行、不同会话可并行。消息及观察关系与游标在同一事务保存，重启可续跑。
- 每位授权人最多一个 coordinator；默认全局最多 3 个在途 Lark 请求、每秒 10 个，每人优先最多占 2 个会话槽位，其他人无等待时可借用剩余槽位。
- coordinator 数量不占用 HTTP 并发名额。会话目录由单独线程逐人准备；已准备账号按在途数、最久未推进顺序轮转，排队账号不必等大型账号整轮结束。原租约、代次和游标校验不变。
- 同步短写事务先取得数据库材料写锁；消息总结在名单/授权行锁之前取得对应读锁，避免会话与身份锁反序。锁随事务释放，所有网络抓取和模型请求均在锁外；既有授权与正文校验不变。
- 工作台状态卡每 10 秒读取一次，隐藏页面暂停轮询；计划等待、排队和限流不显示运行动画，只在实际同步或目录发现时转圈。
- 群成员刷新是低优先级任务，默认最多 1 个在途请求；消息任务优先。单会话临时失败单独退避，429 或 Lark 限流响应触发共享暂停，不紧密重试。
- 归档/解散会话保留发现事实，但不回填且不计入可同步分母；专题群按群聊展示。未来已排程的任务不等于故障或正在处理。
- 生产保持一个 worker 进程，内部线程池执行并发；不能单凭加进程就认为吞吐提高。

## 5. AI 与确认

### 当前队列

Worker 将运营助手与用户主动生成的回复放入独立的 2 个交互请求名额；名单导入、消息总结、handle、负责人、群分类、维护优先级和附件解析轮转使用 5 个后台名额，后台满载不占交互执行器。两组均用 PostgreSQL 租约限制跨进程在途数，共用当前 OpenAI 项目的服务商退避与欠费暂停，不设本地 RPM/Token 预算。准备线程独立于网络请求，桌面主动请求与群发发送仍独立。同步过期租约每分钟恢复一次，不随忙碌循环高频抢锁。

### OpenAI 调用入口

所有现行调用使用 `openai_client.py`，向同一个 Responses 接口发送单次请求并设置 `store: false`。同步、可取消异步和工具循环是三个调用方式，不是三套客户端；不叠加 SDK 自动重试。客户端统一构造请求、合并所有文字输出、提取安全用量／请求编号和识别服务商错误；业务模块保留各自材料、prompt、人工事实及入库校验。

| 功能配置 | 调用模块 | 触发与执行 |
|---|---|---|
| `creator_import` | `creator_imports` | 用户确认导入批次后调用；没有自动启用开关，数据库 `enabled=false` 不等于禁止已明确启动的导入 |
| `creator_handle_matching` | `creator_handle_matching` | 已启用的达人—账号匹配队列 |
| `creator_responsibility_matching` | `creator_responsibility_matching` | 已启用的达人—负责人匹配队列 |
| `creator_group_classification` | `creator_group_matching` | 已启用的群用途分类队列 |
| `creator_importance` | `creator_importance` | 已启用的维护优先级评估队列 |
| `offhours_summary` | `summary_dialogues`、`summary_replies` | 固定时段总结及用户主动起草／翻译；两条链路共用此配置 |
| `operations_assistant` | `assistant` | 用户提问后按工具循环查询、分段总结、回复；`assistant_window` 和 `assistant_replies` 不另开 API 链路 |
| `desktop_reply` | `desktop_replies` | 用户主动提交文字的可取消异步请求，不占后台 5 个名额 |

八份现行配置、九条业务调用链。历史 `contact_assessment`、`contact_assignment`、`creator_workflow` 配置与旧请求数据只保留，不恢复生产调用或扫描入口。模型和 Key 均由现有配置决定，代码发布不更换或开启它们。

各链路保留 API 实际返回的可用用量和安全回执；输出不完整或校验失败也保留这些计数与请求编号。没有返回用量时，不能据空值或零计数断言未计费。总结与回复建议先用现有加密检查点保存当前响应，再本地解析、校验和落地；本地保存失败后重放响应，不重复付费，完成后清除待处理响应。相同调用回执只累计一次，撤权或任务失效后仍不能展示旧结果。

明确的临时 HTTP 错误沿用队列重试上限和 `Retry-After`；账单／配额、网络结果未知及已返回但无法使用的输出不自动重发。匹配队列每次只计算一次退避时间，共享暂停与任务等待使用同一数值。桌面失败日志只记录白名单错误、编号、用量和耗时，不记录正文、完整返回、prompt 或密钥；成功用量保存在原回执，成功诊断日志取决于部署的 INFO 日志级别。

名单使用 `creator_imports`、`creator_import_evidence`、`creator_roster`；取证共用 `evidence_text`、`evidence_access`、`handle_evidence`。模型判断聊天含义，后端核对当前授权、引用、材料版本、人工事实及关系冲突。群名结合成员与聊天可构成强证据，Curva 仅作辅助目录。

相同输入复用结果，不为已有正式账号重复付费。明确 HTTP 临时错误有限重试；网络结果未知或运行租约失效不自动重发可能已计费请求。历史请求和成功分段保留，未变化材料继续复用。

维护优先级按历史顺序累积事实，结果未知会冻结该达人与 CM 的评估批次，后续消息保留待处理；当前没有网页恢复入口，需先核实上游结果再人工恢复。旧总结试运行同样不允许用保存配置绕过运行中或未知请求；关闭功能仍可正常执行。

旧泛扫身份、旧负责人和反向 handle 三阶段已退役，不进入调度或并发统计。历史密文域和旧外键用于读取及审计，原迁移不可重放。旧后台路径返回 404；少量历史操作返回无写入的 410，POST 仍校验登录与 CSRF。

## 6. 姓名和 @

`name`、`en_name` 保留 Lark 来源字段，`display_name` 是派生显示。OAuth/当前有效个人资料优先，旧消息与旧提及只补缺值；英文名字段缺失保留旧值，明确返回空值才清除。

`identity_names` 统一页面显示及可核验 CM 别名；单身份联系人可同步标签，多身份联系人保留人工标签。名称变化不合并身份、不修改归属。

`message_mentions` 只按同条消息保存的明确身份映射还原 `@_user_N`，同时处理正文/富文本及中文相邻字符。未知或冲突不猜，URL、代码和邮箱中的同样字符不替换。先检查阅读权限，再解析和展示。

维护脚本 `backend/scripts/backfill_identity_names.py` 默认只检查；显式 `--apply --owner-id …` 才写入，`--refresh-members` 可在已有权限内刷新群成员。实时资料不足时保留已有姓名，不扩大权限、不重跑达人 AI。请求前后检查授权与稳定 ID，失败不阻塞消息同步。

### 人员头像

头像缓存放在 `lark_identities`，以 `open_id` 为键，保存官方 URL、检查/更新时间与重试状态；不按姓名合并，不下载到自建图片服务。OAuth 本人资料与后台补齐使用同一保存逻辑。目录从所展示的主身份读取缓存，图片加载成功才覆盖姓名首字母；无权限、空值和坏图不产生破图，也不在页面触发资料同步。

后台刷新独立于 OpenAI，沿用现有 Lark 总并发/速率门控，并对头像请求单独限速；每天更新，失败退避，权限恢复后再次尝试。所有外部联系人请求都必须有当前有效授权及可见会话依据，请求返回后再次检查。接口返回的 ID 不匹配时禁止保存；不保存未经验证的非 HTTPS 或非官方 CDN 地址。

2026-09-11 上线前小批验证（3 位授权 CM、3 位内部同事、3 位外部达人）：

| 样本 | 本人资料 / 用户令牌 | 应用令牌查询通讯录 |
|---|---|---|
| 已授权 CM × 3 | 本人 `user_info` 均返回对应 ID 和头像 | 无需替代本人接口 |
| 内部同事 × 3 | `99991679`，缺通讯录基础读取 scope | 返回 ID，但没有头像字段 |
| 外部达人 × 3 | `99991679`，缺通讯录基础读取 scope | `41050`，不在可访问通讯录范围 |

实测头像域为 `s16-imfile-sg.feishucdn.com`。通讯录接口权限 `contact:contact.base:readonly` 与头像字段权限 `contact:user.base:readonly` 是两层要求，返回 200 不保证返回头像。外部人员还受可访问范围限制；不得用 IM 读取权限推导完整资料权限。发布不自动扩大应用权限、不重新触发 OAuth。[Lark 官方用户信息接口](https://open.larksuite.com/document/server-docs/contact-v3/user/get)

## 7. 已移除的旧入口

后台达人目录、归属复核、专用聊天详情和整理清单已下线。此次继续移除 `/dashboard/creators`、全部配套 API、旧 AI 配置入口、负责人资格编辑及联系人分配接口；旧地址不可再启动或修改名单。

036 迁移处理的是早期页面权限；046 不删除权限或业务数据。现有系统主人、后台页面授权与消息权限均保留。旧测试编辑工程随旧前台移除，新前台按上述 CM 自己与主人管理规则运行。历史代码从 Git 恢复；此前数据清理仅为 [历史记录](../status/2026-09-10-reset-and-docs.md)，不重复执行。

运行配置、发布、备份和恢复统一见 [部署手册](../runbooks/deploy-mvp.md)，不在这里复制命令。
