# Lark User Ingress：技术基线与第一期设计

> 文档状态：历史方案，已由 `lark-message-stats/` 取代，不再作为施工基线  
> 最后核对：2026-08-26  
> 应用：`Lark User Ingress`（TT Media International 企业自建应用）
>
> **实施更新（2026-08-26）**：本文件保留为最初的 Node.js 技术基线；实际部署的测试版已改为独立的 Python 项目 `lark-message-stats/`，详见 [`../lark-message-stats/TECHNICAL_DESIGN.md`](../lark-message-stats/TECHNICAL_DESIGN.md)。当前实现不保存消息正文，旧文中“加密消息正文”相关设计不适用于本测试版。

## 1. 第一期开工结论

第一期不做“机器人进群监听”，而是做 **CM/AM 用户授权的网页应用**：

```text
CM/AM 在 Lark 打开网页应用
        ↓
授权 Lark 访问
        ↓
服务端以该 CM/AM 的 user_access_token 调用 Lark API
        ↓
枚举该用户可见的群 → 回填群消息 → 增量同步
```

第一期产品边界：

- 同步授权用户可见的 **群聊**，包括已验证的外部群。
- 仅读取；不自动发消息、不把机器人加入群、不调用 AI 模型。
- 每位 CM/AM 的授权、群列表、同步游标和数据相互隔离。
- 单聊（P2P）能力虽已申请，但在本期默认关闭，待独立 POC 后再决定是否启用。

“全量”定义为：Lark API 在该用户授权和当前访问权限下可分页返回的全部群和消息；不是越权读取企业内其他人的会话，也不保证突破 Lark 的数据留存边界。

## 2. 当前 Lark 配置状态

| 项目 | 当前状态 | 说明 |
|---|---|---|
| 正式版本 | `0.1.2`，已发布 | 当前修改均已发布。 |
| 应用能力 | 网页应用、应用机器人均已启用 | 机器人仅作为 Lark IM 能力前置开关，不自动加入群。 |
| 桌面端默认入口 | 网页应用 | 符合授权应用模式。 |
| 移动端默认入口 | 机器人 | 生产前需评估是否改为网页应用；CM/AM 若主要手机操作，应以网页应用为默认入口。 |
| 可用范围 | 部分成员 | 当前仍是小范围。真实上线前应扩展到首批 CM/AM。 |
| Redirect URL | 已配置 | 指向 `https://larkbot.ttapp.help/auth/lark/callback`。 |
| H5 可信域名 | 已配置 | `https://larkbot.ttapp.help`。 |
| IP 白名单 | 仅保留 Hetzner 服务器出口 | 本机/VPN 测试 IP 已删除。 |
| App Secret | 未写入代码或文档 | 生产时仅安全录入服务器的 root 所有文件。 |

## 3. 已开通权限

下表来自 2026-08-26 的 Lark 开发者后台“权限管理”。“第一期用途”描述的是服务实际应调用的权限，而不是仅表示已获授权。

| 权限 | 身份 | 当前状态 | 第一期用途 |
|---|---|---:|---|
| `im:chat.membership_application:read` | 用户身份 | 已开通 | 暂不使用。 |
| `im:chat:read` | 用户身份 | 已开通 | **必需**：列出授权用户可见的群并获得 `chat_id`。 |
| `im:chat:read` | 应用身份 | 已开通 | 第一期不使用；同步必须以用户身份执行。 |
| `im:message` | 用户身份 | 已开通 | 当前第一期不应使用发送能力；后续应做最小权限复核。 |
| `im:message.group_msg:get_as_user` | 用户身份 | 已开通 | **必需**：以 CM/AM 身份读取群聊消息。 |
| `im:message.p2p_msg:get_as_user` | 用户身份 | 已开通 | 暂不使用；P2P 单聊功能的预留权限。 |
| `im:message.pins:read` | 用户身份 | 已开通 | 暂不使用。 |
| `im:message.pins:write_only` | 用户身份 | 已开通 | 暂不使用，第一期不得写入 Pin。 |
| `im:message.reactions:read` | 用户身份 | 已开通 | 暂不使用。 |
| `offline_access` | OAuth 授权范围 | 已申请/已通过 | **必需**：服务端刷新长期同步所需的用户令牌。该范围不在“权限管理”表格中展示。 |

### 第一期实际最小调用集

