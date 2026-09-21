# 后端 API 与方法链审查

日期：2026-09-21  
代码基线：`f0dee27`  
范围：本地仓库 `backend/app`。只读审查，未修改业务代码、数据库或生产配置。

## 1. 结论

当前不需要重写后端，也不建议为了“统一”而把所有操作合成一个万能接口。现有 FastAPI + PostgreSQL + 后台任务的结构可以继续使用。

优先做三类工作：

1. **去掉确定的重复工作**：重复登录查询、逐人查库、调度中的全表计数。
2. **理顺已有共用能力的归属**：OpenAI 调用、消息文本解析、HTTP 响应工具、达人访问规则。
3. **把新旧流程的边界说清楚**：部分旧入口虽然停用，但旧后台链路仍被引用，需完整退役，不能只删除路由。

本次核对结果：

| 范围 | 数量 | 口径 |
| --- | ---: | --- |
| HTTP 路由 | 59 | 按“方法 + 路径”计数，已通过 app.routes 核对 |
| GET | 29 | 包含 HTML 页面、JSON、重定向、下载和健康检查 |
| POST | 30 | 包含配置、任务操作、人工核对及停用入口 |
| 明确返回 410 的入口 | 7 | 登录和 CSRF 校验通过后返回；已经包含在上述 59 个中 |
| 另一个拒绝旧操作的入口 | 1 | ownership 的 confirm，返回 400；不是有效的确认操作 |
| Python 文件 | 33 | backend/app 顶层文件，含 __init__.py |
| 函数/方法定义 | 613 | 含类方法、闭包、嵌套路由函数 |

另有 `/static` 静态挂载，不计为单个业务 API。应用明确关闭了 `/docs`、`/redoc`、`/openapi.json`，不能依靠在线 Swagger 枚举接口。

**核验限制：**本轮是源码审查及应用路由注册核对，没有进行生产压测，也没有重新执行测试套件。下面的耗时风险是代码依据，不是本轮测出的毫秒收益。完整静态方法清单见 [后端方法索引](backend-method-index_2026-09-21.md)。

## 2. 优先改什么

### P1：异步接口里的同步查库，先纠正执行位置

证据：

- `admin.py:893` 的异步联系人识别入口直接查询身份并调用同步业务方法。
- `admin.py:925` 的异步组织确认入口直接查库、执行同步组织确认。
- `creator_handles_routes.py:38` 的异步表单入口直接调用 actor；actor 内会同步查询会话。
- `offhours_summary_routes.py:101` 在移交业务任务到线程池前，先同步执行 reply_actor。
- `db.py:15` 明确使用同步 psycopg 连接。

风险：在 async 函数里直接运行这些同步函数，不会因为外层是 async 就自动进入线程池。数据库慢时，会占住处理该请求的事件循环，可能影响同一进程其他请求。

建议：保留同步数据库栈；异步读取请求体后，把完整的同步鉴权/事务/业务工作交给 `run_in_threadpool`。上传等需要先鉴权的入口，鉴权也通过线程池执行。连接和事务在同步工作单元内创建、结束，不跨线程传递活动连接。

不建议为此把整个项目改为异步 ORM。验收要覆盖并发请求和授权撤销，不只测单请求成功。

### P1：摘要准备阶段不应靠两次全表 COUNT 判断进展

证据：`offhours_summary.py:970–977` 的 `prepare_next_summary`：

```text
COUNT 全部 offhours_summary_tasks
→ schedule_if_due
→ schedule_merges
→ 再 COUNT 全部 offhours_summary_tasks
→ 比较前后数量
```

建议让两个调度函数返回“本次新增/推进了多少任务”，调用方据此判断进展。按当前语义，优先返回实际新增数；如要纳入状态推进，应单独定义并测试，避免改变调度节奏。

收益：移除每次执行时确定存在的两次全表计数，并避免用全局数量猜本次工作成果。不增加缓存或计数表。实际性能收益仍需按生产任务量测量。

