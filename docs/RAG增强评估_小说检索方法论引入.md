# RAG 增强方案：小说检索方法论引入（v2.1 定稿版）

> 状态：**设计决策已锁定**，方法论评审结束，进入"阶段零评测设计 + LexicalRetriever 接口设计"
> 日期：2026-08-02
> 范围：只读评估 + 设计决策，尚未开始开发
> 修订记录：v1（初评）→ v2（外部评审修订，10 项全部采纳）→ v2.1（拍板决策锁定，本版）

---

## 1. 结论摘要

- 方向确认可行，**不再进行宽泛迭代评审**
- 三块核心补强：**定稿章节正文检索**、**词法检索（LexicalRetriever）**、**评测基线**
- 前置条件已定死（§5 设计决策），开发期不再争论
- 明确不做的：向量检索不进入阶段一；Writer 主链路不改（仅通过 intent 策略映射控制注入）

---

## 2. 项目检索体系现状（代码事实）

**三块职责并存：**

| 职责 | 入口 | 说明 |
|------|------|------|
| 章节创作上下文聚合（主链路） | `services/chapter_context.py` → `build_chapter_context()` | 统一入口（生成/方向建议/问答共用）；full_pipeline 严格模式不注入旧稿/资料正文 |
| 知识库查询检索 | `services/knowledge_source.py` + `knowledge_retrieval.py` + `knowledge_query_planner.py` | 切片 + ILIKE 打分 + 7 类证据 + 否定证据 |
| 旧 RAG fallback | `rag/context_loader.py` | 异常/旧模式兜底；embedding 为哈希伪向量 |

**关键事实：**
- 双数据库：桌面端 SQLite（`desktop_server.py`）+ 服务端 PostgreSQL（`models/base.py` TypeDecorator 层）
- `FloatList` = PG ARRAY / SQLite JSON，**非 pgvector 类型**
- 定稿快照：`Chapter.final_version_id` → `ChapterVersion`（不可变）
- `CharacterAppearance` 已存在（每章每人物出场记录，含 canonical_name/chapter_no/evidence_text 等）
- 别名资产：`services/alias_service.py` + `alias_backfill.py` + `canonical_name`
- `ProjectSourceChunk` 是素材导入业务切片（含 summary/facts/constraints 等业务字段）

---

## 3. 外部实践实证基线（参考）

外部实践（《全职法师》TXT → SQLite FTS5 检索库）已验证：
- 词法全文检索对专名（人名/技能名/地名）命中率高、秒级返回（"玫炎"25 章、"雷穴"2 章）
- 摄取管线：切分/清洗/去重/完整性体检有效（23MB → 3117 章，检出重复与重号）
- 多源交叉验证可识别 AI 编造内容（全文 0 命中判定）
- 锚点验证可确认文本版本可靠性

方法论核心与项目理念同构：权威分级 ↔ canon_level；否定证据 ↔ negative_evidence；检索先于生成 ↔ "检索返回命中，不生成回答"。

---

## 4. 最终设计决策（拍板锁定，不可摇摆）

### 决策 D1：`retrieval_chunks` 是可重建的检索投影

> **`retrieval_chunks` 是由业务事实数据派生、允许删除和重建的检索投影，不是业务事实来源。业务表仍是事实来源。**

- 独立于 `ProjectSourceChunk`（后者继续承载素材导入的业务切片：摘要/事实/约束）
- 统一承载未来所有可检索来源：定稿章节、素材切片、角色档案、能力设定、时间线事件、世界观条目
- 上层返回统一的 `RetrievalHit`

```text
retrieval_chunks
├── id
├── project_id
├── source_kind          -- chapter_version | source_chunk | character_profile | ability_profile | timeline_event | world_entry | ...
├── source_id
├── source_version_id
├── index_generation_id  -- 所属索引版本
├── chunk_index
├── content              -- 原始文本片段
├── search_text          -- Python 分词后的检索文本
├── content_hash
├── metadata             -- JSON：章节号/标题/来源类型等检索展示字段
└── created_at
```

配套**索引版本状态表 + 活动版本指针**（拍板：采用指针方案，不逐行 active、不用"事务内状态切换"的简化版）：

