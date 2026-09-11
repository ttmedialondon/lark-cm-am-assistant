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

部分测试需要独立 PostgreSQL：共享夹具使用 `TEST_DATABASE_URL`，姓名与总结等模块另有测试 DSN。未设置时相关用例会跳过，不能把“普通测试通过”说成已完成全部数据库验证。不要使用生产数据库跑测试。

测试优先覆盖登录与权限、消息读取范围/加密、同步续跑、全员身份识别、头像、总结和数据保护。旧归属/导入工程的专属测试随功能删除，活跃功能的共享夹具保留。页面以少量真实浏览器流程验证；不为每个文案、CSS 类、重复按钮状态单独建测试，不能靠 skip 或缩小收集范围掩盖失败。

## 运行服务

配置字段见 [.env.example](.env.example)。应用读取进程环境变量，不会自动加载这个示例文件；本地由开发者注入测试配置，生产由 systemd 的 `EnvironmentFile` 注入。真实密钥和本地配置不能提交。

连接独立开发数据库并注入配置后：

```sh
.venv/bin/python scripts/migrate.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8100
```

另开终端，用同一开发环境运行 `.venv/bin/python -m app.worker`。worker 会领取同步及已启用的 AI 任务；不要把生产授权或 API Key 放进普通本地测试。

## 修改与发布

- 页面在 `app/templates/`、`app/static/`，不是仓库外的旧原型或独立写作助手。
- 数据库修改通过 `migrations/` 的顺序 SQL；迁移可能修改数据，执行前核对目标和备份。
- 生产操作只按 [部署手册](../docs/runbooks/deploy-mvp.md)。纯文档更新不运行迁移、不重启服务、不重新分析消息。
