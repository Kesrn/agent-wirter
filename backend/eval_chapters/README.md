# 章节流水线离线评测

## 数据格式

评测集使用 UTF-8 JSONL，每行一个对象，字段包括 `id`、`split`（只能是 `dev` 或 `holdout`）、`source`、`target_words`、`context`、`task_card` 和可空的 `reference`。`context` 常用字段为 `project_brief`、`characters` 和 `prior_summary`；`task_card` 应包含场景、信息规则和禁写要求。`reference` 是用户提供的人工参考稿；为空时 `ref_edit_ratio` 输出 `null`。仓库中的 `tests/data/eval_chapters_sample.jsonl` 只有三条原创合成样本，不是黄金集，也不包含版权文本。

## 运行

从 `backend/` 目录运行：

```sh
python scripts/eval_chapters.py --dataset tests/data/eval_chapters_sample.jsonl --split dev --variants baseline rules --provider mock --out eval_out/smoke --seed 7 --max-total-tokens 250000 --limit 3
```

`--provider mock` 仅用于离线验证。真实模型用 `--provider config`；估算 token 大于零时必须显式加 `--yes`。可用 `--judge-provider mock|config` 指定评分模型，默认与生成模型相同。`rules_fewshot` 可通过 `--examples-file pairs.jsonl` 读取 `{ "before": "...", "after": "..." }` 行，或通过 `--examples-from-db /path/to/copy.sqlite` 只读导入所选 SQLite 文件里的已应用修改示例。没有合格示例时仍会运行，并在结果中记录“无示例”。

变体为 `baseline`（规则与 few-shot 关闭）、`rules`（规则开启）和 `rules_fewshot`（规则与 few-shot 开启）。当前工作流只接受统一的 `llm_config`，没有按节点分别选模型的配置入口，因此本工具不提供分节点模型变体。

批跑器执行真实 `workflow_v2` 图的写手、规则质量门、审稿、编辑和连续性节点；评测适配器从样本提供上下文与任务卡，并将人工审核路由视为批准后结束。使用 LangGraph `MemorySaver`，不创建数据库会话；适配器替换的外部数据入口仅为上下文加载、任务卡生成和人工审核后的路由。

## 输出和解读

输出目录包含 `results.jsonl`（每个样本×变体一行）、`summary.json`、`compare.md` 和 `run_meta.json`。主表的规则通过率附 Wilson 95% 区间；其它连续指标按同一 `sample_id` 配对比较，使用固定种子的 5000 次 bootstrap，并报告均值差区间及胜/平/负。配对差值正向代表有利；参考稿改动率、成本和耗时按降低为有利。区间包含 0 时标为“不显著”。少于 30 个样本时报告会显示样本量警告；区间可能很宽，只有提升明显大于区间才适合称为改善。

## 预算与留出集纪律

运行前按“（所有样本输入字符数 / 2 + 每样本 33024 输出 token 上限）× 变体数”保守估算；估算超过 `--max-total-tokens` 会在任何模型调用前中止。实际累计用量超过预算时立即停止，并写出已完成结果。未知价格写为 `null`。先用开发集调试和比较；留出集只应在方案冻结后运行。`--split holdout` 必须带 `--confirm-holdout`，每次运行追加记录到输出目录同级的 `holdout_runs.log`；已有记录会醒目警告，但不会阻止再次运行。评测输出默认放在被忽略的 `backend/eval_out/`。
