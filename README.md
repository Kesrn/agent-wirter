# AI 创作平台

一个面向小说/长文创作者的 AI 辅助写作平台。项目支持创作项目管理、章节编辑、角色/世界观/大纲/暗线维护，并通过多 Agent 工作流完成章节生成、续写、润色、审校和读者视角反馈。

## 项目亮点

- **多 Agent 创作流水线**：基于 LangGraph 编排 `ContextLoader -> Writer -> Critic / ConsistencyChecker -> HumanReview`。
- **SSE 流式输出**：后端以 Server-Sent Events 推送生成进度和 token，前端实时展示 AI 写作过程。
- **RAG 上下文注入**：支持用户主动选择素材按 ID 精确加载，也支持基于 embedding 的相似素材检索。
- **自定义 Agent**：用户可以配置专家角色、system prompt、temperature、max tokens 和工作流位置。
- **多模型适配**：默认 mock 模式可直接运行；真实模型通过 OpenAI 兼容接口接入 OpenAI、DeepSeek、通义千问、智谱、Moonshot 等。
- **用户级 LLM 配置**：支持在设置页保存 provider、base URL、model 和 API Key，API Key 加密存储。
- **完整写作工作台**：章节编辑、素材维护、Agent 面板、生成审批、导出 txt/md。

## 技术栈

### 前端

- Vue 3
- TypeScript
- Vite
- Pinia
- Vue Router
- 原生 fetch + SSE 流解析

### 后端

- FastAPI
- SQLAlchemy Async
- Pydantic
- LangGraph
- PostgreSQL / pgvector 镜像
- JWT + bcrypt
- Docker Compose

## 目录结构

```text
.
├── backend/                         # FastAPI 后端
│   ├── agents/                      # LangGraph 工作流、LLM Provider、专家模板
│   ├── api/                         # 业务路由、认证、LLM 设置、限流
│   ├── config/                      # 环境配置
│   ├── db/                          # 数据库连接
│   ├── models/                      # SQLAlchemy 模型
│   ├── rag/                         # 上下文加载与 embedding
│   ├── schemas/                     # Pydantic 请求/响应模型
│   └── tests/                       # 冒烟测试
├── frontend/                        # Vue 前端
│   ├── src/api/                     # API 和 SSE 客户端封装
│   ├── src/components/              # 编辑器、Agent 面板、弹窗组件
│   ├── src/stores/                  # Pinia 状态管理
│   └── src/views/                   # 登录、项目列表、工作台、设置页
├── docker-compose.yml               # PostgreSQL + pgvector
├── .env.example                     # 后端环境变量示例
├── AI创作平台_项目经验.pdf
├── AI创作平台_项目经验讲解版.pdf
└── 面试问答准备.md
```

## 快速启动

### 1. 启动数据库

```bash
docker compose up -d
```

默认数据库配置：

```text
POSTGRES_DB=ai_creative
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
PORT=5432
```

### 2. 启动后端

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

后端默认地址：

```text
http://localhost:8000
```

健康检查：

```bash
curl http://localhost:8000/health
```

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev
```

前端默认地址：

```text
http://localhost:5173
```

Vite 已配置 `/api` 代理到 `http://localhost:8000`。

## 环境变量

复制 `.env.example` 后按需修改：

```bash
cp .env.example .env
```

主要配置：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL 连接地址 | `postgresql+asyncpg://postgres:postgres@localhost:5432/ai_creative` |
| `LLM_PROVIDER` | LLM 提供商 | `mock` |
| `LLM_API_KEY` | LLM API Key | 空 |
| `LLM_BASE_URL` | OpenAI 兼容接口地址 | 空 |
| `LLM_MODEL` | 模型 ID | `gpt-4o-mini` |
| `EMBEDDING_PROVIDER` | embedding 提供商 | `mock` |
| `EMBEDDING_API_KEY` | embedding API Key | 空 |
| `JWT_SECRET` | JWT 和 API Key 加密相关密钥 | 空，开发环境会自动生成 |
| `CORS_ORIGINS` | 允许的前端源 | `http://localhost:5173,http://localhost:3000` |
| `MAX_UPLOAD_BYTES` | 图片等通用上传大小上限 | `5242880` |
| `MAX_TXT_IMPORT_BYTES` | TXT 小说导入大小上限，不限制章节数量 | `52428800` |

默认 `mock` 模式不需要 API Key，适合本地演示和开发。

