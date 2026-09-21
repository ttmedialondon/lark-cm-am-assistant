# 当前后端方法链与精简建议

日期：2026-09-21；本地代码：`caf596b`。本轮只读审查业务代码，新增两份审查文档，没有部署、改数据库或调用模型。

## 结论

目前不需要换框架、上微服务或统一成万能接口。应先减少重复数据库工作，再清理旧执行链，最后做有测量依据的连接复用。已有公共模块足够，不宜继续增加抽象层。

本次重新扫描 `backend/app`：**39 个 Python 文件、616 个函数／方法定义、59 个 HTTP 路由（29 GET、30 POST）**。包含嵌套函数；HTTP 路由包含 HTML、认证、下载和健康检查，不全是 JSON API。数量已用应用实际注册表核对。

全部接口、函数位置及函数体直接调用见 [当前完整索引](backend-current-method-index_2026-09-21.md)。该清单是源码静态索引，不是运行时全路径追踪；范围不含同级其他仓库。

## 1. 当前主要方法链

| 业务 | 当前主链 | 去向 |
| --- | --- | --- |
| 登录授权 | main.start_lark_authorization → Lark 授权 → lark_callback → exchange_code / user_info | 授权、加密 token、会话、同步任务 |
| 工作台 | dashboard / dashboard_stats → _current_authorization → _dashboard_stats | 人员同步状态与页面 |
| 管理页面 | admin 路由 → 页面／Owner 权限 → admin_reporting 对应查询 | 人员、联系人、群聊、任务等 |
| 查看消息 | admin_messages → message_users → message_conversations → message_conversation → message_page → record_message_view | 分页正文及审计 |
| 全量导出 | export_messages → 来源权限 → stream_export → _write_export | 分批读库、每会话 Excel、流式 ZIP、过程撤权复核 |
| 达人名单 | creators page/data → creator_access → creator_roster.page_data → creator_imports.page_data + 正式名单查询 | 候选与正式达人 |
| 导入名单 | imports preview → creator_imports.preview_import → prepare_next_import → claim_next_import → run_claimed_import | 群／人员材料、模型判断、冲突复核、入库 |
| 导入人工审核 | confirm_row / defer_row / row_evidence | 当前权限、材料复核、人工处理及审计 |
| 联系人分类 | claim_next_contact_assessment → run_claimed_contact_assessment → _run | 分类证据、模型结果、角色校验 |
| 负责人建议 | prepare_next_preview → claim_next_ownership → run_claimed_ownership | 互动证据及候选建议 |
| 时段摘要生成 | prepare_next_summary → schedule_if_due / schedule_merges → claim_next_summary → run_claimed_summary → summary_dialogues | 会话抽取及合并任务 |
| 时段摘要读取 | routes.read → summary_periods.selection → get_summary_view → _authorize / _roster / _related_chats → view_events / merge_input | 当前材料校验后返回页面或 JSON |
| 回复草稿 | summary_replies.enqueue → worker claim/run/step → summary_replies.read | 生成草稿，不是发送 Lark 消息 |
| 消息同步 | worker.run_forever → _start_pending_jobs → 会话发现 → _fill_lark_slots → _dispatch_lark_turn → _fetch_claim → _record_claimed_page | Lark 消息、去重、加密正文、游标、增量调度 |
| 成员与资料 | 成员快照任务、identity_profiles.fill_slots、organization_sync.sync_if_due | 群成员、人员资料、协作组织 |

箭头仅说明主要业务关系，分支和条件以源码为准。现有 AI 调度为 **5 类队列**：联系人、摘要、负责人、导入、回复；证据准备为 **3 类**：负责人、导入、摘要。旧 handle 扫描已不在常驻 worker 中。

## 2. 最值得继续优化的地方

### P1：摘要读取的证据复核 N+1

位置：`offhours_summary._related_chats` → `creator_imports._conflict / _valid_chain` → `creator_import_evidence.validate_references` → `creator_handle_evidence._members`。

当前对每条已确认导入关系，分别检查群、消息引用、成员、人员身份和冲突；不同材料反复读取同一批基础事实。

已有测量记录显示，一个含 31 条导入关系的摘要请求执行 **481 条 SQL**：CM、群、消息各读取 62 次，两类成员查询各 62 次。该数据来自此前执行记录，本轮没有重新测量生产。当前源码仍保留对应逐条循环，因此热点尚未消除。

建议：一次请求内批量读取已授权范围中的事实，再逐条比对各材料；冲突事实也可批量读取。必须先排除失效授权／群，再读取相关材料，并按每条材料自己的群范围判断身份可见性，不能将不同群的成员混为一个集合。保留原有证据哈希、冲突和授权校验；不要用跳过复核或跨请求权限缓存换速度。

验收：相同数据下 SQL 数明显下降；撤权、消息变化、跨群人员混用仍被拒绝；记录请求 p50/p95，不能仅凭文件变短宣称提速。

### P2：达人列表嵌套连接与重复访问校验

