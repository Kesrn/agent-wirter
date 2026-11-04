# 检索评测运行器（阶段一：多检索器对比）

检索评测框架：加载评测数据集 → 逐样本调检索器 → 计算指标 → 保存结果 JSON。

阶段零：ILIKE 关键词检索基线（`services.knowledge_source.search_project_knowledge`），
已产出 regression（154 条）与 natural（70 条）两套基线结果（2026-08 扩充后；
扩充前 76/28 条的旧结果见 `results/baseline_regression_76.json` 与
`results/baseline_natural_28.json`）。
阶段一：评测框架支持多检索器对比（`--retriever ilike|lexical`），并新增结果对比工具
`compare_results.py`，供 ilike 基线 vs lexical 检索器横向对比。

## 运行环境（重要）

**不要新建 venv**。使用项目已有的 `backend/venv`（Python 3.10，已装 aiosqlite / sqlalchemy / asyncpg）：

```bash
cd backend
./venv/bin/python retrieval_eval/run_baseline.py
```

运行器内部会在导入任何项目模块之前设置
`DATABASE_URL=sqlite+aiosqlite:///<db路径>`（`config/settings.py` 在 import 时读取环境变量，
必须先设置后导入）。因此直接运行即可，无需手动 export。

## 运行命令

```bash
# 冒烟模式（默认数据集文件不存在时自动启用内置冒烟样本）
./venv/bin/python retrieval_eval/run_baseline.py

# ILIKE 基线：regression 数据集（阶段零）
./venv/bin/python retrieval_eval/run_baseline.py \
    --dataset retrieval_eval/datasets/regression_candidates.json \
    --project d62d4447-b8d7-4fc3-9193-a6d363d8f541 \
    --limit 5 \
    --out retrieval_eval/results/baseline_reg.json

# ILIKE 基线：自然查询集（阶段一补充，70 条）
./venv/bin/python retrieval_eval/run_baseline.py \
    --dataset datasets/natural_queries.json \
    --out results/baseline_natural_70.json

# lexical 检索器：同一数据集跑另一套（lexical_retriever.py 合入后）
./venv/bin/python retrieval_eval/run_baseline.py \
    --retriever lexical \
    --dataset datasets/regression_candidates.json \
    --out results/lexical_regression_154.json

# 大数据集调试：只跑前 N 条
./venv/bin/python retrieval_eval/run_baseline.py --max-samples 20
```

相对路径解析规则：`--dataset` / `--out` 相对路径先按当前工作目录解析，
cwd 下不存在时回退到 `retrieval_eval/` 目录（所以从项目根或 backend/ 下跑都能找到
`datasets/` 与 `results/`）。

参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--db` | `backend/dev-web.sqlite3` | SQLite 数据库路径（只读打开） |
| `--project` | 库中第一个项目 | 项目 ID（`projects` 表第一行） |
| `--dataset` | `datasets/regression_candidates.json` | 评测数据集 JSON；文件不存在时用内置冒烟样本 |
| `--retriever` | `ilike` | 检索器：`ilike`（现有关键词检索）或 `lexical`（`services.lexical_retriever`） |
| `--limit` | 5 | 检索 top-k |
| `--source-kinds` | 无 | 仅 lexical 生效：限定 source_kind，逗号分隔（如 `source,note`）；ilike 忽略 |
| `--chapter-num` | 无 | 仅 lexical 生效：限定章节号，透传给 `LexicalRetriever.search(chapter_num=...)`；ilike 忽略 |
| `--out` | `results/baseline_<时间戳>.json` | 结果 JSON 输出路径（lexical 模式默认 `results/lexical_<时间戳>.json`） |
| `--max-samples` | 全部 | 最多跑前 N 条样本（调试用） |

## 检索器（--retriever）

| 检索器 | 实现 | 说明 |
|---|---|---|
| `ilike`（默认） | `services.knowledge_source.search_project_knowledge` | 阶段零既有逻辑，不做任何改动 |
| `lexical` | `services.lexical_retriever.LexicalRetriever` | 阶段一新增，由 lexical retriever 任务并行实现 |

lexical 模式共享契约（`backend/services/lexical_retriever.py`）：

```python
@dataclass
class RetrievalHit:
    chunk_id, project_id, source_kind, source_id, source_version_id,
    chapter_no, title, content, snippet, score, rank

