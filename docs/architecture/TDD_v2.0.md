# Lark CM/AM 夜间消息管理与知识沉淀助手技术设计文档

**文档编号：** TDD-LARK-CMAM-001  
**版本：** v2.0  
**状态：** 技术评审稿  
**最近更新日期：** 2026-08-28  
**对应 PRD：** [Lark助手机器人_PRD_v2.0_飞书版.md](./Lark助手机器人_PRD_v2.0_飞书版.md)  
**当前代码基线：** [backend/](../../backend/)
**适用读者：** 技术负责人、后端、前端、测试、产品、安全与运维  
**保密级别：** 内部使用

> 本文描述 v2.0 的目标技术方案和从现有原型迁移到目标方案的路径。除“当前已经实现”章节外，其余内容均不代表已经上线。

---

## 0. 技术结论

本项目建议继续沿用现有 Python 技术底座，采用“模块化单体 + 独立后台任务进程”的架构。

最终选型如下：

- **后端：** Python 3.12+、FastAPI、Pydantic、SQLAlchemy 2、Alembic、psycopg 3、httpx。
- **前端：** React、TypeScript、Vite。
- **界面：** shadcn/ui、Tailwind CSS、Lucide Icons。
- **前端路由：** React Router。
- **接口数据：** TanStack Query。
- **复杂列表：** TanStack Table。
- **表单：** React Hook Form + Zod。
- **图表：** Recharts；只有复杂分析图表出现后再评估 ECharts。
- **数据库：** PostgreSQL。
- **后台任务：** PostgreSQL 任务表 + 独立 Python Scheduler/Worker。
- **登录：** Lark OAuth。
- **会话：** 服务端 Session + Secure/HttpOnly Cookie。
- **权限：** RBAC 控制“能做什么”，数据范围控制“能看哪些数据”。
- **AI：** 自建轻量模型适配层，分别连接 OpenAI 和 DeepSeek。
- **部署：** v2.0 试点继续使用 Hetzner、Caddy、systemd 和 PostgreSQL。
- **CI/CD：** GitHub Actions。
- **知识检索：** v2.0 先使用 PostgreSQL；正式 RAG 确认后再评估 pgvector。

一句话架构原则：

> 先把系统做成边界清楚、可审计、可重试的单体应用；在真实负载证明需要之前，不引入微服务、Kubernetes、Kafka、Redis、Celery、Elasticsearch或独立向量数据库。

---

## 1. 设计目标

### 1.1 业务目标

技术方案必须支持：

1. CM/AM 使用 Lark 完成登录、数据授权和群范围选择。
2. 后台回填并持续同步授权范围内、Lark Open API 实际可提供的群聊消息。
3. 按 `Europe/London` 时区计算 18:30 至次日 10:00 的夜间业务窗口。
4. 将相关消息合并成可追溯事件，并给出 P0—P3 紧急程度。
5. 在网页工作台呈现事件、依据、状态、负责人和数据新鲜度。
6. 在 10:05 前生成晨间摘要，并按配置发送至内部 Lark 目标。
7. 接入 OpenAI 或 DeepSeek，为内部人员提供不可自动外发的回复建议。
8. 建立数据治理、AI 评测和候选知识看板。
9. 支持暂停、撤销授权、删除、失效传播和审计。
10. 支持 CM/AM、团队负责人、产品运营、系统管理员和安全合规等不同角色。

### 1.2 工程目标

- 第一位开发者可以独立维护。
- 新增页面时不需要改动整个后端。
- 新增角色或权限时不依赖前端硬编码。
- OpenAI 与 DeepSeek 可以切换，不让业务代码绑定某个 SDK。
- 同一个任务重复执行不会产生重复消息、事件或通知。
- AI、Lark 通知或部分群同步失败时，核心工作台仍然可用。
- 每个派生结论都能追溯到原始消息。
- 敏感正文不进入普通日志、错误追踪或前端缓存。
- 数据删除能够传播到事件、AI 建议和候选知识。

### 1.3 非目标

v2.0 不实施：

- 自动向客户或外部群发送 AI 回复。
- 一对一单聊（P2P）同步。
- 图片、语音、视频或附件内容理解。
- 正式知识库、RAG 或自动问答。
- 多人实时协作编辑。
- 面向外部客户的公共 SaaS。
- 微服务、Kubernetes、Kafka 或复杂 Agent 平台。

---

## 2. 设计假设

### 2.1 试点规模假设

为便于选择简单架构，暂按以下试点规模设计：

- 不超过 20 名授权用户。
- 不超过 1,000 个启用同步的群聊。
- 不超过 500 万条受控消息。
- 每日新增消息不超过 50,000 条。
- 同时使用网页工作台的用户不超过 50 人。

这些数字是架构设计基线，不是产品承诺。若实际规模明显更高，应在开发 Phase 1 前重新进行容量评估。

### 2.2 数据范围假设

- 只处理用户明确授权并由 Lark API 返回的群聊数据。
- v2.0 以文本消息为主要分析对象。
- 不支持的消息类型只保存必要元数据和“内容未解析”状态。
- 用户登录身份、业务角色与 Lark 数据授权是三个不同概念，不得继续绑定成一条记录。
- 正文保留期限、删除时限和允许使用的 AI 供应商必须在正式入库前配置完成。

### 2.3 时间假设

- 数据库统一保存 UTC。
- 业务日期和界面显示默认使用 `Europe/London`。
- 时间窗口采用半开区间：`[18:30, 次日 10:00)`。
- 英国夏令时由 Python 标准库 `zoneinfo` 计算，禁止用固定 UTC 偏移代替。

---

## 3. 当前代码事实

### 3.1 可以直接复用的能力

当前主工程为 [backend/](../../backend/)，已经具备：

- Lark OAuth、PKCE 和一次性 state。
- Secure、HttpOnly、SameSite Cookie。
- 服务端 Session。
- Access Token 和 Refresh Token 加密保存。
- Token 到期前自动刷新及并发刷新保护。
- 授权用户可见群发现。
- 历史消息分页回填。
- 分页游标、任务租约、中断续传和失败重试。
- 默认约三分钟一次的增量同步。
- 十分钟重叠窗口和消息 ID 去重。
- 不可读群状态。
- 基础统计看板。
- Caddy、systemd 和服务器 Secret 录入脚本。

主要代码入口：

- [app/main.py](../../backend/app/main.py)：网页、OAuth、Session 和统计看板。
- [app/worker.py](../../backend/app/worker.py)：群发现、历史回填、增量同步、任务租约和 Token 刷新。
- [app/lark_api.py](../../backend/app/lark_api.py)：Lark API 封装。
- [app/crypto.py](../../backend/app/crypto.py)：Token 加解密。
- [migrations/001_initial.sql](../../backend/migrations/001_initial.sql)：当前数据库结构。

### 3.2 当前不能满足 PRD 的部分

当前系统仍是“同步统计原型”，主要缺口为：

- `message_ledger` 不保存正文、发送者、类型、回复关系和编辑状态。
- 消息主键包含 `authorization_id`；同群被多人授权时会重复保存同一消息。
- Session 直接绑定 Lark 授权记录，无法表达组织、团队和角色。
- 没有授权说明版本、重新同意、群选择、暂停、撤销和删除流程。
- 没有夜间业务窗口。
- 没有事件、证据、P0—P3、人工状态和负责人模型。
- 没有晨间摘要和 Lark 内部通知。
- 没有 OpenAI 或 DeepSeek 接入。
- 没有 AI 评测、回复建议反馈和成本统计。
- 没有候选知识和来源失效传播。
- 没有多角色权限和业务数据范围。
- 自动化测试目前只覆盖少量纯函数与加密行为。

### 3.3 必须先处理的授权变化

当前授权页面明确说明“不保存消息正文”。因此：

1. 新版授权说明必须先通过产品、安全和法务评审。
2. 正文用途、保留期限、第三方模型、撤销方式和删除方式必须明确展示。
3. 已授权用户必须完成适用版本的重新同意。
4. 在同意版本不满足要求时，`message_body_ingestion` 功能开关必须保持关闭。
5. 未经重新同意，不得把现有索引同步自动升级为正文采集。

---

## 4. 技术选型说明

### 4.1 后端：Python + FastAPI

继续使用 FastAPI，不改为 Django、Flask 或 Node.js。

原因：

- 当前 Lark OAuth、同步和 Worker 已经是 Python。
- Python 更方便接入 AI、文本处理、评测和数据分析。
- FastAPI 的依赖注入适合统一实现登录、权限和数据范围检查。
- Pydantic 可以同时校验 API、后台任务和 AI 结构化输出。
- OpenAPI 可以生成前端 TypeScript 类型。
- FastAPI 支持按模块拆分路由，不需要提前拆成微服务。

