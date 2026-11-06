"""P-1 阶段四 CLI：切分代次全链路（legacy 迁移 / 新代构建 / 验收 / CAS 激活 / 回滚）。

用法（db 必须传**副本/开发库**，禁止指向生产桌面库）：
  venv/bin/python scripts/split_generation_cli.py init-legacy <db> <project_id> <source_id>
  venv/bin/python scripts/split_generation_cli.py build <db> <project_id> <source_id> <ledger.json>
  venv/bin/python scripts/split_generation_cli.py validate <db> <project_id> <source_id> <generation_id>
  venv/bin/python scripts/split_generation_cli.py activate <db> <project_id> <source_id> <generation_id> [--expect <old_id>]
  venv/bin/python scripts/split_generation_cli.py rollback <db> <project_id> <source_id> <target_generation_id> [--expect <old_id>]
  venv/bin/python scripts/split_generation_cli.py status <db> <project_id> <source_id>

activate 默认带交互确认；加 --yes 跳过。
db 为 SQLite 路径或 SQLAlchemy URL（如 postgresql+asyncpg://...）。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.split_generation_service import (
    SplitGenerationError,
    activate_generation,
    build_new_generation,
    ensure_legacy_generation,
    get_active_generation,
    get_chapters_for_generation,
    list_generations,
    rollback_generation,
    validate_generation,
)


def _engine_url(db_arg: str) -> str:
    if "://" in db_arg:
        return db_arg
    return f"sqlite+aiosqlite:///{Path(db_arg).resolve()}"


async def _run(coro, *, commit: bool = False):
    """执行服务协程并统一 commit/rollback。"""
    try:
        result = await coro
        if commit:
            await _session.commit()
        return result
    except Exception:
        await _session.rollback()
        raise


# 会话在 main 里创建，传给各子命令
_session: AsyncSession = None  # type: ignore


async def _cmd_init_legacy(args) -> int:
    legacy_id = await _run(ensure_legacy_generation(
        _session, args.project_id, args.source_id), commit=True)
    active = await _run(get_active_generation(
        _session, args.project_id, args.source_id))
    chapters = await _run(get_chapters_for_generation(
        _session, args.project_id, args.source_id, legacy_id))
    print(f"legacy generation: {legacy_id}")
    print(f"回填章节: {len(chapters)} 行（source_ordinal = 旧 chapter_no）")
    print(f"活动指针: {active.id if active else None} (status={active.status if active else '-'})")
    return 0


async def _cmd_build(args) -> int:
    ledger = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
    result = await _run(build_new_generation(
        _session, args.project_id, args.source_id, ledger), commit=True)
    print(f"新代: {result['id']}")
    print(f"状态: {result['status']} | 保留章节: {result['kept_chapters']}")
    report = result["validation_report"]
    print(f"验收: {report['verdict']}")
    for f in report.get("failed", []):
        print(f"  FAIL: {f}")
    print(f"决策哈希: {result['decisions_hash'][:16]}…")
    print("（未激活。验收通过后单独执行 activate。）")
    return 0 if result["status"] == "ready" else 1


async def _cmd_validate(args) -> int:
    from models.project_source import ProjectSource
    from sqlalchemy import select

    src = (await _run(_session.execute(
        select(ProjectSource.content)
        .where(ProjectSource.id == args.source_id)
        .where(ProjectSource.project_id == args.project_id)
    ))).scalar_one_or_none()
    if src is None:
        print(f"source not found: {args.source_id}")
        return 1
    report = await _run(validate_generation(_session, args.generation_id, src))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


async def _cmd_activate(args) -> int:
    if not args.yes:
        print(f"将激活代 {args.generation_id}（活动指针 CAS 切换）——确认继续？[y/N]")
        if input().strip().lower() not in ("y", "yes"):
            print("已取消")
            return 2
    result = await _run(activate_generation(
        _session, args.project_id, args.source_id,
        args.generation_id, args.expect), commit=True)
    print(f"激活成功: {result['switched_from']} → {result['switched_to']}")
    return 0


async def _cmd_rollback(args) -> int:
    result = await _run(rollback_generation(
        _session, args.project_id, args.source_id,
        args.target_generation_id, args.expect), commit=True)
    print(f"回滚成功: {result['switched_from']} → {result['switched_to']}")
    return 0


async def _cmd_status(args) -> int:
    from models.split_generation import SplitGeneration
    from sqlalchemy import select

    active = await _run(get_active_generation(
        _session, args.project_id, args.source_id))
    gens = await _run(list_generations(
        _session, args.project_id, args.source_id))
    print(f"活动代: {active.id if active else None} "
          f"(status={active.status if active else '-'})")
    print(f"代列表: {len(gens)} 个")
    for g in gens:
        mark = " ← active" if active and g.id == active.id else ""
        report = g.validation_report
        verdict = report.get("verdict", "-") if isinstance(report, dict) else "-"
        print(f"  {str(g.id)[:8]} | {g.status:10} | parser={g.parser_version:24} "
              f"| decisions={str(g.decisions_hash)[:8]}… | verdict={verdict}{mark}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="切分代次 CLI（阶段四）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-legacy")
    p.add_argument("db"); p.add_argument("project_id"); p.add_argument("source_id")
    p.set_defaults(func=_cmd_init_legacy)

    p = sub.add_parser("build")
    p.add_argument("db"); p.add_argument("project_id"); p.add_argument("source_id")
    p.add_argument("ledger")
    p.set_defaults(func=_cmd_build)

    p = sub.add_parser("validate")
    p.add_argument("db"); p.add_argument("project_id"); p.add_argument("source_id")
    p.add_argument("generation_id")
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("activate")
    p.add_argument("db"); p.add_argument("project_id"); p.add_argument("source_id")
    p.add_argument("generation_id")
    p.add_argument("--expect", default=None, help="期望的当前旧代 id（CAS 必传）")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_cmd_activate)

    p = sub.add_parser("rollback")
    p.add_argument("db"); p.add_argument("project_id"); p.add_argument("source_id")
    p.add_argument("target_generation_id")
    p.add_argument("--expect", default=None)
    p.set_defaults(func=_cmd_rollback)

    p = sub.add_parser("status")
    p.add_argument("db"); p.add_argument("project_id"); p.add_argument("source_id")
    p.set_defaults(func=_cmd_status)

    args = parser.parse_args()

    global _session
    engine = create_async_engine(_engine_url(args.db))
    factory = async_sessionmaker(engine, class_=AsyncSession,
                                 expire_on_commit=False)

    async def _dispatch() -> int:
        global _session
        async with factory() as session:
            _session = session
            return await args.func(args)

    try:
        return asyncio.run(_dispatch())
    except SplitGenerationError as e:
        print(f"错误: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