class LexicalRetriever:
    async def search(self, db, project_id, query, *, source_kinds=None,
                     limit=10, entity_first=True, chapter_num=None) -> list[RetrievalHit]
```

- 运行器在函数内**延迟导入**该模块：文件尚未实现时给出清晰报错（提示改用 `--retriever ilike`），
  不会在脚本启动时崩溃。
- 返回的 `RetrievalHit` 会被映射为与 ilike 结果同构的 dict（`retrieval_hit_to_dict`），
  命中判定逻辑与输出 JSON 结构两种模式完全一致。
- lexical 结果自带 `chapter_no` / `source_kind` / `rank`，会正常参与命中判定
  （`chapter_no` 全局约束）并额外写入每样本 `results[]`。

## 数据集格式

数据集文件放在 `datasets/` 下。当前已有：

- `regression_candidates.json`（154 条，生成器 `generate_regression.py`：76 条结构化回归 + 78 条困难样本 alias/typo/homophone/same_name/conflict/cross_chapter/no_result）
- `natural_queries.json`（70 条，人工自然查询 + 困难样本；28 条人工精修 + 42 条由 `generate_natural_more.py` 构造）

顶层可以是 JSON 数组，也可以是 `{"samples": [...]}` / `{"items": [...]}` / `{"data": [...]}`。
每条样本：

```json
{
  "query_id": "reg-001",
  "category": "character_first_appearance",
  "query": "问句",
  "expected": {
    "evidence_type": "character_profile|ability_profile|event_timeline|world_rule|source_chunk",
    "chapter_no": 12,
    "source_ids": ["..."],
    "entities": ["..."]
  },
  "difficulty": "normal",
  "notes": ""
}
```

必填：`query_id`、`query`、`expected`（`expected.source_ids` 或 `expected.entities` 至少一个）。
缺字段的样本会被跳过并计数。

**内置冒烟样本**：`--dataset` 文件不存在（或为空）时，运行器用 sqlite3 只读读取该项目
`project_sources` / `project_source_chunks` 的真实内容，按关键词模板动态构造最多 5 条
查询（`smoke-001` ~ `smoke-005`，difficulty=`smoke`），保证开箱即用。

## 命中判定规则（两种检索器共用）

ilike 检索器返回 chunk 级结果：`source_kind / source_id / chunk_id / title / snippet / score / matched_query`；
lexical 检索器返回 `RetrievalHit`，运行器先映射为同构 dict 再判定。
`evidence_type` / `chapter_no` 仅当结果里真的存在时才参与判定。

对单条结果 r，命中 `expected` 当且仅当：

1. `r.source_id ∈ expected.source_ids`，或
2. `r.chunk_id ∈ expected.source_ids`（兼容数据集写 chunk 级 id），或
3. r 带 `evidence_type` 且等于 `expected.evidence_type`，且 r 的 snippet/title 包含 `expected.entities` 任一实体
   （ilike 结果无该字段，此分支不触发，为未来结构化证据预留）；
4. `chapter_no` 是全局约束：结果与期望都提供且不相等时，任何分支都不算命中
   （ilike 结果无此字段不触发；lexical 结果自带 chapter_no，会正常参与约束）。

样本级 hit = top-k 结果中任意一条命中。

注意：仅提供 `expected.entities` 而不提供 `source_ids` 的样本，在 ILIKE 基线
（结果 dict 无 `evidence_type` 字段）下按上述规则必然 miss。数据集生成任务应尽量
给每条样本标注真实 `source_ids`，否则基线无法区分检索质量。

## 指标定义

| 指标 | 定义 |
|---|---|
| Recall@K | 样本级命中比例（期望证据出现在 top-K 即记 1） |
| MRR | 每样本第一个命中结果的 1/rank（无命中记 0），再取平均 |
| Precision@K | 每样本 top-K 中命中条数 / min(K, 实际返回条数)，再取平均；无返回结果记 0 |
| 误召回率 | 1 - Precision@K |
| P95 延迟 | 所有查询耗时的 95 分位（ms），n 小时取第 ceil(0.95n) 小值 |

控制台会打印总体 + 按 `category` / `difficulty` 分组的汇总表。

## 结果对比工具（compare_results.py）

多检索器产出的结果 JSON 可直接对比（纯 Python 标准库实现，无第三方依赖）：

```bash
cd backend