后端编码约束：

- Python 采用受支持版本，具体版本由 `.python-version` 和锁文件固定。
- 使用 `pyproject.toml` 管理依赖和工具配置。
- 新代码使用类型标注。
- API Schema、领域对象和数据库对象分层，禁止一个类承担所有职责。
- 网页请求只做轻量事务；同步、AI 和通知必须进入 Worker。

### 4.2 数据访问：SQLAlchemy 2 + Alembic

新增业务模块使用 SQLAlchemy 2 和 Alembic。

迁移原则：

- 不一次性重写所有稳定的原生 SQL。
- 现有任务领取所使用的 `FOR UPDATE SKIP LOCKED` 可以继续保留原生 SQL。
- 新增组织、权限、事件、AI 和知识模块使用 SQLAlchemy。
- 原有表只有在被功能修改时才逐步迁移。
- 所有数据库变更必须有 Alembic 迁移和回滚说明。

不选择 SQLModel，原因是本项目后续存在复杂权限、关系和查询，保持 SQLAlchemy 与 Pydantic 的职责分离更清楚。

### 4.3 前端：React + TypeScript + Vite

新的事件工作台和管理页面使用 React、TypeScript 和 Vite。

选择 React 的原因：

- 适合持续增加列表、筛选器、详情抽屉、表单、趋势图和权限页面。
- 后续招聘、外包和交接更容易。
- 表格、表单、图表、路由和测试生态成熟。
- TypeScript 可以降低前后端字段不一致和权限标识拼写错误。

选择 Vite 而不是 Next.js：

- 本产品是登录后的内部工作台，不需要 SEO。
- 不需要服务端渲染。
- Vite 构建为静态文件，生产环境不需要额外 Node.js 服务。
- FastAPI 保持唯一业务后端，部署和排错更直接。

现有 Jinja2/HTMX 页面不会立即删除：

- 登录页和旧统计页在迁移期间保留。
- 新页面从夜间事件工作台开始使用 React。
- React 功能达到验收标准后，再移除被替代的旧页面。

### 4.4 界面：shadcn/ui + Tailwind CSS

选择 shadcn/ui 的原因：

- 默认风格简洁、克制，适合内部业务系统。
- 组件源码进入项目，能够按业务需要修改。
- 不会被封闭组件库的主题和 API 锁定。
- 可使用统一的按钮、表单、抽屉、对话框、侧栏、标签和提示组件。

界面约束：

- 使用中性背景、单一品牌主色、轻边框和少量阴影。
- 不使用大面积渐变、玻璃拟态或无业务价值的动画。
- 颜色只表达语义：P0 红、P1 琥珀、P2 蓝、P3 灰。
- 原始消息、规则判断和 AI 建议在视觉上明确分区。
- AI 建议固定显示“内部草稿，需人工确认”。
- v2.0 不设计“自动发送给客户”按钮。

### 4.5 后台任务：PostgreSQL 队列

继续使用现有 PostgreSQL 任务表和独立 Worker，不引入 Redis、RabbitMQ 或 Celery。

理由：

- 当前代码已有任务领取、租约、重试和中断恢复。
- 试点规模不需要额外消息中间件。
- Web、Worker 和数据库使用同一套事务与数据模型，维护成本低。
- `FOR UPDATE SKIP LOCKED` 可以支持多个 Worker 并发领取任务。

只有出现以下情况时才重新评估 Redis + Dramatiq/Celery：

- 队列延迟长期超过业务目标。
- 需要十个以上 Worker 独立扩缩。
- 大量短任务使 PostgreSQL 队列表产生明显锁竞争或膨胀。
- 多个独立服务需要高吞吐事件分发。

### 4.6 部署：保留现有单机方案

v2.0 试点继续使用：

- Hetzner 欧洲服务器。
- Caddy 作为唯一公网入口。
- systemd 管理 API、Scheduler 和 Worker。
- PostgreSQL 仅监听本机或私有网络。
- React 静态文件由 Caddy 提供。

本阶段不同时迁移到 Kubernetes 或生产 Docker Compose。Docker Compose 可以用于本地开发和 CI；待核心功能稳定后，再单独评估生产容器化。

---

## 5. 总体架构

```text
用户浏览器
    │ HTTPS
    ▼
Caddy
    ├── /           → React 静态前端
    ├── /legacy     → 过渡期 Jinja 页面
    └── /api/*      → FastAPI
                         │
                         ├── Identity / Session
                         ├── Authorization / RBAC
                         ├── Event / Dashboard API
                         └── Admin / Governance API
                                  │
                                  ▼
                             PostgreSQL
                                  ▲
                                  │
                  ┌───────────────┴──────────────┐
                  │                              │
            Python Scheduler               Python Worker
            到期窗口和周期任务              同步、分析、通知、删除
                  │                              │
                  └───────────────┬──────────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
               Lark Open API   OpenAI API   DeepSeek API
```

架构约束：

- FastAPI 与 Worker 共享领域代码和数据库模型。
- Scheduler 只创建幂等任务，不执行长任务。
- Worker 不信任任务参数，执行前重新检查组织、授权和资源范围。
- 外部 API 适配器不得直接写业务表，只返回规范化结果。
- 模块共用一个 PostgreSQL，但禁止跨模块随意修改表。
- 所有业务数据从第一天包含 `organization_id`。

---

## 6. 目标工程结构

现有 `lark-message-stats` 逐步调整为：

```text
lark-message-stats/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/
│   │   │   ├── dependencies.py
│   │   │   ├── errors.py
│   │   │   └── v1/
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   ├── security.py
│   │   │   ├── permissions.py
│   │   │   ├── logging.py
│   │   │   └── time.py
│   │   ├── db/
│   │   │   ├── base.py
│   │   │   ├── session.py
│   │   │   └── models/
│   │   ├── modules/
│   │   │   ├── identity/
│   │   │   ├── lark_auth/
│   │   │   ├── chat_scope/
│   │   │   ├── ingestion/
│   │   │   ├── windowing/
│   │   │   ├── events/
│   │   │   ├── ai_gateway/
│   │   │   ├── suggestions/
│   │   │   ├── notifications/
│   │   │   ├── knowledge/
│   │   │   ├── governance/
│   │   │   └── reporting/
│   │   ├── workers/
│   │   │   ├── scheduler.py
│   │   │   ├── runner.py
│   │   │   └── handlers/
│   │   └── adapters/
│   │       ├── lark/
│   │       ├── openai/
│   │       └── deepseek/
│   ├── migrations/
│   └── tests/
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   ├── routes/
│   │   ├── features/
│   │   ├── components/
│   │   ├── api/
│   │   ├── permissions/
│   │   ├── styles/
│   │   └── test/
│   └── public/
├── infra/
│   ├── caddy/
│   ├── systemd/
│   ├── backup/
│   └── smoke/
├── docs/
└── .github/workflows/
```

模块规则：

1. `api` 只负责 HTTP 参数、认证、权限和响应转换。
2. `modules/*/service.py` 负责业务用例和事务边界。
3. `modules/*/repository.py` 负责数据读取和资源范围过滤。
4. `adapters` 负责 Lark 或模型供应商的具体接口。
5. Worker 调用业务 service，不复制业务逻辑。
6. 前端按业务功能分目录，不按“components/pages/utils”无限堆积。

---

## 7. 业务模块

### 7.1 Identity

负责：

- 公司、用户、团队和成员关系。
- 用户启用、停用和离职状态。
- 固定角色与能力列表。
- 当前用户的 `capabilities` 和数据范围。

### 7.2 Lark Authorization

负责：

- OAuth、PKCE、state 和回调。
- Token 加密、刷新、撤销和失效。
- 授权说明版本、同意时间和权限范围。
- Lark tenant/app/user 身份映射。

### 7.3 Chat Scope

负责：

- 发现可见群。
- 用户选择、排除、暂停和恢复群。
- 多人授权同群时的访问关系。
- 群与客户、项目、团队的映射。

### 7.4 Ingestion

负责：

- 历史回填。
- 增量同步。
- 消息规范化和公司级去重。
- 编辑、撤回、权限失效和定期对账。
- 同步游标、延迟和错误状态。

### 7.5 Windowing

负责：

- 业务日期。
- 18:30 至次日 10:00 窗口。
- 英国夏令时。
- 10:00 封窗、迟到数据和补偿。

### 7.6 Event Engine

负责：

- 候选消息筛选。
- 同主题消息聚合。
- 固定风险规则。
- P0—P3 裁决。
- 事件状态、负责人和证据。

### 7.7 AI Gateway

负责：

- OpenAI、DeepSeek 和规则降级适配。
- 模型能力判断和路由。
- Prompt 版本。
- 结构化输出校验。
- 调用量、费用、延迟和错误。

### 7.8 Suggestions