### P1：受限消息页面存在逐来源查库

证据：`admin.py:523–525` 对 `source_limit` 中每个人调用 `admin_reporting.message_user`；后者在 `admin_reporting.py:377` 独立打开连接、查询。

建议给现有人员读取方法增加“限定来源 ID 集合”的批量读取能力，一次查询取回允许展示的人员；当前选中人员若已在结果中，复用其基础信息。

收益：这部分由 N 次来源读取改为一次。**只影响配置了来源限制的分支**，不能将其描述为所有 Owner 请求都有 N+1。

保留页面权限、来源范围和正文授权校验；空集合必须返回空，不能被解释为不限制。

### P2：同一页面请求重复查询登录状态

证据：

- `creator_handles_routes.py:48–51`：页面先 current_authorization，再 actor 内查一次。
- `offhours_summary_routes.py:59–64`：页面先 current_authorization，再 read 内查一次。

建议一次取出登录态，在当前请求中向下传递。已有同步 service 可以接收 actor，而不需要重复取 Cookie。

这里只消除同一请求入口的重复读取。后台任务领取、付费调用前、结果入库前、长下载过程中的授权复核仍需保留；不要改为跨请求长期缓存用户权限。

### P2：OpenAI 通用客户端已存在，但放错了模块

证据：`contact_assessments.py:1034` 的 call_openai 已被 ownership、handles、imports、summary_dialogues 使用；summary_replies 还借用该模块的异常和重试工具。摘要与其他业务引用了联系人识别模块的私有方法。

建议把**现有实现原样搬出**为小型 `openai_client.py`：

- 请求、响应解析、用量及错误记录。
- 提供商请求异常、Retry-After 解析、退避计算。
- 清晰的公共函数名。

各业务继续保留自己的输入证据、输出 schema、结果校验、授权复核和任务重试政策。`ai_dispatch.py` 已负责共享并发/限流，不再造一套。

这是依赖关系整理，不是“六套 OpenAI 请求重写成一套”——请求发送本来就共用了。先不做多供应商适配器或通用 Agent 框架。当前使用顶层 httpx.post，若后续测得建连成本明显，再考虑复用 Client；它不是本轮已证实的瓶颈。

### P2：从路由和旧业务模块抽出真正共用的小工具

可做两组小幅整理：

| 当前依赖 | 建议 |
| --- | --- |
| creators、offhours 路由从 admin 导入 _csrf_matches / _form_fields | 抽到小型 HTTP 工具模块，避免前台依赖后台路由 |
| 多处重复 no-store、JSON 编码、异常映射、ETag | 复用响应辅助函数；允许页面专属 CSP，保留 HTML 重定向和 JSON 错误差异 |
| creator_imports 多处调用 creator_handles._access | 抽出达人领域访问规则，而不是让新导入功能依赖旧 handle 扫描模块 |
| 多业务借用 ownership._uuid、_encode、_audit 等工具 | 先区分领域规则与无业务工具，再移到真正共享的位置 |

不要把管理台页面授权、达人管理权限、消息来源权限合成一个 `is_admin`。这几类能力并不等价。也不需要为了减少少量重复创建通用 BaseService 或 Repository 基类。

### P2：消息解析层不应挂在报表层或联系人识别层

证据：`contact_assessments.py:250` 从 admin_reporting 引入展示正文解析；`offhours_summary.py:374` 又使用 contact_assessments._readable_body。报表、识别、摘要对正文有不同用途。

建议共用底层 Lark 文本结构解析和 mention 解析；展示格式、AI 证据截断、脱敏和导出规则留在各自调用方。

不能简单将同名解析函数全部替换为一个版本：要覆盖富文本、换行、@人、非文字消息、空正文、损坏 JSON。目标是减少底层重复，不是让 AI 输入和用户显示强行一致。

### P2：旧达人流程需要“退役清单”，不是直接删模块

证据：

