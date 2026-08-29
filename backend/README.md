# Lark Message Stats (测试版)

这是一个独立于现有 `lark-user-ingress/` Node 骨架的 Python 服务。

它让 CM/AM 通过 Lark OAuth 授权后，统计自己当前可见的**群聊**：

- 已加入群聊数；
- 已同步消息总数；
- 当前授权用户发送的消息数；
- 每个可见群的 Lark 人类成员总数；
- 最近一次同步状态。

## 数据边界

- 仅调用用户身份的 `im:chat:read`、`im:chat.members:read` 与只读的 `im:message:readonly`，并使用 `offline_access` 刷新授权。
- 不读取或处理 P2P 单聊，不发送 Lark 消息。
- 不保存消息正文、附件、@ 提及、发送者姓名或完整发送者资料。
- 为了分页断点、去重和计数，数据库仅保存 `message_id`、`chat_id`、创建时间和“是否为授权用户发送”的布尔值；成员信息只保存 Lark 返回的人类成员总数，不保存成员名单。

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

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # 仅本地；不要提交真实密钥
.venv/bin/python -m pytest
```

生产部署使用仓库根目录 `../infra/` 中的 systemd 和 Caddy 配置；具体步骤见 [`docs/runbooks/deploy-mvp.md`](../docs/runbooks/deploy-mvp.md)。App Secret 必须由管理员在服务器终端的隐藏输入提示中录入，不能提交到 Git 或发送到聊天。
