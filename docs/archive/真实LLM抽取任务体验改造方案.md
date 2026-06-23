# 真实 LLM 抽取任务体验改造方案

## 0. 背景

当前小说结构化抽取已经能调用真实 LLM，并能把结果写入结构化知识库。

但真实页面测试暴露出一个严重体验问题：

- 用户点击“开始抽取”后，前端按钮长时间显示“启动中...”。
- 后端其实在同步处理一批章节。
- 真实 LLM 每章可能耗时几十秒到一分钟。
- 一批 5 章就可能让用户等待 5 分钟以上，看起来像页面卡死。

已经做过一个临时止血修复：

- 真实 LLM 下前端每次只推进 1 章。
- 按钮文案改为“抽取中（约1章）...”。
- commit：`6f1e12b fix: reduce real llm extraction batch wait`

这个只是止血，不是最终方案。

本阶段目标是把抽取从“长请求卡页面”改成“可观察、可控制、可继续”的任务体验。

## 1. 本阶段目标

实现一个轻量级抽取任务控制系统，不引入 Celery / Redis / 新任务队列。

必须支持：

1. 点击开始后，接口快速返回。
2. 前端能看到当前进度。
3. 支持继续推进下一章/下一批。
4. 支持暂停。
5. 支持取消。
6. 支持失败章节列表。
7. 支持单章重试。
8. 页面不再长时间卡在一个请求里。

本阶段不要做：

- 不要上 Celery。
- 不要上 Redis。
- 不要做全自动全书抽取。
- 不要做 embedding。
- 不要做新的结构化表。
- 不要重构整个抽取流水线。

## 2. 当前问题复盘

当前后端接口：

```text
POST /api/projects/{project_id}/knowledge/sources/{source_id}/extract
```

实际行为：

- 创建或复用 extraction job。
- 在同一个 HTTP 请求里调用 `advance_extraction_job`。
- `advance_extraction_job` 会同步处理章节。
- 处理完成后才返回。

这会导致：

- 真实 LLM 请求期间，前端一直 await。
- 用户看不到“正在第几章”。
- 无法中途暂停/取消。
- 请求失败时体验很差。

现有模型已有：

- `ExtractionJob`
- `ExtractionStaging`
- `ProjectSourceChapter`

所以不需要重新设计大架构，只需要在现有 job 表基础上补控制字段和接口。

## 3. 推荐实现方案

采用“前端轮询 + 后端短推进”的方式。

核心原则：

```text
一个 HTTP 请求最多推进 1 章。
请求结束后立即返回最新 job 状态。
前端根据状态决定是否自动继续调用下一次推进。
暂停/取消通过 job 状态控制。
```

这样不需要后台 worker，也不会长请求卡死。

## 4. 后端改造

### 4.1 Job 状态补充

当前已有状态大致包括：

- `PENDING`
- `RUNNING`
- `BATCH_DONE`
- `COMPLETED`
- `FAILED`
- `PARTIAL_FAILED`
- `CANCELLED`

本阶段需要确认或补充：

```text
PAUSED
CANCELLING，可选
```

如果不想加 `CANCELLING`，可以只加 `PAUSED`，取消直接落 `CANCELLED`。

### 4.2 ExtractionJob 建议补字段

如果已有字段则复用，没有则新增 migration。

建议字段：

```text
current_chapter_no INT NULL
last_error TEXT NULL
paused_at TIMESTAMP NULL
cancelled_at TIMESTAMP NULL
last_run_started_at TIMESTAMP NULL
last_run_finished_at TIMESTAMP NULL
last_run_outcome TEXT NULL
```

说明：

- `current_chapter_no`：前端显示“正在处理第 X 章”。
- `last_error`：前端显示最近错误。
- `last_run_outcome`：可记录 `merged / validation_failed / llm_error / cancelled`。

不要为了这一步做复杂日志表。

### 4.3 抽取推进接口

保留现有接口，但语义改清楚：

```text
POST /api/projects/{project_id}/knowledge/sources/{source_id}/extract
```

行为：