负责：

- 内部回复建议。
- 风险提示和待确认事实。
- 查看、复制、编辑、采用和放弃反馈。
- 保证不存在外部自动发送路径。

### 7.9 Notifications

负责：

- 晨间摘要快照。
- 内部 Lark 发送目标。
- Outbox、幂等、重试和投递记录。

### 7.10 Knowledge Candidates

负责：

- 结构化事实。
- 未审核候选知识。
- 来源关系、适用范围和失效传播。
- 不负责正式知识库或自动问答。

### 7.11 Governance

负责：

- 保留期限。
- 删除请求。
- 授权撤销。
- 审计和导出。
- 敏感访问记录。

### 7.12 Reporting

负责：

- 夜间事件工作台的只读模型。
- 数据沉淀看板。
- AI 评测和成本汇总。
- 避免页面每次加载都扫描大表。

---

## 8. 身份、角色与权限

### 8.1 三种身份必须分开

- **登录身份：** 谁正在使用网页。
- **业务身份：** 该用户在公司中的角色和团队。
- **数据授权：** Lark 允许系统读取哪些数据。

一个人可以登录系统但尚未授权消息；也可以撤销 Lark 授权后继续登录查看删除进度。数据连接不得继续充当系统用户主键。

### 8.2 初始角色

#### CM/AM

默认能力：

- 查看本人有效群范围内的事件。
- 查看有权限的原始依据。
- 更新事件状态。
- 使用并反馈 AI 建议。
- 管理自己的授权和群范围。

#### Team Lead

默认能力：

- 查看团队聚合和事件分配。
- 查看经过配置的团队业务范围。
- 处理升级事项。

团队负责人不会因为角色名称自动获得全公司消息正文。

#### Product/Ops

默认能力：

- 查看脱敏指标、规则效果和 AI 评测。
- 管理事件类型、规则和展示配置。

默认无权浏览与评测无关的完整客户消息。

#### System Admin

默认能力：

- 管理服务配置、任务、同步故障和系统状态。

系统管理员默认不能读取业务正文。

#### Security/Compliance

默认能力：

- 查看授权、删除、导出和访问审计。
- 审核供应商、保留期限和权限策略。

如确需查看正文，必须走单独的临时授权流程并完整审计。

### 8.3 权限标识

权限使用稳定的能力字符串：

- `event.read`
- `event.update_status`
- `event.assign`
- `message.read_body`
- `suggestion.generate`
- `suggestion.use`
- `knowledge.read_candidate`
- `knowledge.review`
- `chat.manage_own`
- `chat.manage_team`
- `sync.read`
- `sync.manage`
- `authorization.manage_own`
- `audit.read`
- `data.export`
- `data.delete_request`
- `data.delete_approve`
- `admin.operations`

### 8.4 数据范围

权限通过后，还必须检查资源范围：

- `own`：本人有效授权的群。
- `team`：指定团队的获批业务范围。
- `customer`：指定客户。
- `project`：指定项目。
- `chat`：指定群。
- `org_metadata`：只允许组织级统计，不包含正文。

授权判断示例：

```python
def can_read_event(actor: Actor, event: Event) -> bool:
    return (
        actor.has_permission("event.read")
        and actor.organization_id == event.organization_id
        and actor.scope.allows(event.customer_id, event.project_id, event.chat_ids)
    )
```

### 8.5 前后端责任

- 后端是唯一权限裁决方。
- 前端根据 `GET /api/v1/me` 返回的能力隐藏菜单和按钮。
- 前端路由守卫只改善体验，不能代替后端校验。
- 每个详情、导出和正文接口必须重新检查权限。
- 列表查询必须在数据库层带上组织和资源范围，禁止“先查全部，再在 Python 过滤”。

### 8.6 会话与 CSRF

- 浏览器只保存随机 Session ID。
- Session Cookie 使用 Secure、HttpOnly、SameSite=Lax 和 `__Host-` 前缀。
- 数据库只保存 Session ID 哈希。
- 不把 Lark Token 或长期 JWT 放进 Local Storage。
- 所有写请求验证 CSRF Token 和 Origin。
- Session 在用户停用、授权撤销或高风险权限变更时可立即失效。

---

## 9. 数据模型

所有业务表必须包含 `organization_id`，所有主键使用 UUID，所有时间保存为 `TIMESTAMPTZ`。

### 9.1 组织与用户

#### organizations

关键字段：

- `id`
- `name`
- `timezone`
- `body_retention_days`
- `ai_provider_policy`
- `status`

#### users

关键字段：

- `id`
- `organization_id`
- `lark_tenant_key`
- `lark_open_id`
- `display_name`
- `status`

唯一约束：

- `(organization_id, lark_tenant_key, lark_open_id)`

#### memberships

关键字段：

- `user_id`
- `organization_id`
- `role`
- `status`
- `valid_from`
- `valid_until`

#### teams / team_memberships

用于表达团队层级、团队负责人和有效时间。

### 9.2 授权与同意

#### lark_authorizations

关键字段：

- `id`
- `organization_id`
- `user_id`
- `lark_app_id`
- `consent_version`
- `consented_at`
- `scopes`
- `access_token_ciphertext`
- `refresh_token_ciphertext`
- `key_version`
- `access_token_expires_at`
- `refresh_token_expires_at`
- `status`
- `revoked_at`

Token 密文必须带 `key_version`，为未来密钥轮换保留能力。

### 9.3 群与访问关系

#### chats

公司级群实体：

- `id`
- `organization_id`
- `lark_tenant_key`
- `lark_chat_id`
- `name`
- `chat_type`
- `status`
- `last_seen_at`

唯一约束：

- `(organization_id, lark_tenant_key, lark_chat_id)`

#### chat_grants

表达某个用户或授权对某个群的可见与同步关系：

- `authorization_id`
- `user_id`
- `chat_id`
- `sync_enabled`
- `readable`
- `selected_by_user`
- `last_verified_at`
- `status`

同一群有多个 `chat_grants`，但消息正文只保存一份。

#### chat_context_mappings

可把群映射到：

- 客户。
- 项目。
- 负责人。
- 团队。

### 9.4 消息

#### messages

关键字段：

- `id`
- `organization_id`
- `chat_id`
- `lark_message_id`
- `sender_open_id`
- `message_type`
- `body_ciphertext`
- `body_key_version`
- `content_hash`
- `reply_to_message_id`
- `thread_root_message_id`
- `created_at`
- `edited_at`
- `withdrawn_at`
- `source_status`
- `ingested_at`

唯一约束：

- `(organization_id, lark_message_id)`

强制规则：

- 同一消息在公司范围只入库一次。
- 正文与 Token 使用不同的加密用途和密钥版本。
- 不支持的消息只保存类型、时间和必要索引。
- 撤回不直接伪装成“没有消息”，而是更新来源状态。
- 日志只记录内部消息 ID，不记录正文。

#### message_access

如果单靠群关系不足以表达历史权限变化，可使用关系表保存：

- `message_id`
- `user_id` 或 `grant_id`
- `valid_from`
- `valid_until`
- `reason`

首期优先从有效 `chat_grants` 推导；只有真实权限历史需求出现后再物化到消息级。

### 9.5 同步与任务

#### sync_cursors

按授权、群和同步模式保存：

- 历史回填游标。
- 增量窗口。
- 最近成功时间。
- 最近错误。
- 可读状态。

#### jobs

关键字段：

- `id`
- `organization_id`
- `job_type`
- `payload`
- `priority`
- `status`
- `scheduled_at`
- `attempts`
- `max_attempts`
- `lease_until`
- `idempotency_key`
- `last_error_code`
- `last_error_summary`

唯一约束：

- `idempotency_key`

错误摘要不得包含 Token、正文或供应商原始响应。

### 9.6 夜间窗口

#### business_windows

关键字段：

- `id`
- `organization_id`
- `business_date`
- `timezone`
- `start_at_utc`
- `end_at_utc`
- `closed_at`
- `summary_due_at`
- `status`
- `revision`

唯一约束：

- `(organization_id, business_date)`

### 9.7 事件

#### events

关键字段：

- `id`
- `organization_id`
- `business_window_id`
- `customer_id`
- `project_id`
- `event_type`
- `title`
- `summary`
- `severity`
- `status`
- `assignee_user_id`
- `first_message_at`
- `last_message_at`
- `has_cm_am_reply`
- `late_arrival`
- `confidence`
- `revision`
- `created_at`
- `updated_at`

唯一与幂等设计：

- 每次聚合计算生成稳定的 `event_fingerprint`。
- 同一窗口、群和 fingerprint 只创建一个活动事件。
- 新消息更新原事件并增加 `revision`。

#### event_messages

关联字段：

- `event_id`
- `message_id`
- `relation_type`：`evidence` 或 `context`。
- `position`

