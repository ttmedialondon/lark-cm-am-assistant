# Lark CM/AM Assistant 部署手册

本手册用于应用代码或数据库变更的发布。当前系统同步授权范围内的群聊/私聊、加密正文和最新成员快照，后台按页面权限开放。运营助手和事件总结按需在内存中读取图片及常用文件，只加密保存解析结果，不保存原始二进制；群发用户主动上传的原图仍单独加密保存在私有目录，两者不要混淆。业务与权限边界见 [文档总入口](../README.md)。

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
  pg_restore --file=/dev/null "$backup_path"
  sha256sum "$backup_path" > "$backup_path.sha256"
  sha256sum --check "$backup_path.sha256"
  chmod 0600 "$backup_path" "$backup_path.sha256"
  if test -d /var/lib/lark-message-stats/broadcast-images; then
    tar -czf "$backup_path.images.tar.gz" -C /var/lib/lark-message-stats broadcast-images
    tar -tzf "$backup_path.images.tar.gz" >/dev/null
    sha256sum "$backup_path.images.tar.gz" > "$backup_path.images.tar.gz.sha256"
    sha256sum --check "$backup_path.images.tar.gz.sha256"
    chmod 0600 "$backup_path.images.tar.gz" "$backup_path.images.tar.gz.sha256"
  fi
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

旧令牌不会因应用权限已开通自动获得发送权限。验收必须检查新令牌实际返回的两项 scope；此入口不会发送消息，也不会开启群发队列。普通登录有有效本站会话时，会继续请求本人已授予的权限；无会话时不猜测账号权限。只保存 Lark 实际返回的 scope，缺少发送权限时仍需本人从此入口补充，不伪造授权。

#### 授权回调与并发同步

- 授权最终写事务先取得已有 `lark-sync-material` 事务锁，再修改授权、同步任务及身份缓存；Lark 换令牌和读取资料在锁外执行。资料能力检查先锁并核验父授权，再写 provider，旧令牌／权限／正文同意快照不能覆盖新授权。同步错误回写同时核对请求对应的凭证版本；旧令牌失败只重试原页或会话发现，不把新授权误标为失效。
- 本地保存设置 5 秒锁等待、15 秒单语句上限。仅对已回滚的死锁、序列化冲突和锁等待超时最多尝试 3 次；不重复 Lark 授权码交换，连接或提交结果不确定时不自动重试。
- 无法完成时返回 503 和“重新授权”入口，保留现有登录会话；Lark 换令牌／资料失败返回 502。问题编号用于查找 `oauth_database_failed`、`oauth_database_retry`、`oauth_provider_failed` 日志，日志只保留请求编号、账号 UUID、阶段和错误码，不记录 SQL、异常全文、OAuth code/state、令牌或正文。
- 已有正文同意、账号未撤销且正文读取权限仍完整时，普通登录及发送／图片／资料增权保留同意时间、聊天水位、分页游标和正在运行的同步任务；无活跃任务才补一个增量任务。新接入、已撤销账号重新接入、原无正文同意或原缺正文读取权限，才建立新采集边界并重置历史。较旧的 OAuth 链接不能覆盖后续成功登录或增权。发布不代用户授权、不重新开启 AI、不自动发送消息。
- 状态、正文同意、账号身份或正文读取能力变化，只写入按账号去重的 `creator_authorization_rescans` 队列；发送／图片／资料权限及 scope 顺序变化不全量重排 AI。现有本地准备 Worker 每批处理 10 个达人或会话，提交游标后轮转；不长持队列行锁或授权同步锁。旧批次只能推进自身代次，失败回滚并延后 30 秒重试，重启后续跑。队列未完成时旧自动重要度不展示，人工分层保留；授权失效与证据校验仍实时执行。

#### 新人员接入与自动续跑