```text
retrieval_index_generation          -- 一次索引构建的记录
├── project_id
├── generation_id
├── source_kind
├── source_id
├── status               -- building | active | superseded | failed
├── build_started_at / finished_at
└── error

retrieval_index_source_state        -- 每个来源当前活动的版本指针
├── project_id
├── source_kind
├── source_id
├── active_generation_id            -- 指向 retrieval_index_generation.generation_id
└── updated_at
```

- 检索时通过 `retrieval_index_source_state.active_generation_id` 找到当前活动 generation，再取其 chunks
- 新版本构建**全部成功后**才 UPDATE 指针（原子切换）；失败则指针不动，旧索引继续使用、半成品 generation 置 failed 后废弃
- 构建期间不出现两个活动版本；指针更新与 generation 状态更新在同一事务

### 决策 D2：双平台索引策略分平台定死

**桌面 SQLite：**
- 单章定稿：**同一事务内同步增量索引当前章节**（不重建全项目）
- 角色/能力少量修改：同步增量更新对应检索记录
- 3000 章全量重建：**后台任务执行 + 展示进度**，不阻塞定稿接口与界面（即使 15MB 构建仅十几秒，也不放入按钮请求：避免界面转圈/误判卡死/SQLite 写锁/退出后状态不完整）
- 全量构建失败：旧索引继续可用，未完成的新索引废弃

**服务端 PostgreSQL：**
```text
章节定稿事务
  → 保存正式版本（事实来源）
  → 写入索引任务 / outbox
  → 提交业务事务
  → Worker 幂等构建新索引版本
  → 构建成功后原子切换 active_generation
```
- 索引任务失败**不回滚已定稿章节**，保留旧索引、任务进入 failed/retryable

### 决策 D3：项目级隔离分词器 + 已确认别名优先

> **复用已确认别名，并使用按项目隔离、带版本号的动态专名词典；禁止使用跨项目全局词典。**

- 每个项目独立分词器实例：`(project_id, vocabulary_version) → jieba.Tokenizer`
- 词典来源：角色正式名、**已确认**的角色别名、能力名称及确认别名、稳定的组织/地点/世界观专名
- **别名候选必须经用户确认后才能入词典**（复用现有 alias_service/alias_backfill 流程）
- 禁止服务端全局 `jieba.add_word`（避免项目间串词、删除后残留、测试顺序敏感、多进程状态不一致）

**别名精确命中发生在分词之前（实体优先通道）：**
```text
用户查询
  → 识别正式名或已确认别名
  → 归一化为 canonical_name
  → 精确实体检索（命中优先返回）
  → 再走词法、向量补充召回
```
即使分词偶发不佳，核心人物查询不丢失。

### 决策 D4：三通道检索

| 通道 | 实现 | 阶段 |
|------|------|------|
| 精确实体命中 | 别名归一 → canonical_name → 实体检索（优先） | 阶段一 |
| 词法检索 | Python 项目级分词 → search_text → SQLite FTS5 / PG tsvector+pg_trgm+GIN | 阶段一 |
| 向量语义检索 | 真实 embedding + RRF 融合 | 阶段二 |

- 统一 `LexicalRetriever` 接口：统一输入过滤条件、统一输出 `RetrievalHit`，**不泄露数据库方言**
- 双后端不追求原始分数一致（FTS5 bm25 / PG ts_rank / trigram 数值不可比），对齐**行为**：过滤条件、精确命中优先级、返回字段、Top-K 结果重合度、最低质量门槛；分数各自后端内归一化，业务层用确定性加权或 RRF
- 概念统一称"词法检索"，`ts_rank` 不是 BM25

### 决策 D5：intent 集中映射为检索策略

复用 `build_chapter_context` 的 `intent`，**集中维护一张策略映射**（禁止各处散落 `if intent == ...`）：

```text
intent → {
  允许检索哪些来源,
  是否检索旧章节正文,
  每类最大条数,
  总 token 预算,
  是否精确实体优先,
  无结果时如何处理
}
```

| 场景 | 是否注入章节正文 |
|------|----------------|
| 章节问答/剧情回溯/时间线/一致性检查 | 是 |
| 润色/续写（continue/enhance） | 是（当前正文为主输入） |
| 从头生成完整章节（full_pipeline） | **默认不注入当前旧稿全文**（可检索定稿上文） |
| 下一章衔接 | 优先上章定稿结尾锚点 + 摘要 |

