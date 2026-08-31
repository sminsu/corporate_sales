#!/usr/bin/env python3
"""질문 하나를 에이전트에 넣어 SQL·조회 결과·답변을 바로 확인한다.

    python scripts/ask.py "2025년 12월 가맹점별 매출 상위 10곳을 보여줘"
    python scripts/ask.py "질문1" "질문2"     # 여러 건을 순서대로 실행
    python scripts/ask.py                     # 인자가 없으면 질문을 계속 입력받는다

파라미터가 부족하면 CLI와 동일하게 추가 입력을 요청한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def ask(question: str) -> None:
    """Run one question through the same graph the CLI uses and print the answer."""
    from text2sql_agent.cli import print_result_table
    from text2sql_agent.workflow import run_agent_with_prompts

    result = run_agent_with_prompts(question)

    sql = result.get("final_sql", "")
    if sql:
        print("\n[실행 SQL]")
        print(sql)

    columns = result.get("query_columns", [])
    rows = result.get("query_rows", [])
    if columns and rows:
        print(f"\n[쿼리 결과] ({len(rows)}건)")
        print_result_table(columns, rows)

    print("\n[답변]")
    print(result.get("answer") or result.get("error_message") or "(답변 없음)")
    print("\n" + "-" * 60)


def main() -> None:
    questions = [q.strip() for q in sys.argv[1:] if q.strip()]
    if questions:
        for question in questions:
            print(f"\n질문: {question}")
            ask(question)
        return

    while True:
        try:
            question = input("\n질문 (종료: q): ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            continue
        if question.lower() in ("q", "quit", "exit"):
            break
        ask(question)


if __name__ == "__main__":
    main()