# 对比 ilike 基线 vs lexical 检索器（同一数据集 regression 76 条）
./venv/bin/python retrieval_eval/compare_results.py \
    retrieval_eval/results/baseline_regression_76.json \
    retrieval_eval/results/lexical_regression_76.json \
    --label-base ilike --label-cmp lexical

# 等价写法（--base / --cmp）
./venv/bin/python retrieval_eval/compare_results.py \
    --base retrieval_eval/results/baseline_regression_76.json \
    --cmp retrieval_eval/results/lexical_regression_76.json

# 同时导出对比 JSON（供后续阶段/报告使用）
./venv/bin/python retrieval_eval/compare_results.py a.json b.json --out comparison.json
```

输出内容：

- **总体对比**：n_samples / recall@k / MRR / precision@k / 误召回率 / p95 延迟，含差值 Δ（对比 - 基准）；
- **按 category 分组对比** 与 **按 difficulty 分组对比**：每组的 n / recall / MRR / precision 及差值；
- 两份文件样本数不一致时输出警告（可能不是同一数据集），但仍继续对比。

指标从两份文件的 `samples[]`（每样本 hit / mrr / precision / latency_ms）重新聚合，
不依赖文件里已有的 summary / by_* 字段，保证对比口径一致。自对比验证：

```bash
./venv/bin/python retrieval_eval/compare_results.py \
    retrieval_eval/results/baseline_regression_76.json \
    retrieval_eval/results/baseline_regression_76.json
