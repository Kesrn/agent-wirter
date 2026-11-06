#!/usr/bin/env python3
"""阶段二向量检索评测运行器：三通道消融（ILIKE 基线 / 词法 / 向量 / hybrid-RRF）。

流程：
    1. （默认）为 dev-web.sqlite3 的 retrieval_chunks 生成向量并写入新增的
       retrieval_vectors 表（index_project_vectors，本地可复现 embedding，
       EMBEDDING_PROVIDER=local）——**只写新增的 retrieval_vectors 表**，
       业务表与 retrieval_chunks 等既有表一律不动（建表用
       Base.metadata.create_all(tables=[...]) 显式限定）；
    2. 对 regression_76 + natural_28 两个评测集分别跑 vector（纯向量通道）
       与 hybrid（RRF 三通道融合：精确实体 + 词法 + 向量）两套检索；
       词法与 ILIKE 复用既有结果文件（results/lexical_*.json /
       results/baseline_*.json，不重跑）；
    3. 输出四路对比表：recall@5 / MRR / precision@5 / p95 延迟；
    4. 结果 JSON 存 retrieval_eval/results/vector_*.json、hybrid_*.json，
       对比表存 vector_hybrid_compare_<时间戳>.json。

复用 run_baseline.py 的评测机制（load_dataset / run_eval / aggregate /
group_summaries / 命中判定 / 指标口径），保证与阶段一完全同构可比；
vector/hybrid 的检索执行本身由 run_baseline.run_eval 完成（--retriever
参数扩展，见 run_baseline.py 模块 docstring）。

安全约束（与 index_dev_db.py 同约定）：
    - 只允许写新增表 retrieval_vectors（index_project_vectors 幂等写入）；
      业务表全程只读；
    - 其余行为与 run_baseline.py 完全一致（不修改任何既有代码文件）。

用法：:

    cd backend
    venv/bin/python retrieval_eval/run_vector_eval.py                # 默认全流程
    venv/bin/python retrieval_eval/run_vector_eval.py --skip-index   # 只跑评测（向量已索引）
    venv/bin/python retrieval_eval/run_vector_eval.py --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
DEFAULT_DB = BACKEND_DIR / "dev-web.sqlite3"

DATASETS = {
    "regression": EVAL_DIR / "datasets" / "regression_candidates.json",
    "natural": EVAL_DIR / "datasets" / "natural_queries.json",
}
# 既有结果文件（不重跑，直接读 summary 参与对比）
PRIOR_RESULTS = {
    "regression": {
        "ilike": EVAL_DIR / "results" / "baseline_regression_76.json",
        "lexical": EVAL_DIR / "results" / "lexical_regression_76.json",
    },
    "natural": {
        "ilike": EVAL_DIR / "results" / "baseline_natural_28.json",
        "lexical": EVAL_DIR / "results" / "lexical_sqlite_natural_28.json",
    },
}


def _load_summary(path: Path) -> dict | None:
    """读取既有结果文件的 summary（文件缺失/损坏返回 None）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("summary")
    except (OSError, json.JSONDecodeError):
        return None


def print_comparison_table(
    rows: list[tuple[str, dict, dict | None, dict | None, dict | None, dict | None]],
) -> None:
    """打印四路对比表。

    rows: (retriever_label, summary, by_difficulty, by_category, sample_hit_ids, _)
    简化：直接按 (label, summary_dict|None) 打印指标列。
    """
    header = (
        f"{'retriever':<10}{'recall@5':>10}{'MRR':>8}"
        f"{'prec@5':>9}{'err':>8}{'p95(ms)':>10}"
    )
    print("\n" + "-" * len(header))
    print(header)
    print("-" * len(header))
    for label, summary in rows:
        if summary is None:
            print(f"{label:<10}{'(无既有结果)'}")
            continue
        print(
            f"{label:<10}{summary.get('recall_at_k', 0.0):>10.3f}"
            f"{summary.get('mrr', 0.0):>8.3f}"
            f"{summary.get('precision_at_k', 0.0):>9.3f}"
            f"{summary.get('false_recall_rate', 0.0):>8.3f}"
            f"{summary.get('p95_latency_ms', 0.0):>10.1f}"
        )