所有等级、建议和候选知识必须能追溯到 `evidence` 消息。

#### event_status_history

保存：

- 前状态。
- 后状态。
- 操作人。
- 原因。
- 时间。
- 事件 revision。

### 9.8 AI 与回复建议

#### ai_runs

保存：

- `provider`
- `model`
- `prompt_version`
- `schema_version`
- `event_id`
- `event_revision`
- `source_message_ids`
- `input_tokens`
- `output_tokens`
- `latency_ms`
- `estimated_cost`
- `status`
- `error_code`
- `created_at`

默认不重复保存完整 Prompt 正文；使用 Prompt 版本和来源消息引用重建。

#### event_analyses

保存：

- 规则版本。
- 模型建议等级。
- 最终裁决等级。
- 判断理由。
- 风险标签。
- 待确认事实。
- 证据消息 ID。
- 置信度。
- 是否需要人工升级。

#### reply_suggestions

保存：

- `event_id`
- `event_revision`
- `ai_run_id`
- `content_ciphertext`
- `risk_notes`
- `facts_to_confirm`
- `status`

#### suggestion_feedback

保存：

- 查看。
- 复制。
- 采用。
- 编辑后采用。
- 放弃。
- 人工评分和可选原因。

不保存不必要的鼠标轨迹、键盘行为或个人画像。

### 9.9 通知

#### summary_snapshots

在 10:05 前生成不可变摘要快照：

- 窗口。
- P0/P1 数量。
- 待人工确认数量。
- 最高优先级事件引用。
- 数据更新时间。
- 同步异常。

#### notification_deliveries

保存：

- `summary_snapshot_id`
- `target_type`
- `target_id`
- `status`
- `attempts`
- `sent_at`
- `last_error`
- `idempotency_key`

唯一约束：

- `(organization_id, business_date, target_type, target_id, notification_kind)`

### 9.10 候选知识

#### knowledge_candidates

保存：

- 类型。
- 内容密文。
- 客户/项目/全局适用范围。
- 未审核、已批准、已拒绝或已失效状态。
- 提取模型和规则版本。

#### knowledge_sources

将候选知识关联到消息和事件。

没有有效来源的候选知识不得进入审核队列。

### 9.11 治理与审计

#### deletion_requests

保存：

- 请求人。
- 删除范围。
- 法律或业务保留例外。
- 状态。
- 计划时间。
- 完成时间。
- 验证结果。

#### audit_events

保存：

- 操作人。
- 动作。
- 资源类型与内部 ID。
- 组织。
- 请求 ID。
- 结果。
- 时间。

审计日志不保存已删除正文副本。

---

## 10. 核心数据不变量

以下规则必须通过数据库约束、业务代码和测试共同保证：

1. 同一组织内的同一 Lark 消息只保存一次。
2. 同群多人授权不会重复分析或重复通知。
3. 所有消息、事件、建议和候选知识均属于一个组织。
4. 所有派生对象都能追溯到有效来源。
5. 源消息编辑、撤回、删除或失权后，派生对象必须失效或重算。
6. AI 不得把固定 P0 红线规则降级。
7. “出现 CM/AM 回复”只是一项事实，不能自动关闭事件。
8. 事件状态只能由具备权限的人员修改。
9. 任务、窗口、事件和通知都具备稳定幂等键。
10. 时间窗口按本地业务时区计算，数据库存储 UTC。
11. 原始正文和 AI 建议不得进入普通日志。
12. 本期不存在任何面向外部客户的自动发送接口。

---

## 11. 同步与数据接入

### 11.1 授权流程

```text
用户登录
  ↓
阅读当前授权说明
  ↓
Lark OAuth + PKCE
  ↓
保存加密 Token 和 consent_version
  ↓
发现可见群
  ↓
用户确认需要同步的群
  ↓
为每个选中群创建历史回填任务
```

### 11.2 历史回填

- 每个群独立保存游标。
- 大群回填不会阻塞其他群的增量任务。
- 历史回填使用低优先级。
- 每处理有限页数就提交进度并续租。
- 429 和 5xx 使用指数退避与随机抖动。
- 401/403 转为授权或权限异常，不无限重试。
- 用户暂停或撤销后，尚未执行的任务取消。

### 11.3 增量同步

- 默认每三分钟调度。
- 保留重叠窗口，依靠消息唯一键去重。
- 增量任务优先于历史回填。
- 每个群显示最近成功时间和延迟。
- 不可读、同步失败和真实零消息是三种不同状态。
- `httpx.Client` 在 Worker 生命周期内复用连接池，不为每个请求重新创建。

### 11.4 编辑、撤回与失权

v2.0 采用：

- 近期增量重叠窗口。
- 定期较长窗口对账。
- 如 Lark Webhook Spike 证明覆盖范围可用，再增加实时事件通道。

变更处理：

1. 更新来源消息状态或正文。
2. 查找关联事件、分析、建议和候选知识。
3. 标记为 `stale`。
4. 根据数据是否仍可用执行重算或删除。
5. 写入审计。

### 11.5 公司级去重

现有 `message_ledger` 按授权人保存，必须迁移为：

```text
一个 chats 记录
    ├── 多个 chat_grants
    └── 一组唯一 messages
             └── 一次分析
```

访问权限来自 `chat_grants`，不能通过复制消息正文来实现隔离。

---

## 12. 夜间窗口与调度

### 12.1 窗口计算

对于业务日期 D：

- 开始：D 前一日 18:30，`Europe/London`。
- 结束：D 当日 10:00，`Europe/London`。
- 查询条件：`message_time >= start AND message_time < end`。

窗口创建时把本地时间转换成 UTC 并持久化。禁止每天简单增加 24 小时，因为夏令时切换日可能不是 24 小时。

### 12.2 Scheduler

Scheduler 每分钟运行一次 UTC tick：

1. 确保未来需要的业务窗口已创建。
2. 查找应当关闭但尚未关闭的窗口。
3. 创建 `close_window` 任务。
4. 在 10:00 后创建 `reconcile_window`。
5. 在数据达到可接受完整度后创建 `build_summary`。
6. 在 10:05 前创建 `deliver_summary`。

Scheduler 使用 PostgreSQL advisory lock，确保多进程时只有一个实例创建周期任务。

### 12.3 迟到数据

- 10:00 后同步到的窗口内消息标记为迟到。
- 工作台更新事件并显示“延迟到达”。
- 已处理事件出现新事实时可以重新打开。
- 默认不重复发送完整晨间摘要。
- 是否补发 P0/P1 更新由独立策略配置。

### 12.4 周末和公共假日

使用组织级日历配置：

- 是否创建工作台窗口。
- 是否发送晨间摘要。
- 是否转发到值班目标。

业务规则未配置时，系统继续生成网页结果，但不默认夜间打扰个人。

---

## 13. 后台任务设计

### 13.1 任务类型

- `discover_chats`
- `backfill_chat`
- `incremental_chat`
- `reconcile_chat`
- `close_window`
- `reconcile_window`
- `cluster_event`
- `classify_event`
- `generate_suggestions`
- `build_summary`
- `deliver_summary`
- `extract_knowledge_candidates`
- `propagate_source_invalidation`
- `retention_cleanup`
- `delete_subject_data`
- `cleanup_sessions_and_jobs`

### 13.2 优先级

- **最高：** P0 规则处理、权限撤销和删除传播。
- **高：** 增量同步、窗口关闭和晨间摘要。
- **中：** 事件分类和回复建议。
- **低：** 历史回填、候选知识和历史聚合。

### 13.3 状态

- `queued`
- `running`
- `succeeded`
- `failed`
- `attention_required`
- `cancelled`

### 13.4 重试与租约

- Worker 通过 `FOR UPDATE SKIP LOCKED` 领取任务。
- 领取后设置租约。
- Worker 崩溃后，过期租约恢复为 queued。
- 可重试错误使用指数退避和抖动。
- 认证或权限问题进入 `attention_required`。
- Schema 错误和不可恢复业务错误进入 failed。
- 重试前重新验证幂等键和资源状态。

### 13.5 任务幂等

示例：

- 增量同步：`incremental:{authorization}:{chat}:{window_end}`
- 关闭窗口：`close_window:{organization}:{business_date}`
- 事件分析：`analyze:{event}:{revision}:{analysis_version}`
- 回复建议：`suggest:{event}:{revision}:{prompt_version}:{provider}`
- 晨间摘要：`summary:{organization}:{business_date}:{target}`
- 删除传播：`delete:{deletion_request}:{resource_type}:{resource_id}`

---

## 14. 事件识别与紧急程度

### 14.1 处理管线