- 新 CM 完成本人 Lark 授权后进入工作台，后台发现会话、同步消息与成员；普通用户只查询本人资料，Owner 保留跨人员测试权限。
- 首次准备仅在首页显示一张非阻塞提示卡，达人名单和消息总结在尚无结果时显示对应提示；已有资料和功能照常使用。复用现有刷新，近期资料就绪后收起提示，不等待更早历史，不显示队列、同步数量或预计耗时；连接失效仍单独显示本人重新连接入口。
- 同一账号优先更新近期消息，有历史待处理时，近期最多连续领取四页后给历史一页；近期无到期任务时直接补历史。近期会话按轮次续页，长分页群不能一直占先。未完成历史的会话先读最近七天，随后按实际截止水位续读；两套分页游标独立，重启后继续，不把处理完成时间当作读取截止时间。
- 每四个近期页最多一个优先读取已到期的活跃会话：近三小时有已知消息、已知消息晚于本人水位、首次近期准备或窗口续页；其余按原陈旧轮次推进。活跃线索只读取索引中的时间，按账号和任务代次短缓存；不提高并发，不保证大账号全部会话五分钟内更新。
- 群目录和到期私聊列表在现有单线程发现中刷新，不等待整轮消息同步结束；私聊暂不可用仍间隔一小时重试。复用共享 Lark 请求名额，新接入账号优先；刷新期间旧会话继续分页，新会话进入原近期消息与历史队列。保留 coordinator、代次、水位和游标，不增加并发，不承诺固定分钟内发现全部新群。
- 群目录完整分页成功后才隐藏已退出群，并只释放这些群的旧预留，不删除消息或历史进度；部分失败不批量隐藏。周期发现失败单独延后，旧群消息同步继续；发现与 coordinator 收尾互斥，重启自动释放发现锁。
- `recent_coverage_start_at` 仅在近期窗口最后一页成功后更新。按查询时段判断覆盖，不把近期已齐误报为全历史完成；只有新采集边界才清空该起点，同读取范围的登录和补授权保留进度。后台区分首次准备、补充历史、重新授权后更新；受限会话单列，不计入可读历史分母，也不阻止其他正常功能。已经被旧版本重置的水位从当前检查点续跑，不伪造恢复。
- 新确认的达人进入现有负责人队列。新 CM 接入或真实相关材料变化会唤醒受影响任务；重复页和无关消息不反复调用，人工解除与已有正式关系保留。新增负责关系继续触发重要度与七天事件补算，不因发布全量重跑。
- 新可见会话、消息实际新增可读范围、成员或私聊对端真实变化，会为相关正式负责人和达人合并七天事件补算信号，覆盖已关闭时段。复用原队列，三十秒合并且不推迟已有重试；重复目录、成员和消息页不重复排队。旧有效结果和未知请求保护保留，不重算无关对象。
- 已观察到的撤回消息也会定向唤醒受影响的七天事件结果，但不将撤回正文交给模型。若撤回使会话失去唯一关联，则退出该会话的旧结果；在途付费请求及旧回执保留，仍由原队列处理，不全量重跑。
- 跨群合并按该达人的实际事项和资料判断变化，不以整群任务 UUID 判断；同群新增达人不会让其他人的相同事项重新付费。旧成功结果须验证原输入及当前原文权限后才能复用；已收到的返回和结果未知的请求不能因指纹升级丢弃或重发。旧来源完全未变时不重算个人指纹、已完成任务不读取检查点；单人补算不额外扫描其他人的合并任务。
- 各功能分别准备材料，同一功能同时只准备一批；慢总结不阻止身份、负责人或重要度取材，实际后台 API 并发仍为 5。重要度优先生成尚无画像的首次结果，随后按批次可执行时间轮转；旧任务读完一批历史后回到队尾，不因最初创建时间较早持续占先。任务行会在新材料进入时复用，不能用创建日期判断已等待多久。事件合并在同一次事务内复用各 CM 的名单和会话关系，跨事务仍重新读取与校验权限。
- 单群拒绝只延后该会话，明确的账号令牌／权限错误才要求重新授权。私聊列表暂不可用时，在后续会话发现中间隔一小时重试；明确缺权限仍由本人重新授权，不阻止正常群聊继续同步。
- 运营助手保留主流程十二次模型调用上限，最后一次停止工具取材并生成实际回答；分段总结仍独立计数。已保存返回先本地处理；旧检查点已经耗尽上限时明确结束，不伪装完成或追加未知请求。
- 头像刷新沿用每个提供账号的能力探测和冷却；全部外部来源冷却时只读取本人资料，不重复全目录成员／消息补扫。新授权、权限变化和新增可见性仍可恢复逐人检查，不绕过正文同意或访问范围。
- 身份／账号任务从等待材料或已保存结果重新唤醒时，开启新的防抖窗口；同一轮待处理信号继续合并，保留 30 秒等待、两分钟合并上限和已有未来重试时间，不能把排期写回旧窗口。迁移只纠正可明确证明复用了旧窗口的空闲增量任务，不改正在执行的请求、费用回执或业务关系。
- 达人身份以当前正式角色为准，与账号待核分开显示。历史识别任务被取消不等于正式达人被删除；只纠正显示，不重新打开已结束任务或重复调用模型。真实身份撤销和证据失效仍按当前事实处理。

