"""依赖声明防回归（任务单 0.8）。

三个断言：
1. backend/ 下所有非测试代码 import 的第三方包，都必须出现在 requirements.in
   （或显式豁免列表 / import 名映射表）里，防止再次出现「用了但没声明」；
2. requirements.txt 的有效内容就是 `-r requirements.lock`（薄入口，不许手加包）；
3. requirements.lock / requirements-dev.lock 里每一行依赖都带 `==` 精确版本。

全部是文件解析，不联网、不装包。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

# 一次性脚本之间的互相导入（retrieval_eval/ 下以 sys.path 同目录方式互导），
# 不是第三方包，显式豁免。
IMPORT_EXEMPT = {
    "run_baseline",  # retrieval_eval/compare_results.py 导入同目录的 run_baseline.py
}

# import 名 → 发行版名（PEP 503 规范化前的人类可读写法）。
# 没有列出的名字按「同名」处理（大小写与 -/_ 差异会自动归一）。
IMPORT_TO_DIST = {
    "docx": "python-docx",
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "langchain_core": "langchain-core",
    "typing_extensions": "typing-extensions",
}


def _dist_name(import_name: str) -> str:
    """import 名转 PEP 503 规范化的发行版名。"""
    mapped = IMPORT_TO_DIST.get(import_name, import_name)
    return re.sub(r"[-_.]+", "-", mapped).lower()


def _collect_local_module_names() -> set[str]:
    """backend/ 下的本地顶层模块名：目录名 + 任意 .py 的 stem。"""
    names: set[str] = set()
    for path in BACKEND_DIR.rglob("*.py"):
        rel = path.relative_to(BACKEND_DIR)
        if any(part in ("venv", "__pycache__", ".workbuddy") for part in rel.parts):
            continue
        names.add(path.stem)
    for path in BACKEND_DIR.rglob("*"):
        rel = path.relative_to(BACKEND_DIR)
        if path.is_dir() and not path.name.startswith((".", "venv")) and "venv" not in rel.parts:
            names.add(path.name)
    return names


def _collect_third_party_imports() -> dict[str, list[str]]:
    """AST 扫描（排除 tests/ 与 venv/，包含 scripts/ 与 alembic/ 等）。

    返回 {import 名: [引用文件...]}，只含既不是标准库、也不是本地模块的名字。
    """
    stdlib = set(sys.stdlib_module_names)
    local = _collect_local_module_names()

    found: dict[str, set[str]] = {}
    for py in BACKEND_DIR.rglob("*.py"):
        rel = py.relative_to(BACKEND_DIR)
        if any(part in ("venv", "__pycache__", ".workbuddy") for part in rel.parts):
            continue
        if rel.parts[0] == "tests":
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0 or not node.module:
                    continue  # 相对导入，肯定是本地模块
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                if name in stdlib or name in local or name in IMPORT_EXEMPT:
                    continue
                found.setdefault(name, set()).add(str(rel))
    return {name: sorted(files) for name, files in sorted(found.items())}


def _declared_dists(path: Path) -> set[str]:
    """解析 .in 文件里的发行版名（忽略注释、-r 引用行、环境标记）。"""
    declared: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        name = re.split(r"[<>=!~;\s\[]", line, maxsplit=1)[0]
        declared.add(re.sub(r"[-_.]+", "-", name).lower())
    return declared


def test_all_third_party_imports_are_declared():
    """每个第三方 import 必须出现在 requirements.in（按发行版名）。"""
    declared = _declared_dists(BACKEND_DIR / "requirements.in")
    imports = _collect_third_party_imports()

    problems = []
    for name, files in imports.items():
        if _dist_name(name) not in declared:
            where = ", ".join(files[:3])
            problems.append(f"import '{name}'（{where}）未声明，请把 {_dist_name(name)} 加入 requirements.in 或映射表/豁免表")

    assert not problems, "存在未声明的第三方依赖：\n" + "\n".join(problems)


def test_requirements_txt_is_thin_entry():
    """requirements.txt 的有效内容就是 -r requirements.lock，不许手加包。"""
    lines = [
        line.strip()
        for line in (BACKEND_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines == ["-r requirements.lock"], f"requirements.txt 应只含 -r requirements.lock，实际是：{lines}"


def _assert_lock_lines_pinned(lock_path: Path) -> None:
    offenders = []
    for raw in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue  # 注释与 uv 的 "# via" 溯源行
        if line.startswith("-"):
            offenders.append(f"{line}  （锁文件里不应有选项/引用行）")
            continue
        if "==" not in line:
            offenders.append(f"{line}  （缺 == 精确版本）")
    assert not offenders, f"{lock_path.name} 存在不带 == 的依赖行：\n" + "\n".join(offenders)


def test_lock_files_are_pinned():
    """两个锁文件里每一行依赖都带 == 精确版本。"""
    _assert_lock_lines_pinned(BACKEND_DIR / "requirements.lock")
    _assert_lock_lines_pinned(BACKEND_DIR / "requirements-dev.lock")


def test_new_declared_packages_are_present():
    """0.1-0.7 期间开始直接 import 的包必须已声明（防映射表/扫描器失效的底线断言）。"""
    declared = _declared_dists(BACKEND_DIR / "requirements.in")
    for dist in ("cryptography", "openai", "numpy", "alembic", "typing-extensions"):
        assert dist in declared, f"{dist} 必须在 requirements.in 里声明"
