# 后端全量优化执行记录

目标：落实 backend-api-callchain-audit_2026-09-21.md 的全部优化结论，包括必要的测量、旧流程核查、回归和发布，而非仅让局部测试通过。

基线：f0dee27。当前处于本地实施阶段，尚未发布优化代码。

## 已实施

| 审查项 | 当前实现 | 核验 |
| --- | --- | --- |
| 同步数据库阻塞 async 路由 | 管理台 POST 改为同步端点，FastAPI 线程池处理鉴权、事务、业务；async 依赖仅读取有界表单。creators/summary 异步入口的登录查询移交线程池 | 原有路由权限测试加入无运行中事件循环断言 |
| 摘要前后 COUNT | schedule_if_due / _schedule_run / schedule_merges 返回实际插入数，利用新任务 UUID 与 RETURNING id 比较区分插入和冲突复用；保留原新增任务判断语义 | 幂等调度测试断言第一次新增 1、重复执行新增 0 |
| 消息来源 N+1 | message_users 增加 source_ids 批量读取，空列表不扩大范围；已选人员复用当前列表 | 来源撤权/越权测试、真实数据库空范围/重复 ID/未知 ID 核验 |
| 页面重复登录查询 | creators 与摘要 HTML 入口复用单次登录结果，不引入跨请求缓存 | 现有 UI 测试记录登录读取次数 |
| 共用 OpenAI 客户端 | 现有传输、响应解析、遥测、异常和退避移动至 openai_client.py，各业务仍保留各自材料、校验、重试预算 | 更新测试替身指向新传输位置；store:false、用量、限流与异常测试保留 |
| HTTP 共用能力 | http_helpers.py 提供有界表单读取、CSRF、JSON/错误、安全响应头与校验后 ETag | 路由/CSRF/ETag/失效授权回归保持 |
| 达人权限依赖 | creator_access.py 承接 UUID、Owner、CM 和达人域访问规则；imports 不再依赖旧 handles 私有权限函数 | 达人、导入、归属测试继续覆盖原权限行为 |
| 消息文本解析 | message_text.py 提供公共文本提取；display/evidence 两种规则明确分开，导出使用同一底层解析 | 富文本标题、@、卡片、损坏 JSON、无文本及保守 AI 输入测试 |

路由注册核对：仍为 59 条，29 GET、30 POST。未迁移数据、未修改功能开关、未调用真实模型。

## 已获得的线上只读证据

2026-09-21 核对部署源码仍为 f0dee27：

- creator_handle_tasks：30 succeeded、1 skipped；无待处理任务。
- creator_handle_runs：1 completed。
- offhours_summary_tasks：3622 条。
- contact_assessment / contact_assignment / creator_workflow：关闭。
- offhours_summary：开启。

这些证据支持进一步评估旧后台扫描退役，但不构成删除历史数据或人工核对接口的理由。

## 尚待完成的完整范围

1. 清查旧 handle 生产、领取、自动入库及历史人工查看链，决定并实施安全的退役边界；保留历史数据。
2. 继续消除新导入对 ownership 私有序列化/加密工具的依赖；必须维持原加密域和旧数据可读性。
3. 按实际职责整理仍过大的路由、报表与 worker 模块；避免新建通用框架和无意义的拆分。
4. 补齐连接开销、队列空转、摘要读取和 ETag 的测量；据证据决定是否需要连接池、调度降频或版本检查。未经证明不引入跨请求敏感数据缓存。
5. 对最终全部差异执行完整数据库回归、lint、路由和关键页面/下载验证；记录最终而非中间结果。
6. 按部署手册提交、推送、备份校验、迁移/重启并核验生产服务；保持原 AI 开关和数据。
7. 更新完成记录，逐项对照原审查报告验收后才能标记全量优化完成。

## 测试环境

隔离本地 PostgreSQL：/tmp/lark-full-opt-pg.kUTdNj/data，监听 127.0.0.1:55439。该目录由本次任务新建，不是生产库。

完整测试需同时设置 TEST_DATABASE_URL、IDENTITY_NAMES_TEST_DATABASE_URL、OFFHOURS_SUMMARY_TEST_DATABASE_URL 为 postgresql://postgres@127.0.0.1:55439/postgres；测试各自使用隔离 schema 并回滚。

本阶段最终本地验证：449 项全部通过，耗时 29.06 秒，无跳过；backend/app 的 Ruff 检查通过，git diff --check 通过。完整目标尚未完成，后续改动与发布仍需重新验证，不能将阶段性结果当作全量发布验收。

用户原有未跟踪周报 docs/status/project-weekly-report_2026-08-28_to_2026-09-04_draft.md 保持不动，不纳入本次提交。
