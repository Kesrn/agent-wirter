# 评测数据集（阶段零：评测集构建）

本目录存放检索评测数据集，供 `retrieval_eval/run_baseline.py`（ILIKE 基线运行器）消费，
也是后续向量检索 / 重排 / Query Planner 增强阶段的统一评测输入。

## 目录与文件

| 文件 | 条数 | 说明 |
|---|---|---|
| `regression_candidates.json` | 154 | 结构化回归候选查询：76 条由 `generate_regression.py` 从库中四张结构化表自动生成；78 条困难样本（alias/typo/homophone/same_name/conflict/cross_chapter/no_result，每类 ≥6 条）同样由该生成器构造并只读核对 |
| `natural_queries.json` | 70 | 人工编写的自然查询集 + 困难样本（alias/typo/homophone/same_name/no_result/conflict/cross_chapter/no_inject）：28 条人工精修，42 条由 `generate_natural_more.py` 基于真实数据构造 |
| `../dataset_schema.py` | — | 样本格式定义（dataclass）+ 校验器 + 读写工具，本目录两份 JSON 的契约源头 |

数据来源：`backend/dev-web.sqlite3`（89MB 开发库，只读访问，绝不修改）。
库内实际内容概况（2026-08-02 核对）：

- 项目 3 个：`d62d4447`（"111"，全职法师全文，7276 切片）、`49aee26a`（E2E测试）、`de04afc8`（小说抽取测试）
- 结构化表：`ability_profile` 5 行、`event_timeline` 27 行（第1-25章，含两个项目）、`character_profile` 4 行、`world_rule` 2 行
- `character_alias_clusters` 表存在但**为空（0 行）**；别名类查询无法从该表构造，
  本数据集中的 alias 样本改为**从正文提取**（见 natural_queries.json 的 verification_notes）

## 样本格式

样本契约定义在 `../dataset_schema.py`（`EvalSample` / `EvalExpected` dataclass），
顶层为 `{"meta": {...}, "samples": [...]}`，meta 描述生成来源、表行数、注意事项。

```json
{
  "query_id": "reg-001",
  "category": "character_first_appearance|ability_query|timeline_event|world_rule|source_chunk|no_result|meta",
  "query": "用户可能输入的问句",
  "expected": {
    "evidence_type": "character_profile|ability_profile|event_timeline|world_rule|source_chunk",
    "chapter_no": 12,
    "source_ids": ["uuid或id"],
    "entities": ["实体名"]
  },
  "difficulty": "normal|alias|typo|homophone|same_name|no_result|conflict|cross_chapter|no_inject",
  "notes": "标注说明"
}
```

与 `run_baseline.py` 的约定保持一致：

- **必填**：`query_id`、`query`、`expected`（`source_ids` 或 `entities` 至少一个），缺字段的样本会被运行器跳过。
- `expected.source_ids` 填 **source 级 id（`project_sources.id`，检索结果里的 `source_id`）**，
  也可填 **chunk 级 id（`project_source_chunks.id`，即结果的 `chunk_id`）**，命中判定两者兼容。
- 扩展字段（运行器忽略，仅溯源）：`expected.project_id`（样本所属项目）、
  `expected.record_ids`（结构化表行 id，如 `ability_profile.id`，用于回查原始记录）。

### difficulty 语义

| difficulty | 含义 | 示例 |
|---|---|---|
| `normal` | 普通正常查询 | "莫凡的火系魔法是什么等级？" |
| `alias` | 别名/缩写 | "猴子是什么系的法师？"（猴子=张小侯） |
| `typo` | 错别字 | "张小候的魔法是什么系的？"（候→侯；原文自身就误写为"张小候"） |
| `homophone` | 同音字 | "莫烦的火系是什么时候觉醒的？"（烦→凡） |
| `same_name` | 同名不同人 | "莫凡觉醒了什么系？"（主项目=火+雷，E2E=仅火，需项目上下文消歧） |
| `no_result` | 查无结果 | "林小雨是什么系的法师？"（全库 0 命中） |
| `conflict` | 多来源设定冲突 | "张小侯是什么时候掌握风系的？"（结构化表 16/1，正文第19章，三处不一致） |
| `cross_chapter` | 跨章节聚合 | "从莫凡觉醒魔法到唐月成为新老师隔了多少章？" |
| `no_inject` | 元查询：哪些内容不该注入正文 | "我写到第15章，张小侯的风系设定先不要写进正文"（风系 first_seen=16） |

注意 `no_result` / `no_inject` 两类样本的判定语义是**反向**的：期望行为是检索
**不**召回（或注入层拦截）相关证据。若基线运行器判定 hit，代表系统错误召回，
应按"违规"解读，不要当作正常命中。

## 生成命令

