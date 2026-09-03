# Lark CM/AM Assistant 部署手册

本手册部署 Lark OAuth、授权用户可见的群聊/私聊、加密消息正文和 owner 专用消息管理台。不保存附件二进制或成员名单。

## 先决条件

- Hetzner 主机已有 PostgreSQL、Caddy、`larkstats` 系统用户、`/etc/lark-message-stats/app.env` 和 Python 虚拟环境。
- 仓库位于 `/opt/lark-message-stats`，虚拟环境位于 `/opt/lark-message-stats/venv`。
- `app.env` 不进入 Git，且权限保持为 `root:larkstats` / `0640`。
- Lark 应用已启用消息同步所需的用户权限：`offline_access`、`im:chat:read`、`im:message:readonly`。`im:chat.members:read` 仅在需要展示成员总数时额外启用。
- Lark OAuth 回调地址为 `https://larkbot.ttapp.help/auth/lark/callback`；服务器公网出口 IP 已在应用白名单中。

## 发布步骤

在服务器上执行。命令不会打印 App Secret、OAuth Token 或消息正文。`005_message_bodies_and_p2p.sql` 为现有规范化表增加正文加密与 P2P 状态字段，因此发布前必须短暂停止 Web/Worker 并完成数据库备份。

```bash
cd /opt/lark-message-stats
sudo git pull --ff-only origin main
sudo /opt/lark-message-stats/venv/bin/pip install -r backend/requirements.txt
sudo install -m 0644 infra/systemd/lark-message-stats-web.service /etc/systemd/system/lark-message-stats-web.service
sudo install -m 0644 infra/systemd/lark-message-stats-worker.service /etc/systemd/system/lark-message-stats-worker.service
# Caddy 配置由多项服务共享。普通应用发布只校验线上配置；不得用仓库
# 中的单应用示例覆盖它。只有合并并复核全部现有路由后才单独更新/reload。
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl stop lark-message-stats-web.service lark-message-stats-worker.service
sudo bash -euo pipefail -c '
  umask 077
  backup_path="/var/backups/lark-message-stats/pre-release-$(date -u +%Y%m%dT%H%M%SZ).dump"
  sudo -u postgres pg_dump --format=custom --dbname=lark_message_stats > "$backup_path"
  test -s "$backup_path"
  pg_restore --list "$backup_path" >/dev/null
  sha256sum "$backup_path" > "$backup_path.sha256"
  chmod 0600 "$backup_path" "$backup_path.sha256"
  echo "Verified backup: $backup_path"
'
sudo systemd-run --quiet --wait --pipe --collect \
  --property=User=larkstats --property=Group=larkstats \
  --property=EnvironmentFile=/etc/lark-message-stats/app.env \
  --property=WorkingDirectory=/opt/lark-message-stats/backend \
  /opt/lark-message-stats/venv/bin/python scripts/migrate.py
sudo systemctl daemon-reload
sudo systemctl restart lark-message-stats-web.service lark-message-stats-worker.service
sudo systemctl --no-pager --full status lark-message-stats-web.service lark-message-stats-worker.service
curl --fail --silent --show-error https://larkbot.ttapp.help/healthz
```

在第一次启用成员人数功能时，`app.env` 的 `LARK_OAUTH_SCOPES` 还必须包含 `im:chat.members:read`；保留既有 scope，不要覆盖 App Secret 或加密密钥。正文与 P2P 已用当前 `im:message:readonly` 完成真实 user-token 预检，不需要为了本版本替换为更宽泛的 scope。

预期健康检查返回 `{"status":"ok","database":"ok",...}`。若服务未启动，先查看不含消息正文的 systemd 日志：

```bash
sudo journalctl -u lark-message-stats-web.service -u lark-message-stats-worker.service -n 100 --no-pager
```

## Lark 权限与验收

1. 已有 CM/AM 必须打开 `https://larkbot.ttapp.help/` 并重新完成 OAuth；这是正文和 P2P 采集的明确授权边界。
2. 回到“消息提醒中心”，确认显示接入状态和最近检查时间；个人页不展示会话或消息统计。
3. 若管理员启用成员人数功能，需在 Lark 开发者后台发布 `im:chat.members:read`，并让测试用户重新 OAuth；该数据仅用于内部管理台，不会阻塞消息同步。
4. owner 重新登录后进入 `https://larkbot.ttapp.help/admin/messages`，选择 CM/AM 和会话，确认仅在新版授权后的回填完成后出现正文。不要在终端、日志或 Git 中输出正文。

## 回退原则

先停止发布并保留数据库。不得通过删除数据库或令牌来“回退”。部分迁移会删除派生数据，仅回滚代码不能恢复这些数据。若迁移提交后发现异常，保持 Web/Worker 停机，先把发布前的 custom dump 恢复到一个空白临时数据库并完成校验，再用已验证备份完整替换 live 数据库；不得把 full dump 直接叠加到非空 live 数据库。最后恢复上一个已验证的 Git 提交和对应 systemd 单元。备份包含清理前数据，权限必须保持 `0600`，并按内部保留策略安全删除。
