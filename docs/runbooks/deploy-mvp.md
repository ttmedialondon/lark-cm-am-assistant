# Message Stats MVP 部署手册

本手册部署的是群聊统计 MVP：Lark OAuth、可见群列表、群内人类成员总数和已同步消息记录数。它不保存消息正文、成员名单或附件。

## 先决条件

- Hetzner 主机已有 PostgreSQL、Caddy、`larkstats` 系统用户、`/etc/lark-message-stats/app.env` 和 Python 虚拟环境。
- 仓库位于 `/opt/lark-message-stats`，虚拟环境位于 `/opt/lark-message-stats/venv`。
- `app.env` 不进入 Git，且权限保持为 `root:larkstats` / `0640`。
- Lark 应用已启用消息统计所需的用户权限：`offline_access`、`im:chat:read`、`im:message:readonly`。`im:chat.members:read` 仅在需要展示成员总数时额外启用。
- Lark OAuth 回调地址为 `https://larkbot.ttapp.help/auth/lark/callback`；服务器公网出口 IP 已在应用白名单中。

## 发布步骤

在服务器上执行。命令不会打印 App Secret 或 OAuth Token。

```bash
cd /opt/lark-message-stats
sudo git pull --ff-only origin main
sudo /opt/lark-message-stats/venv/bin/pip install -r backend/requirements.txt
sudo install -m 0644 infra/systemd/lark-message-stats-web.service /etc/systemd/system/lark-message-stats-web.service
sudo install -m 0644 infra/systemd/lark-message-stats-worker.service /etc/systemd/system/lark-message-stats-worker.service
sudo install -m 0644 infra/caddy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo bash -c 'set -a; . /etc/lark-message-stats/app.env; set +a; cd /opt/lark-message-stats/backend && /opt/lark-message-stats/venv/bin/python scripts/migrate.py'
sudo systemctl daemon-reload
sudo systemctl reload caddy
sudo systemctl restart lark-message-stats-web.service lark-message-stats-worker.service
sudo systemctl --no-pager --full status lark-message-stats-web.service lark-message-stats-worker.service
curl --fail --silent --show-error https://larkbot.ttapp.help/healthz
```

在第一次启用成员人数功能时，`app.env` 的 `LARK_OAUTH_SCOPES` 还必须包含 `im:chat.members:read`；保留既有 scope，不要覆盖 App Secret 或加密密钥。

预期健康检查返回 `{"status":"ok","database":"ok",...}`。若服务未启动，先查看不含消息正文的 systemd 日志：

```bash
sudo journalctl -u lark-message-stats-web.service -u lark-message-stats-worker.service -n 100 --no-pager
```

## Lark 权限与验收

1. 测试 CM/AM 打开 `https://larkbot.ttapp.help/` 并完成 OAuth。
2. 回到仪表盘。群列表通常会先出现，随后每群的“已同步消息记录”和同步状态持续更新。
3. 需要重新核对一个群的历史时，点击该行的“同步全部历史”。页面会显示“已请求同步”或“正在主动同步”；Worker 串行重新扫描 Lark API 可返回的历史，已有消息 ID 自动去重。
4. 若管理员启用成员人数功能，需在 Lark 开发者后台发布 `im:chat.members:read`，并让测试用户重新 OAuth；成员人数来自 Lark 的 `user_total`。未启用时显示“Lark 未返回”，但不会阻塞消息统计。

## 回退原则

先停止发布并保留数据库。不得通过删除数据库或令牌来“回退”。若新版本异常，恢复上一个已验证的 Git 提交和对应 systemd 单元，然后重启 Web/Worker；`002_chat_member_counts.sql` 只增加可空/默认字段，旧版本可以安全忽略它。
