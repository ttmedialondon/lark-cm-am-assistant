# 后端开发入口

业务规则见 [文档总入口](../docs/README.md)，数据与同步机制见 [系统技术说明](../docs/architecture/system-design.md)。这里仅说明开发方式，不再重复产品需求。

## 安装与检查

在 `backend/` 目录，使用 Python 3.12（与 CI 一致）：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest
```

数据库测试需要独立 PostgreSQL；CI 已配置隔离服务。完整本地验证将 `TEST_DATABASE_URL`、`IDENTITY_NAMES_TEST_DATABASE_URL`、`OFFHOURS_SUMMARY_TEST_DATABASE_URL`、`CREATOR_WORKFLOW_AI_TEST_DATABASE_URL` 指向测试库，并注入示例应用配置。未设置时相关用例会跳过，不能当作完整验证。不要使用生产数据库跑测试。

测试优先覆盖登录与权限、消息读取范围/加密、同步续跑、名单找群找人取证、并发/原子入库、旧机制退役与数据保全、头像、总结和数据保护。旧归属/导入工程的专属测试随功能删除，活跃功能的共享夹具保留。页面以少量真实浏览器流程验证；不为每个文案、CSS 类、重复按钮状态单独建测试，不能靠 skip 或缩小收集范围掩盖失败。

## 运行服务

配置字段见 [.env.example](.env.example)。应用读取进程环境变量，不会自动加载这个示例文件；本地由开发者注入测试配置，生产由 systemd 的 `EnvironmentFile` 注入。真实密钥和本地配置不能提交。

连接独立开发数据库并注入配置后：

```sh
.venv/bin/python scripts/migrate.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8100
```

另开终端，用同一开发环境运行 `.venv/bin/python -m app.worker`。worker 会领取同步、已启用的 AI 任务及用户明确确认的群发任务；群发使用独立并发，不占 AI 槽位。不要把生产授权或 API Key 放进普通本地测试。正式发送及临时演示配置见 [部署手册](../docs/runbooks/deploy-mvp.md#群发页面与正式发送)。

## 桌面回复接口（schema 1.1）

`POST /v1/reply-suggestions`，JSON。正式地址为 `https://larkbot.ttapp.help/v1/reply-suggestions`。
桌面可通过 `Authorization: Bearer <现有本站登录会话>` 使用已连接 CM；此凭据是本站
`web_sessions` 对应的原始不透明会话，不是 Lark access token 或 OpenAI Key。校验复用登录会话的
哈希、有效期和 CM active 状态，不新增 Lark OAuth。网页 Cookie 调用还必须提供现有 `X-CSRF-Token`。
桌面设置可打开 `/auth/desktop/start`，由现有网页登录账号进行 CSRF 确认。
新版通过 HTTPS `/auth/desktop/poll`，用随机私有 verifier 的 S256 PKCE 领取同一网页会话；
未确认返回 202，确认记录有效 60 秒且只能领取一次。旧版严格 loopback 与 code/exchange 保留兼容；
不新建会话或延长期限，原会话过期、删除或 CM 撤销后立即失效。
桌面使用系统加密存储保存会话并限定正式域名；本机退出只清理密文，网页登录不受影响。

必填请求字段：`schema_version: "1.1"`、UUID `request_id`、带时区的 `captured_at`、
`conversation: {label, label_source, identity_verified: false}`、`reply_scope`、
`selected_message_text`、`selected_messages`、`selection_source`、`conversation_context`。
正式服务拒绝未知字段及 synthetic 来源。

- `selected_messages` 模式要求非空选区、连续 ordinal 和非空 text；合并正文必须等于各条 text 用换行拼接，来源为 `clipboard_html`。
  全部选区共同构成回复目标，多选按事件整体回应；屏幕上下文只补充相关事实和进展，不以最后一条可见消息替换选区。模型的 ready 结果必须引用选区依据，不能只引用屏幕上下文。
- `recent_context` 模式要求空选区和空合并正文，来源为 `none`。
- 两种模式均提交 `conversation_context: {capture_id, captured_at, source: "lark_accessibility", scope: "visible_text", complete_history: false, messages}`；片段保留顺序、重复文字和未验证的文字 hint。
- 请求最多 1 MiB，响应最多 256 KiB；不截断。采集时间不是发送时间，片段不保证完整消息或已验证发言人；没有真实会话/用户 ID，后端不据此查询其他聊天、更新身份、负责人或事件。

成功原样回传 `request_id`、非空 `suggestion`、对应 `chinese_translation`、
`english_translation: null` 和 `is_mock: false`。生成与中文对照来自一次模型调用。
资料无法支持安全回复时返回 422，而不是把内部分析写入可回填草稿。
错误采用非 2xx 与 `{"error": {"code": "…", "message": "…"}}`，不重定向。

`desktop_reply` 有独立功能配置，迁移复制消息总结的加密凭据、Project 和模型并默认关闭；
启用或关闭仅影响桌面主动请求。调用不进入 worker、不使用 `ai_dispatch` 的共享并发、限速或暂停状态。
服务端总期限为 55 秒，单次上游总期限至多 45 秒；客户端断开会取消上游请求，但不能据此保证上游未计费。

去重键为当前 CM + UUID request_id；相同编号不同内容返回 409，进行中重复返回 409，
成功重复返回加密缓存，失败/取消/超期重复返回原安全错误，不重新付费调用。
需要重新生成时使用新编号。回执有效期 24 小时，按该 CM 后续请求清理过期记录；
这不是严格在第 24 小时物理删除。仅保存 hash、加密结果、安全错误与用量耗时，不保存输入正文或凭据。
编辑后的重新翻译不属于此接口。

## 修改与发布

- 页面在 `app/templates/`、`app/static/`，不是仓库外的旧原型或独立写作助手。
- 数据库修改通过 `migrations/` 的顺序 SQL；迁移可能修改数据，执行前核对目标和备份。
- 生产操作只按 [部署手册](../docs/runbooks/deploy-mvp.md)。纯文档更新不运行迁移、不重启服务、不重新分析消息。
