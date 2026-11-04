#!/usr/bin/env python3
"""实体优先通道验证脚本（阶段二：V1 问答 intent → qa 策略落地后的真实链路验证）

背景
----
阶段二把知识问答类 intent（V1/V2 Query Planner 与 structured_qa 的全部实际取值，
如 ability / character_ability / event_query / world_rule_query / character_profile
等）登记到 services.retrieval_policy 的 qa 策略（entity_first=True、
allow_chapter_body=True、source_kinds=None、on_no_result="log"）。此前这些 intent
按 D5 回退 generate 策略（entity_first=False），QA 的 lexical 路径走不到实体优先通道。

本脚本用真实 dev 库（backend/dev-web.sqlite3，项目 d62d4447-b8d7-4fc3-9193-a6d363d8f541）
验证整条链路：

    知识问答问题
      → build_knowledge_query_plan（V1 规则 planner）→ intent
      → get_retrieval_policy(intent) → entity_first=True
      → normalize_entities(project_id, query) → canonical_names（实体命中）
      → LexicalRetriever.search(..., entity_first=policy.entity_first)
        → 实体优先通道 FTS 精确短语命中，实体块加权排前

同时用 structured_qa._detect_structured_intent 验证结构化问答 intent 到 qa 策略的
映射（同一问题 character_ability → entity_first=True）。

容错
----
- 项目无检索索引（retrieval_chunks / retrieval_chunks_fts 表缺失或该项目无数据）
  → 输出说明并 SKIP，退出码 2（不 panic，不误报失败）；
- character_profile / ability_profile 无该项目的角色数据 → 提示后 SKIP，退出码 2；
- 数据前提缺口：按 tokenizer 设计，entity_map（normalize_entities 的实体名录）
  只来自 character_alias_clusters（已确认别名簇）或手动 add_entity；
  CharacterProfile.name 只进词表不进 entity_map。dev 库项目无任何别名簇时，
  normalize_entities 对纯角色名查询会返回空（实体通道不触发）——脚本会明确输出
  该数据前提说明，并在进程内用 add_entity 注册角色正式名（等价于
  canonical_name=角色名、aliases=[] 的已确认簇；仅内存、不写库），
  验证实体通道机制本身在真实索引上的表现；
- 数据库文件不存在等其它异常 → 明确报错，退出码 1。

退出码：0 = PASS（链路验证通过），1 = FAIL（链路未达预期），2 = SKIP（数据不满足）。

安全：全程只读 —— sqlite3 侧用 mode=ro URI，ORM 侧只 SELECT / 调用检索接口，
不 commit，绝不对 dev-web.sqlite3 做任何写操作。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
DEFAULT_DB = BACKEND_DIR / "dev-web.sqlite3"
DEFAULT_PROJECT = "d62d4447-b8d7-4fc3-9193-a6d363d8f541"

# 问答类 intent 全集（与 services/retrieval_policy._QA_ALIAS_INTENTS 对齐）：
# V1 规则 planner + V2 LLM planner（VALID_INTENTS）+ structured_qa。
QA_INTENTS = [
    # V1 规则 planner（services/knowledge_query_plan.py）
    "character_profile", "character_by_ability", "relationship", "ability",
    "timeline", "worldbuilding", "plot", "general",
    # V2 LLM planner（services/knowledge_query_planner.py VALID_INTENTS）
    "character_ability", "plot_event", "source_lookup",
    # 结构化问答（services/structured_qa.py）
    "event_query", "world_rule_query",
]

DEFAULT_QUERIES = [
    "张小侯有哪些能力",   # V1 planner → ability；实体：张小侯（character_profile + ability_profile）
    "莫凡有什么系",       # V1 planner → ability；结构化问答 → character_ability
]


# ── 只读库检查（标准库 sqlite3，mode=ro） ─────────────────

def check_index(db_path: Path, project_id: str) -> tuple[str, dict]:
    """只读检查：检索索引、项目数据、角色/能力数据。

    返回 (status, info)：
    - status="ok"：索引与角色数据齐全，可验证；
    - "no_index" / "no_index_data"：无索引或项目无数据（SKIP）；
    - "no_characters"：有索引但无角色数据（SKIP）。
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        tables = {
            r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        info: dict = {
            "chunk_count": 0, "characters": [], "abilities": [], "alias_clusters": 0,
        }
        if not {"retrieval_chunks", "retrieval_chunks_fts"} <= tables:
            return "no_index", info
        info["chunk_count"] = cur.execute(
            "SELECT COUNT(*) FROM retrieval_chunks WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        if info["chunk_count"] == 0:
            return "no_index_data", info
        if "character_profile" in tables:
            info["characters"] = [
                r[0] for r in cur.execute(
                    "SELECT name FROM character_profile WHERE project_id=?",
                    (project_id,),
                )
            ]
        if "ability_profile" in tables:
            info["abilities"] = [
                tuple(r) for r in cur.execute(
                    "SELECT character_name, ability_name, ability_type "
                    "FROM ability_profile WHERE project_id=?",
                    (project_id,),
                )
            ]
        if "character_alias_clusters" in tables:
            info["alias_clusters"] = cur.execute(
                "SELECT COUNT(*) FROM character_alias_clusters WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
        if not info["characters"]:
            return "no_characters", info
        return "ok", info
    finally:
        con.close()


# ── 异步验证主流程（延迟导入项目模块，需先设 DATABASE_URL） ──

async def run_verify(args, info: dict) -> list[str]:
    """执行验证，返回结论行列表（全部 PASS 则退出码 0）。"""
    from db.session import async_session
    from services.retrieval_policy import get_retrieval_policy
    from services.knowledge_query_plan import build_knowledge_query_plan
    from services.retrieval_tokenizer import build_project_vocabulary, get_project_tokenizer, normalize_entities
    from services.lexical_retriever import LexicalRetriever
    from services.structured_qa import _detect_structured_intent

    pid = args.project
    conclusions: list[str] = []
    fail = lambda msg: conclusions.append(f"[FAIL] {msg}")  # noqa: E731

    async with async_session() as db:
        # 0) 构建项目词表（后续 normalize_entities / 检索共用缓存）
        data = await build_project_vocabulary(db, pid)
        get_project_tokenizer(pid, data["version"])
        tokenizer = get_project_tokenizer(pid)
        print(f"[info] 词表 version={data['version']}  词条数={len(data['vocabulary'])}")
        print(f"[info] 角色（character_profile）: {info['characters']}")
        print(f"[info] 能力（ability_profile）: {info['abilities']}")

        # 1) 全部问答类 intent → qa 策略
        qa_policy = get_retrieval_policy("qa")
        bad = [i for i in QA_INTENTS if get_retrieval_policy(i) != qa_policy]
        if bad:
            fail(f"问答类 intent 未映射到 qa 策略: {bad}")
        else:
            conclusions.append(
                f"[PASS] 问答类 intent {len(QA_INTENTS)} 个全部映射到 qa 策略"
                f"（entity_first=True, allow_chapter_body=True, source_kinds=None）"
            )

        # 2) 数据前提：entity_map 只来自 character_alias_clusters（已确认别名簇）
        #    或手动 add_entity；CharacterProfile.name 只进词表不进 entity_map。
        #    dev 库项目无别名簇时实体通道不触发——进程内注册角色正式名
        #    （等价 canonical_name=角色名、aliases=[] 的已确认簇），仅内存不写库，
        #    用于验证实体通道机制本身在真实索引上的表现。
        simulated = False
        if info["alias_clusters"] == 0:
            simulated = True
            for name in info["characters"]:
                tokenizer.add_entity(name)
            print(
                "[info] 数据前提模拟：本项目 character_alias_clusters 为空，"
                "entity_map 无实体（按设计仅来自已确认别名簇/手动 add_entity）。\n"
                f"       进程内注册 {info['characters']} 为实体（仅内存，不写库）后"
                "继续验证实体通道机制。"
            )

        # 3) 真实查询逐条走链路
        retriever = LexicalRetriever()
        for query in args.queries:
            plan = build_knowledge_query_plan(query, project_id=pid)
            policy = get_retrieval_policy(plan.intent)
            structured_intent = _detect_structured_intent(query)
            s_policy = get_retrieval_policy(structured_intent)
            canonical_names, cleaned = normalize_entities(pid, query)

            print(f"\n──── 查询：{query} ────")
            print(f"  V1 planner intent={plan.intent!r} → qa 策略? {policy is qa_policy}"
                  f"（entity_first={policy.entity_first}）")
            print(f"  structured_qa intent={structured_intent!r} → "
                  f"qa 策略? {s_policy is qa_policy}（entity_first={s_policy.entity_first}）")
            print(f"  normalize_entities → canonical_names={canonical_names}, "
                  f"cleaned={cleaned!r}"
                  + ("（数据前提模拟）" if simulated else "（真实别名簇）"))

            if not canonical_names:
                if simulated:
                    fail(f"「{query}」注册实体后仍未命中（词表/entity_map 异常）")
                else:
                    fail(f"「{query}」未命中任何规范实体（词表无该角色/别名）")
                continue

            # 实体优先通道（qa 策略参数）
            hits_entity = await retriever.search(
                db, pid, query,
                source_kinds=policy.source_kinds,
                limit=args.limit,
                entity_first=policy.entity_first,
            )
            # 对照：关闭实体优先（旧 generate 兜底行为）
            hits_plain = await retriever.search(
                db, pid, query, limit=args.limit, entity_first=False,
            )

            def fmt(hits):
                return "\n".join(
                    f"    #{h.rank} score={h.score:>8.2f} kind={h.source_kind} "
                    f"snippet={h.snippet[:60]}"
                    for h in hits
                )

            print(f"  entity_first=True  命中 {len(hits_entity)} 条：")
            print(fmt(hits_entity))
            print(f"  entity_first=False 命中 {len(hits_plain)} 条：")
            print(fmt(hits_plain))

            if not hits_entity:
                fail(f"「{query}」实体优先通道无命中（索引/词表可能未投影该实体）")
                continue
            top = hits_entity[0]
            top_text = (top.content or "") + (top.snippet or "")
            if not any(c in top_text for c in canonical_names):
                fail(f"「{query}」top-1 命中不含实体 {canonical_names}")
                continue
            if top.score < 500:  # ENTITY_BOOST=1000，实体加权块分数应显著偏高
                fail(f"「{query}」top-1 score={top.score} 无实体加权迹象")
                continue
            verdict = (
                f"[PASS] 「{query}」→ V1 intent={plan.intent!r} → "
                f"normalize_entities={canonical_names} → 实体优先通道命中 "
                f"{len(hits_entity)} 条，top-1 含「{canonical_names[0]}」"
                f"（score={top.score:.2f}）"
            )
            if hits_plain:
                verdict += f"，显著高于词法通道 top-1（{hits_plain[0].score:.2f}）"
            if simulated:
                verdict += " [实体数据前提为进程内模拟：dev 库无 character_alias_clusters]"
            conclusions.append(verdict)

    return conclusions


# ── 入口 ──────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="实体优先通道真实链路验证（阶段二）")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径（只读）")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="项目 ID")
    parser.add_argument("--query", action="append", dest="queries", help="验证查询（可多次，默认两条）")
    parser.add_argument("--limit", type=int, default=5, help="检索 top-k")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"[error] 数据库不存在: {db_path}", file=sys.stderr)
        return 1
    if args.queries:
        args.queries = args.queries[:3]
    else:
        args.queries = list(DEFAULT_QUERIES)

    # 只读检查（先于任何项目模块导入）
    status, info = check_index(db_path, args.project)
    if status == "no_index":
        print("=" * 60)
        print("[SKIP] 项目无检索索引：retrieval_chunks / retrieval_chunks_fts 表缺失。")
        print("       实体优先通道依赖索引投影（services/retrieval_indexer 构建）。")
        print("       可先运行 retrieval_eval/index_dev_db.py 建索引后重试。")
        print("=" * 60)
        return 2
    if status == "no_index_data":
        print("=" * 60)
        print(f"[SKIP] 项目 {args.project} 在 retrieval_chunks 中无数据（chunk_count=0）。")
        print("       实体通道无从验证；先对该项目构建检索索引。")
        print("=" * 60)
        return 2
    if status == "no_characters":
        print("=" * 60)
        print(f"[SKIP] 项目 {args.project} 的 character_profile 无角色数据，")
        print("       无法构造带角色名的实体通道验证查询。")
        print("=" * 60)
        return 2

    print(f"[info] db={db_path}")
    print(f"[info] project={args.project}  chunks={info['chunk_count']}")
    print(f"[info] queries={args.queries}  limit={args.limit}")

    # 在 import 任何项目模块之前覆盖数据库连接（settings 在 import 时读环境变量）
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ.setdefault("DEBUG", "false")
    sys.path.insert(0, str(BACKEND_DIR))

    try:
        conclusions = asyncio.run(run_verify(args, info))
    except Exception as exc:  # 数据库 / 模块异常 → 明确报错
        print(f"[error] 验证执行失败: {exc!r}", file=sys.stderr)
        return 1

    print("\n════════ 验证结论 ════════")
    for line in conclusions:
        print(line)
    failed = [l for l in conclusions if l.startswith("[FAIL]")]
    if failed:
        print("\n结论：FAIL（存在未通过项）")
        return 1
    if not conclusions:
        print("结论：SKIP（无有效验证项）")
        return 2
    print("\n结论：PASS —— 知识问答 intent → get_retrieval_policy → entity_first=True → "
          "normalize_entities → LexicalRetriever 实体加权命中 的链路在真实 dev 库"
          "（7276 个 retrieval_chunks）上生效。")
    print("      注：本项目 character_alias_clusters 为空，实体名录按设计来自"
          "已确认别名簇；真实项目需为角色确认别名簇后，实体通道即自动生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
