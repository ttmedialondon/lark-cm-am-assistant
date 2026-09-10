# 系统技术说明

核对日期：2026-09-10。本文合并旧后端设计、人员模型、姓名机制及后台实现说明，只描述现有结构。业务以 [达人名单](../product/creator-management.md)、[消息总结](../product/message-summary.md)、[后台权限](../product/admin-access.md) 为准；未来改版尚未定义。

## 1. 实际架构

```text
浏览器 / Lark 网页入口
        │ OAuth + HTTPS
        ▼
Caddy → FastAPI + Jinja2 / HTMX / Tabler → PostgreSQL
                                             ▲
                       独立 Python worker ────┘
                            ├─ Lark 授权同步
                            └─ 已启用的 AI 会话分析
```

单体应用、顺序 SQL 迁移和 PostgreSQL 持久任务。现有页面在 `backend/app/templates/`、`static/`；不把旧 React、Alembic、Redis 或长期事件工作台设计写成已上线能力。

## 2. 数据怎么关联

| 对象 | 存什么、不能混成什么 |
|---|---|
| `authorizations` | 已接入 CM 的授权、正文同意和接入状态；不是全体 Lark 人口库 |
| `chats`、`chat_messages` | 公司级去重的会话与消息，正文加密；去重不代表所有人都能读 |
| `authorization_chats`、`authorization_message_observations` | 哪位授权人可见哪个会话、观察到哪些消息，以及各自同步状态 |
| `lark_identities`、`contacts`、`chat_members` | 稳定 Lark 身份、联系人与最新群成员快照；不能按显示姓名合并用户 |
| `contact_roles` | 业务类别及主/辅助角色；未知保持待识别，不写成“其他” |
| `organizations`、`organization_tenants`、`contact_organization_links` | 组织及可验证租户/人员关系；租户相同不等于达人、员工或负责人 |
| `social_accounts`、`contact_social_accounts` | 平台账号及关联用户；一个用户可以关联多个 handle |
| `creator_workflow_candidates`、`creator_workflow_sources`、`creator_workflow_requests` | 待确认线索、来源和请求幂等记录 |
| `creator_account_assignments` | 新流程的账号级 CM 正式归属；是名单与总结取数依据 |
| `contact_assignments` | 旧联系人级负责人关系；不能代替账号级确认，也不能认领一个 handle 就转走同人的其他 handle |
| `creator_workflow_ai_runs`、`creator_workflow_ai_tasks`、`creator_workflow_ai_chat_checks` | AI 候选扫描、会话任务及变化检查 |
| `offhours_summary_settings`、`offhours_summary_runs`、`offhours_summary_tasks` | 指定总结时段、执行版本和会话结果 |
| `admin_roles`、`admin_page_permissions`、`audit_events` | 主人身份、逐页权限、必要的查看/修改审计 |

现有人员类别为 creator、partner_contact、internal_colleague、merchant_contact、platform_contact、other_external_contact。内部同事依内部租户事实识别；外部人的角色、所属组织和负责人是独立事实。

## 3. 授权与正文边界

- Lark 使用 CM 自己的 user access token，读取 API 实际返回且其本来有权查看的范围。用户权限为 `offline_access`、`im:chat:read`、`im:message:readonly`、`im:chat.members:read`。
- 旧的仅统计授权不能静默升级。只有记录正文同意、且该来源在同意后重新观察到的消息，才可按有效范围展示正文。
- 群和私聊共享底层存储；私聊对端身份记录在授权来源的会话关系上。P2P 发现结果单独标记，不可用不阻塞群聊同步。
- 消息正文、OAuth 令牌、AI 密钥及派生总结使用各自加密域；正文、完整 prompt、密钥和令牌不进入普通日志或 Git。不保存附件二进制。
- 最新群成员快照包含稳定 ID、姓名和可用租户信息，不保存成员历史版本；API 未返回某人不等于已离群。
- 后台逐页权限、前台名单编辑、测试编辑和正文读取分别校验。每请求查询当前权限，不将权限缓存到登录会话。写操作有 CSRF、版本/冲突校验、事务和审计。
- 正文读取同时核对授权、同意、会话可见且启用、有效消息观察和删除状态；选用户或建立关联不新增权限。详细入口范围见后台权限文档。