位置：`creator_roster.page_data` 已持有数据库连接，又调用自行建立连接和校验的 `creator_imports.page_data`。

建议：在确有共用需求的名单读取层提供接收当前连接的内部读取方法，公开入口仍自行鉴权。只复用同一请求、相同范围的读取上下文；不要全局缓存权限，也不要做通用 Repository 框架。

收益：减少一次嵌套借用及可能的重复查询，并降低未来有界连接池的嵌套等待风险。具体延迟收益尚未测量。

### P2：旧 handle 扫描代码可继续退役，但不能整文件删除

位置：`creator_handles.py`，以及 `creator_handle_evidence.py`。

worker 已移除旧任务领取与准备；但 confirm/defer/evidence 仍由路由调用，导入证据也仍复用旧 evidence 模块的方法。应检查脚本、测试和存量队列后，只删除无使用方的旧生产／执行代码。保留历史数据和人工核对链。

7 个返回 410 的兼容入口可以暂时保留；另有 ownership confirm 拒绝旧确认方式。不要为了降低路由数量破坏旧客户端可理解的错误行为。

### P2：连接复用值得评估，不能直接替换

位置：`db.connection` 每次新建并关闭连接；`openai_client.call_openai` 使用顶层 `httpx.post`。

建议先消除 N+1，再分别测数据库建连和模型 HTTP 建连占比。必要时采用现成小型有界池／客户端生命周期管理，不自建池，不引入 Redis。

数据库池必须保持现有事务语义：当前没有显式提交的事务退出时回滚。还需覆盖嵌套借用、并发上限、连接异常、服务关闭，以及长下载占用连接的情况。不能直接换成成功退出自动提交的上下文接口。

### P3：大模块按职责整理，别把搬文件当性能优化

当前 worker 2760 行、admin_reporting 1866 行、contact_assessments 1486 行、creator_imports 1313 行、admin 1273 行。

可在后续实际修改时按消息、联系人、AI 配置等领域移动路由和查询，worker 可分开调度与 Lark 落库。优先跟随实际需求重构，不为行数新增层级。搬移与 SQL 行为修改应分开验收。

### P3：ETag 不代表读取免费

`http_helpers.conditional_response` 在完整读取、校验及编码之后生成 ETag。304 能减少传输，不能消除前面的证据复核。

先优化实际查询。只有能可靠覆盖授权、正文、配置和结果变化时，才考虑轻量版本检查；否则提前返回可能展示过期授权的数据。

## 3. 已完成，不应重复建设

| 原问题 | 当前源码状态 |
| --- | --- |
| async 路由直接做同步数据库工作 | 相关入口已改同步端点或移交线程池 |
| 摘要调度前后全表 COUNT | 已改为调度函数返回实际新增数 |
| 限定消息来源逐人查库 | 已支持批量 source_ids；空集合不扩大权限 |
| 页面重复读取登录态 | creators 与摘要入口已复用当前请求登录结果 |
| OpenAI 传输挂在联系人识别模块 | 已抽到 openai_client.py |
| 路由通用工具散落 | 已有 http_helpers.py |
| 新业务依赖旧模块权限／加密工具 | 已有 creator_access.py、creator_data.py |
| 正式达人名单挂在旧扫描器 | 已抽到 creator_roster.py |
| 消息正文解析重复 | 已有 message_text.py，显示与证据格式仍分别保留 |
| 空 AI 队列跟随忙循环频繁查库 | 已有空队列降频，任务完成立即唤醒 |
| 导出等全量生成后才下载 | 当前已为流式 ZIP，不应恢复全量内存打包 |

这些是本地代码状态，不等于本轮已确认线上运行版本或全部性能改善。

## 4. 外部服务调用

| 外部服务 | 已实现调用 |
| --- | --- |
| Lark OAuth | 浏览器授权、授权码换 token、刷新 token、授权人资料 |
| Lark Contact | contact/v3/users/{open_id} 人员资料 |
| Lark IM | im/v1/chats 会话、im/v1/messages 消息、群 members/list |
| Lark Directory | tenant_access_token/internal、collaboration_tenants |
| OpenAI | 共用 Responses API：各业务独立构建证据和校验结果 |

本仓库后端清单不包含已实现的 TikTok 商品探测或 Lark 群发消息业务，不能把规划写成现有能力。

## 5. 不应合并的边界

- GET 展示和 POST 修改，不做万能 action API。
- Owner 权限、消息来源、正文同意、达人归属，不合成单一 is_admin。
- 不同 AI 任务的状态机和证据规则，不强行统一成通用工作流。
- 查看、付费调用前、入库前、下载过程中的复核，不因表面重复删除。
- 游标、幂等、租约、加密、CSRF 和审计，必须保留。

建议顺序：**批量证据复核 → 消除嵌套读取 → 安全退役旧链 → 测量后决定连接复用 → 按需整理大模块**。

本轮核验：源码搜索、AST 全量索引、实际 app.routes 注册数量。未执行新的全量测试或生产压测，不以历史 449 项通过替代当前发布验收。