```text
im:chat:read
im:message.group_msg:get_as_user
offline_access
```

原则：虽然后台已拥有更多权限，生产代码只实现上述只读调用。发送消息、Pin 写入、应用身份读群等能力必须保持未调用，直到对应产品需求和独立审核完成。

## 4. 已完成验证

| 验证项 | 结果 | 结论 |
|---|---|---|
| 用户 OAuth 授权 | 成功 | 可获得 CM/AM 的用户身份令牌。 |
| 群列表读取 | 成功 | 授权用户可列出自己已加入的外部群，并获取 `oc_...` 群 ID。 |
| 外部群消息读取 | 成功 | 授权用户可读取测试外部群的消息历史。 |
| 机器人是否需加入群 | 否 | 本次成功路径未把机器人加入测试群。 |
| 本地测试数据 | 已清理 | 一次性令牌已撤销；测试脚本、临时 IP 和本地测试文件已删除。 |

尚未验证：

- P2P 单聊历史读取；即使可列出单聊，也不能直接假设消息读取同样可行。
- 高消息量群的完整分页、限流与断点续传。
- 用户撤销授权、用户退出群、群解散、消息编辑/撤回后的同步行为。
- 生产服务器上的 OAuth 回调、令牌刷新和数据库恢复。

## 5. 已有代码与当前缺口

现有代码目录：`lark-user-ingress/`

当前已有最小服务骨架：

- `GET /healthz`：数据库健康检查。
- `GET /auth/lark/start`：创建一次性 OAuth `state` 并跳转 Lark 授权页。
- `GET /auth/lark/callback`：校验 `state`、换取用户令牌、加密后保存至 PostgreSQL。
- 使用 AES-256-GCM 加密 `access_token` 与 `refresh_token`。
- 服务仅监听 `127.0.0.1:3000`，设计上由 Caddy 提供公网 HTTPS。

当前尚未实现，因此不能视为生产可用：

- 刷新 `user_access_token`。
- 枚举并持久化授权用户的群列表。
- 群消息历史回填、分页、重试、限流及断点恢复。
- 增量同步 worker。
- 用户可见的“授权范围 / 同步所有群 / 暂停 / 删除数据”页面。
- 数据迁移、监控、备份、systemd 服务及 Caddy 部署。

## 6. 第一期开工架构

```text
Lark 网页应用
  └─ https://larkbot.ttapp.help
        │
        ▼
Caddy（HTTPS、反向代理）
        │
        ▼
Node.js / TypeScript 服务（127.0.0.1:3000）
  ├─ OAuth 路由
  ├─ 授权与群同步管理页面
  └─ 同步任务 API
        │
        ├──────────────► Lark Open API（以每位 CM/AM 的用户身份）
        │
        ▼
PostgreSQL
  ├─ 授权与加密 refresh token
  ├─ 群、授权范围与同步游标
  ├─ 加密的消息正文及元数据
  └─ 同步运行与审计日志

独立 worker（同一服务器、独立 systemd 服务）
  └─ 领取同步任务、刷新令牌、回填或增量拉取消息
```

部署原则：不使用 Docker；Node.js、PostgreSQL、Caddy 和 systemd 直接安装在 Hetzner Ubuntu 服务器上。

## 7. 同步流程设计

### 7.1 CM/AM 授权流程

1. CM/AM 在 Lark 打开网页应用。
2. 页面清晰展示“同步你可访问的群聊，不发送消息”的授权说明。
3. 用户点击“授权并开始配置”。
4. 服务端生成随机且一次性的 `state`，写入数据库并以 Secure + HttpOnly Cookie 绑定浏览器。
5. Lark 回调携带授权码；服务端校验 `state` 后换取用户令牌。
6. 仅在服务器端加密存储 refresh token；浏览器永不接触 App Secret 或 refresh token。
7. 用户进入群同步配置页。

### 7.2 群选择与首次回填

第一期建议提供一个明确选择：

- `同步我当前可见的全部群聊`（推荐默认选项）；或
- 由用户取消勾选不希望同步的群。

服务端随后：