async def index_vectors(db_path: str, project_id: str, args) -> dict:
    """建 retrieval_vectors 表（显式限定）并索引项目向量，返回统计。"""
    from db.session import async_session, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        from models.base import Base
        from models.retrieval_index import RetrievalChunk
        from models.retrieval_vector import RetrievalVector

        # 只建这两张投影表（retrieval_chunks 已存在时 create_all 为 no-op）
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[RetrievalChunk.__table__, RetrievalVector.__table__],
            )
        )
        print("[index] retrieval_vectors 表已就绪（retrieval_chunks 已存在则跳过）")

    from services.vector_retriever import index_project_vectors

    progress: list[tuple[int, int, str]] = []
    async with async_session() as db:
        stats = await index_project_vectors(
            db, project_id, progress_cb=lambda d, t, s: progress.append((d, t, s))
        )
    print(
        f"[index] project={project_id}: total={stats['total']} "
        f"indexed={stats['indexed']} skipped={stats['skipped']} removed={stats['removed']}"
    )
    return stats


async def run_retriever_eval(args, project_id: str, samples: list[dict], retriever: str) -> list[dict]:
    """调用 run_baseline.run_eval（复用其检索执行 + 指标采集逻辑）。"""
    from retrieval_eval.run_baseline import run_eval

    ns = Namespace(
        retriever=retriever,
        limit=args.limit,
        source_kinds=None,
        chapter_num=None,
    )
    return await run_eval(ns, project_id, samples)


def build_payload(
    args, db_path: str, project_id: str, project_title: str,
    dataset_label: str, retriever: str, records: list[dict],
    total_sec: float,
) -> dict:
    """构造与 run_baseline 输出同构的结果 JSON payload。"""
    from retrieval_eval.run_baseline import aggregate, group_summaries

    summary = aggregate(records)
    summary["total_runtime_sec"] = round(total_sec, 2)
    grouped = group_summaries(records)
    return {
        "meta": {
            "runner": "run_vector_eval.py (phase2: vector / hybrid)",
            "retriever": retriever,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "db_path": db_path,
            "project_id": project_id,
            "project_title": project_title,
            "dataset": dataset_label,
            "top_k": args.limit,
            "hit_rules": (
                "hit = any top-K result: source_id/chunk_id in expected.source_ids, "
                "or (evidence_type matches AND snippet/title contains any expected.entities); "
                "chapter_no compared when both present"
            ),
            "embedding": {
                "provider": os.environ.get("EMBEDDING_PROVIDER", "local"),
                "note": (
                    "local 伪语义 embedding：字符 unigram/bigram/trigram 词袋 + IDF 加权 "
                    "+ feature hashing，维度 = min(EMBEDDING_DIMENSION, 512)；"
                    "见 services/embedding_provider.py docstring"
                ),
            },
        },
        "summary": summary,
        "by_category": grouped["by_category"],
        "by_difficulty": grouped["by_difficulty"],
        "samples": records,
    }