```text
消息同步
  ↓
规范化与公司级去重
  ↓
归属业务窗口
  ↓
固定 P0/P1 规则扫描
  ↓
按群、线程、客户/项目和时间邻近关系生成候选事件
  ↓
AI 给出类型、等级、理由和待确认事实
  ↓
业务规则进行最终裁决
  ↓
保存事件、证据和分析版本
  ↓
生成内部回复建议
```

### 14.2 首期聚合边界

首期自动聚合只允许在以下范围内进行：

- 同一组织。
- 同一群。
- 同一回复线程或明确引用关系。
- 同一客户或项目映射。
- 可配置的时间间隔。

跨群事件只显示“疑似相关”，不自动合并，避免把不同客户的信息错误混合。

### 14.3 固定规则与 AI 的关系

- 固定安全、诈骗、隐私、法律和重大付款风险规则先执行。
- 规则命中 P0 后，AI 只能补充说明，不能降级。
- AI 可以建议升级，但最终等级必须通过业务裁决器。
- 模型置信度低时仍创建“待人工确认”事件，不能静默过滤。
- 没有 AI 结果时，规则事件照常出现。

### 14.4 等级规则

#### P0

- 工作台置顶。
- 强制保留证据。
- 必须人工处理。
- 是否夜间通知由值班策略决定。

#### P1

- 晨间摘要优先展示。
- 明确说明时限、投诉或履约/付款风险。

#### P2

- 进入常规待处理列表。
- 可以生成正常回复建议。

#### P3

- 默认折叠。
- 仍可查询和用于数据统计。

### 14.5 事件状态机

允许状态：

- `pending_confirmation`：待人工确认。
- `viewed`：已查看。
- `resolved`：已处理。
- `no_action`：无需处理。
- `false_positive`：误报。

规则：

- 初始状态只能是 `pending_confirmation`。
- AI 不能改变人工状态。
- `resolved`、`no_action`、`false_positive` 需要操作人和理由。
- 新消息带来新事实、再次催办或风险升级时，事件可重新进入 `pending_confirmation`。
- 状态修改使用 `revision` 乐观锁，避免两人互相覆盖。

### 14.6 证据校验

模型返回的每一个 `evidence_message_id` 必须满足：

- 属于当前事件允许的上下文。
- 当前用户或 Worker 具备访问权限。
- 来源没有被撤回、删除或失权。
- 不属于其他客户的隔离范围。

校验失败时拒绝模型输出并记录结构化错误。

---

## 15. AI 适配方案

### 15.1 不使用复杂 Agent 框架

v2.0 不引入 LangChain、自动工具调用或多 Agent 编排。

本项目需要的是：

- 可预测的分类。
- 结构化事件分析。
- 一至三条内部回复草稿。
- 明确的超时、成本和降级。

使用小型 Provider 接口更容易审计和测试。

### 15.2 Provider 接口

```python
from typing import Protocol

class AIProvider(Protocol):
    provider_name: str

    def analyze_event(
        self,
        request: EventAnalysisRequest,
    ) -> EventAnalysisResult:
        ...

    def suggest_replies(
        self,
        request: ReplySuggestionRequest,
    ) -> ReplySuggestionResult:
        ...
```

实现：

- `OpenAIProvider`
- `DeepSeekProvider`
- `FallbackRuleProvider`

业务模块只依赖协议，不导入供应商 SDK 类型。

### 15.3 统一结构化输出

```python
class EventAnalysisResult(BaseModel):
    event_type: EventType
    suggested_severity: Severity
    reasons: list[str]
    evidence_message_ids: list[UUID]
    facts_to_confirm: list[str]
    risk_tags: list[RiskTag]
    next_actions: list[str]
    confidence: float = Field(ge=0, le=1)
    requires_human_escalation: bool
```

回复建议结果包含：

- 一至三条建议。
- 每条建议的语气。
- 待确认事实。
- 风险提示。
- 禁止承诺项。
- 证据消息 ID。

### 15.4 OpenAI 接入

OpenAI 适配器使用 Responses API，并要求：

- 使用 JSON Schema Structured Outputs。
- 设置 `store=false`。
- 关闭不需要的工具。
- 设置超时和最大输出。
- 保存 provider、model、Prompt 版本、Token、延迟和费用。
- 具体模型名由配置固定，不写死在业务代码。

重要说明：

- `store=false` 不等于零数据保留。
- OpenAI 官方数据控制说明指出，API 数据默认不用于训练，除非客户主动选择共享；但默认滥用监控日志可能保留最多 30 天。
- Zero Data Retention 和区域数据能力需要资格及合同条件。
- 因此生产消息是否允许发送给 OpenAI，仍需安全、法务和采购批准。

### 15.5 DeepSeek 接入

DeepSeek 保持独立适配器：

- 按供应商当前能力选择 Responses API 或 Chat Completions。
- 优先使用 JSON Schema；不支持时使用 JSON 模式并进行 Pydantic 校验。
- 具体模型能力通过配置声明，不能假设所有模型都支持同一种结构化输出。
- 数据地域、保留、跨境传输和合同条件必须单独审核。
- 未获批准时仅允许脱敏离线样本评测，不发送生产消息正文。

### 15.6 供应商路由

默认不进行无提示的自动跨供应商切换。

路由决策必须同时满足：

- 当前组织已批准该供应商。
- 当前 consent_version 包含该供应商或供应商类别。
- 数据地域和保留策略符合配置。
- 请求类型在供应商能力列表中。
- 费用预算未超限。

如果主供应商失败而备选供应商未获批准，系统回退到规则和固定模板，不把数据自动发送给备选供应商。

### 15.7 Prompt 安全

- 群消息被标记为“不可信业务内容”，不是系统指令。
- 模型无权调用发送消息、数据库、文件或网络工具。
- Prompt 不包含 Token、Cookie、内部密钥或其他客户信息。
- 上下文只包含完成当前事件判断所需的消息。
- 能脱敏的邮箱、电话、账户和金额按策略脱敏。
- 输出中的消息 ID 必须由系统校验。
- Prompt 和 Schema 都有版本号。

### 15.8 AI 降级

失败类型包括：

- 超时。
- 限流。
- 供应商不可用。
- 非法 JSON。
- Schema 不匹配。
- 证据 ID 越界。
- 费用上限。
- 供应商未获批准。

降级流程：

1. 对可重试错误进行有限重试。
2. 仍失败时使用固定规则等级。
3. 回复建议使用固定内部模板或显示“暂不可生成”。
4. 事件工作台继续可用。
5. 记录失败范围，但不展示供应商敏感错误。

### 15.9 AI 评测

上线人工建议前，建立不少于 300 条脱敏评测样本。

覆盖：

- 中文和英文。
- 投诉。
- 付款。
- 合同与交期。
- 安全、隐私和诈骗。
- 常规咨询。
- 无需行动。
- 多轮催办。
- 存在 CM/AM 回复但未确认解决。

指标：

- P0 强制场景漏判数。
- P0/P1 召回率。
- 等级准确率。
- 事件聚合准确率。
- 证据引用有效率。
- 编造事实率。
- 建议可用率。
- 直接采用或轻微修改后采用率。
- 每事件费用和延迟。

每次修改模型、Prompt、Schema 或规则后执行回归评测。

---

## 16. API 设计

### 16.1 通用规则

- API 前缀：`/api/v1`。
- 请求和响应使用 JSON。
- 时间使用带时区的 ISO 8601。
- ID 使用 UUID。
- 列表使用 cursor pagination。
- 筛选条件进入 URL Query，便于分享和返回。
- 长任务返回 HTTP 202 和 `job_id`。
- 副作用请求支持 `Idempotency-Key`。
- 事件修改使用 `If-Match` 或 revision。
- 错误响应包含 `code`、`message`、`request_id` 和可选 `details`。
- API 不直接返回供应商 Token、Prompt Secret 或数据库密文。

### 16.2 身份与授权

- `GET /api/v1/me`
- `POST /api/v1/auth/logout`
- `GET /api/v1/authorizations/current`
- `PATCH /api/v1/authorizations/current`
- `POST /api/v1/authorizations/current/revoke`
- `POST /api/v1/deletion-requests`

`GET /api/v1/me` 返回：

- 用户基础信息。
- 当前组织和团队。
- 角色。
- capabilities。
- 可用数据范围摘要。
- 当前 consent_version。

### 16.3 群与同步

- `GET /api/v1/chats`
- `GET /api/v1/chats/{chat_id}`
- `PATCH /api/v1/chats/{chat_id}`
- `GET /api/v1/sync-runs`
- `GET /api/v1/sync-runs/{run_id}`
- `POST /api/v1/sync-runs/{run_id}/retry`

### 16.4 夜间事件