```bash
cd backend

# 1. 重新生成结构化回归候选 + 困难样本（只读库，写 datasets/regression_candidates.json）
python3 retrieval_eval/generate_regression.py
# 可选：指定库与输出路径
# python3 retrieval_eval/generate_regression.py /path/to/dev-web.sqlite3 /path/to/out.json

# 1b. 批量扩充自然查询集（在既有 28 条基础上追加 42 条并更新 meta，写 datasets/natural_queries.json）
python3 retrieval_eval/generate_natural_more.py

# 2. 校验任一数据集（严格校验 category/difficulty/evidence_type 枚举与必填字段）
python3 retrieval_eval/dataset_schema.py retrieval_eval/datasets/regression_candidates.json
python3 retrieval_eval/dataset_schema.py retrieval_eval/datasets/natural_queries.json

# 3. 跑 ILIKE 基线（示例，扩充后 154 条）
./venv/bin/python retrieval_eval/run_baseline.py \
    --dataset retrieval_eval/datasets/regression_candidates.json \
    --project d62d4447-b8d7-4fc3-9193-a6d363d8f541 \
    --out results/baseline_regression_v2.json
```

`natural_queries.json` 为半人工维护文件（28 条人工精修 + 42 条生成器构造），
改动后请用第 2 步校验。

## 数据质量与已知问题（重要）

1. **`character_alias_clusters` 为空表**：别名类困难样本无法从该表生成。
   已在 natural_queries.json 与 regression_candidates.json 中改为从正文提取真实别名
   （猴子/泥猴子=张小侯、大魔头/全校公敌/冲榜狂魔/凡哥=莫凡、绿茶男=穆白、
   黑白无常妇=撒朗/伊之纱、冷面判官=杰西、移动电灯泡=光系），并附了出处章节。
   若后续该表有数据，可补充基于别名的回归样本。
2. **`event_timeline` 内容是占位**：`event_title` / `event_desc` 为"第N章事件 / 本章主要事件"，
   结构化信息有限；正文细节在 `project_source_chunks`（全职法师 7276 切片，含第1~880+章）。
   因此事件类查询的 `expected.source_ids` 同时挂了 source 级 id，正文切片可命中。
3. **跨项目同名角色**：`d62d4447`（全职法师）与 `49aee26a`（E2E）、`de04afc8`（测试小说）
   存在同名角色 莫凡 / 张小侯 / 赵满延，且能力设定不同。相关样本已标记 `same_name` / `conflict`，
   并带 `expected.project_id`。基线运行器按单项目检索，跨项目样本主要用于人工/后续
   多项目评测，勿只用一个项目去跑全量数据集后直接下结论。
4. **`project_knowledge_facts` 主语噪声大**（"一些""一位""一名"等），本次未作为
   回归样本的证据来源；仅 natural_queries.json 少量引用其中明确指向人名的事实
   （如"张小侯和赵满延都是水系法师"）。
5. **no_result 样本已核验**：`林小雨`、`血型`、`莫凡的妈妈`、`王小明`、`李小雨`、
   `林黛玉`、`乾坤袋`、`金丹期`、`炼丹术`、`查克拉`、`张伟`、`苏小小`、`生辰八字`、
   `陈明` 及短语 `莫凡的爷爷`/`莫凡的奶奶` 均全库 0 命中（生成时再次核对，
   有命中的样本会记入 meta.hard_sample_warnings）；注意 `秦羽`（75 处）、
   `莫凡生日`（6 处）、`传送阵`（17 处）等在语料中存在，不能用作 no_result。
6. **张小侯风系时间冲突**：主项目 `ability_profile.first_seen_chapter=16`、
   E2E 项目 =1、正文第19章才首次展示风轨（chunk `03ba3287`）。这是数据本身的一致性
   问题，恰被用作 `conflict` 样本，也提醒评测时不要把结构化表当作唯一事实源。
7. **错别字来源**：`typo` 类前 5 条（张小候/张候/慕白/雷息/风鬼）的错别字来自
   语料自身（SELECT 核对有命中），其余为近形字构造；`homophone` 类为同音字构造，
   其中「张小猴」与真实别名「猴子」重合，用于检验别名误导。

## 如何扩展数据集

1. **结构化回归 + 困难样本**：改 `../generate_regression.py` 的模板函数（`query_for_*`）、
   `DIFFICULTY_OVERRIDES` 映射（按 `(表名, 行id)` 覆盖 difficulty/notes）或
   `HARD_SAMPLE_DEFS` 困难样本定义（每类 ≥6 条，引用词生成时自动只读核对），
   重新运行生成命令。
2. **自然查询**：直接向 `natural_queries.json` 的 `samples` 数组追加条目，要求：
   - 人名/能力名/事件名必须来自真实库内容（先 SELECT 核对）；
   - `expected.source_ids` 用真实 source/chunk id，`record_ids` 用结构化表行 id；
   - 新样本通过 `dataset_schema.py` 校验后提交。
   批量扩充用 `../generate_natural_more.py`（在既有 28 条基础上追加并更新 meta）。
3. **新增证据类型**：若未来加入新的结构化表（如 `project_knowledge_facts` 做事实查询），
   在 `../dataset_schema.py` 的 `EVIDENCE_TYPES` 中登记，并在 `run_baseline.py` 的
   命中规则中补充对应分支（当前基线只按 source/chunk id 判定）。