async def main_async(args) -> int:
    from db.session import async_session

    db_path = str(Path(args.db).resolve())
    project_id = args.project
    project_title = getattr(args, "project_title", "") or ""

    # 1) 索引向量（默认执行；--skip-index 跳过）
    if not args.skip_index:
        t0 = time.perf_counter()
        stats = await index_vectors(db_path, project_id, args)
        print(f"[index] 索引耗时 {time.perf_counter() - t0:.1f}s")
        if stats["total"] == 0:
            print("[error] 项目没有任何 retrieval_chunks，无法生成向量", file=sys.stderr)
            return 1
    else:
        print("[index] --skip-index：假设向量已索引，直接评测")

    # 2) 逐数据集 × 逐检索器（vector / hybrid）跑评测
    from retrieval_eval.run_baseline import (
        aggregate,
        load_dataset,
        print_summary_table,
        resolve_eval_path,
    )

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    all_results: dict[str, dict] = {}

    for ds_name, ds_path in DATASETS.items():
        if args.datasets and ds_name not in args.datasets:
            continue
        samples, dataset_label, _ = load_dataset(str(ds_path), project_id, db_path)
        if args.max_samples:
            samples = samples[: args.max_samples]
        print(f"\n════════ 数据集: {ds_name}（{len(samples)} 条样本, top_k={args.limit}）════════")

        for retriever in ("vector", "hybrid"):
            print(f"\n── retriever={retriever} ──")
            t0 = time.perf_counter()
            records = await run_retriever_eval(args, project_id, samples, retriever)
            total_sec = time.perf_counter() - t0
            summary = aggregate(records)
            summary["total_runtime_sec"] = round(total_sec, 2)
            print(
                f"\n[{retriever}] {ds_name}: recall@5={summary['recall_at_k']:.3f} "
                f"mrr={summary['mrr']:.3f} prec@5={summary['precision_at_k']:.3f} "
                f"p95={summary['p95_latency_ms']:.1f}ms runtime={total_sec:.1f}s"
            )
            payload = build_payload(
                args, db_path, project_id, project_title, dataset_label,
                retriever, records, total_sec,
            )
            out_path = out_dir / f"{retriever}_{ds_name}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"[ok] 结果已写入: {out_path}")
            all_results[(retriever, ds_name)] = payload

    # 3) 四路对比表（ILIKE / lexical 读既有结果文件，vector / hybrid 用本次）
    print("\n\n════════ 四路消融对比（top_k={}) ════════".format(args.limit))
    compare_payload: dict = {"meta": {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                      "top_k": args.limit}, "datasets": {}}
    for ds_name, ds_path in DATASETS.items():
        if args.datasets and ds_name not in args.datasets:
            continue
        prior = PRIOR_RESULTS[ds_name]
        summaries = {
            "ilike": _load_summary(prior["ilike"]),
            "lexical": _load_summary(prior["lexical"]),
            "vector": (all_results[("vector", ds_name)]["summary"]
                       if ("vector", ds_name) in all_results else None),
            "hybrid": (all_results[("hybrid", ds_name)]["summary"]
                       if ("hybrid", ds_name) in all_results else None),
        }
        print(f"\n── {ds_name}（{ds_path.name}）──")
        print_comparison_table([(label, summaries[label]) for label in ("ilike", "lexical", "vector", "hybrid")])
        compare_payload["datasets"][ds_name] = {
            "dataset_path": str(ds_path),
            "summaries": {label: s for label, s in summaries.items() if s is not None},
        }

    compare_path = out_dir / f"vector_hybrid_compare_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    with open(compare_path, "w", encoding="utf-8") as f:
        json.dump(compare_payload, f, ensure_ascii=False, indent=2)
    print(f"\n[ok] 对比结果已写入: {compare_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="阶段二向量检索评测：索引向量 → vector/hybrid 评测 → 四路消融对比"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径（默认 backend/dev-web.sqlite3）")
    parser.add_argument("--project", default=None, help="项目 ID；默认取库中第一个项目")
    parser.add_argument("--datasets", nargs="+", choices=sorted(DATASETS), default=None,
                        help="评测数据集（默认两个都跑：regression natural）")
    parser.add_argument("--limit", type=int, default=5, help="检索 top-k（默认 5）")
    parser.add_argument("--embedding-provider", choices=["openai", "local"], default="local",
                        help="embedding provider（默认 local 伪语义，可复现）")
    parser.add_argument("--skip-index", action="store_true", help="跳过向量索引（假设已索引）")
    parser.add_argument("--out-dir", default=str(EVAL_DIR / "results"), help="结果输出目录（默认 retrieval_eval/results）")
    parser.add_argument("--max-samples", type=int, default=None, help="最多跑前 N 条样本（调试用）")
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit 必须 >= 1")
    db_path = str(Path(args.db).resolve())
    if not Path(db_path).exists():
        raise SystemExit(f"数据库文件不存在: {db_path}")

    # 关键：在导入任何项目模块之前设置环境（settings 在 import 时读环境变量）
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["EMBEDDING_PROVIDER"] = args.embedding_provider
    os.environ.setdefault("DEBUG", "false")
    sys.path.insert(0, str(BACKEND_DIR))

    from retrieval_eval.run_baseline import fetch_first_project

    args.project, project_title = fetch_first_project(db_path, args.project)
    args.project_title = project_title
    print(f"[info] db={db_path}")
    print(f"[info] project={args.project} ({project_title})")
    print(f"[info] embedding_provider={args.embedding_provider}")
    t_start = time.perf_counter()
    code = asyncio.run(main_async(args))
    print(f"\n[ok] 总耗时 {time.perf_counter() - t_start:.1f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