- `GET /api/v1/night-windows`
- `GET /api/v1/events`
- `GET /api/v1/events/{event_id}`
- `POST /api/v1/events/{event_id}/transitions`
- `PUT /api/v1/events/{event_id}/assignee`
- `POST /api/v1/events/{event_id}/reply-suggestions:generate`
- `POST /api/v1/suggestions/{suggestion_id}/feedback`

事件列表筛选：

- 业务日期。
- 等级。
- 状态。
- 负责人。
- 客户。
- 项目。
- 群。
- 是否迟到。
- 是否有同步异常。

### 16.5 看板

- `GET /api/v1/dashboards/nightly`
- `GET /api/v1/dashboards/data-governance`
- `GET /api/v1/dashboards/ai-evaluation`

管理总览不返回大批原始正文。

### 16.6 候选知识

- `GET /api/v1/knowledge-candidates`
- `GET /api/v1/knowledge-candidates/{candidate_id}`
- `POST /api/v1/knowledge-candidates/{candidate_id}/review`

正式知识发布不属于 v2.0，因此不提供 publish-to-production 接口。

### 16.7 管理与审计

- `GET /api/v1/admin/users`
- `GET /api/v1/admin/teams`
- `GET /api/v1/admin/jobs`
- `GET /api/v1/audit-events`
- `GET /api/v1/deletion-requests`
- `POST /api/v1/deletion-requests/{request_id}/approve`

### 16.8 事件响应示例

```json
{
  "id": "4d9a5fd8-cd0a-4d18-974b-e18a912ee781",
  "business_date": "2026-08-28",
  "title": "客户要求确认今日付款状态",
  "severity": "P1",
  "status": "pending_confirmation",
  "reason_codes": ["PAYMENT_DELAY", "EXPLICIT_DEADLINE"],
  "confidence": 0.91,
  "has_cm_am_reply": false,
  "late_arrival": false,
  "assignee": null,
  "evidence_count": 3,
  "revision": 4,
  "data_freshness": {
    "last_synced_at": "2026-08-28T09:58:42Z",
    "status": "fresh"
  }
}
```

---

## 17. 前端架构与界面

### 17.1 页面规划

#### 夜间事件工作台

路由：`/inbox`

包含：

- 日期选择。
- P0—P3 摘要。
- 状态和数据新鲜度。
- 多条件筛选。
- 事件列表。
- 详情抽屉。
- 状态、负责人和建议操作。

#### 事件详情

路由：`/events/:eventId`

包含：

- 事件摘要。
- 等级及原因。
- 原始证据与上下文。
- CM/AM 回复事实。
- 待确认信息。
- AI 内部回复建议。
- 状态历史和审计摘要。

#### 数据沉淀看板

路由：`/data-governance`

包含：

- 授权和群范围。
- 回填与增量状态。
- 数据覆盖与延迟。
- 失败和删除请求。
- AI 调用与费用。
- 候选知识数量。

#### AI 评测

路由：`/ai-evaluation`

包含：

- 模型与 Prompt 版本。
- 离线评测结果。
- 线上采用和编辑反馈。
- 失败率、延迟与成本。

#### 授权与群设置

路由：

- `/settings/authorization`
- `/settings/chats`
- `/settings/notifications`

#### 管理页面

路由：

- `/admin/users`
- `/admin/teams`
- `/admin/jobs`
- `/admin/audit`

只对相应 capability 开放。

### 17.2 页面布局

- 左侧固定主导航。
- 顶部显示当前组织、团队、业务日期和数据更新时间。
- 主区域使用列表 + 详情抽屉，减少反复跳页。
- 移动端允许查看和处理重点事件，但复杂管理功能以桌面端为主。
- 原始消息与 AI 建议使用不同背景和标题。

### 17.3 四种空状态必须区分

前端不得把以下情况显示成同一个“暂无数据”：

1. 本周期真实没有事件。
2. 数据仍在同步。
3. 同步或分析失败。
4. 用户没有权限或授权已经失效。

### 17.4 前端状态管理

- TanStack Query 管理服务器状态、缓存、重新获取和 mutation。
- URL Query 保存日期、等级、状态和负责人筛选。
- 组件局部状态管理抽屉、弹窗和临时输入。
- 不引入 Redux。
- 用户和 capabilities 可放在顶层 Context。
- 原始正文不写入 Local Storage、IndexedDB 或持久化前端缓存。

### 17.5 API 类型

- FastAPI 输出 OpenAPI。
- 当前原型关闭了公开 OpenAPI 地址；目标方案在 CI 中直接从应用对象导出 Schema，生产环境仍可关闭或保护公开文档入口。
- CI 生成前端 TypeScript 类型。
- 生成代码只包含类型和基础 Client，不把业务逻辑生成进组件。
- OpenAPI 发生不兼容变化时 CI 失败。

### 17.6 可访问性

- 目标达到 WCAG AA 的基本要求。
- 所有状态不能只依赖颜色。
- 键盘可完成列表、抽屉、表单和确认操作。
- 错误提示关联具体字段。
- P0/P1 标签包含文字。

---

## 18. 晨间摘要与 Lark 通知

### 18.1 输出原则

- 网页工作台是唯一可信的完整状态。
- Lark 只发送摘要和入口。
- 摘要不包含不必要的敏感正文。
- 只发送到经过配置的内部目标。
- v2.0 不存在外部客户发送路径。

### 18.2 摘要内容

- 业务日期。
- P0 和 P1 数量。
- 待人工确认总数。
- 最高优先级事件标题。
- 数据更新时间。
- 同步失败或不完整提示。
- 工作台链接。

### 18.3 Transactional Outbox

生成摘要和创建投递记录放在同一数据库事务中。

Worker：

1. 领取待发送记录。
2. 重新检查目标和权限配置。
3. 调用 Lark 内部消息接口。
4. 保存平台消息 ID 和发送结果。
5. 使用幂等键避免重复发送。

### 18.4 发送失败

- 工作台不受影响。
- 可重试错误退避重试。
- 权限或目标失效进入 attention_required。
- 10:05 未发送成功触发运维告警。
- 绝不回退到客户群或其他未配置目标。

---

## 19. 数据安全、隐私与合规

### 19.1 数据最小化

- 只同步启用的群。
- 只保存完成 PRD 所需字段。
- 不解析的附件不下载内容。
- AI 只接收当前事件所需的最小上下文。
- 管理总览默认不展示正文。

### 19.2 加密

- HTTPS 由 Caddy 终止。
- Lark Token 使用现有 AES-256-GCM 模式并增加 key_version。
- 消息正文、回复建议和候选知识使用独立用途密钥加密。
- 数据库磁盘和备份同时启用加密。
- 密钥不进入 Git、镜像、日志或聊天。
- 密钥轮换采用“新写入用新版本、后台逐步重加密”。

应用层密钥与数据库在同一台服务器时，主要保护备份和数据库文件泄漏；它不能抵御整台服务器和运行时密钥同时被控制。因此还需要主机加固、最小权限和备份隔离。

### 19.3 日志

禁止记录：

- OAuth code。
- Access Token 或 Refresh Token。
- Cookie。
- 消息正文。
- AI 完整输入。
- 回复建议正文。
- 数据库密文。

允许记录：

- request_id。
- job_id。
- event_id。
- 内部资源 ID。
- 状态码。
- 延迟。
- 结构化错误码。

### 19.4 审计

以下动作必须审计：

- 授权和重新同意。
- 群选择、排除和暂停。
- 查看敏感正文。
- 导出。
- 事件状态和负责人修改。
- AI 调用和建议使用。
- 角色和权限变化。
- 删除请求和执行。
- 临时 break-glass 正文访问。

### 19.5 删除传播

```text
授权撤销或删除请求
        ↓
立即停止新增同步和 AI 调用
        ↓
计算受影响的群与消息
        ↓
删除或失效原始正文
        ↓
失效事件分析与回复建议
        ↓
删除或失效候选知识
        ↓
清除相关前端缓存与导出
        ↓
记录不含正文的完成审计
```

多人授权同群时，单人撤销不一定删除公司仍合法持有的唯一消息。必须依据其他有效授权、公司用途和已确认政策计算是否继续保留；这一规则需要在 D-08 中明确。

### 19.6 AI 供应商

正式启用前需要确认：

- 数据是否用于训练。
- 默认保留时间。
- 是否可以关闭存储。
- UK/EU 数据地域条件。
- 子处理商。
- 数据处理协议。
- 跨境传输依据。
- 删除与审计能力。

技术配置不能代替安全、法务和采购批准。

### 19.7 Secret 管理

v2.0 试点继续使用服务器受限 EnvironmentFile：

- 文件只允许服务账户和管理员读取。
- systemd unit 使用最小权限。
- 定期轮换 Lark App Secret、Token 加密密钥和模型 API Key。
- Secret 轮换有运行手册和回滚步骤。

后续迁移多服务器时再接入云 Secret Manager 或 Vault。

