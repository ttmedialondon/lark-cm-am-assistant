# Lark CM/AM Assistant

TT Media 内部助手：同步已授权的 Lark 聊天、维护达人与 CM 的关联、按 P0–P3 整理指定时段的消息。不自动发送消息，不生成回复建议。

## 从哪里看

- 产品和开发都从 [文档总入口](docs/README.md) 开始：当前只维护三份业务规则和一份技术说明。
- [本地开发](backend/README.md) · [部署与回退](docs/runbooks/deploy-mvp.md) · [AI 协作约定](AGENTS.md)。
- 2026-09-11：改为按人准备少量业务对话，再明确启动 AI；首批最多 30 人、共享 3 路并发，只做核对预览。handle 和 CAP 类型可后补，Excel 补充 CAP 名单及账号资料。详见 [达人管理](docs/product/creator-management.md)。
- 旧全量聊天扫描已停用。本轮保留已有正式名单、联系人、原始消息和授权；历史数据重置不重复执行，不自动重传 Excel 或扫描所有 CM。

## 代码在哪里

| 目录 | 内容 |
|---|---|
| `backend/app/` | FastAPI 路由、业务逻辑、后台 worker |
| `backend/app/templates/`、`backend/app/static/` | 当前正式界面：Jinja2、HTMX、Tabler 和原生 JavaScript |
| `backend/migrations/`、`backend/tests/` | SQL 迁移和测试 |
| `infra/` | Caddy 与 systemd 配置 |
| `docs/` | 当前规则、工程手册、明确标注日期的历史记录 |

当前不是独立 React 工作台；早期 React、实时告警、回复建议和知识库大方案不作为本轮待办。

## 工程底线

GitHub 是代码发布源，不直接修改生产应用文件。密钥、令牌、生产消息、原始导入文件和备份不得提交；消息正文加密保存，权限及审计在服务端检查。关联某个用户不增加聊天访问权。