### 决策 D6：否定证据三态

`explicit_negative`（原文明说没有）/ `not_found`（当前检索未命中）/ `unknown`（语料可能不完整）——"0 命中"不得直接等同于"设定中不存在"（别名、错别字、未索引草稿、切片失败均可能造成误判）。

---

## 5. 落地路径与排期

### 阶段零：评测基线（先于一切检索改造）

1. **结构化回归集**：从 `CharacterAppearance` / `AbilityProfile` / `EventTimeline` 自动生成候选查询 + 期望证据，人工校正
2. **自然查询集**：人工编写真实模糊表达查询 + 标注期望证据
3. 标注规范：标注**期望证据来源与实体**（chapter 号/版本/记录 id），**不以当前系统生成答案当标准答案**（避免固化历史抽取错误）
4. 困难样本必含：别名、错别字、同音不同字、同名人物、查无结果、新旧设定冲突、跨章节事件、禁止注入场景
5. 规模：每查询类型 ≥ 10~15 条（POC 够用），上线前扩充至 200+ 条
6. 基线：现有 ILIKE 检索，统计 **Recall@5、MRR、证据准确率、误召回率、P95 延迟**

> **✅ 阶段零已完成（2026-08-02）**，产物位于 `backend/retrieval_eval/`：
>
> - `dataset_schema.py`：评测样本契约（EvalSample/EvalExpected + 校验）
> - `generate_regression.py` + `datasets/regression_candidates.json`：结构化回归候选 **76 条**（normal 70 / conflict 2 / same_name 4）
> - `datasets/natural_queries.json`：自然查询+困难样本 **28 条**，9 类难度全覆盖（normal/alias/typo/homophone/same_name/no_result/conflict/cross_chapter/no_inject）
> - `run_baseline.py` + `results/baseline_regression_76.json`：ILIKE 基线运行器与结果
>
> **基线指标（dev-web.sqlite3，76 条，4.45s）**：
>
> | 指标 | 值 |
> |------|-----|
> | Recall@5 | 0.132 |
> | MRR | 0.132 |
> | Precision@5 | 0.132 |
> | 误召回率 | 0.868 |
> | P95 延迟 | 84.4ms |
>
> **基线诊断结论（对阶段一直接有用）**：
> 1. `timeline_event` 类 54 条**全 miss**：现有 `_extract_search_keywords` 对"第1章发生了什么？"类问句拆不出关键词（整句 ILIKE 必 0 命中）→ 印证词法检索+查询规划的必要性
> 2. ILIKE 对词序/助词敏感（"莫凡是在哪里…"可命中、"莫凡在哪里…"拆词失败）
> 3. 数据集发现真实数据质量线索：张小侯风系时间三方冲突、同名角色跨项目设定不同、语料自带错别字"张小候"——已作为 conflict/typo/same_name 样本固化为评测资产

### 阶段一：词法检索（双后端）

1. `retrieval_chunks` + `retrieval_index_generation` 表（D1）
2. 项目级分词器 + 词典管理（D3）
3. `LexicalRetriever` 统一接口：SQLite FTS5 实现 + PG tsvector/trgm 实现（D4）
4. 定稿索引闭环：SQLite 事务内同步增量 / PG outbox+Worker 幂等构建+原子切换（D2）
5. intent 策略映射接入 `build_chapter_context`（D5）
6. 与 ILIKE 基线对比评测，达标后词法检索正式启用