# 预期：所有 Δ 均为 0
```

## 结果文件说明

`--out` JSON 结构：

```json
{
  "meta": { "runner": "...", "retriever": "ilike|lexical", "created_at": "...",
            "db_path": "...", "project_id": "...", "project_title": "...",
            "dataset": "...", "top_k": 5, "hit_rules": "..." },
  "summary": { "n_samples", "recall_at_k", "mrr", "precision_at_k", "false_recall_rate",
               "p95_latency_ms", "total_runtime_sec" },
  "by_category":   { "<category>":   {同上指标} },
  "by_difficulty": { "<difficulty>": {同上指标} },
  "samples": [
    { "query_id", "category", "difficulty", "query", "expected",
      "latency_ms", "hit", "mrr", "precision",
      "results": [ { "source_id", "chunk_id", "title", "score", "matched_query", "hit", "snippet" } ] }
  ]
}
```

`meta.retriever` 为阶段一新增字段；lexical 模式每样本 `results[]` 额外携带
`chapter_no` / `source_kind` / `rank`（ilike 模式输出与阶段零完全一致）。

已产出结果：

| 文件 | 数据集 | 指标（top_k=5） |
|---|---|---|
| `results/baseline_regression_76.json` | regression_candidates.json（76 条，扩充前） | recall@k=0.132 / mrr=0.132 |
| `results/baseline_regression_v2.json` | regression_candidates.json（154 条，扩充后） | recall@k=0.325 / mrr=0.325 |
| `results/baseline_natural_28.json` | natural_queries.json（28 条，扩充前） | recall@k=0.607 / mrr=0.607 |
| `results/baseline_natural_70.json` | natural_queries.json（70 条，扩充后） | recall@k=0.471 / mrr=0.471 |

## 知识问答接入 RETRIEVAL_MODE（阶段一启用）

评测证明 lexical 检索质量显著优于 ILIKE 基线（recall@5：0.132 → 0.816）后，
词法检索已接入知识问答检索链路，由环境变量 `RETRIEVAL_MODE` 控制
（`backend/config/settings.py`）：

| 模式 | 行为 |
|---|---|
| `ilike`（默认） | 原 ILIKE 关键词检索（`search_project_knowledge` 原逻辑），行为完全不变 |
| `lexical` | 项目存在检索索引（`retrieval_chunks` 有该项目数据）时走 `LexicalRetriever`；索引缺失 / 检索抛异常 / 策略要求回退时降级 ILIKE（记日志） |
| `hybrid` | lexical 结果 + ILIKE 结果合并去重（lexical 优先，按 `chunk_id` 去重） |

启用方式：启动前设置环境变量（或写入 `.env`）：

```bash
cd backend
RETRIEVAL_MODE=lexical ./venv/bin/python -m uvicorn main:app
```

### 接入位置

`backend/services/knowledge_source.py`（原函数签名不变，内部按 flag 分发）：

| 函数 | 职责 |
|---|---|
| `search_project_knowledge` | 检索分发入口：`ilike` 直接走原逻辑；`lexical/hybrid` 先尝试 `_dispatch_lexical_or_ilike`，返回 None（降级）时回退 ILIKE |
| `_dispatch_lexical_or_ilike` | 分发函数：检查检索索引 → 按 `retrieval_policy.get_retrieval_policy(intent)` 调 `LexicalRetriever.search(source_kinds/entity_first)`；hybrid 时再并入 ILIKE 结果 |
| `_retrieval_hit_to_result` | `RetrievalHit` → knowledge_source 同构结果 dict（source_kind 归一：`source_chunk→project_source_chunk`、`chapter_version→chapter` 等），下游证据分类/重排/分组无需改动 |
| `_merge_lexical_and_ilike` | hybrid 合并去重（lexical 优先） |
| `_project_has_retrieval_index` | 索引存在性检查（表缺失/查询失败一律视为无索引） |
| `_search_project_knowledge_ilike` | 原 ILIKE 检索逻辑（改名保留，作为降级路径） |

策略参数按 D5 集中在 `services/retrieval_policy.get_retrieval_policy(intent)`：
`source_kinds` / `entity_first` 来自策略；`limit` 沿用调用方 limit
（`RetrievalPolicy` 无 flat limit 字段，`max_per_kind` 是每类来源上限，由下游
证据分类层 `knowledge_retrieval.MAX_CHUNKS_PER_SOURCE` 执行）。

阶段二落地（实体优先通道）：`qa/query/ask` 之外，知识库 Query Planner 与结构化
问答产出的全部问答类 intent 已登记到 qa 策略（`entity_first=True`、
`allow_chapter_body=True`、`source_kinds=None`、`on_no_result="log"`）：

- V1 规则 planner（`services/knowledge_query_plan.py`）：`character_profile` /
  `character_by_ability` / `relationship` / `ability` / `timeline` /
  `worldbuilding` / `plot` / `general`；
- V2 LLM planner（`services/knowledge_query_planner.py` `VALID_INTENTS`）：
  `character_ability` / `plot_event` / `source_lookup`；
- 结构化问答（`services/structured_qa.py`）：`event_query` / `world_rule_query`。

即 QA 的 lexical 路径现在走实体优先通道（此前这些 intent 按 D5 回退 generate
兜底策略，`entity_first=False`，实体通道不生效；分发代码无需改动——这正是走
`get_retrieval_policy` 集中映射的好处）。真实链路验证脚本：

```bash
cd backend
./venv/bin/python retrieval_eval/verify_entity_channel.py
```

脚本用 dev 库（项目 d62d4447，7276 个 retrieval_chunks）验证：问答 intent →
qa 策略（entity_first=True）→ `normalize_entities` 实体归一 →
`LexicalRetriever.search(entity_first=True)` 实体加权命中（top-1 score ≈ 1000，
显著高于词法通道）。注意实体名录（entity_map）按设计只来自
`character_alias_clusters`（已确认别名簇），本项目该表为空时脚本会输出说明并
在进程内模拟该数据前提（仅内存、不写库）；无检索索引的项目输出 SKIP（退出码 2）。

### 降级行为（不破坏既有问答可用性）

1. 项目无检索索引（`retrieval_chunks` 表不存在或没有该项目数据）→ ILIKE，`logger.warning`；
2. `LexicalRetriever.search` 抛异常 → ILIKE，`logger.warning`；
3. `lexical` 模式无命中且策略 `on_no_result == "fallback"` → ILIKE（更宽泛检索），`logger.info`；
4. `lexical` 模式无命中且策略 `on_no_result in ("empty", "log")` → 返回空结果；
5. `hybrid` 模式任何情况下都并入 ILIKE 结果，不会丢召回；
6. 带 `source_type` 过滤的检索（ILIKE 独有能力）→ 直接走 ILIKE。

检索索引由 `services/retrieval_indexer` 构建（定稿章节 / 资料切片 / 结构化知识
投影），项目尚无索引时所有模式都安全回退到 ILIKE。

## 安全与边界

- 数据库只读：sqlite3 侧用 `mode=ro` URI，ORM 侧只调检索接口（纯 SELECT），全程不 commit。
  绝不修改 `dev-web.sqlite3`（唯一例外：`retrieval_eval/fix_dev_schema.py` 显式补列修复
  schema 漂移，备份先行，见下文"旧 RAG 影子对比"章节）。
- 不修改 `services/`（含 `lexical_retriever.py`，由 lexical retriever 任务实现）或任何现有代码；
  本目录（`retrieval_eval/`）是独立的评测工具。
- 后续阶段（向量检索 / 重排 / Query Planner 增强）可直接复用同一数据集与
  结果文件格式做横向对比（`compare_results.py` 开箱即用）。

## 旧 RAG 影子对比（shadow_compare.py，退役流程第一步）

背景：项目存在新旧两条上下文/检索链路。

- **新链路（主链路）**：`services/chapter_context.build_chapter_context` +
  `services.knowledge_source.search_project_knowledge`（`RETRIEVAL_MODE=lexical`
  时经 `retrieval_policy` 分发到 `services.lexical_retriever.LexicalRetriever`，
  读 `retrieval_chunks` / `retrieval_chunks_fts` 检索投影表）；
- **旧链路（退役候选）**：`rag/context_loader.ContextLoader`（`search_similar` /
  `load_context`，world_entries / characters 的 embedding 内存余弦，embedding 为
  SHA256 哈希伪向量；仍挂在 `agents/workflow.py` 作最终 fallback）。

退役流程约定：**先影子对比，稳定观察后再删**。本工具不删除任何代码，只做对比观测。

### 用法

```bash
cd backend
# 内置 10 条演示查询（基于 dev-web.sqlite3 项目 d62d4447 的真实小说内容）
./venv/bin/python retrieval_eval/shadow_compare.py --out retrieval_eval/results/shadow_compare.json

