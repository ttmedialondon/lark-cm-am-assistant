# Lark CM/AM Assistant 部署手册

本手册用于应用代码或数据库变更的发布。当前系统同步授权范围内的群聊/私聊、加密正文和最新成员快照，后台按页面权限开放；不保存附件二进制。业务与权限边界见 [文档总入口](../README.md)。

纯文档更新只提交并推送 Git，不执行下方安装、迁移、停机或重启步骤，也不重新开启任何 AI 功能。正文采集权限已有效的用户，不因普通发布重复 OAuth。

## 先决条件

- 生产 SSH：`ssh -i /Users/warrior/.ssh/lark_ingress_setup_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes root@91.99.50.232`（2026-09-20 验证）。本机 `id_ed25519_ttmedia_lark_cm_am` 用于 GitHub，不是生产主机登录密钥。

- Hetzner 主机已有 PostgreSQL、Caddy、`larkstats` 系统用户、`/etc/lark-message-stats/app.env` 和 Python 虚拟环境。
- 仓库位于 `/opt/lark-message-stats`，虚拟环境位于 `/opt/lark-message-stats/venv`。
- `app.env` 不进入 Git，且权限保持为 `root:larkstats` / `0640`。
- Lark 应用已启用消息同步所需的用户权限：`offline_access`、`im:chat:read`、`im:message:readonly`、`im:chat.members:read`。最后一项用于成员资料与快照，不只是展示人数。
- 人员资料补充权限：`contact:contact.base:readonly`、`contact:user.base:readonly`，必须同时加入服务器 `LARK_OAUTH_SCOPES`；当前应用已开通并发布，但旧用户令牌不会自动获得。新 OAuth 回调只接受令牌实际返回的完整授权范围，续期不申请新增 scope。
- Lark OAuth 回调地址为 `https://larkbot.ttapp.help/auth/lark/callback`；服务器公网出口 IP 已在应用白名单中。

## 发布步骤

在服务器上执行。命令不会打印 App Secret、OAuth Token 或消息正文。先核对待发布差异和迁移，准备代码、依赖及服务配置，再停止 Web/Worker、验证数据库备份、应用迁移并启动；不要为了文档清理运行这些步骤。

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

首次接入成员快照时，`app.env` 的 `LARK_OAUTH_SCOPES` 必须包含 `im:chat.members:read`，应用权限发布后由缺少该 scope 的用户重新授权。保留既有 scope，不要覆盖 App Secret 或加密密钥；不为普通发布扩大权限。

### 仅为本人补充发送权限

`im:message`、`im:message.send_as_user` 必须在当前上线应用的**用户身份权限**中开通。不要将它们加入全局 `LARK_OAUTH_SCOPES`，避免让只需同步消息的 CM/AM 也被要求授权发送。

已经同意正文授权、当前状态为 active 或 attention_required 的人员打开 `/auth/lark/start?send=1&account_id=<本人授权UUID>`，经 Lark 明确同意后补充本人发送授权。不能代别人授权，也不能恢复已撤销的授权。一次性 OAuth state 保存本次权限和目标账号；回调同时核对本站会话、state 和真实 Lark 账号，拒绝账号切换、权限缺失、失效或重复的旧链接。成功时只更新本人令牌、资料和登录会话，保留原有正文授权、同步游标和业务关系，并记录 `oauth_send_permissions_upgraded`，不触发全量回填。已开通群发功能的人员回群发页，其他人员回个人首页；此入口不会增加后台访问权限。

旧令牌不会因应用权限已开通自动获得发送权限。验收必须检查新令牌实际返回的两项 scope；此入口不会发送消息，也不会开启群发队列。普通登录与既有资料权限升级仍按原流程执行；后续普通登录若未返回发送 scope，需再次使用此增权入口。

#### 群发页面与正式发送