- 如果没有 job，创建 job。
- 如果 job 是 `PAUSED`，不推进，直接返回状态。
- 如果 job 是 `CANCELLED/COMPLETED/FAILED`，直接返回状态或按 force_reextract 创建新 job。
- 如果 job 可运行，则最多处理 1 章。
- 每处理一章后立即返回。

注意：

- 不要在一个请求里处理 5 章。
- 不要让请求超过 90 秒。
- 如果单章 LLM 超时，记录失败并返回，不要让前端一直等。

### 4.4 状态接口

现有：

```text
GET /api/projects/{project_id}/knowledge/sources/{source_id}/extract/status
```

需要返回更完整字段：

```json
{
  "job_id": "...",
  "status": "RUNNING|BATCH_DONE|PAUSED|COMPLETED|FAILED|PARTIAL_FAILED|CANCELLED",
  "total_chapters": 3130,
  "extracted_count": 5,
  "merged_count": 5,
  "failed_count": 0,
  "pending_count": 3125,
  "current_chapter_no": 6,
  "provider": "deepseek",
  "is_mock": false,
  "last_error": null,
  "last_run_outcome": "merged",
  "updated_at": "..."
}
```

### 4.5 暂停接口

新增：

```text
POST /api/projects/{project_id}/knowledge/sources/{source_id}/extract/pause
```

行为：

- 找到当前未完成 job。
- 如果是 `RUNNING` 或 `BATCH_DONE`，置为 `PAUSED`。
- 不打断正在执行中的 LLM 请求，但下一次推进必须停止。
- 返回最新状态。

### 4.6 恢复接口

可以不单独做 resume，复用 `/extract`：

- 如果 job 是 `PAUSED`，第一次 `/extract` 可以把状态改回 `BATCH_DONE` 或 `RUNNING` 并推进一章。

如果做单独接口：

```text
POST /api/projects/{project_id}/knowledge/sources/{source_id}/extract/resume
```

也可以，但 MVP 不强制。

### 4.7 取消接口

新增：

```text
POST /api/projects/{project_id}/knowledge/sources/{source_id}/extract/cancel
```

行为：

- 当前 job 置为 `CANCELLED`。
- 不删除已有 staging 和结构化结果。
- 不打断正在执行中的 LLM 请求，但本次请求返回后不能继续推进。
- 前端显示“已取消，可重置或重新开始”。

### 4.8 失败章节列表接口

新增：

```text
GET /api/projects/{project_id}/knowledge/sources/{source_id}/extract/failures
```

返回：

```json
{
  "items": [
    {
      "chapter_no": 12,
      "chapter_title": "第12章 ...",
      "status": "VALIDATION_FAILED|FAILED|MERGE_FAILED",
      "retry_count": 2,
      "error_message": "...",
      "updated_at": "..."
    }
  ],
  "total": 1
}
```

来源：

- `ExtractionStaging`
- 只取失败状态。
- 必须限制 `project_id + source_id`。

### 4.9 单章重试接口

新增：

```text
POST /api/projects/{project_id}/knowledge/sources/{source_id}/extract/retry-chapter
```

请求体：

```json
{
  "chapter_no": 12,
  "force_reextract": true
}
```

行为：

- 找到该章节。
- 清理或复用该章失败 staging。
- 只重跑这一章。
- 成功后刷新 job counts。
- 返回该章结果和 job status。

注意：

- 不要影响其他章节。
- 不要重置整本书。

## 5. 前端改造

文件重点：

- `frontend/src/components/NovelExtraction.vue`
- `frontend/src/api/client.ts`

### 5.1 API client

新增：

```ts
pauseExtraction(projectId, sourceId)
cancelExtraction(projectId, sourceId)
listExtractionFailures(projectId, sourceId)
retryExtractionChapter(projectId, sourceId, chapterNo)
```

### 5.2 状态展示

结构化抽取区域需要显示：

- 状态：未开始 / 运行中 / 本批完成 / 已暂停 / 已取消 / 已完成 / 部分失败
- 当前进度：`已合并 X / 总章节 Y`
- 当前章节：`正在处理第 N 章`
- provider：`deepseek / mock`
- 最近错误：如果有 `last_error`