- `creator_handles_routes.py:63–75` 的 7 个入口已返回 410。
- `worker.py:221–224` 仍会领取并执行 handle 任务。
- `worker.py:259–260` 仍会准备 handle 证据。
- `creator_handles.py:494` 仍会从已完成 ownership 运行中播种旧任务。
- 新导入还调用旧模块的权限方法；正式达人页面仍使用 page_data。
- 旧候选的 confirm、defer、evidence 入口仍存在。
- ownership 的 confirm 路由明确拒绝操作，但仍保留地址。

因此不能宣称“这些方法已无调用”。建议先检查真实待处理队列、自动入库开关、旧页面调用、脚本和测试依赖；确定是否完全转向 Excel CAP 导入，再依次停止旧任务生产、处理存量任务、移走共用权限与名单读取、删除无调用执行路径。

410 兼容入口成本很小，短期保留合理；不必为了路由数好看先删。历史数据也不在本轮建议删除范围内。

### P3：模块可按领域拆，但先不要升级技术栈

当前 worker 2747 行、admin_reporting 1901 行、contact_assessments 1695 行、creator_imports 1313 行、admin 1303 行。行数大只说明导航成本，不等于性能慢。

在前述工作完成后，可将：

- admin 拆成消息、人员/联系人、AI 配置等路由文件。
- admin_reporting 按真实查询领域分组，不引入逐表 Repository。
- worker 保留主调度器，把 Lark 抓取/落库与身份补齐迁出。
- creator_handles 中的正式达人列表与旧扫描器分开。

先按现有函数边界搬移，避免搬文件时同时修改 SQL 和业务结果。

### 需要测量后再决定的升级

1. **数据库连接池**：当前每次 connection 都新建连接；先减少 N+1 和重复鉴权，再统计建连时间、并发连接数。确有压力时加有上限的小池，不直接引入 Redis 或新数据库。
2. **ETag 前置轻量版本检查**：当前摘要和 ownership 的 ETag 在完整读取之后生成，304 节省响应内容和前端渲染，但不免除此前查库/证据校验。只有具备覆盖授权、证据、配置、任务结果的可靠版本号时，才考虑更早返回。
3. **队列领取开销**：worker 轮询六类 AI 队列，需测空转查询量再考虑降低无工作时的检查频率；不要合并业务状态机来省几次函数调用。

## 3. 完整 HTTP 路由表

公共规则：下表省略重复的 connection、参数校验、错误分支和渲染工具。箭头是主要业务顺序，不代表穷尽每条分支。完整直接调用表达式另见方法索引。

- 管理台：session → admin_permissions → 页面/Owner 限制；消息另校验来源及正文授权。
- 达人前台：session → creator_handles / creator_imports 的达人领域权限；写操作包含 CSRF。
- 摘要：session → 时段选择 → 摘要人员/角色/正文材料权限；回复只是生成草稿，不是发消息。
- 59 个路由不是 59 个公共 JSON API；HTML 页面、下载、认证回调都包含在内。


### backend/app/main.py