采用按功能增量授权，参考 [Google](https://developers.google.com/identity/protocols/oauth2/resources/granular-permissions)、[Microsoft](https://learn.microsoft.com/en-us/entra/identity-platform/consent-types-developer) 和 [Slack](https://docs.slack.dev/authentication/installing-with-oauth/) 的官方流程。不把应用已开通的全部权限都要求用户授权。

- 每次进入、恢复页面及切换收件范围时，通过带会话和 CSRF 校验的 `POST /creators/broadcast/permissions` 核对当前登录者本人；选择其他 CM 只改变预览范围，不检查其凭据，也不能以其身份发送。
- 结合最新 OAuth/刷新响应记录的 scope 与只读 `user_info` 核对基线、两项发送权限、凭证和本人身份。只有令牌临近到期才沿用现有行锁刷新；刷新明确返回 scope 时同步保存，未返回则保留历史记录。
- 编辑、保存、匹配和预览要求本人 active、正文同意和读取基线完整；通常由 Owner 或 `authorizations.broadcast_enabled` 单独开通。不再因缺发送 scope 自动跳 OAuth、锁住整个编辑区；实际发送前由本人点击补授权。草稿仅在当前标签页临时保留，返回后重新准备，不自动发送。普通用户只使用自己的达人、可见群和个人名单／模板，伪造其他 CM 拒绝；“自选群”允许选择未分类的本人可见群，但不改变正式分类。
- 演示可配置 `BROADCAST_PREVIEW_UNTIL=2026-10-11T00:00:00+01:00`，仅在期限内绕过功能开通标志以预览；仍要求登录、正文授权和读取权限。到期自动恢复，不新增后台角色或发送授权。正式发送、重试及 Worker 始终要求 Owner 有效或本人功能标志为 true，并核对两项发送 scope。
- `POST /creators/broadcast/prepare` 核验本人凭据、固定当前数据库范围和 @ 身份，并加密保存 15 分钟有效快照；`POST /creators/broadcast/send` 经再次确认、CSRF 与最新授权检查后入队。队列只有本人可创建/查看/停止/重试，Owner 不可冒充其他人。准备和确认不重复逐群请求 Lark；Worker 临发送逐群核验实际成员、发言、@ 对象及 @all 权限。
- `/creators/broadcast/batches` 与 `/{id}` 展示本人结果；`/{id}/cancel` 停止尚未开始 POST 的项，已发出不撤回；`/{id}/retry` 只重排明确失败项。成功及结果未知不重发，未知项需到 Lark 核实。重复请求/预览复用同一任务；10 分钟内相同本人、正文、目标去重，每群持久化固定 UUID。
- 独立发送 Worker 最多 2 个账号并行，同一账号串行，不占 AI 并发。限流尊重 Retry-After，最多 3 次自动尝试；不确定的网络/服务端结果及已开始发送的失效租约标记 unknown，未开始的失效租约恢复 pending。错误与审计不写正文或令牌，正文只加密保存并由本人在回执页查看。
- [Lark CLI 的 auth check](https://github.com/larksuite/cli/blob/main/cmd/auth/check.go) 检查本地 scope；`user_info` 不返回 scope。因此授权就绪不等于外部群、发言或 @all 已经可用。正式验收需经网站确认并核对 Lark 回执与实际消息，不以一次直接 API 测试替代；外部群需另获指定群测试授权。

预期健康检查返回 `{"status":"ok","database":"ok",...}`。若服务未启动，先查看不含消息正文的 systemd 日志：

```bash
sudo journalctl -u lark-message-stats-web.service -u lark-message-stats-worker.service -n 100 --no-pager
```

## Lark 权限与验收

2026-09-11 人员资料权限升级：仅将明确选定用户标记 `attention_required` / `profile_scope_upgrade_required`，清空本站旧令牌和登录会话，不删除授权 UUID、聊天、身份或达人关系。操作前停止 Web/Worker 并完成备份，取消选定用户的旧同步任务与临时租约，保留正文同意时间、游标及会话代次。用户本人完成含新增权限的 OAuth 后恢复增量同步；重叠的旧升级链接不能再次触发全量回填。普通重新授权仍按原规则重建正文采集边界。

1. 仅持旧统计授权或缺少必需 scope 的 CM/AM，需要打开 `https://larkbot.ttapp.help/` 完成新的 OAuth；这是正文和 P2P 采集的明确授权边界。
2. 回到“工作台”，确认显示接入状态和最近检查时间；个人页不展示会话或消息统计。业务导航和群发入口按原有角色显示，不因更新界面扩大权限。
3. 核对 `im:chat.members:read` 与成员刷新状态；成员接口缺失或失败应显示缺口，不把它混成正文同步失败。
4. owner 重新登录后进入 `https://larkbot.ttapp.help/admin/messages`，选择 CM/AM 和会话，确认仅在新版授权后的回填完成后出现正文。不要在终端、日志或 Git 中输出正文。

## 回退原则

先停止发布并保留数据库。不得通过删除数据库或令牌来“回退”。部分迁移会删除派生数据，仅回滚代码不能恢复这些数据。若迁移提交后发现异常，保持 Web/Worker 停机，先把发布前的 custom dump 恢复到一个空白临时数据库并完成校验，再用已验证备份完整替换 live 数据库；不得把 full dump 直接叠加到非空 live 数据库。最后恢复上一个已验证的 Git 提交和对应 systemd 单元。备份包含清理前数据，权限必须保持 `0600`，并按内部保留策略安全删除。