#### 定向修复初始化任务

`scripts/repair_creator_initialization.py --manifest <固定名单.json>` 默认只读预览。名单包含 `cm_id`、`created_at`、`identity_contact_ids`、`importance_job_ids`、`responsibility_job_ids`；只保存 UUID，不保存聊天或凭证。先固定授权范围内有本人文字、尚未识别的联系人，不能执行时重新扩大名单或用实时 `LIMIT` 代替固定名单。

按发布步骤停止服务、验证数据库与素材备份后，使用同一文件加 `--apply`。已入身份队列、已确认或不再有有效可读文字的联系人会跳过；其余进入原身份队列，不直接写身份或负责人。仅恢复列出的 `failed / invalid_output` 或明确 HTTP 400 任务：材料、手册、配置和版本仍有效时从冻结批次续跑，否则重取当前批次，保留历史 checkpoint、游标、待处理消息、尝试次数和旧费用记录。未知结果不重试，原有确认关系不修改，重复执行不会再次重排已恢复的任务。HTTP 400 不自动循环重试，定向恢复后应先核对诊断。

定向补救可另列 `identity_retry_job_ids`、`responsibility_saved_job_ids`、`event_chat_ids`。身份仅允许已知 HTTP 400 的一次显式诊断重试；负责人先重建当前材料，本地复用完整成功回执。仅明确已有成功回执且实际输入变更的指定任务才重新准备新材料，不能把真正未知请求当成可重试。内容被服务商拒绝的任务单独标明并停止重试；账本和旧确认关系保留。

负责人结果写回事务先取得原材料锁，再锁配置、任务和来源；网络请求不持锁。成功返回后本地保存中断的任务，恢复为“结果已返回，等待本地保存”，优先复用原成功回执，不再错误地混入网络结果未知。

重要度任务因来源变化清空冻结材料后，若最新费用回执仍为结果未知，保留暂停状态，不从头重新扫描历史。已有成功返回、有效冻结批次和正常消息增量仍沿用原流程。