### 5.3 按钮

根据状态显示按钮：

未开始：

- 切分章节
- 开始抽取

运行中：

- 暂停
- 取消

BATCH_DONE：

- 继续抽取下一章
- 自动连续抽取开关
- 暂停
- 取消

PAUSED：

- 继续抽取
- 取消

CANCELLED：

- 重置抽取
- 重新开始，可选

COMPLETED：

- 重置抽取

### 5.4 自动连续抽取开关

新增一个轻量开关：

```text
[ ] 自动连续抽取
```

默认关闭。

用户打开后：

- 前端每次 `/extract` 返回 `BATCH_DONE`，如果还有未处理章节，就自动继续调用 `/extract`。
- 每次只推进 1 章。
- 每章之间可以间隔 1-2 秒。
- 用户点击暂停或取消后必须停止自动推进。

注意：

- 不要默认自动跑 3130 章。
- 真实 LLM 下默认必须让用户手动选择是否连续。

### 5.5 失败章节面板

在抽取区下面增加一个小面板：

```text
失败章节（N）
第12章 xxx  VALIDATION_FAILED  重试
第39章 xxx  LLM_ERROR          重试
```

点击重试只重试该章。

### 5.6 用户提示

真实 LLM 下显示提示：

```text
真实模型每章可能需要几十秒。当前采用单章推进，避免页面长时间无响应。
```

mock 下显示：

```text
当前为 mock，结果仅用于流程测试。
```

## 6. 测试要求

### 6.1 后端测试

新增或补充测试：

1. `test_extract_advances_only_one_chapter_for_real_provider`
   - 非 mock provider 下，一次 `/extract` 只处理 1 章。

2. `test_pause_extraction_stops_future_advance`
   - pause 后再次 `/extract` 不推进。

3. `test_cancel_extraction_stops_future_advance`
   - cancel 后再次 `/extract` 不推进。

4. `test_list_extraction_failures`
   - 构造失败 staging，接口能列出。

5. `test_retry_failed_chapter_only_reprocesses_that_chapter`
   - 只重试指定章节，不影响其他章节。

6. `test_status_returns_current_chapter_and_last_error`
   - 状态接口包含新增字段。

### 6.2 前端验证

必须通过：

```bash
cd frontend
npm run build
```

页面冒烟：

- 点击开始抽取，按钮不应长时间显示“启动中”。
- 真实模型下每次处理 1 章。
- 打开自动连续抽取后，会一章一章推进。
- 点击暂停后停止推进。
- 点击取消后停止推进。
- 失败章节能展示。
- 单章重试按钮能调用接口。

## 7. 验收标准

可以认为本阶段完成的标准：

- 点击开始抽取不会让页面 5 分钟无反馈。
- 状态能看到当前处理进度。
- 用户可以暂停。
- 用户可以取消。
- 用户可以查看失败章节。
- 用户可以单章重试。
- 自动连续抽取默认关闭。
- 真实 LLM 下一次只推进 1 章。
- 后端全量测试通过。
- 前端 build 通过。

## 8. 禁止事项

本阶段禁止：

- 不要引入 Celery / Redis。
- 不要做新 embedding。
- 不要做全书自动抽取默认开启。
- 不要改动结构化抽取 schema。
- 不要改动人工修正 CRUD。
- 不要删除现有抽取结果。

## 9. 回报格式

完成后按以下格式回报：

```text
## 抽取任务体验改造报告

### 后端改动
- 新增字段/迁移:
- 新增接口:
- 修改接口:

### 前端改动
- 状态展示:
- 按钮:
- 自动连续抽取:
- 失败章节:
- 单章重试:

### 验证结果
- 后端测试:
- 前端 build:
- git diff --check:
- 页面实测:

### 已解决的问题
- 长请求卡页面:
- 暂停:
- 取消:
- 失败章节:
- 单章重试:

### 剩余问题
- P0:
- P1:
- P2:

### 是否建议提交/push
- 是/否:
- 理由:
```
