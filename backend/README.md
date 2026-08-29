# Lark Message Stats (测试版)

这是一个独立于现有 `lark-user-ingress/` Node 骨架的 Python 服务。

它让 CM/AM 通过 Lark OAuth 授权后，统计自己当前可见的**群聊**：

- 已加入群聊数；
- 已同步消息总数；
- 当前授权用户发送的消息数；
- 每个可见群的 Lark 人类成员总数；
- 最近一次同步状态。
- 对单个群手动请求重新扫描 Lark API 可返回的全部历史。

## 数据边界

- 消息统计仅调用用户身份的 `im:chat:read` 与只读的 `im:message:readonly`，并使用 `offline_access` 刷新授权；`im:chat.members:read` 仅在管理员额外启用成员总数展示时使用。
- 不读取或处理 P2P 单聊，不发送 Lark 消息。
- 不保存消息正文、附件、@ 提及、发送者姓名或完整发送者资料。
- 为了分页断点、去重和计数，数据库将公司级 `chat_messages` 与授权用户级 observation 分开保存：只记录 `message_id`、`chat_id`、创建时间、撤回状态和“是否为授权用户发送”的布尔值；不保存消息正文、附件或成员名单。

## 架构

```text
浏览器 / Lark 网页应用
        │ OAuth
        ▼
FastAPI + Jinja2 + HTMX + Tabler    ←→ PostgreSQL
        │                                      ▲
        └── 独立 systemd worker ─── Lark Open API（用户令牌）
```

`app/main.py` 只处理网页和 OAuth；`app/worker.py` 作为独立进程领取 PostgreSQL 任务。历史回填不使用 FastAPI `BackgroundTasks`。

仪表盘的“同步全部历史”按钮不会直接清除分页游标，也不会创建并发 worker。它只为当前授权用户的该群写入一个请求标记；现有串行 worker 会在当前分页安全结束后，从头回填该群。公司级 `chat_messages` 以消息 ID 去重，因此重扫或多人看到同一群都不会重复统计。

## 内部管理台

`/admin` 是独立的只读运营管理台，展示公司范围的授权、唯一群、去重消息与同步任务。它由数据库的 `admin_roles` 强制控制；无角色用户即使直接输入 URL 也会得到 403。当前部署只写入一个 `owner`，不自动授予其他 CM/AM。

数据模型与页面口径见 [`docs/as-built/admin-console-v0.3.md`](../docs/as-built/admin-console-v0.3.md)。

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # 仅本地；不要提交真实密钥
.venv/bin/python -m pytest
```

生产部署使用仓库根目录 `../infra/` 中的 systemd 和 Caddy 配置；具体步骤见 [`docs/runbooks/deploy-mvp.md`](../docs/runbooks/deploy-mvp.md)。App Secret 必须由管理员在服务器终端的隐藏输入提示中录入，不能提交到 Git 或发送到聊天。