结构化响应优先读取正式消息的 `final_answer`，不把 commentary 混入结果；拒答和未完成输出仍拒绝。只兼容完整 JSON 代码围栏，不截断 `HOOK`、`pruned` 或其他任意尾文，更不能从两个矛盾 JSON 中挑一个入库。负责人失败返回加密保留有界原文与校验错误，常规日志仅记录安全输出结构。协议依据 [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) 和 [Responses 消息 phase](https://developers.openai.com/api/docs/guides/reasoning)。

负责人和重要度只要求最终 JSON，不请求前言或中间说明；负责人按候选数量预留输出容量，重要度最多 8192 token。这是单次返回长度，不是每日预算或额外调用；未完成的返回仍不入库，并记录响应状态、截断原因和实际用量。身份／handle 共用队列保留历史轮转，按本次可执行时间排队；失效或新材料重排的旧任务不凭最初创建时间反复占先。

HTTP 错误常规诊断仅保留有界的错误代码、类型和参数标识。HTTP 400 的简短服务端说明单独加密保存在失败回执，不进入普通日志或统计；身份、账号、负责人和重要度沿用此规则。失败回执不作为成功缓存复用，区分响应失败与连接结果未知，见 [OpenAI 错误说明](https://developers.openai.com/api/docs/guides/error-codes)。负责人取材须补入选中上下文的实际原问题，仍受条数、长度和授权限制；确实无法读取的引用继续标记缺口，不凭高分绕过。

#### 负责人匹配手册

文档管理员在「助手文档 → 负责人匹配」维护关系判断、取材与交接、关联与案例三份手册。新准备的任务加密固定手册快照，一次请求判断本批全部 CM；文档版本参与结果缓存，手册元数据不重复发送到模型，也不作为新聊天绕过未知费用保护。类型和后台范围固定，不加入普通助手导航或回复资料。

普通编辑不重排旧任务、不修改已有关系。草稿或停用会暂停依赖任务；恢复发布仅续跑尚未实际调用的缺手册任务。已付费但暂停的相同材料不因恢复发布、配置保存或重复材料信号自动重发，真实新材料和明确人工重查沿用原入口。历史结果与原文仍按原授权可查。发布不提升旧任务全局版本、不全量重跑、不修改阈值或开关。

#### 维护优先级手册

文档管理员在「助手文档 → 维护优先级」编辑事实提取、分档说明、判断案例。只有事实和案例进入模型，一次请求使用两份手册；分档说明仅供查阅，修改它不改变程序中的五档、7／30 天窗口或人工调整优先规则。

新准备的批次加密固定规则版本，已冻结批次仍用原版本，后续增量读取新版；不清空历史检查点、不全量重跑、不更改旧画像或人工值。事实或案例手册转为草稿／停用后暂停依赖批次，恢复发布后续跑；已有有效付费返回先本地复用，不重复请求。未知费用、来源、授权与配置变更仍走原有校验，不因改手册自动重试。后台手册不加入运营助手导航或回复资料，不增加查询权限、表、队列或 API Key。

#### 群用途分类手册

文档管理员在「助手文档 → 群用途分类」维护用途判断、取材与上下文、入库与案例三份手册；一次请求共同读取，不额外增加模型调用。现有五类与未确认、当前配置阈值、有效正文引用和授权校验保持，任何人工分类及锁定结果都不被自动覆盖，群用途不授予发送权限。

新准备的任务加密固定手册版本，旧冻结任务继续原版本；普通编辑不重排历史、清除结果或触发全量重跑。草稿／停用暂停依赖处理，恢复后续跑并优先本地复用已付费有效返回；真正材料变化、结果未知和配置变化仍走现有校验。只使用原有队列和凭证，手册不加入运营助手导航或回复资料，不增加查询权限。

#### 群发页面与正式发送

采用按功能增量授权，参考 [Google](https://developers.google.com/identity/protocols/oauth2/resources/granular-permissions)、[Microsoft](https://learn.microsoft.com/en-us/entra/identity-platform/consent-types-developer) 和 [Slack](https://docs.slack.dev/authentication/installing-with-oauth/) 的官方流程。不把应用已开通的全部权限都要求用户授权。

- 富文本采用本地 Quill 2.0.3，只启用文字样式、简单列表、链接、Emoji 和图片；正文为受限 insert-only Delta v2，服务端拒绝任意 HTML、远端图片 URL、客户端 image_key 和非 http(s) 链接。旧纯文字模板、草稿和历史继续可读。富文本、图片顺序与 @ 都参与冻结及防重，模板后续编辑不改变发送任务。
- 图片首次上传或载入带图消息时检查本人 `im:resource`，缺失时保存标签页草稿并进入 `/auth/lark/start?send=1&media=1&account_id=<本人UUID>`，返回后不自动发送；不加入全局读取 scope。应用须先开通并发布该**用户身份**权限，CM 仍需本人同意。图片和消息使用同一用户令牌，不混用机器人图片。
- `POST /creators/broadcast/images` 接收带 CSRF 的原图，验证实际格式（JPEG/PNG/WebP/GIF）、非空、10 MiB、尺寸和解码工作量；GIF 保留原始动画。每条最多 5 张；上传中、失败、越权、缺素材或权限变化均阻止发送，不省略图片。图片素材 UUID 对应私有 AES-GCM 原图和服务器 image_key，本人范围读取 `GET /creators/broadcast/images/{id}` 且 `no-store`，Owner 也不能读取其他员工素材。
- `BROADCAST_IMAGE_DIR` 默认 `/var/lib/lark-message-stats/broadcast-images`，0700 目录／0600 文件，应纳入与数据库相同时间点的加密素材备份和 SHA 校验；若改变目录，同步修改备份路径与 systemd 可写范围。回退数据库时恢复对应素材，不能仅恢复 pg_dump。每日清理超过 7 天且无任何模板／发送预览引用的素材及孤立密文；历史发送和已删除模板引用仍保留，不删除聊天或业务关系。

- 首次进入、主动重查及返回页面时，通过带会话和 CSRF 校验的 `POST /creators/broadcast/permissions` 核对当前登录者本人；返回页面的检查在后台进行，不反复锁住编辑区。切换 CM／载入名单只重查目标范围，复用本标签页已取得的本人发送状态，不再重复外部令牌探测。实际准备和发送仍重新检查当前权限；选择其他 CM 不能以其身份发送。
- 结合最新 OAuth/刷新响应记录的 scope 与只读 `user_info` 核对基线、两项发送权限、凭证和本人身份。只有令牌临近到期才沿用现有行锁刷新；刷新明确返回 scope 时同步保存，未返回则保留历史记录。
- 编辑、保存、匹配和预览要求本人 active、正文同意和读取基线完整；新接入账号默认开通群发，保留现有账号明确关闭的 `authorizations.broadcast_enabled`。功能开通不授予 Lark 发送权限或后台权限。不再因缺发送 scope 自动跳 OAuth、锁住整个编辑区；实际发送前由本人点击补授权。草稿仅在当前标签页临时保留，返回后重新准备，不自动发送。普通用户只使用自己的达人、可见群和个人名单／模板，伪造其他 CM 拒绝；“自选群”允许选择未分类的本人可见群，但不改变正式分类。
- 本人发送就绪时，点击“预览群发”直接调用 `POST /creators/broadcast/prepare`，核验本人凭据并加密保存 15 分钟有效快照；弹窗内确认一次后调用 `/send`，经 CSRF 与最新授权检查入队，不再先 `/preview` 后重复准备。未发送就绪或跨 CM 仍只读预览。队列只有本人可创建/查看/停止/重试，Owner 不可冒充其他人。准备和确认不重复逐群请求 Lark；Worker 临发送逐群核验实际成员、发言、@ 对象及 @all 权限。
- `/creators/broadcast/batches` 与 `/{id}` 展示本人结果；`/{id}/cancel` 停止尚未开始 POST 的项，已发出不撤回；`/{id}/retry` 只重排明确失败项。成功及结果未知不重发，未知项需到 Lark 核实。重复请求/预览复用同一任务；提交及重试均按十分钟内相同本人、实际群、正文格式、图片内容与 @ 对象逐群去重，标签及未被 @ 的名单不影响防重。重叠群在回执中显示“已去重”，新群正常入队，每群持久化固定 UUID。
- 独立发送 Worker 最多 2 个账号并行，同一账号串行，不占 AI 并发。限流尊重 Retry-After，最多 3 次自动尝试；不确定的网络/服务端结果及已开始发送的失效租约标记 unknown，未开始的失效租约恢复 pending。错误与审计不写正文或令牌，正文只加密保存并由本人在回执页查看。
- [Lark CLI 的 auth check](https://github.com/larksuite/cli/blob/main/cmd/auth/check.go) 检查本地 scope；`user_info` 不返回 scope。因此授权就绪不等于外部群、发言或 @all 已经可用。正式验收需经网站确认并核对 Lark 回执与实际消息，不以一次直接 API 测试替代；外部群需另获指定群测试授权。

预期健康检查返回 `{"status":"ok","database":"ok",...}`。若服务未启动，先查看不含消息正文的 systemd 日志：

```bash
sudo journalctl -u lark-message-stats-web.service -u lark-message-stats-worker.service -n 100 --no-pager
```

## Lark 权限与验收

2026-09-11 人员资料权限升级：仅将明确选定用户标记 `attention_required` / `profile_scope_upgrade_required`，清空本站旧令牌和登录会话，不删除授权 UUID、聊天、身份或达人关系。操作前停止 Web/Worker 并完成备份，取消选定用户的旧同步任务与临时租约，保留正文同意时间、游标及会话代次。用户本人完成含新增权限的 OAuth 后恢复增量同步；重叠的旧升级链接不能再次触发全量回填。是否重建正文采集边界以实际读取授权变化为准，不以是否再次经过 OAuth 页面判断。

1. 仅持旧统计授权或缺少必需 scope 的 CM/AM，需要打开 `https://larkbot.ttapp.help/` 完成新的 OAuth；这是正文和 P2P 采集的明确授权边界。
2. 回到“工作台”，确认显示接入状态和最近检查时间；个人页不展示会话或消息统计。业务导航和群发入口按原有角色显示，不因更新界面扩大权限。
3. 核对 `im:chat.members:read` 与成员刷新状态；成员接口缺失或失败应显示缺口，不把它混成正文同步失败。
4. owner 重新登录后进入 `https://larkbot.ttapp.help/admin/messages`，选择 CM/AM 和会话，确认仅在新版授权后的回填完成后出现正文。不要在终端、日志或 Git 中输出正文。

## 回退原则

先停止发布并保留数据库。不得通过删除数据库或令牌来“回退”。部分迁移会删除派生数据，仅回滚代码不能恢复这些数据。若迁移提交后发现异常，保持 Web/Worker 停机，先把发布前的 custom dump 恢复到一个空白临时数据库并完成校验，再用已验证备份完整替换 live 数据库；不得把 full dump 直接叠加到非空 live 数据库。最后恢复上一个已验证的 Git 提交和对应 systemd 单元。备份包含清理前数据，权限必须保持 `0600`，并按内部保留策略安全删除。
