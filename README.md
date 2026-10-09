# Lark CM/AM Assistant

TT Media 内部工作台：同步已授权的 Lark 消息，管理已确认达人、账号和多位负责人，提供群用途分类、维护优先级、指定时段总结及人工核对的回复建议。群发由本人准备、确认后进入独立发送队列；桌面回复助手使用同一后端。

## 从哪里看

- 产品和开发都从 [文档总入口](docs/README.md) 开始：当前只维护三份业务规则和一份技术说明。
- [本地开发](backend/README.md) · [部署与回退](docs/runbooks/deploy-mvp.md) · [AI 协作约定](AGENTS.md)。
- 身份、账号、负责人和群用途分别管理；不再使用 CAP/非 CAP 分类，也不恢复旧三阶段泛扫描。
- 各 AI 功能独立配置，启用状态以线上配置为准；发布不自动开关功能或重跑历史。人工关系与原始数据保留。

## 代码在哪里

| 目录 | 内容 |
|---|---|
| `backend/app/` | FastAPI 路由、业务逻辑、后台 worker |
| `backend/app/templates/`、`backend/app/static/` | 当前正式界面：Jinja2、HTMX、Tabler 和原生 JavaScript |
| `backend/migrations/`、`backend/tests/` | SQL 迁移和测试 |
| `infra/` | Caddy 与 systemd 配置 |
| `docs/` | 当前规则、工程手册、明确标注日期的历史记录 |

网页使用服务端模板，不是独立 React 工作台。桌面客户端在独立的 `lark-reply-assistant-desktop` 仓库，使用 Electron、React 和原生采集助手；长期知识库及无人值守自动回复不属于已交付能力。

## 工程底线

GitHub 是代码发布源，不直接修改生产应用文件。密钥、令牌、生产消息、原始导入文件和备份不得提交；消息正文加密保存，权限及审计在服务端检查。关联某个用户不增加聊天访问权。