## 核心功能

### 创作项目管理

- 创建小说或文章项目
- 设置项目简介、目标字数和创作模式
- 按用户隔离项目数据

### 章节编辑

- 创建章节
- 编辑章节正文和大纲
- 自动保存
- 导出 txt / md

### 创作素材库

- 世界观条目
- 角色资料
- 角色关系
- 大纲条目
- 暗线/伏笔

### AI 生成模式

| 模式 | 说明 |
| --- | --- |
| `full_pipeline` | 完整多 Agent 流水线：加载上下文、生成正文、审校、一致性检查、人工确认 |
| `continue` | 先生成转折建议，再按用户选择方向续写 |
| `enhance` | 先生成润色方向，再按用户选择方向改写 |
| `summarize` | 从读者视角给出阅读反馈，不修改原文 |

### Agent 配置

项目创建后会自动生成内置专家：

- 创意大师
- 残酷大师
- 情节转折大师
- 渲染大师
- 专业编辑
- 概括者

也可以创建自定义 Agent，并配置其插入工作流的位置：

- `pre_writer`
- `post_writer`
- `replace_writer`
- `pre_critic`
- `post_critic`
- `replace_critic`
- `standalone`

## API 概览

所有业务接口默认以 `/api` 为前缀。

### 认证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/auth/register` | 注册 |
| `POST` | `/api/auth/login` | 登录 |
| `GET` | `/api/auth/me` | 当前用户 |

### 项目与章节

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/projects` | 项目列表 |
| `POST` | `/api/projects` | 创建项目 |
| `GET` | `/api/projects/{project_id}` | 项目详情 |
| `GET` | `/api/projects/{project_id}/chapters` | 章节列表 |
| `POST` | `/api/projects/{project_id}/chapters` | 创建章节 |
| `PATCH` | `/api/projects/{project_id}/chapters/{sequence_number}` | 更新章节 |
| `POST` | `/api/projects/{project_id}/chapters/generate` | SSE 流式生成 |
| `GET` | `/api/projects/{project_id}/export` | 导出项目 |

### 素材与 Agent

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET/POST/PATCH/DELETE` | `/api/projects/{project_id}/world-entries` | 世界观管理 |
| `GET/POST/PATCH/DELETE` | `/api/projects/{project_id}/characters` | 角色管理 |
| `GET/POST/PATCH/DELETE` | `/api/projects/{project_id}/character-relations` | 角色关系管理 |
| `GET/POST/PATCH/DELETE` | `/api/projects/{project_id}/outlines` | 大纲管理 |
| `GET/POST/PATCH/DELETE` | `/api/projects/{project_id}/hidden-threads` | 暗线管理 |
| `GET/POST/PATCH` | `/api/projects/{project_id}/experts` | Agent 管理 |
| `POST` | `/api/projects/{project_id}/experts/{expert_id}/test` | 测试 Agent |

## SSE 事件格式

后端返回标准 SSE，每个事件块由空行分隔：

```text
event: progress
data: {"message":"开始生成"}

event: agent_start
data: {"agent":"writer","step":"running"}

event: writer_output
data: {"token":"夜色渐深"}

event: critic_output
data: {"critiques":["节奏自然，但人物对话可加强。"]}

event: consistency_check
data: {"report":"与当前世界观设定一致。"}

event: done
data: {"message":"生成完成"}
```

## 测试

后端提供 SQLite 内存数据库冒烟测试：

```bash
cd backend
pytest
```

## 项目经验材料

仓库内包含两份项目经验文档，适合用于面试复盘：

- `AI创作平台_项目经验.pdf`
- `AI创作平台_项目经验讲解版.pdf`

另外：

- `面试问答准备.md`：围绕 LangGraph、RAG、SSE、LLM Provider 等模块整理的问答。
- `generate_project_explainer_pdf.py`：项目经验讲解版 PDF 生成脚本。

## 后续优化方向

- 将 LangGraph `MemorySaver` 升级为数据库持久化 checkpoint。
- 将当前 Python 内存余弦相似度检索升级为 pgvector 索引检索。
- 引入 reranker 和关键词 + 向量混合检索。
- 增加端到端测试，覆盖登录、创建项目、章节生成和导出流程。
- 优化前端编辑器，支持 diff 高亮、AI 修改对比和富文本编辑。

## License

This project is for learning, portfolio, and interview demonstration purposes.
