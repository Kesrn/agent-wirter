"""shadow_compare.py 有效性门控测试（v2：schema 不兼容 / 无旧链路语料）。

以子进程跑真实的 retrieval_eval/shadow_compare.py CLI（验证实际退出码），
对临时 SQLite 库断言两个门控场景：

1. 旧链路语料全 0（schema 兼容）→ 退出码 0，报告 eligible=false、
   valid_for_retirement_decision=false、schema_ok=true，
   不含 summary / per_query（不产生任何重合度指标）；
2. world_entries 缺 chapter_sequence_number（模拟修复前的 dev 库）→ 退出码 3，
   报告 schema_ok=false、valid_for_retirement_decision=false。

运行：
    cd backend
    ./venv/bin/python -m pytest tests/test_shadow_compare_gating.py -q
"""

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND_DIR / "retrieval_eval" / "shadow_compare.py"

# 与 models/world_entry.py / models/character.py 一致的列集合
# （chapter_sequence_number 在 schema_broken 场景下被剔除）
WORLD_ENTRIES_COLS = [
    "id", "project_id", "title", "category", "scope_type",
    "chapter_sequence_number", "content", "rules", "confidence", "embedding",
    "created_at", "updated_at",
]
CHARACTERS_COLS = [
    "id", "project_id", "name", "role_type", "scope_type", "profile",
    "faction", "appearance_count", "metadata", "embedding", "created_at", "updated_at",
]


def _make_db(path: Path, *, schema_broken: bool, with_corpus: bool = False) -> None:
    """构造最小可跑 shadow_compare 的临时库。

    - projects + retrieval_chunks（过新链路前置检查，避免先触发退出码 2）；
    - world_entries / characters：schema_broken=True 时 world_entries 缺
      chapter_sequence_number（模拟修复前的 dev 库），两表均无数据行（语料为 0）；
    - with_corpus=True 时为 world_entries / characters 各插 1 行（含 embedding），
      用于验证通过门控后的正式报告路径。
    """
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO projects (id, title) VALUES ('p-test-0001', '门控测试项目')")
    conn.execute(
        "CREATE TABLE retrieval_chunks (id TEXT PRIMARY KEY, project_id TEXT, title TEXT, content TEXT)"
    )
    conn.execute(
        "INSERT INTO retrieval_chunks (id, project_id, title, content) "
        "VALUES ('c1', 'p-test-0001', 't', 'c')"
    )
    we_cols = [c for c in WORLD_ENTRIES_COLS
               if not (schema_broken and c == "chapter_sequence_number")]
    conn.execute("CREATE TABLE world_entries (" + ", ".join(f"{c} TEXT" for c in we_cols) + ")")
    conn.execute("CREATE TABLE characters (" + ", ".join(f"{c} TEXT" for c in CHARACTERS_COLS) + ")")
    if with_corpus:
        conn.execute(
            "INSERT INTO world_entries (id, project_id, title, content, embedding) "
            "VALUES ('we1', 'p-test-0001', '魔法体系', '元素魔法七系', '[0.1]')"
        )
        conn.execute(
            "INSERT INTO characters (id, project_id, name, profile, embedding) "
            "VALUES ('ch1', 'p-test-0001', '莫凡', '雷系法师', '[0.2]')"
        )
    conn.commit()
    conn.close()


def _run(db: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--out", str(out)],
        capture_output=True, text=True, timeout=180,
    )


def test_ineligible_when_no_old_chain_corpus(tmp_path):
    """schema 兼容但旧链路语料全 0：eligible=false、退出码 0、无重合度指标。"""
    db = tmp_path / "no_corpus.sqlite3"
    _make_db(db, schema_broken=False)
    out = tmp_path / "report.json"

    proc = _run(db, out)

    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "不具备影子比较资格" in proc.stdout
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["schema_ok"] is True
    assert report["eligible"] is False
    assert report["valid_for_retirement_decision"] is False
    assert "不具备影子比较资格" in report["ineligibility_reason"]
    assert "world_entries=0" in report["ineligibility_reason"]
    # 不得产出任何可用于 7 天观察的重合度指标
    assert "summary" not in report
    assert "per_query" not in report


def test_schema_broken_exits_3(tmp_path):
    """world_entries 缺 chapter_sequence_number：valid=false、退出码 3、无重合度指标。"""
    db = tmp_path / "broken_schema.sqlite3"
    _make_db(db, schema_broken=True)
    out = tmp_path / "report.json"

    proc = _run(db, out)

    assert proc.returncode == 3, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["schema_ok"] is False
    assert report["eligible"] is False
    assert report["valid_for_retirement_decision"] is False
    assert "chapter_sequence_number" in report["ineligibility_reason"]
    assert "summary" not in report
    assert "per_query" not in report


def test_valid_report_when_eligible(tmp_path, monkeypatch):
    """通过门控（schema 兼容 + 语料非空）：eligible=true、valid=true、含重合度指标。

    用 monkeypatch 替换两条链路的执行函数（避免依赖完整检索栈），验证正式报告
    路径的判定字段与 summary / per_query 产出。
    """
    import retrieval_eval.shadow_compare as sc

    db = tmp_path / "with_corpus.sqlite3"
    _make_db(db, schema_broken=False, with_corpus=True)

    async def fake_new_chain(db_path, project_id, queries, limit):
        records = [{"query_id": q["query_id"], "latency_ms": 1.0, "results": []} for q in queries]
        return records, 0.0

    async def fake_old_chain(db_path, project_id, queries, old_limit, old_db_url):
        records = [{"query_id": q["query_id"], "latency_ms": 1.0, "results": []} for q in queries]
        ctx = {"chapter_id": None, "latency_ms": 1.0, "context_len": 0, "context_preview": ""}
        return records, ctx

    monkeypatch.setattr(sc, "run_new_chain", fake_new_chain)
    monkeypatch.setattr(sc, "run_old_chain", fake_old_chain)
    out = tmp_path / "report.json"

    code = asyncio.run(sc.run(
        str(db), "p-test-0001", "门控测试项目", list(sc.DEMO_QUERIES), 5, 5, None, str(out), False,
    ))

    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["schema_ok"] is True
    assert report["eligible"] is True
    assert report["valid_for_retirement_decision"] is True
    assert report["ineligibility_reason"] is None
    assert "summary" in report and "per_query" in report
    assert "mean_overlap_ratio" in report["summary"]