# 自定义查询列表（JSON 数组或 {"queries": [...]}，每条含 query / query_id / category / notes）
./venv/bin/python retrieval_eval/shadow_compare.py --queries q.json

# 旧链路指向其他库（如生产 PG，schema 通常比 dev sqlite 新）
./venv/bin/python retrieval_eval/shadow_compare.py \
    --old-db-url postgresql+asyncpg://postgres:postgres@localhost:5432/ai_creative
```

参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--db` | `backend/dev-web.sqlite3` | 新链路数据库（只读） |
| `--project` | 库中第一个项目 | 项目 ID |
| `--queries` | 内置 10 条演示查询 | 查询列表 JSON |
| `--limit` | 5 | 新链路 top-k |
| `--old-limit` | 5 | 旧链路 top-N |
| `--old-db-url` | 同一 dev 库 | 旧链路目标库连接串（生产 PG 等） |
| `--out` | 仅控制台 | 结果 JSON 输出路径 |

前置条件：新链路检索投影已建立（`retrieval_chunks` 有该项目数据）。
缺失时脚本提示先运行 `retrieval_eval/index_dev_db.py` 并退出（码 2）。
dev-web.sqlite3 全程只读（mode=ro + 纯 SELECT，绝不 commit；schema 修复走
`fix_dev_schema.py`，本工具自身保持只读）。

### 有效性门控与退出码（v2，2026-08 起）