---

## 20. 异常与降级

### 20.1 Lark 授权失效

- 停止新增读取。
- 受影响授权标记 attention_required。
- 保留最后成功时间。
- 提示重新授权。
- 不显示虚假“零消息”。

### 20.2 单群不可读

- 只标记该群。
- 其他群继续同步。
- 看板显示缺失范围。

### 20.3 同步延迟

- 显示数据新鲜度。
- 恢复后补偿。
- 迟到消息标记原窗口。
- 超过 P95 目标触发告警。

### 20.4 AI 不可用

- 规则分级继续。
- 工作台继续。
- 固定回复框架可用。
- 建议显示暂不可生成。

### 20.5 晨间摘要失败

- 网页结果保持可用。
- 通知重试。
- 告警运维人员。
- 不改投递目标。

### 20.6 数据库或 Worker 故障

- Readiness 失败时停止接收需要数据库的流量。
- Worker 租约过期后任务可恢复。
- 进程由 systemd 重启。
- 数据库恢复后从游标和幂等键继续。

### 20.7 权限失败

- 返回 403。
- 不返回资源是否存在的敏感细节。
- 写入拒绝审计。
- 前端显示“无权访问”，不伪装成数据不存在。

---

## 21. 可观测性

### 21.1 健康检查

- `/health/live`：进程存活。
- `/health/ready`：数据库、迁移版本和关键配置可用。
- Worker heartbeat。
- Scheduler heartbeat。

健康检查不得调用 OpenAI、DeepSeek 或 Lark 真实业务接口。

### 21.2 结构化日志

日志采用 JSON，至少包含：

- timestamp。
- level。
- service。
- environment。
- request_id 或 job_id。
- organization_id。
- action。
- status。
- duration_ms。
- error_code。

### 21.3 指标

必须监控：

- 授权成功和失败。
- 可读、不可读和暂停群。
- 回填进度。
- 增量同步延迟 P50/P95/P99。
- 最老 queued job。
- 各任务类型成功、重试和失败。
- 窗口关闭时间。
- 10:05 摘要成功率。
- 事件数量和 P0—P3 分布。
- AI 结构化输出失败率。
- AI 延迟、Token 和费用。
- 数据库连接、磁盘和备份。

### 21.4 告警

至少配置：

- 数据新鲜度 P95 超过 5 分钟。
- 10:05 后摘要仍未成功。
- P0 处理任务积压。
- OAuth/403 错误突然增加。
- AI Schema 错误或超时显著增加。
- AI 日预算或月预算超限。
- 数据库空间不足。
- 备份失败或恢复演练过期。
- Worker/Scheduler heartbeat 丢失。

### 21.5 错误追踪

可以使用 Sentry，但必须：

- 关闭默认 PII 收集。
- 对请求体、Cookie、Authorization 和消息字段做服务端清洗。
- 不上传正文或完整模型输入。
- 生产启用前完成一次敏感数据泄漏测试。

---

## 22. 测试方案

### 22.1 单元测试

覆盖：

- 18:30 和 10:00 边界。
- 英国夏令时切换。
- 窗口业务日期。
- P0 固定规则。
- 状态机。
- 事件 fingerprint。
- 幂等键。
- 权限矩阵。
- 数据范围。
- AI Schema 校验。
- 删除传播计划。

### 22.2 集成测试

覆盖：

- OAuth state 和 PKCE。
- Token 刷新并发锁。
- Lark 分页、429、5xx、401 和 403。
- 回填断点续传。
- 多 Worker 同时领取任务。
- 同群多人授权的公司级去重。
- 编辑、撤回和失权传播。
- Window Scheduler 幂等。
- 晨间 Outbox 重试。
- OpenAI/DeepSeek 超时、非法 JSON 和 Schema 错误。
- 数据库迁移升级和回滚。

使用真实 PostgreSQL 测试实例，不用 SQLite 代替核心数据库测试。

### 22.3 API 契约测试

- 验证 OpenAPI。
- 验证前端生成类型。
- 验证错误格式。
- 验证 cursor pagination。
- 验证 Idempotency-Key。
- 验证 revision 冲突返回 409。

### 22.4 前端测试

- Vitest + Testing Library：组件和权限显示。
- Playwright：各角色端到端流程。
- 覆盖真实零事件、同步中、同步失败和无权限四种状态。
- 验证正文不会持久化到浏览器存储。

### 22.5 安全测试

- 横向越权。
- 跨组织访问。
- Team Lead 超范围读取。
- System Admin 正文访问。
- CSRF。
- Session 固定与失效。
- 导出权限。
- 删除后缓存和派生数据。
- Prompt injection。
- 日志敏感信息扫描。

### 22.6 AI 回归测试

- 至少 300 条脱敏样本。
- 固定测试集和隐藏测试集。
- 人工双人复核关键 P0/P1 样本。
- 对比规则、OpenAI 和 DeepSeek。
- 每次发布记录模型、Prompt、Schema 和结果。

### 22.7 性能测试

至少验证：

- 500 万消息索引下的列表与聚合查询。
- 1,000 群的调度公平性。
- 大群历史回填期间增量同步不被阻塞。
- 50 并发工作台用户。
- 10:00 至 10:05 汇总高峰。
- 删除传播和重算不会阻塞在线查询。

---

## 23. CI/CD 与代码管理

### 23.1 GitHub

- 代码进入公司 GitHub Organization。
- `main` 分支保护。
- Pull Request 审核后合并。
- 禁止真实 Secret、Token、消息样本和生产导出提交到仓库。
- 使用 CODEOWNERS 管理安全和数据模块。

### 23.2 后端 CI

每个 PR 执行：

- Ruff lint 和 format check。
- 类型检查。
- pytest 单元与集成测试。
- Alembic migration check。
- 依赖漏洞扫描。
- Secret scanning。

### 23.3 前端 CI

每个 PR 执行：

- TypeScript typecheck。
- ESLint。
- 单元测试。
- 生产构建。
- 关键 Playwright 流程。
- OpenAPI 类型一致性检查。

### 23.4 发布

1. CI 生成不可变构建产物。
2. 先在 staging 使用脱敏或合成数据验证。
3. 备份数据库。
4. 执行向前兼容迁移。
5. 部署 API、Worker、Scheduler 和前端。
6. 执行 smoke test。
7. 逐步开启 feature flag。
8. 观察指标后完成发布。

---

## 24. 部署、备份与恢复

### 24.1 生产进程

systemd 服务建议拆为：

- `lark-assistant-api.service`
- `lark-assistant-scheduler.service`
- `lark-assistant-worker@.service`
- `lark-assistant-retention.service` 或 timer。
- `lark-assistant-backup.service` 或 timer。

### 24.2 网络

- 仅 Caddy 开放 80/443。
- FastAPI 只监听 127.0.0.1 或 Unix socket。
- PostgreSQL 不开放公网。
- 出站只允许必要的 Lark、OpenAI/DeepSeek 和备份目标。
- 管理 SSH 使用密钥、最小人员和审计。

### 24.3 环境

至少区分：

- local：合成数据。
- staging：脱敏或合成数据，不复制生产正文。
- production：真实授权数据。

### 24.4 备份

- PostgreSQL 每日自动备份。
- 备份使用独立加密。
- 备份复制到异地目标。
- 保留策略与消息删除要求一致。
- 备份访问写入审计。
- 每月至少完成一次可重复恢复演练。

### 24.5 恢复目标

试点建议目标，正式上线前确认：

- RPO：24 小时以内。
- RTO：4 小时以内。

如果业务要求更高，需要增加更频繁 WAL 归档或托管数据库。

### 24.6 回滚

- 数据库迁移采用 expand-and-contract。
- 发布期间旧代码和新代码可在短时间内共同读取兼容 Schema。
- 破坏性删除在独立后续版本执行。
- 功能通过 feature flag 关闭。
- AI 和 Lark 通知可以独立关闭，不影响同步和工作台。

---

## 25. 从现有原型迁移

禁止一次性“大爆炸重写”。

### Phase 0：治理前置

- 确认授权说明版本。
- 确认群默认范围。
- 确认正文保留和删除时限。
- 确认角色和数据范围。
- 确认 OpenAI/DeepSeek 供应商政策。
- 创建 GitHub、CI、staging、备份和监控基线。

### Phase 1：组织级数据底座

- 新增 organizations、users、memberships 和 teams。
- 将登录用户与 lark_authorizations 分离。
- 新增 chats 和 chat_grants。
- 新增公司级 messages。
- 保留旧 message_ledger，先进行双写和计数校验。
- 引入 SQLAlchemy 和 Alembic。
- 拆分 main.py 与 worker.py。

切换顺序：