> **✅ 阶段一核心已完成（2026-08-02）**，新增文件：
>
> - `backend/models/retrieval_index.py`：RetrievalChunk / RetrievalIndexGeneration / RetrievalIndexSourceState（D1 指针方案）
> - `backend/services/retrieval_tokenizer.py`：项目级隔离分词器（D3，多项目隔离、版本化缓存、实体归一）
> - `backend/services/retrieval_indexer.py`：FTS5 建表 + generation 生命周期 + 六类来源索引 + 幂等/双删 + 全量重建（进度回调）
> - `backend/services/lexical_retriever.py`：LexicalRetriever（实体优先通道 + 词法通道 + 质量门槛；SQLite bm25 / PG tsvector 双分支）
> - `backend/services/retrieval_policy.py`：intent 集中策略映射（D5）
> - `backend/services/chapter_save.py`：finalize 定稿接入索引骨架（D2，延迟导入+异常降级，失败不回滚章节）
> - `backend/retrieval_eval/index_dev_db.py`：开发库索引脚本（只写 4 张检索表，幂等）
> - 测试：38 个用例全部通过（models/tokenizer/retriever/policy + finalize 回归）
>
> **评测对比（regression 76 条，top_k=5，dev-web.sqlite3）**：
>
> | 指标 | ILIKE 基线 | Lexical | Δ |
> |------|-----------|---------|-----|
> | Recall@5 | 0.132 | **0.816** | +0.684 |
> | MRR | 0.132 | **0.816** | +0.684 |
> | Precision@5 | 0.132 | **0.816** | +0.684 |
> | 误召回率 | 0.868 | **0.184** | −0.684 |
> | P95 延迟 | 84.4ms | **35.0ms** | −49.4ms |
>
> - 提升来源：jieba 项目词典（角色/法系专名成词）+ FTS5 OR 匹配解决"整句子串 LIKE 召不回"（timeline_event 0.000→0.926），bm25 使命中集中在 rank=1，索引使延迟减半
> - 剩余 14 条 miss 为**数据集结构问题**（expected.source_ids 指向 E2E 项目，评测固定取第一个项目）：可评估 62 条上 lexical **100%** vs ilike 16.1%
> - 已知短板：① 实体优先通道在评测库为空（alias 表 0 行，收益由单测覆盖）；② 中文 FTS 依赖索引/查询同一分词器，词典外专名会被切碎；③ PG tsvector 分支未在真实 PG 验证（待 PG worker 接入时验证）

> **✅ 阶段一收尾已完成（2026-08-02）**：
>
> **PG 双后端验证**（`retrieval_eval/setup_pg.py` / `verify_pg.py` / `run_eval_pg.py`）：
> - 真实 PG 16.9 建表+索引+检索+评测全链路跑通；修复 2 个 PG 特有 bug（JOIN uuid=character varying 类型不匹配；表达式索引性能问题 → `search_tsv` 生成列 + GIN，单查询 15x 提速）
> - **双后端语义等价验证**：SQLite FTS5 vs PG tsvector 在 regression_76 / natural_28 上 recall/MRR/precision **完全一致**（0.8158 / 0.8571），延迟同量级（p95 83ms vs 35ms）
>
> **知识库问答接入**（feature flag `RETRIEVAL_MODE=ilike|lexical|hybrid`）：
> - `config/settings.py` 新增配置；`knowledge_source.py` 增加检索分发（`_dispatch_lexical_or_ilike`），RetrievalHit → ILIKE 同构结果映射，下游证据分类/重排零改动
> - 降级链：lexical 索引缺失/异常 → ILIKE；hybrid = lexical 优先 + ILIKE 合并去重；ilike 模式行为完全不变
> - 接入按 D5 policy（`get_retrieval_policy(intent)`）取参数；已知缺口：Query Planner 的 V1 intent（ability/relationship）暂回退 generate 策略（entity_first 未生效），策略表后续映射即可启用
>
> **集成修复**：`ensure_fts_table` 幂等化（存在性检查+方言兼容 AsyncSession/AsyncConnection）+ 写入路径自动建表，解决定稿索引骨架在未建 FTS 表的库（如 test_smoke 组合场景）触发 `no such table: retrieval_chunks_fts` 的问题
>
> 测试：检索相关 5 个测试文件 + test_smoke 组合 **157 passed**