| 方法 | 路径 | 处理链（省略公共数据库连接） |
| --- | --- | --- |
| GET | `/healthz` | [healthz](../../backend/app/main.py#L53) → database_is_ready → PostgreSQL SELECT 1；JSON/503 |
| GET | `/` | [index](../../backend/app/main.py#L65) → _current_authorization → 登录/工作台重定向 |
| GET | `/auth/lark/start` | [start_lark_authorization](../../backend/app/main.py#L73) → oauth_states + PKCE → LarkClient.authorization_url → 重定向 |
| GET | `/auth/lark/callback` | [lark_callback](../../backend/app/main.py#L113) → exchange_code → user_info → 授权/会话/同步任务入库 → 重定向 |
| GET | `/dashboard` | [dashboard](../../backend/app/main.py#L295) → _current_authorization → admin_permissions.access_for → _dashboard_stats → HTML |
| GET | `/dashboard/stats` | [dashboard_stats](../../backend/app/main.py#L340) → _current_authorization → _dashboard_stats → 统计 HTML 片段 |

### backend/app/admin.py

| 方法 | 路径 | 处理链（省略公共数据库连接） |
| --- | --- | --- |
| GET | `/admin` | [admin_overview](../../backend/app/admin.py#L309) → access → admin_reporting.overview → HTML |
| GET | `/admin/users` | [admin_users](../../backend/app/admin.py#L321) → access → admin_reporting.users + users_access_data → HTML |
| POST | `/admin/users/{authorization_id}/permissions` | [admin_set_page_permissions](../../backend/app/admin.py#L344) → owner_access + CSRF → admin_permissions.set_permissions → 重定向 |
| GET | `/admin/users/{authorization_id}` | [admin_user_detail](../../backend/app/admin.py#L379) → access → user_detail + access_for(目标人员) → HTML |
| GET | `/admin/chats` | [admin_chats](../../backend/app/admin.py#L439) → access → admin_reporting.chats → HTML |
| GET | `/admin/messages/export` | [export_messages](../../backend/app/admin.py#L456) → access + 来源授权 → stream_export → _write_export → Excel/ZIP 流 |
| GET | `/admin/messages` | [admin_messages](../../backend/app/admin.py#L498) → access + 来源范围 → message_users/message_user → message_conversations → message_conversation → message_page + record_message_view → HTML |
| GET | `/admin/ai` | [admin_ai_config](../../backend/app/admin.py#L652) → access → render_ai_config → 配置 HTML |
| GET | `/admin/ai/creator-ownership/data` | [admin_creator_ownership](../../backend/app/admin.py#L669) → access → creator_ownership.page_data → HTML 或 JSON/ETag |
| GET | `/admin/ai/creator-ownership` | [admin_creator_ownership](../../backend/app/admin.py#L669) → access → creator_ownership.page_data → HTML 或 JSON/ETag |
| POST | `/admin/ai/creator-ownership/preview` | [admin_creator_ownership_preview](../../backend/app/admin.py#L699) → owner_access + CSRF → creator_ownership.create_preview → 入队 |
| POST | `/admin/ai/creator-ownership/{run_id}/defer` | [admin_creator_ownership_action](../../backend/app/admin.py#L717) → owner_access + CSRF → creator_ownership.defer_tasks → JSON |
| POST | `/admin/ai/creator-ownership/{run_id}/confirm` | [admin_creator_ownership_action](../../backend/app/admin.py#L717) → owner_access + CSRF → 明确拒绝旧确认方式，400；不入库 |
| POST | `/admin/ai/creator-ownership/{run_id}/resume` | [admin_creator_ownership_action](../../backend/app/admin.py#L717) → owner_access + CSRF → creator_ownership.resume_run → JSON |
| POST | `/admin/ai/creator-ownership/{run_id}/pause` | [admin_creator_ownership_action](../../backend/app/admin.py#L717) → owner_access + CSRF → creator_ownership.pause_run → JSON |
| POST | `/admin/ai/creator-ownership/{run_id}/start` | [admin_creator_ownership_action](../../backend/app/admin.py#L717) → owner_access + CSRF → creator_ownership.start_run → JSON |
| GET | `/admin/ai/creator-ownership/tasks/{task_id}/evidence` | [admin_creator_ownership_evidence](../../backend/app/admin.py#L762) → owner_access → creator_ownership.task_evidence → JSON |
| GET | `/admin/contacts` | [admin_contacts](../../backend/app/admin.py#L774) → access → admin_reporting.contacts → HTML |
| GET | `/admin/contacts/{open_id}` | [admin_contact_detail](../../backend/app/admin.py#L818) → access → contact_detail；Owner 另取 latest_contact_assessment → HTML |
| POST | `/admin/contacts/{open_id}/assessments` | [admin_continue_contact_assessment](../../backend/app/admin.py#L892) → owner_access + CSRF → 身份查找 → requeue_contact_assessment → 重定向 |
| POST | `/admin/contacts/{open_id}/organization` | [admin_confirm_contact_organization](../../backend/app/admin.py#L924) → owner_access + CSRF → 身份查找 → manually_confirm_contact_organization → 重定向 |
| GET | `/admin/contacts/{open_id}/assessments/{assessment_id}` | [admin_contact_assessment](../../backend/app/admin.py#L995) → owner_access → render_contact_assessment → assessment_detail/归属核对/审计 → HTML |
| POST | `/admin/contacts/{open_id}/assessments/{assessment_id}/roles` | [admin_confirm_assessment_role](../../backend/app/admin.py#L1023) → owner_access + CSRF → assessment_matches_contact → confirm_assessment_role → HTML/重定向 |
| POST | `/admin/contacts/{open_id}/assessments/{assessment_id}/dismiss` | [admin_dismiss_contact_assessment](../../backend/app/admin.py#L1092) → owner_access + CSRF → assessment_matches_contact → dismiss_assessment → HTML/重定向 |
| POST | `/admin/ai` | [admin_save_ai_config](../../backend/app/admin.py#L1157) → owner_access + CSRF → save_feature_config / save_settings_and_enqueue → HTML/重定向 |
| POST | `/admin/ai/resume` | [admin_resume_ai_queue](../../backend/app/admin.py#L1240) → owner_access + CSRF → ai_dispatch.resume → 重定向 |
| GET | `/admin/jobs` | [admin_jobs](../../backend/app/admin.py#L1266) → access → admin_reporting.jobs → HTML |

### backend/app/offhours_summary_routes.py

| 方法 | 路径 | 处理链（省略公共数据库连接） |
| --- | --- | --- |
| GET | `/dashboard/offhours-summary` | [page](../../backend/app/offhours_summary_routes.py#L58) → read → summary_periods.selection → get_summary_view → HTML |
| GET | `/dashboard/offhours-summary/data` | [data](../../backend/app/offhours_summary_routes.py#L78) → read → selection → get_summary_view → JSON/ETag |
| POST | `/dashboard/offhours-summary/replies` | [reply_create](../../backend/app/offhours_summary_routes.py#L100) → reply_actor + CSRF → summary_replies.enqueue → 202 |
| GET | `/dashboard/offhours-summary/replies/{job_id}` | [reply_read](../../backend/app/offhours_summary_routes.py#L142) → reply_actor → summary_replies.read → JSON；资料冲突 409 |

### backend/app/creator_handles_routes.py

| 方法 | 路径 | 处理链（省略公共数据库连接） |
| --- | --- | --- |
| GET | `/creators/data` | [page](../../backend/app/creator_handles_routes.py#L45) → actor → creator_handles.page_data → creator_imports.page_data/正式达人查询 → HTML/JSON |
| GET | `/creators` | [page](../../backend/app/creator_handles_routes.py#L45) → actor → creator_handles.page_data → creator_imports.page_data/正式达人查询 → HTML/JSON |
| POST | `/creators/people/{contact_id}/type` | [retired_action](../../backend/app/creator_handles_routes.py#L63) → actor + CSRF → 410；不启动原业务 |
| POST | `/creators/tasks/{task_id}/recheck` | [retired_action](../../backend/app/creator_handles_routes.py#L63) → actor + CSRF → 410；不启动原业务 |
| POST | `/creators/runs/{run_id}/auto-admit` | [retired_action](../../backend/app/creator_handles_routes.py#L63) → actor + CSRF → 410；不启动原业务 |
| POST | `/creators/runs/{run_id}/resume` | [retired_action](../../backend/app/creator_handles_routes.py#L63) → actor + CSRF → 410；不启动原业务 |
| POST | `/creators/runs/{run_id}/pause` | [retired_action](../../backend/app/creator_handles_routes.py#L63) → actor + CSRF → 410；不启动原业务 |
| POST | `/creators/runs/{run_id}/start` | [retired_action](../../backend/app/creator_handles_routes.py#L63) → actor + CSRF → 410；不启动原业务 |
| POST | `/creators/preview` | [retired_action](../../backend/app/creator_handles_routes.py#L63) → actor + CSRF → 410；不启动原业务 |
| POST | `/creators/tasks/{task_id}/defer` | [review](../../backend/app/creator_handles_routes.py#L77) → actor + CSRF → creator_handles.confirm_task / defer_task → JSON |
| POST | `/creators/tasks/{task_id}/confirm` | [review](../../backend/app/creator_handles_routes.py#L77) → actor + CSRF → creator_handles.confirm_task / defer_task → JSON |
| GET | `/creators/tasks/{task_id}/evidence` | [evidence](../../backend/app/creator_handles_routes.py#L98) → actor → creator_handles.task_evidence → JSON |
| POST | `/creators/imports/preview` | [import_preview](../../backend/app/creator_handles_routes.py#L106) → actor + CSRF + 文件限制 → creator_imports.preview_import → parse_xlsx/预览批次入库 |
| GET | `/creators/imports/data` | [import_data](../../backend/app/creator_handles_routes.py#L133) → actor → creator_imports.page_data → JSON |
| GET | `/creators/imports/users` | [import_users](../../backend/app/creator_handles_routes.py#L141) → actor → creator_imports.search_users → JSON |
| POST | `/creators/imports/{batch_id}/recheck` | [import_action](../../backend/app/creator_handles_routes.py#L149) → actor + CSRF → creator_imports.recheck_batch → JSON |
| POST | `/creators/imports/{batch_id}/resume` | [import_action](../../backend/app/creator_handles_routes.py#L149) → actor + CSRF → creator_imports.resume_import → JSON |
| POST | `/creators/imports/{batch_id}/pause` | [import_action](../../backend/app/creator_handles_routes.py#L149) → actor + CSRF → creator_imports.pause_import → JSON |
| POST | `/creators/imports/{batch_id}/start` | [import_action](../../backend/app/creator_handles_routes.py#L149) → actor + CSRF → creator_imports.start_import → JSON |
| GET | `/creators/imports/rows/{row_id}/evidence` | [import_evidence](../../backend/app/creator_handles_routes.py#L167) → actor → creator_imports.row_evidence → JSON |
| POST | `/creators/imports/rows/{row_id}/defer` | [import_review](../../backend/app/creator_handles_routes.py#L175) → actor + CSRF → creator_imports.confirm_row / defer_row → JSON |
| POST | `/creators/imports/rows/{row_id}/confirm` | [import_review](../../backend/app/creator_handles_routes.py#L175) → actor + CSRF → creator_imports.confirm_row / defer_row → JSON |


## 4. 后台完整主链路

### 4.1 登录与消息同步

```text
OAuth callback
  → 校验 state/PKCE
  → Lark 换 token / 读取个人信息
  → 加密 token、更新授权、建立 Web 会话、排入同步任务
worker.run_forever
  → _start_pending_jobs → _prepare_job → _discover_chats / _discover_p2p
  → _fill_lark_slots → _dispatch_lark_turn → _claim_next_page
  → _usable_access_token（需要时 refresh_token）
  → executor / _fetch_claim → LarkClient.list_chat_messages
  → _reap_page_futures → _record_claimed_page
  → 消息去重、正文加密、来源观察、发送者身份、游标推进
  → _finish_completed_jobs → _finish_and_schedule_incremental
```

成员旁路：`_schedule_member_snapshot → _claim_member_snapshot → _read_member_snapshot → _reap_member_futures → _store_claimed_member_snapshot`。

身份旁路：`identity_profiles.fill_slots` 补齐允许读取的头像/资料；`organization_sync.sync_if_due` 同步协作组织。各自失败不应阻断所有消息采集。

恢复链：过期租约恢复、限流、重试、token 刷新、失效授权处理。这里的“多次校验”大部分是防止过期任务写入，不应按重复代码删除。

### 4.2 AI 与证据准备

统一入口：

```text
run_forever
  ├─ _schedule_creator_preview → _prepare_creator_preview
  │    ├─ ownership.prepare_next_preview
  │    ├─ handles.prepare_next_preview
  │    ├─ imports.prepare_next_import
  │    └─ offhours_summary.prepare_next_summary
  └─ _fill_ai_slots → _dispatch_ai_turn → 各队列 claim → executor.submit(runner)
```

| 业务队列 | 领取 → 执行 | 主要处理 |
| --- | --- | --- |
| 联系人识别 | claim_next_contact_assessment → run_claimed_contact_assessment → _run | 准备证据 → AI 分类 → 引用/阈值校验 → 保存结果及允许的决策 |
| 负责人建议 | claim_next_ownership → run_claimed_ownership | 互动证据 → AI 建议 → 结果校验 → 保存候选 |
| 旧 handle 流程 | claim_next_handle → run_claimed_handle | 所属候选材料 → handle 证据 → 结果保存；旧准备链仍在 |
| Excel CAP 导入 | claim_next_import → run_claimed_import | 名单预览 → 准备群/人材料 → AI 找群/找人 → 链路校验 → 人工或规则允许的入库 |
| 时段摘要 | claim_next_summary → run_claimed_summary → summary_dialogues.run/step | 会话材料切分 → 分段抽取 → 合并任务 → 事件报告 |
| 回复草稿 | summary_replies.claim → run → step | 当前事件/资料 → 草稿生成或后续修改 → 保存供人查看 |

并非所有 AI 结果都严格“只能人工确认”：现代码包含受规则限制的自动分类、导入入库分支。这里只报告代码能力，不代表这些开关现在在生产启用。

共享控制：`ai_dispatch.can_claim / throttle / pause_for_quota`；共享 HTTP 发送：`contact_assessments.call_openai`。不建议把六套不同的材料校验和状态迁移合并为一个万能工作流引擎。

### 4.3 消息查看与下载

```text
消息页面
  → 登录/页面权限 → 允许的消息来源
  → 人员 → 会话列表 → 选中会话 → 分页正文 → 查看审计

下载
  → 登录/页面权限/来源授权
  → StreamingResponse → stream_export → _write_export
  → 数据库游标分批读消息 → 解密/文字转换
  → 每会话一个 Excel → 写入 ZIP → 逐步输出
  → 下载过程中重新检查访问权 → 审计
```

当前已经是流式 ZIP，不需要重新引入“先生成完整压缩包，再开始下载”的路径。流式传输不保证打开连接后立即有大量字节，每个会话的生成时间仍会影响首批及后续输出；应分别测首字节和总耗时。

### 4.4 摘要读取与回复

```text
摘要 page/data → read → summary_periods.selection
  → get_summary_view → _authorize / _roster / _related_chats
  → summary_dialogues.view_events / merge_input → 当前证据有效性 → 展示

生成回复 → enqueue → summary_reply_tasks
  → worker claim/run/step → 模型 → 保存草稿
查看回复 → read → 权限与资料校验 → 返回草稿
```

已有请求内 read_cache，不等于跨用户共享正文缓存。已有轮询降频及 ETag，也不等于服务器完全不做工作。

## 5. 外部 API 调用清单

以下是仓库实现，不是本轮对平台 API 可用范围的外部调研。

| 调用 | 外部入口/配置 | 使用方 |
| --- | --- | --- |
| OAuth 浏览器授权 | settings.oauth_authorize_url | main.start_lark_authorization |
| 授权码换 token | settings.oauth_token_url | LarkClient.exchange_code → _token_request |
| 刷新 token | 同上，refresh_token grant | worker → LarkClient.refresh_token |
| 授权人资料 | settings.user_info_url | 登录回调、资料更新 |
| 单个用户资料 | GET /open-apis/contact/v3/users/{open_id} | LarkClient.contact_user / 身份补齐 |
| 会话列表 | GET /open-apis/im/v1/chats | 群聊、私聊发现 |
| 消息列表 | GET /open-apis/im/v1/messages | 消息回溯和增量同步 |
| 群成员列表 | GET /open-apis/im/v1/chats/{chat_id}/members/list | 成员快照 |
| 应用 tenant token | POST /open-apis/auth/v3/tenant_access_token/internal | 协作组织同步 |
| 协作组织列表 | GET /open-apis/directory/v1/collaboration_tenants | organization_sync |
| OpenAI Responses | POST https://api.openai.com/v1/responses | 共用 call_openai；各业务分别构建材料和 schema |

本次 backend/app 扫描未发现实现 Lark 发消息或 TikTok 商品探测的后端接口。不能把这些未来规划写成当前已有能力。静态前端资源和浏览器直接请求不在此服务端清单范围。

## 6. 不应该合并或删除的部分

- GET 展示和 POST 修改应继续分开，不要为缩短接口表改成一个 action 万能入口。
- 不同业务的人工确认、暂停、重试是不同操作，可以共用校验代码，不必合并 URL。
- 页面权限、消息来源、当前授权、正文同意、达人归属不是同一件事。
- 同步游标、租约、去重、付费前复核、入库前复核和查看审计必须保留。
- 新旧识别流程的证据规则和正式入库冲突检查不能因为相似就直接共用。
- 已做过的列表查询优化、批量名单读取、轮询降频和流式导出，不要重复改造。
- 不因文件大就拆微服务，不为统一接口引入新 ORM、Redis、Celery 或通用 Agent 平台。

## 7. 建议执行顺序与验收

**第一批：减少实际工作量。** 同步查库线程隔离、摘要 COUNT、来源批量读取、重复登录查询。每项单独改、单独比较查询次数与延迟。

**第二批：整理已有共用能力。** HTTP 工具、OpenAI 客户端、达人权限、底层消息解析；只搬确实复用的能力，尽量不改接口返回结构。

**第三批：决定旧链路去留。** 核对队列和使用方后完整退役；再按领域拆大文件。连接池和更早返回的缓存机制以测量为前提。

验收至少包含：

- 59 个路由的地址、方法、鉴权行为不意外改变；若退役则明确变更记录。
- 未登录、授权失效、来源不允许、没有正文同意、CSRF 错误都不能绕过。
- 所有相关数据库测试执行而非因环境缺失跳过。
- 批量来源空集合、人员切换、会话分页和并发授权撤销。
- 下载仍逐步输出，不全量攒入内存；中途撤权、断开连接可结束。
- 摘要/回复结果、任务幂等、租约和付费次数不因重构改变。
- 记录请求查询数、数据库连接数、p50/p95、下载首字节和总耗时；不凭文件变短宣称系统更快。

### 精简审查摘要

按 ponytail 精简原则，优先级是删除无意义工作，而不是新增抽象层：

- `shrink:` offhours_summary.prepare_next_summary —— 用调度结果替代前后全表计数。
- `shrink:` admin_messages —— 批量来源查询替代逐人查询。
- `shrink:` creators/offhours 页面入口 —— 一次请求只读取一次入口登录态。
- `shrink:` contact_assessments.call_openai —— 搬到真实共享模块，不新建客户端框架。
- `shrink:` 路由与达人权限工具 —— 去除对 admin/旧扫描模块的反向依赖。
- `delete（有前提）:` 旧 handle 执行路径 —— 完成存量/依赖核对后再退役，不能现在直接删除。
- `yagni:` 暂不升级为微服务、通用工作流引擎或多提供商平台。

潜在删减行数暂不估计：其中不少工作属于职责搬移，不能拿“新文件变短”当作总复杂度下降。

