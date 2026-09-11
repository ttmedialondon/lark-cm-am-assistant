# Lark CM/AM Assistant

TT Media 内部助手：同步已授权的 Lark 聊天并识别人员类别；通过独立第二步判断达人归属，人工确认后关联负责人。保留 P0–P3 指定时段总结功能；不自动发送消息，不生成回复建议。

## 从哪里看

- 产品和开发都从 [文档总入口](docs/README.md) 开始：当前只维护三份业务规则和一份技术说明。
- [本地开发](backend/README.md) · [部署与回退](docs/runbooks/deploy-mvp.md) · [AI 协作约定](AGENTS.md)。
- 2026-09-11：保留全员六分类，旧三阶段/Excel 工程不恢复；后台 AI 功能新增独立的达人归属判断，选 CM、先本地筛选、明确启动后才调用 OpenAI，人工确认后写人级关系。详见 [人员身份与达人名单](docs/product/creator-management.md)。
- 已有名单、分类、原始消息、授权和头像保留；不重复清库、不自动导入或启动新的扫描，消息总结维持关闭。

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