> **✅ 一致性闭环已完成（2026-08-03）——服务端索引一致性 P0/P1 修复**：
>
> **P0-1 索引服务不再提前提交业务事务**（`retrieval_indexer.py`）：
> - 移除服务内部全部 `db.commit()`/`db.rollback()`（finish_generation_active / fail_generation / PG 分支 / 异常兜底），事务边界完全由调用方（路由层/Worker）控制
> - SQLite 索引写入用 `begin_nested()` savepoint 隔离：写入失败只回滚索引侧，外层定稿/HITL 事务不受影响、不提前提交
> - 验证：`test_index_failure_does_not_commit_outer_transaction` 等 5 个新用例（含真实 `finalize_chapter_content` 遇索引失败不抛异常、定稿生效、failed 审计落库）
>
> **P0-2 章节索引用稳定 `chapter_id` 作为 source_id**（版本 ID 放 source_version_id）：
> - 同一章节再次定稿 → 同 source_id 下新 generation 原子替换旧块（旧块双删 + 指针切换），检索只召回最新版
> - 验证：`test_refinalize_same_chapter_replaces_old_version`（两次定稿后旧版不可召回）
>
> **Alembic 正式迁移**（`alembic/versions/031_retrieval_index_projection.py`）：
> - 5 张表（4 张检索投影表 + outbox jobs），PG 专属：retrieval_chunks_fts 同构表、search_tsv 生成列 + GIN 索引、pg_trgm 可选（权限不足时跳过不阻塞）
> - 本地 PG `alembic upgrade head` 实测通过（030→031），含 downgrade 往返验证
>
> **PG outbox + 幂等 Worker**（`models/retrieval_index_job.py` + `services/retrieval_index_worker.py`）：
> - enqueue 去重（同源 pending/processing 唯一，部分唯一索引兜底）；`index_chapter_version` 按方言分发：SQLite 同步构建 / PG 写 outbox
> - Worker：超时回收（claimed_at 300s → pending，重启可重试）、每 job 独立事务、失败 attempts+1、done 不重复处理
> - 验证：9 个 worker 测试 + PG 端到端用例（enqueue→poll→检索命中，跑完清理）
>
> **P1 恢复版本语义**（`api/routes.py` restore 端点）：
> - 恢复历史版本后章节状态**改回 draft**；`final_version_id` 保留原定稿快照（可信索引继续指向原定稿）；恢复不触发索引，再次定稿才更新快照+索引
> - 验证：2 个新用例（恢复后 draft+快照保留；再次定稿才触发索引指向新版本）
>
> **回归**：检索核心 81 + restore/finalizr 15 + smoke 106 = **202 passed**（分块运行；组合运行时曾遇 checkpoints.sqlite 偶发挂起，与改动无关，分块确认全绿）

> **✅ PG 索引 Worker 完整闭环已完成（2026-08-03）**：
>
> - **入队/构建拆分**（无布尔参数）：`request_chapter_index`/`request_source_index`/`request_structured_index`（finalize/调用方入队，SQLite 下直接同步构建）+ `build_chapter_index`/`build_source_index`/`build_structured_index`（Worker 纯构建，返回 generation_id）；chapter_save 接入点已切换
> - **Worker 统一流程**：原子领取（PG `FOR UPDATE SKIP LOCKED` / SQLite 条件 UPDATE rowcount）→ 纯构建 → `finish_generation_active` 切指针 → done，每 job 一次 commit；构建异常先 rollback 再新事务记 failed+attempts
> - **同源合并策略**：pending 合并为最新 source_version_id；processing 允许追加 pending；部分唯一索引约束 `WHERE status='pending'`（processing 不锁死）；**激活前校验** `source_version_id == Chapter.final_version_id`，旧版本任务不构建不切指针（StaleVersionError）
> - **生产运行入口**：`run_worker_loop`（SIGINT/SIGTERM 优雅退出）+ `scripts/run_index_worker.py` + docker-compose `index-worker` 服务；SQLite 方言拒绝启动（桌面端不走 Worker）
> - **PG 验收 7 项全部真实执行通过**（本地 PG，不可 skip）：① 定稿→outbox→Worker→active→检索命中 ② 连续定稿两次只命中新版 ③ v1 processing 时 v2 入队合并 ④ 双 Worker 不重复消费 ⑤ 崩溃超时回收 ⑥ 构建失败无可见半成品 ⑦ stale 版本不构建不切指针；另有并发竞态合并、source_chunk/structured 入队端到端用例
> - 测试：worker 套件 24 个用例全过（含 PG 9 项）

