# 数据库部署说明

## 桌面端 SQLite

桌面端在启动后端前设置 `DATABASE_URL` 为用户数据目录内的 SQLite 文件。`AUTO_CREATE_TABLES`
默认根据 SQLite URL 为 `true`，继续使用 `init_db()` 创建缺失表并执行 SQLite 增量补列；
部署过程不要求手动运行 Alembic。

## PostgreSQL 首次部署

服务器默认 `AUTO_CREATE_TABLES=false`。先配置 PostgreSQL URL 和 `JWT_SECRET`，然后在后端
目录运行：

```sh
alembic upgrade head
python scripts/check_schema_parity.py
```

后端启动时会读取 `alembic_version`，只有数据库处于当前脚本 head 才会继续启动。缺少版本表、
版本未升级或版本不匹配时，启动错误会提示先运行 `alembic upgrade head`。

## PostgreSQL 升级

部署新版本前先备份数据库，再在后端目录执行 `alembic upgrade head`，随后发布后端。启动探针
`/health/ready` 会在数据库未完成迁移时返回未就绪状态。桌面端 SQLite 不走此 PostgreSQL
迁移流程。

## 已知 schema 漂移

`backend/alembic/known_drift.json` 记录当前 Alembic PostgreSQL schema 与 ORM 元数据之间的已知差异：

- 可空性：部分迁移创建的列仍允许空值，而 ORM 定义为非空。
- UUID/字符串：部分 PostgreSQL 列的 UUID 与字符串表示不一致。
- JSON/JSONB：部分列使用 JSON，而 ORM 定义使用 JSONB 或兼容包装类型。
- 旧索引：迁移遗留索引与 ORM 当前索引定义不一致。
- 外键：部分迁移创建的外键与 ORM 当前约束不一致。

这些差异是后续需要专门处理的 schema 债务，本批不逐项修改模型或迁移。检查脚本只会因基线以外的新差异失败；基线中已消失的差异会提示但不失败。