## 4. 同步与并发

- OAuth 后回填可读取历史；常规每 300 秒增量，重看前 600 秒避免边界遗漏。重复消息按稳定消息标识去重。
- 会话目录分页按授权人顺序完成；消息分页用数据库 claim、租约、游标和 generation 保证同会话串行、不同会话可并行。消息及观察关系与游标在同一事务保存，重启可续跑。
- 每位授权人最多一个 coordinator；默认全局最多 3 个在途 Lark 请求、每秒 10 个，每人优先最多占 2 个会话槽位，其他人无等待时可借用剩余槽位。
- 群成员刷新是低优先级任务，默认最多 1 个在途请求；消息任务优先。单会话临时失败单独退避，429 或 Lark 限流响应触发共享暂停，不紧密重试。
- 归档/解散会话保留发现事实，但不回填且不计入可同步分母；专题群按群聊展示。未来已排程的任务不等于故障或正在处理。
- 生产保持一个 worker 进程，内部线程池执行并发；不能单凭加进程就认为吞吐提高。

## 5. AI 与确认

`ai_dispatch` 为 AI 功能共用最多 3 个有效并发槽，与 Lark 请求限流是不同机制。任务、租约、输入指纹、有限重试及配额暂停持久化；结果写入前重验当前配置、授权、任务版本及证据。

新候选 AI 只写待确认，不因高分建立正式归属。人工确认 handle 与用户关联时，在同一事务保存账号归属、用户关联和审计；重复提交不重复入库，跨 CM 冲突不能覆盖。

旧 `contact_assessment`、`contact_assignment` 已停用；新 `creator_workflow` 和 `offhours_summary` 是独立配置。清空某位 CM 的数据不等于关闭全部 AI 配置。总结输入、输出、限额及缺口见消息总结文档；本期不回复、不发通知、不代办业务。

## 6. 姓名和 @

`name`、`en_name` 保留 Lark 来源字段，`display_name` 是派生显示。OAuth/当前有效个人资料优先，旧消息与旧提及只补缺值；英文名字段缺失保留旧值，明确返回空值才清除。

`identity_names` 统一页面显示及可核验 CM 别名；单身份联系人可同步标签，多身份联系人保留人工标签。名称变化不合并身份、不修改归属。

`message_mentions` 只按同条消息保存的明确身份映射还原 `@_user_N`，同时处理正文/富文本及中文相邻字符。未知或冲突不猜，URL、代码和邮箱中的同样字符不替换。先检查阅读权限，再解析和展示。

维护脚本 `backend/scripts/backfill_identity_names.py` 默认只检查；显式 `--apply --owner-id …` 才写入，`--refresh-members` 可在已有权限内刷新群成员。实时资料不足时保留已有姓名，不扩大权限、不重跑达人 AI。请求前后检查授权与稳定 ID，失败不阻塞消息同步。

## 7. 重置与旧快照

Remi 清理记录见 [2026-09-10 状态](../status/2026-09-10-reset-and-docs.md)。`creator_preparation.load_snapshot` 在同 CM 的最新快照之后存在相同或更晚的 `creator_roster_reset` 时隐藏旧快照，不回退更老名单；重置后新发布的有效来源才可重新出现。

`backend/scripts/reset_remi_creator_data.py` 是有固定负责人、数量和来源摘要护栏的一次性操作，不是通用清库接口，不可复用到下一批名单。清理不删除原始 Lark 消息、人口库、授权、Excel 原件或历史审计。本次文档整理不会执行该脚本。

运行配置、发布、备份和恢复统一见 [部署手册](../runbooks/deploy-mvp.md)，不在这里复制命令。