> **✅ P1 部分完成（2026-08-03）**：
>
> - **打包 FTS5 自动化验收**：`backend/scripts/verify_fts5.py`（零依赖，建表/中文插入/MATCH/bm25 7 项断言，退出码语义）已接入 `.github/workflows/build-windows-desktop.yml` 打包后冒烟步骤；本地验证通过（SQLite 3.40.1 ENABLE_FTS5）
> - **评测集扩充至 224 条**（≥200 达标）：regression 154（含 78 条困难样本：alias 12/typo 13/homophone 14/same_name 9/conflict 8/cross_chapter 13/no_result 9）+ natural 70（42 条新增）；新 ILIKE 基线 recall@k=0.325（困难样本对基线更严，符合预期）
> - **旧 RAG 影子对比工具**：`retrieval_eval/shadow_compare.py`（重合度/覆盖/耗时），README 给出观察期门槛（连续 ≥7 天 mean_overlap ≥0.9 且 new_covers_old ≥0.95 才评估删除）；**发现 dev 库 schema 漂移**（world_entries 缺 chapter_sequence_number 列导致旧链路静默空返回）——待修复项
> - **PG 端向量检索实测**：`verify_pg_vector.py` 跑通，PG vs SQLite **逐位一致**（8/8 查询 chunk_id 顺序+分数 Δ=0），耗时同量级（p95 82ms vs 101ms）；测试数据零残留

> **✅ 影子评测/发布验收闭环（2026-08-03）**：
>
> - **dev 库 schema 修复**：`retrieval_eval/fix_dev_schema.py` 补 `world_entries.chapter_sequence_number`（复用 session.py `_ensure_incremental_columns` 等价逻辑，不动迁移代码；备份 `dev-web.sqlite3.bak-20260803`，数据零改动）
> - **影子报告有效性门控**（shadow_compare.py）：schema 不兼容 → `valid_for_retirement_decision=false` + 退出码 3（不产出指标）；旧链路语料门槛（world_entries/characters/embedding 为 0）→ `eligible=false` + 退出码 0（合法判定，无指标）；报告新增 4 个判定字段；**7 天观察期只统计 eligible=true 且 valid=true 的报告**
> - **现状**：dev 库 3 个项目旧链路语料全为 0（缺"全职法师之绘魔者"类数据），**观察期尚未开始**，需提供含旧 RAG 数据的项目/数据库副本后重新起算
> - **实际产物 FTS5 自检**：`desktop_server.py --verify-fts5`（10 项断言，本地 exit 0）；Windows 工作流改为直接运行 `ai-creative-backend --verify-fts5`；新建 `build-macos-desktop.yml`（镜像 Windows 结构，含 FTS5 自检步骤）；**Mac App 尚未重建**（工作流就绪，需触发 CI/本地构建）

### 阶段二：向量语义（在词法稳定后）

1. 真实 embedding 接入（API embedding 优先；本地 bge-m3 不进桌面安装包）
2. 向量存储独立于业务表（检索投影层），**不强行把 FloatList 迁为 pgvector**
3. 向量/词法/精确三路 RRF 消融实验
4. SQLite 桌面端可仅启用词法检索（向量为可选）

> **✅ 阶段二已完成（2026-08-03）**，新增文件：
>
> - `backend/services/embedding_provider.py`：可插拔 embedding（openai API 有 key 即用 / local 本地可复现 n-gram+IDF 伪语义方案，供无 key 评测）
> - `backend/models/retrieval_vector.py`：RetrievalVector 独立向量表（FloatList，不迁 pgvector；UNIQUE(project_id, chunk_id)）
> - `backend/services/vector_retriever.py`：向量索引（幂等）+ VectorRetriever（numpy 余弦，无向量数据返回 []）
> - `backend/services/retrieval_reranker.py`：`rrf_merge`（k=60/权重/去重）+ `search_hybrid` 三路融合（实体/词法/向量，向量缺数据自动退化为两路）
> - `backend/retrieval_eval/run_vector_eval.py`：向量+hybrid 评测；`run_baseline.py` 支持 `--retriever vector|hybrid`
> - 实体优先通道落地：V1/V2/structured_qa 共 **13 个问答 intent 全部映射 qa 策略**（entity_first=True），含枚举漂移防护测试
>
> **消融评测（四路对比）**：
>
> | retriever | regression_76 recall@5 | natural_28 recall@5 | p95 延迟 |
> |-----------|----------------------|--------------------|---------|
> | ilike 基线 | 0.132 | 0.607 | 84ms |
> | lexical | 0.816 | 0.857 | 35ms |
> | vector | 0.816 | 0.857 | 1243ms |
> | hybrid (RRF) | 0.816 | 0.857 | 1383ms |
>
> **结论**：
> 1. 向量通道达标（与词法持平、远超基线），命中均在 rank 1（MRR=recall）
> 2. hybrid 当前无 recall 增益——本地"伪语义"embedding 与词法信号高度相关，RRF 强化同一批证据；**真实语义增益依赖真模型**（openai/BGE 接入点已留，换 provider 无需改检索层）
> 3. 向量延迟是短板（p95 ~1.2s，瓶颈为 SQLite JSON 向量反序列化，非矩阵计算）——实证支持"SQLite 桌面端仅启用词法"决策；PG 端未来可换 pgvector/ANN
> 4. 测试：检索相关 7 个测试文件 + test_smoke 组合 **173 passed**

