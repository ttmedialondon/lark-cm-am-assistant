# 系统技术说明

本文记录后端、人员模型、姓名机制与后台。业务以 [人员身份与达人名单](../product/creator-management.md)、[消息总结](../product/message-summary.md)、[后台权限](../product/admin-access.md) 为准。2026-09-11：第一步识别身份，第二步判断负责人，第三步核实账号再入库；旧工程不恢复，消息总结维持关闭，已有数据保留。

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
| `contact_assessments`、`contact_assessment_refs` | 全员身份任务、规则版本、受控结论、本人消息引用与正文指纹；不是 CM 归属 |
| `organizations`、`organization_tenants`、`contact_organization_links` | 组织及可验证租户/人员关系；租户相同不等于达人、员工或负责人 |
| `social_accounts`、`contact_social_accounts` | 平台账号及关联用户；一个用户可以关联多个 handle |
| `creator_workflow_candidates`、`creator_workflow_sources`、`creator_workflow_requests` | 保留的旧线索、来源和请求历史；无现行写入入口 |
| `creator_account_assignments` | 旧账号级正式归属，继续作为冲突和旧名单读取依据；新版不往旧名单表写入 |
| `contact_assignments` | 人级负责人；当前人工入库事务与账号绑定同时完成，引用 `import_row_id`；旧 AI 任务引用保留为历史 |
| `creator_ownership_runs`、`creator_ownership_tasks`、`creator_ownership_refs` | 历史负责人扫描批次、人×CM任务/受控结论（已退役）、授权绑定且冻结的消息引用；输入与派生说明加密，原文仍来自原存储 |
| `creator_handle_runs`、`creator_handle_tasks` | 独立旧 handle 批次和任务历史；当前 CAP 页面不读取其候选，扫描入口已停用 |
| `creator_import_batches`、`creator_import_rows` | 单列 CAP Excel 批次与账号行；分别记录上传人和所选 CM。预览存于批次，确认导入后同步创建用户可空的账号行；人工确认用户及冲突后入库。用 `import_row_id` 保留来源，不创建空壳联系人，不进入 AI 队列 |
| `creator_workflow_ai_runs`、`creator_workflow_ai_tasks`、`creator_workflow_onboarding` | 旧扫描结果、引用与接入记录，仅保留历史，未完成项已取消 |
| `offhours_summary_settings`、`offhours_summary_runs`、`offhours_summary_tasks` | 指定总结时段、执行版本和会话结果 |
| `admin_roles`、`admin_page_permissions`、`audit_events` | 主人身份、逐页权限、必要的查看/修改审计 |

现有人员类别复用 creator、partner_contact、internal_colleague、merchant_contact、platform_contact、other_external_contact 六个代码。人工已确认身份优先于可信内部同事事实，后者优先于新 AI 类别；人员类别不改变原始租户事实或后台权限。角色、所属组织和负责人是独立事实。达人 CAP 属性保存于 `contacts.creator_type`，值为 `cap`／`non_cap`／`pending`，默认 `pending`；一个人多个账号共用同一属性。当前 Excel 导入固定 CAP，人工确认用户并入库后写到联系人；已有相反属性时提示冲突。底层属性枚举保留，当前前台不提供非 CAP 导入及属性切换。

## 3. 授权与正文边界

- Lark 使用 CM 自己的 user access token，读取 API 实际返回且其本来有权查看的范围。用户权限为 `offline_access`、`im:chat:read`、`im:message:readonly`、`im:chat.members:read`。
- 旧的仅统计授权不能静默升级。只有记录正文同意、且该来源在同意后重新观察到的消息，才可按有效范围展示正文。
- 群和私聊共享底层存储；私聊对端身份记录在授权来源的会话关系上。P2P 发现结果单独标记，不可用不阻塞群聊同步。
- 消息正文、OAuth 令牌、AI 密钥及派生总结使用各自加密域；正文、完整 prompt、密钥和令牌不进入普通日志或 Git。不保存附件二进制。
- 最新群成员快照包含稳定 ID、姓名和可用租户信息，不保存成员历史版本；API 未返回某人不等于已离群。
- 后台逐页权限、主人修改权限和正文读取分别校验。每请求查询当前权限，不将权限缓存到登录会话。写操作有 CSRF、版本/冲突校验、事务和审计；旧前台名单编辑入口已删除。
- 正文读取同时核对授权、同意、会话可见且启用、有效消息观察和删除状态；选用户或建立关联不新增权限。详细入口范围见后台权限文档。

## 4. 同步与并发

- OAuth 后回填可读取历史；常规每 300 秒增量，重看前 600 秒避免边界遗漏。重复消息按稳定消息标识去重。
- 会话目录分页按授权人顺序完成；消息分页用数据库 claim、租约、游标和 generation 保证同会话串行、不同会话可并行。消息及观察关系与游标在同一事务保存，重启可续跑。
- 每位授权人最多一个 coordinator；默认全局最多 3 个在途 Lark 请求、每秒 10 个，每人优先最多占 2 个会话槽位，其他人无等待时可借用剩余槽位。
- 群成员刷新是低优先级任务，默认最多 1 个在途请求；消息任务优先。单会话临时失败单独退避，429 或 Lark 限流响应触发共享暂停，不紧密重试。
- 归档/解散会话保留发现事实，但不回填且不计入可同步分母；专题群按群聊展示。未来已排程的任务不等于故障或正在处理。
- 生产保持一个 worker 进程，内部线程池执行并发；不能单凭加进程就认为吞吐提高。

## 5. AI 与确认

### 当前：CAP 名单匹配与消息总结

2026-09-27 退役全员身份、独立负责人、反向 handle 和旧三阶段识别。Worker 仅轮转 CAP 导入、消息总结、回复建议三个 AI 队列；本地准备仅轮转 CAP 取证与总结准备。旧任务不再进入调度或并发统计。

CAP 使用 `creator_imports`、`creator_import_evidence`、`creator_roster`；共用取证能力收敛到 `evidence_text`（安全解密/脱敏）、`evidence_access`（授权消息范围/指纹）、`handle_evidence`（规范化/群名和消息线索）。这些模块没有旧扫描器、模型提示词或旧确认写入逻辑。

迁移 070 取消旧活动任务、停用旧开关，将既有加密模型配置复制到 `creator_import`，不轮换密钥、不启用自动扫描。CAP 批次仍显式启动。既有密文域、正式关系、历史证据和旧外键保持；原迁移不可重放或删除。`admin_reporting` 仅只读历史身份结论来验证既有分类来源，不再展示或统计旧执行进度。

旧后台路径移除（404），历史 handle 操作保留无写入的停用响应（410），POST 仍校验登录与 CSRF。未来全量 handle 识别不在此次发布中。

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
