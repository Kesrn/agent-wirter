#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_DIR="$BACKEND_DIR/tests"
MODE="${1:---exclude-pg}"
PYTHON_BIN="${PYTHON_BIN:-python}"
TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/aicp-per-file.XXXXXX")"
mkdir -p "$TEMP_ROOT/home"
trap 'rm -rf "$TEMP_ROOT"' EXIT

if [[ "$MODE" != "--exclude-pg" && "$MODE" != "--pg-only" ]]; then
  echo "用法: $0 [--exclude-pg|--pg-only]" >&2
  exit 2
fi

TEST_PG_HOST="${TEST_PG_HOST:-127.0.0.1}"
TEST_PG_PORT="${TEST_PG_PORT:-5432}"
if [[ "$MODE" == "--pg-only" ]]; then
  if [[ ! "$TEST_PG_PORT" =~ ^[0-9]+$ || "$TEST_PG_PORT" == "5432" ]]; then
    echo "PG 测试必须使用 TEST_PG_PORT 指定的非 5432 临时实例" >&2
    exit 2
  fi
fi

passed_files=0
failed_files=0
total_passed=0
total_skipped=0

while IFS= read -r test_file; do
  name="$(basename "$test_file")"
  is_pg=0
  is_pg_only_file=0
  [[ "$name" == *_pg.py || "$name" == *postgres*.py ]] && is_pg=1
  [[ "$name" == *_pg.py || "$name" == *postgres*.py ]] && is_pg_only_file=1
  [[ "$name" == "test_retrieval_index_worker.py" ]] && is_pg=1
  if [[ "$MODE" == "--pg-only" && "$is_pg" -ne 1 ]]; then
    continue
  fi
  if [[ "$MODE" == "--exclude-pg" && "$is_pg_only_file" -eq 1 ]]; then
    continue
  fi

  if [[ "$MODE" == "--pg-only" ]]; then
    database_url="sqlite+aiosqlite:///$TEMP_ROOT/${name%.py}.sqlite3"
    run_pg_tests_env=RUN_PG_TESTS=1
  else
    database_url="sqlite+aiosqlite:///$TEMP_ROOT/${name%.py}.sqlite3"
    run_pg_tests_env=RUN_PG_TESTS=0
  fi

  log_file="$TEMP_ROOT/${name}.log"
  base_temp="$TEMP_ROOT/${name}.pytest"
  mkdir -p "$base_temp"
  if env -i \
    PATH="$PATH" \
    HOME="$TEMP_ROOT/home" \
    TMPDIR="$TEMP_ROOT" \
    PYTHON_DOTENV_DISABLED=1 \
    APP_ENV=development \
    JWT_SECRET='p2-ci-test-only-secret-not-real-00000000000000000000' \
    DATABASE_URL="$database_url" \
    TEST_PG_HOST="$TEST_PG_HOST" \
    TEST_PG_PORT="$TEST_PG_PORT" \
    CHECKPOINTER_BACKEND=memory \
    LLM_PROVIDER=mock \
    EMBEDDING_PROVIDER=mock \
    ALLOW_MOCK_PROVIDER=true \
    "$run_pg_tests_env" \
    "$PYTHON_BIN" -m pytest -q --basetemp="$base_temp" "$test_file" >"$log_file" 2>&1; then
    summary="$(awk '/ passed| skipped| failed| error/{line=$0} END{print line}' "$log_file")"
    if [[ -z "$summary" ]]; then
      echo "FAIL $name | pytest did not report a summary"
      cat "$log_file"
      failed_files=$((failed_files + 1))
      continue
    fi
    passed_files=$((passed_files + 1))
    passed_count="$(printf '%s\n' "$summary" | sed -n 's/^\([0-9][0-9]*\) passed.*/\1/p')"
    skipped_count="$(printf '%s\n' "$summary" | sed -n 's/.*[,( ]\([0-9][0-9]*\) skipped.*/\1/p')"
    total_passed=$((total_passed + ${passed_count:-0}))
    total_skipped=$((total_skipped + ${skipped_count:-0}))
    echo "PASS $name | $summary"
  else
    result=$?
    echo "FAIL $name | exit=$result"
    cat "$log_file"
    failed_files=$((failed_files + 1))
  fi
done < <(find "$TEST_DIR" -maxdepth 1 -type f -name 'test_*.py' -print | sort)

echo "Per-file summary: passed_files=$passed_files failed_files=$failed_files tests_passed=$total_passed tests_skipped=$total_skipped"
[[ "$failed_files" -eq 0 ]]