报告 JSON 顶层新增 4 个判定字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_ok` | bool | 旧链路目标库 world_entries / characters 是否与 ORM 模型列集合兼容（无缺列） |
| `eligible` | bool | 目标项目是否具备影子比较资格：schema 兼容 **且** world_entries / characters / 各自 embedding 数量均 > 0 |
| `ineligibility_reason` | string/null | `eligible=false` 的原因（schema 不兼容，或"项目无旧链路语料，不具备影子比较资格（...）"）；通过门槛时为 null |
| `valid_for_retirement_decision` | bool | 本报告是否可用于退役决策/7 天观察统计（= schema 兼容 且 eligible 且实际产出重合度指标） |

启动时按序门控（未通过的路径不运行两条链路、不产出任何重合度指标）：

1. 新链路检索投影缺失（`retrieval_chunks` 无该项目数据）→ 退出码 **2**（未到判定阶段）；
2. 旧链路目标库 schema 不兼容（缺列，如修复前的 dev 库 `world_entries` 缺
   `chapter_sequence_number`）→ 报告 `schema_ok=false`、
   `valid_for_retirement_decision=false` + 原因，**退出码 3（非零）**；
3. 旧链路语料门槛：world_entries / characters / embedding 数量任一为 0 →
   报告 `eligible=false`、`ineligibility_reason="项目无旧链路语料，不具备影子比较资格（...）"`，
   **退出码 0**（合法判定而非错误），同样不产出重合度指标；
4. 通过门槛 → 正常跑两条链路，报告 `eligible=true`、
   `valid_for_retirement_decision=true`，含 `summary` / `per_query` 重合度指标，退出码 0。

退出码约定：

| 码 | 含义 |
|---|---|
| 0 | 正常完成：`eligible=true`（含指标，可计入观察）或 `eligible=false`（合法判定，报告不含指标） |
| 2 | 新链路检索投影缺失，无法开始对比 |
| 3 | 旧链路目标库 schema 不兼容，判定无效（报告 `valid=false`），需先修复 schema 后重跑 |

> 注意：`eligible=false` 场景**不是错误**（退出码 0），但报告不含 `summary` /
> `per_query`，聚合方必须按字段过滤，不能把"无指标报告"当作重合度 0 计数。

### 指标含义

对每条查询分别跑新链路（top-K）与旧链路（top-N），证据匹配规则
（任一条命中即视为同一证据）：

1. `source_id` 相同（ID 级）；
2. 标题指纹相同；
3. 标题 + snippet/content 指纹相同；
4. 文本包含关系（一方标题/片段包含另一方，且较短文本 ≥ 20 字符）。

| 指标 | 定义 |
|---|---|
| 内容重合度 | 重叠证据数 / 并集（`overlap / (new_n + old_n - overlap)`），逐查询均值 |
| 新链路覆盖旧链路 top-N | 旧链路 top-N 中有多少出现在新链路 top-K（`new_covers_old`）；旧链路无结果记 N/A（按 0 计入均值） |
| 旧链路覆盖新链路 top-K | 反向覆盖 |
| 耗时对比 | 两链路逐查询 mean / p95 / max（ms）；新链路首查 warmup 建词表的一次性开销单独记录、不计入稳态均值 |

输出 JSON（`--out`）结构：

```json
{
  "meta": { "new_chain", "old_chain", "top_k_new", "top_n_old", "retrieval_mode",
            "new_chain_warmup_latency_ms", ... },
  "environment": {
    "new_chain_index": { "retrieval_chunks", "retrieval_vectors" },
    "old_chain_data": { "world_entries", "world_entries_with_embedding",
                        "characters", "characters_with_embedding", "chapters", "project_sources" },
    "old_chain_schema": { "world_entries_missing", "characters_missing", "dialect" },
    "old_chain_schema_broken": true/false,
    "load_context_diag": { "context_len", "latency_ms", "context_preview" }
  },
  "valid_for_retirement_decision": true/false,
  "schema_ok": true/false,
  "eligible": true/false,
  "ineligibility_reason": null | "项目无旧链路语料，不具备影子比较资格（...）",
  "summary": { "n_queries", "queries_with_old_results", "mean_overlap_ratio",
               "new_covers_old_mean", "old_covers_new_mean", "new_latency", "old_latency" },
  "per_query": [ { "query_id", "category", "overlap", "overlap_ratio",
                   "new_covers_old_ratio", "old_covers_new_ratio", "new_top", "old_top", ... } ]
}
```

门控未通过的报告（`eligible=false` 或 `schema_ok=false`）**不包含** `summary` /
`per_query` 字段（无重合度指标），仅含 `meta` / `environment` / 判定字段。

`environment` 三块诊断用于正确解读重合度：

- `old_chain_data`：旧链路在目标库/项目的可用数据量（world_entries / characters
  及其 embedding 数）；
- `old_chain_schema`：ORM 模型列 vs 目标库实际列（dev sqlite 曾出现
  `world_entries` 缺 `chapter_sequence_number` 的 schema 漂移，旧链路内部捕获异常
  后**静默返回空**——此时重合度 0 是错误降级，不是"新链路丢覆盖"）；
- `load_context_diag`：workflow.py fallback 实际调用形态（chapter_id=None）的
  组装结果。

### 观察期建议（进入删除评估的门槛）

影子对比的目标是证明"新链路在**所有线上查询形态**上都能覆盖旧链路的输出"，
因此建议：

1. **观察期 ≥ 连续 7 天**，覆盖真实用户查询（知识问答 / 章节生成 / 方向建议三路
   的线上 query 抽样落盘，逐步替换内置演示查询）；
2. **只统计 `eligible=true` 且 `valid_for_retirement_decision=true` 的报告**
   （即正常产出重合度指标的报告）；`eligible=false`（无旧链路语料）与
   `valid=false`（schema 不兼容，退出码 3）的报告记录在案但**一律不计数**；
3. **当前 dev 库没有任何合格项目**：3 个项目（d62d4447 / de04afc8 / 49aee26a）
   的 world_entries / characters / chapters 全部为 0（仅 project_sources 有数据），
   即使 schema 已修复，旧链路（基于 world_entries / characters 的 embedding 检索）
   依然无任何语料 —— **观察期从"找到有旧链路数据的项目"起算**（如后续抽取/导入
   产生 world_entries 与 characters 及其 embedding 数据后，重新开始累计 7 天）；
4. **重合度门槛**：连续 N 天（建议 N ≥ 7）每天
   `mean_overlap_ratio ≥ 0.9`，且 `new_covers_old_mean ≥ 0.95`
   （旧链路有结果时）才进入删除评估；旧链路目标库无数据/无 embedding 时，
   记录为"旧链路空转"天数，空转期间重合度恒为 0 属预期，需人工确认
   `old_chain_data` 诊断无回退（schema 兼容 + 数据确实为空）；
5. 任一天出现 `schema_ok=false`（此时脚本退出码 3，报告 valid=false）或新链路
   某查询返回空而旧链路有结果（反向覆盖缺口），当天不计数，需修复 schema
   （`fix_dev_schema.py`）或回滚 feature flag；
6. 观察期通过后才可进入删除评估（评审确认后删除 `rag/context_loader.py` 及其
   workflow.py fallback 引用，同时清理 dev sqlite / PG 的 embedding 列数据）。

### dev 库 schema 修复（fix_dev_schema.py）

`backend/dev-web.sqlite3` 曾存在 schema 漂移：`world_entries` 缺
`chapter_sequence_number` 列（Alembic 029 引入，但历史 dev 文件从未跑过迁移；
`shadow_compare.py` 等评测工具只读打开、绕过 `init_db()`，缺列一直未被修复）。

修复（备份先行，只补列、不改任何数据行）：

```bash
cd backend
./venv/bin/python retrieval_eval/fix_dev_schema.py
# 流程：只读预检 → cp 备份 dev-web.sqlite3.bak-<YYYYMMDD> → ADD COLUMN + 建索引
#       → 验证（PRAGMA 含 chapter_sequence_number；与 ORM 模型缺列比对为空）
```

脚本等价复刻 `db/session.py` `_ensure_incremental_columns` 中 world_entries 的
ADD COLUMN 逻辑（来源：`backend/db/session.py` L279-286），未直接调用该函数是因为
它还会执行 llm_configs 数据回填 UPDATE、generation_records 去重 DELETE，并在
`uq_llm_configs_active_user` 唯一索引存在时（dev 库正是如此）DROP 重建
llm_configs 表——超出"只补列、不改数据"的范围。参数：`--db`（默认
`backend/dev-web.sqlite3`）、`--no-backup`（跳过备份，不推荐）。

### 已产出结果

| 文件 | 配置 | 结论 |
|---|---|---|
| `results/shadow_compare_20260802.json` | 新链路 lexical vs 旧链路（dev sqlite） | 新链路 10/10 查询命中；旧链路 schema 漂移（world_entries 缺列）静默空返回；重合度 0（错误降级，非覆盖缺口） |
| `results/shadow_compare_pg_old_20260802.json` | 新链路 lexical vs 旧链路（生产 PG schema） | 旧链路 schema 兼容但 d62d4447 在 PG 无结构化数据（world_entries/characters 为空），合法空返回；重合度 0 |

> 门控字段（v2）落地前的产物：20260802 两份结果没有 `eligible` /
> `valid_for_retirement_decision` 字段，且 dev 库那份在 schema 漂移下生成
> （按新门控会以退出码 3 拒绝）——**均不可作为 7 天观察期计数**。dev 库已于
> 2026-08-03 通过 `fix_dev_schema.py` 补列（备份 `dev-web.sqlite3.bak-20260803`），
> 之后所有项目依旧 `eligible=false`（无旧链路语料），观察期尚未起算。

## PG 向量检索实测（verify_pg_vector.py）

`services/vector_retriever.VectorRetriever`（numpy 内存余弦）在 SQLite dev 库评测后，
PG 分支由本脚本实测：**独立测试 project_id**（随机 UUID）在 PG 上导入真实 chunks →
`index_project_vectors`（本地 provider）→ `VectorRetriever.search` 冒烟 → 与内存
SQLite 同语料对照 → 跑完清理。

### 用法

```bash
cd backend
./venv/bin/python retrieval_eval/verify_pg_vector.py                          # 默认 500 条
./venv/bin/python retrieval_eval/verify_pg_vector.py --n-chunks 200 --limit 5 \
    --out retrieval_eval/results/pg_vector_verify.json