1. 用该用户的令牌分页调用 `GET /open-apis/im/v1/chats`，默认仅获取群聊。
2. 保存 `chat_id`、群名、`external` 标记、群状态和授权用户关系。
3. 为每个启用群建立 backfill job。
4. 分页调用 `GET /open-apis/im/v1/messages?container_id_type=chat&container_id=...`。
5. 以 `(tenant_key, chat_id, message_id)` 作为消息实体唯一键，重复调用不产生重复数据。
6. 记录每个群的最新消息时间、最后页游标和同步状态。

群列表接口默认返回群聊，单页最多 100 个；消息接口单页最多 50 条，应持续读取至 `has_more=false` 或保存进度后安全续跑。

### 7.3 增量同步

第一期采用轮询，不依赖机器人进群事件：

- 建议每 2–5 分钟轮询已启用群。
- 以“最近成功同步水位减去短暂重叠窗口”拉取，防止网络重试或排序边界漏消息。
- 消息唯一键去重；同步失败进入重试状态，不能静默跳过。
- 用户授权失效、离开群或 API 拒绝时，标记为 `attention_required`，停止继续重试并在管理页提示用户。

## 8. 最小数据模型

| 表 | 关键字段 | 用途 |
|---|---|---|
| `oauth_states` | `state_hash`, `expires_at` | OAuth 一次性防重放校验。已有骨架。 |
| `lark_authorizations` | `open_id`, 加密 token、过期时间、scopes | 每位 CM/AM 的授权记录。已有骨架。 |
| `chats` | `tenant_key`, `chat_id`, `name`, `external`, `status` | 已发现的群元数据。 |
| `user_chat_bindings` | `open_id`, `chat_id`, `sync_enabled`, `consent_at` | 用户对各群的同步选择。 |
| `sync_runs` | `id`, `binding_id`, `mode`, `cursor`, `status`, `error` | 回填/增量任务与断点。 |
| `messages` | `tenant_key`, `chat_id`, `message_id`, 元数据、加密正文、版本时间 | 已同步消息。 |
| `audit_events` | `actor`, `action`, `target`, `created_at` | 授权、暂停、删除、失败等审计。 |

## 9. 数据与安全底线

- 不在日志、错误页、前端或 Git 中输出 App Secret、access token、refresh token 或消息正文。
- refresh token 与原消息正文均使用服务器侧密钥加密；密钥不与数据库放在同一位置。
- 为消息原文设置明确保留期；建议第一期默认 90 天，并提供按授权用户删除的流程。
- 用户撤销授权或点击“删除我的同步数据”后，应停用任务、删除令牌，并按保留策略删除消息数据。
- 任何未来的 AI 分析必须在消息同步稳定后单独接入，且与采集层解耦；第一期不向模型提供消息正文。

## 10. 实施里程碑与验收

### M1-A：生产基础设施

- 在 Hetzner 安装 Node.js、PostgreSQL、Caddy。
- 以 systemd 运行 Web 服务和同步 worker。
- `https://larkbot.ttapp.help/healthz` 返回 200。
- App Secret 和加密密钥仅存在服务器受限路径。

### M1-B：授权与群配置

- CM/AM 可在网页应用完成 OAuth。
- 授权成功后可看到自己的群数量及外部群标记。
- 可启用“同步全部群”或按群取消选择。
- 不能看到其他 CM/AM 的群。

### M1-C：历史与增量同步

- 至少一个外部群完成分页历史回填。
- 同一消息无重复入库。
- 服务重启后从游标继续，不丢失或重复整段数据。
- 新消息在设定轮询周期内入库。
- 用户撤销授权后，同步停止并形成可审计记录。

## 11. 开工前仍需确认的产品决定

1. 第一期开启范围：建议“所有群聊”，是否确认不含 P2P 单聊？
2. 消息原文默认保留期：建议 90 天，是否接受？
3. 首批可用范围：哪些 CM/AM 需要加入应用可用范围？
4. 增量轮询目标：建议 2–5 分钟，是否有更严格的实时性要求？
5. 未来 AI 模块是否只消费脱敏摘要，还是需要读取加密原文？该决定应在 M1 稳定后单独确定。

## 12. 参考

- [Lark IM chat list（用户身份、群/P2P、分页）](https://github.com/larksuite/cli/blob/main/skills/lark-im/references/lark-im-chat-list.md)
- [Lark IM message list（按 chat 容器读取消息、分页）](https://github.com/larksuite/cli/blob/main/skills/lark-im/references/lark-im-chat-messages-list.md)
- [现有 OAuth 服务骨架](./src/server.ts)