```text
旁路建新表
  ↓
双写旧表和新表
  ↓
比较消息数、去重数和游标
  ↓
新页面只读新表
  ↓
停止旧表写入
  ↓
观察期结束后归档旧表
```

### Phase 2：正文、权限与删除

- 发布新版授权说明。
- 重新取得同意。
- 实现群选择。
- 启用正文加密入库。
- 实现数据范围检查。
- 实现暂停、撤销和删除传播。
- 通过横向越权与删除测试。

### Phase 3：窗口、事件与 React 工作台

- 实现 business_windows。
- 实现规则分级和事件聚合。
- 实现人工状态和负责人。
- 上线 React 夜间事件工作台。
- 保留旧统计页作为回退。

### Phase 4：晨间交接

- 实现 summary snapshot。
- 实现 Lark 内部发送适配。
- 实现 Outbox、重试和 10:05 监控。
- 验证零事件、失败和迟到数据。

### Phase 5：AI 试点

- 完成 300 条脱敏评测集。
- 接入已批准的主供应商。
- 先离线试运行。
- 再向小范围用户开放内部建议。
- 记录反馈、成本和错误。
- 保持自动外发能力为不存在。

### Phase 6：候选知识与治理看板

- 提取结构化事实。
- 建立候选知识和来源关系。
- 上线数据治理看板。
- 正式知识库和 RAG 另立项目。

---

## 26. Feature Flags

必须支持：

- `message_body_ingestion`
- `event_engine`
- `ai_analysis`
- `reply_suggestions`
- `lark_morning_summary`
- `knowledge_candidates`
- `late_p0_notification`

开关至少支持：

- 全局关闭。
- 按组织。
- 按试点用户。
- 按供应商。

关闭 AI 不得关闭事件规则；关闭通知不得关闭网页工作台。

---

## 27. PRD 功能映射

### FR-001 授权与数据范围管理

技术实现：

- Identity。
- Lark Authorization。
- Chat Scope。
- consent_version。
- RBAC + resource scope。
- 撤销和删除。

关键测试：

- 未授权读取为零。
- 群选择生效。
- 授权失效停止同步。
- 不同角色和群范围不能横向越权。

### FR-002 历史回填与持续同步

技术实现：

- Ingestion。
- sync_cursors。
- PostgreSQL jobs。
- 公司级 messages 唯一约束。
- 编辑、撤回和失权传播。

关键测试：

- 分页续传。
- 重叠窗口。
- 429/5xx。
- 同群多人授权去重。
- P95 新鲜度。

### FR-003 夜间消息窗口

技术实现：

- Windowing。
- Scheduler。
- `zoneinfo`。
- business_windows。
- 迟到数据。

关键测试：

- 18:30/10:00 边界。
- 夏令时。
- 重复调度。
- 迟到补偿。

### FR-004 待关注事件与紧急程度

技术实现：

- Event Engine。
- 固定规则。
- event_messages。
- event_analyses。
- 状态机。

关键测试：

- P0 不可被 AI 降级。
- 证据 100% 可追溯。
- 同主题聚合。
- 回复事实不自动关闭。

### FR-005 次日上午结果

技术实现：

- summary_snapshots。
- notification_deliveries。
- Transactional Outbox。

关键测试：

- 10:05 前完成。
- 重复任务不重复发送。
- 零事件和失败清楚区分。

### FR-006 AI 回复建议

技术实现：

- AI Gateway。
- Provider 协议。
- Pydantic Schema。
- reply_suggestions。
- suggestion_feedback。
- 规则降级。

关键测试：

- 非法 JSON。
- 证据越界。
- 模型超时。
- 未批准供应商。
- 外部自动发送次数为零。

### FR-007 夜间事件工作台

技术实现：

- React/Vite 前端。
- Event API。
- Reporting read model。
- capabilities 驱动导航和操作。

关键测试：

- 筛选。
- 详情。
- 状态更新。
- 各角色页面。
- 四种空状态。

### FR-008 数据沉淀看板

技术实现：

- Reporting。
- Governance。
- 同步、AI、删除和知识聚合。

关键测试：

- 总览默认无正文。
- 数据延迟和失败准确。
- AI 费用与调用量可核对。

### FR-009 知识库准备

技术实现：

- knowledge_candidates。
- knowledge_sources。
- 来源失效传播。

关键测试：

- 无来源不能入队。
- 默认未审核。
- 客户专属内容不能自动升级为全局。
- 删除传播。

---

## 28. 验收门槛

进入小范围生产试点前，至少满足：

1. 新版授权说明已发布，正文用户已重新同意。
2. 用户可以选择、暂停和撤销群同步。
3. 同群多人授权只保存和分析一份消息。
4. 未授权和跨范围访问测试全部拒绝。
5. 18:30—10:00、夏令时和迟到数据测试通过。
6. 事件都有等级、理由和有效证据。
7. AI 关闭时，规则事件和工作台正常运行。
8. AI 无法自动向外部会话发送消息。
9. 10:05 晨间摘要幂等且失败可重试。
10. 删除请求能传播到正文、事件分析、建议和候选知识。
11. 生产日志和错误追踪不包含敏感正文或 Token。
12. 备份成功并完成一次恢复演练。
13. CI、staging、发布和回滚流程可重复。
14. 300 条脱敏 AI 评测完成并达到产品确认门槛。

---

## 29. 暂不引入的技术

以下技术不是永远禁止，而是当前没有足够收益：

- **Django：** 会增加迁移成本，不能直接复用当前 FastAPI。
- **纯 Jinja/HTMX 新工作台：** 可用于过渡，但后续多页面、复杂筛选和权限交互会更难维护。
- **Next.js：** 内部系统不需要 SEO 或 SSR。
- **Redux：** TanStack Query、URL 和组件状态已足够。
- **Redis/Celery：** 当前 PostgreSQL 队列已经可用。
- **微服务：** 会增加部署、追踪、权限和删除复杂度。
- **Kubernetes：** 单机试点没有必要。
- **Kafka：** 当前没有高吞吐跨服务事件流。
- **Elasticsearch：** PostgreSQL 足以支持试点搜索和聚合。
- **独立向量数据库：** 正式 RAG 尚未立项。
- **LangChain/Agent 平台：** 当前只是结构化分类与回复草稿。
- **自动回复：** 明确不属于 v2.0。

---

## 30. 上线前仍需业务确认

这些问题不会阻止技术文档完成，但会影响配置和验收：

1. 试点实际人数、群数量和历史消息量是否明显高于本文假设。
2. 默认同步全部可见群，还是只同步用户主动选择的白名单。
3. 团队负责人可以查看哪些客户、项目和群的正文。
4. System Admin 与 Security/Compliance 的临时正文访问审批流程。
5. 正文保留天数、删除完成时限和备份中的删除规则。
6. 晨间摘要发送给本人、团队群还是负责人群。
7. P0 是否在夜间通知值班渠道。
8. OpenAI 与 DeepSeek 哪一个被批准用于生产消息，允许的数据地域和合同条件。
9. 周末、英国公共假日和特殊工作日是否发送摘要。
10. 迟到 P0/P1 是否补发通知。

---

## 31. 官方技术参考

- [FastAPI：大型应用与多文件组织](https://fastapi.tiangolo.com/tutorial/bigger-applications/)
- [React：使用 TypeScript](https://react.dev/learn/typescript)
- [Vite：开发与生产静态构建](https://vite.dev/guide/)
- [shadcn/ui：Open Code 与可定制组件](https://ui.shadcn.com/docs)
- [TanStack Query：服务器状态获取、缓存和同步](https://tanstack.com/query/latest/docs/framework/react/overview)
- [SQLAlchemy 2 官方教程](https://docs.sqlalchemy.org/en/20/tutorial/)
- [Alembic 官方文档](https://alembic.sqlalchemy.org/en/latest/)
- [PostgreSQL：`FOR UPDATE SKIP LOCKED`](https://www.postgresql.org/docs/current/sql-select.html)
- [PostgreSQL：Row-Level Security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
- [OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
- [OpenAI API 数据控制与保留](https://developers.openai.com/api/docs/guides/your-data)
- [DeepSeek Responses API](https://api-docs.deepseek.com/api/create-response/)
- [DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)

---

## 32. 变更记录

### v2.0｜2026-08-28｜技术评审稿

- 对齐 PRD v2.0 的夜间消息、历史语料、AI 建议和双看板。
- 基于当前 `lark-message-stats` 代码事实设计渐进迁移路径。
- 后端继续选择 Python/FastAPI。
- 新前端选择 React/TypeScript/Vite/shadcn/ui。
- 权限确定为 RBAC + 数据范围。
- 后台任务继续使用 PostgreSQL 队列。
- 补充数据模型、API、AI、权限、安全、测试、部署和 PRD 验收映射。
- 全文不使用 Markdown 表格，便于重新导入飞书。