```

参数：`--pg-url`（默认 postgres:postgres@localhost:5432/ai_creative）、`--sqlite`
（只读源库）、`--source-project`（默认库中第一个项目）、`--n-chunks`（默认 500）、
`--limit`（top-k）、`--queries`（可选，默认 8 条基于导入窗口真实内容的冒烟查询）、
`--out`。

流程与安全约束：

1. PG 不可达 → 退出码 2（可跳过）；`retrieval_vectors` 表缺失时自动跑
   `alembic upgrade head` 补齐，仍缺失报错；
2. sqlite 只读取窗口（`mode=ro`，按 `chunk_index` 取前 N 条 + 对应 fts 行）；
3. PG 只写测试 project 的行；测试块 `index_generation_id` 打哨兵
   `pg-vector-verify`（自愈清理依据，真实数据绝不会撞该值）；
4. 索引与检索都显式传同一 `EmbeddingProvider(provider="local")` 实例（语料一致，
   维度 = min(EMBEDDING_DIMENSION, 512)，与 SQLite 评测一致）；
5. 一致性对照：同一窗口载入内存 SQLite（StaticPool），同 provider 索引后逐查询
   比对 PG / SQLite 的命中 chunk_id 顺序与分数（应完全一致）；
6. `finally` 清理测试 project 的全部检索表行 + 残留断言 + 源项目向量行数
   前后不变断言；退出码 0=全部通过 / 1=存在失败。

注意：向量检索无相似度阈值（无关查询仍返回 top-k，脚本会记录该行为）；
实体命中冒烟断言检查 top-5 是否含查询实体——本地"伪语义" embedding 只在查询
包含该实体时可靠，因此内置查询都包含断言实体。

### 已产出结果

| 文件 | 配置 | 结论 |
|---|---|---|
| `results/pg_vector_verify_20260802.json` | 500 chunks（第 1~15 章真实内容），top_k=5，8 条查询 | 索引 500/500；冒烟 8/8 命中；PG 与 SQLite 命中顺序+分数 8/8 完全一致（Δ=0）；PG 检索 mean 64ms / p95 82ms；清理零残留 |

## 如何加数据集

1. 在 `datasets/` 下新建 JSON（参考上文格式）；
2. `--dataset` 指向该文件运行即可（ilike 与 lexical 各跑一遍产出两份结果）；
3. 建议 `expected.source_ids` 填 `project_sources.id`（search 返回的 `source_id` 即 source 级 id），
   也可填 chunk 级 id（判定兼容）；
4. 两份结果用 `compare_results.py` 对比。
