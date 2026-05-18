# AI 小说创作平台 - 前端

## 启动

```bash
npm install
npm run dev
```

## 构建

```bash
npm run build
```

## 环境变量

复制 `.env.example` 为 `.env`，按需修改：

```bash
cp .env.example .env
```

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `VITE_API_BASE_URL` | 后端 API 地址 | `http://localhost:8000/api` |

## Mock 模式

当前前端使用硬编码 mock 数据运行，无需后端即可预览 UI。mock 数据位于 `src/mock/data.ts`。

当后端就绪后，在 stores 中将 mock 数据替换为 `api.*` 调用即可切换到真实模式。

## 后端已实现 Endpoint

所有 endpoint 前缀为 `VITE_API_BASE_URL`（默认 `http://localhost:8000/api`）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/projects` | 项目列表 |
| GET | `/projects/:id` | 项目详情 |
| POST | `/projects` | 创建项目 |
| GET | `/projects/:id/chapters` | 章节列表 |
| POST | `/projects/:id/chapters` | 创建章节 |
| GET | `/projects/:id/experts` | 专家列表 |
| POST | `/projects/:id/experts` | 创建自定义专家 |
| POST | `/projects/:id/experts/:expertId/test` | 测试专家（SSE 流式） |
| POST | `/projects/:id/chapters/generate` | SSE 流式生成 |

### 生成请求体

```json
{
  "chapter_num": 1,
  "chapter_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "mode": "full_pipeline",
  "expert_id": "f0e1d2c3-b4a5-6789-0abc-def123456789"
}
```

所有字段可选。传 `chapter_id` 或 `chapter_num` 时会定位章节并将 writer 输出落库；都不传则只流式生成不落库。

`mode` 取值：`continue` | `full_pipeline` | `enhance` | `summarize`

### SSE 事件格式

后端返回标准 SSE，每个 block 由 `\n\n` 分隔：

```
event: progress
data: {"message": "loading context..."}

event: agent_start
data: {"agent": "renderer", "role": "writer"}

event: agent_output
data: {"token": "晨光透过舷窗..."}

event: agent_done
data: {"agent": "renderer", "role": "writer"}

event: done
data: {"message": "生成完成"}

event: error
data: {"message": "something went wrong"}
```

## 技术栈

- Vue 3 + TypeScript
- Vite
- Pinia (状态管理)
- Vue Router 4