---

## 6. 风险与开放项（开发前确认）

1. **旧 RAG 退役**：feature flag + 影子对比；退出条件 = 新链路覆盖全部调用场景 + 评测达标 + 影子运行无严重差异 + 连续版本稳定
2. **双后端一致性验证**：Top-K 重合度作为验收指标（§4 D4）
3. **测试保护**：`tests/` 的 test_knowledge / test_chapter_context / test_extraction 等保持兼容
4. **桌面打包 FTS5 验收（阶段一前置，已确认检查结论）**：
   - 开发环境 SQLite 3.40.1，`ENABLE_FTS5=True`，FTS5 建表与中文检索实测通过；桌面包内嵌 `_sqlite3` 亦含 ENABLE_FTS5
   - **自动化构建验收（2026-08-03 已落地）**：`desktop_server.py --verify-fts5` 自检（10 项断言：编译选项/同构建表/中文插入/MATCH/bm25/AND 语义，退出码 0/1/2），Windows 与新建的 macOS 工作流均在 `backend:build` 后**直接运行实际打包产物**执行自检（非单独验证程序）
   - FTS5 是虚拟表，**不能依赖 ORM `create_all`**，必须显式执行 `CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_chunks_fts USING fts5(...)`
5. **建表与迁移边界**：
   - 新 ORM 模型必须加入 `db/session.py` 的显式导入列表（`init_db` 的 `from models import ...` 行）
   - `create_all` 能创建新表，但**不会给旧表补列/补约束/改索引**；涉及旧表变更需 Alembic + 桌面端 `_ensure_incremental_columns` 兼容迁移
6. **定稿索引更新时机**：finalize 增量索引、版本回滚清理、编辑后再定稿的幂等处理

---

## 7. 涉及文件清单

```
backend/models/base.py                    # 双数据库 TypeDecorator
backend/models/chapter.py                 # final_version_id
backend/models/chapter_version.py         # 定稿快照
backend/models/structured_knowledge.py    # CharacterAppearance(L51)/AbilityProfile/EventTimeline/WorldRule
backend/models/project_source.py / project_source_chunk.py
backend/services/chapter_context.py       # build_chapter_context 主链路 + intent
backend/services/knowledge_source.py / knowledge_retrieval.py / knowledge_query_planner.py / knowledge_file_parser.py
backend/services/alias_service.py / alias_backfill.py   # 别名资产（D3 复用）
backend/rag/context_loader.py             # 旧 RAG fallback
backend/agents/workflow.py                # L489-506 fallback
backend/desktop_server.py                 # 桌面端 SQLite 入口
docker-compose.yml
```

---

## 8. 修订记录

| 版本 | 变化 |
|------|------|
| v1 | 初版评估（方法论映射 + 三阶段路径） |
| v2 | 采纳外部评审 10 项：双数据库、ts_rank≠BM25、三块职责、full_pipeline 注入控制、复用 CharacterAppearance、定稿版本索引、否定证据三态、评测前置、落地顺序重排、FloatList 非 pgvector |
| v2.1（本版） | 拍板锁定：D1 检索投影模型（非业务双写）、D2 双平台索引策略（SQLite 同步增量/后台全量、PG outbox+原子切换）、D3 项目级分词器+已确认别名优先、D4 三通道检索、D5 intent 策略映射、D6 否定证据三态；评测集拆分结构化回归集+自然查询集+困难样本；排期定稿 |

---

## 9. 下一步（开发入口）

1. 阶段零：评测集构建 + ILIKE 基线脚本
2. `LexicalRetriever` 接口设计（输入过滤条件 / 输出 RetrievalHit / 双后端实现签名）
3. `retrieval_chunks` + `retrieval_index_generation` 建表与索引写入服务

> 向量检索与 Writer 链路改动不在本阶段范围。
