# 后端启动教程

## 环境要求

- Python 3.11+
- PostgreSQL 14+
- pip / venv

## 1. 配置环境变量

复制示例文件并填写配置：

```bash
cp .env.example .env
```

编辑 `.env`，确认以下配置：

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/ai_creative
SECRET_KEY=your-secret-key
```

## 2. 创建数据库

```bash
psql -U postgres -c "CREATE DATABASE ai_creative;"
```

如果数据库已存在则跳过。

## 3. 创建虚拟环境并安装依赖

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 4. 运行数据库迁移

```bash
source venv/bin/activate
alembic upgrade head
```

如果表已存在但 alembic 版本记录缺失，先 stamp 再升级：

```bash
alembic stamp head
```

## 5. 启动服务

```bash
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

启动成功后访问：

- API 服务：http://localhost:8000
- API 文档（Swagger）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 可选：Docker 启动 PostgreSQL

如果没有本地 PostgreSQL，可以用 Docker：

```bash
docker-compose up -d db
```

这会启动一个 PostgreSQL 容器，连接信息与 `.env` 中默认配置一致。

## 常见问题

**alembic 报表已存在**：表已手动创建但缺少版本记录，执行 `alembic stamp head` 同步版本。

**连接数据库失败**：检查 PostgreSQL 是否运行（`pg_isready`），以及 `.env` 中 `DATABASE_URL` 的用户名、密码、端口是否正确。

**端口被占用**：更换端口启动 `uvicorn main:app --port 8001`。
