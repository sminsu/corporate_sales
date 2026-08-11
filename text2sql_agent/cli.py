"""Interactive command-line interface for the Text2SQL agent."""

import json
from decimal import Decimal

from .config import BAD_DEBT_OUTPUT_DIR, LLM_BASE_URL, LLM_MODEL
from .exports import (
    export_all,
    export_to_csv,
    export_to_excel,
    export_to_text,
    export_to_word,
    format_number_for_report as format_number,
    prepare_export_result,
)
from .config import ENABLE_EMBEDDING_PRECOMPUTE
from .tools.registry import TOOLS
from .workflow import run_agent_with_prompts

_NUMERIC = (int, float, Decimal)


def print_result_table(columns: list[str], rows: list[tuple], max_rows: int = 20):
    if not columns or not rows:
        return
    display_rows = rows[:max_rows]
    widths = [len(c) for c in columns]
    for row in display_rows:
        for i, val in enumerate(row):
            formatted = format_number(val) if isinstance(val, _NUMERIC) else str(val)
            widths[i] = max(widths[i], len(formatted))
    header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    separator = "-+-".join("-" * w for w in widths)
    print(f"  {header}")
    print(f"  {separator}")
    for row in display_rows:
        cells = []
        for i, val in enumerate(row):
            formatted = format_number(val) if isinstance(val, _NUMERIC) else str(val)
            cells.append(formatted.rjust(widths[i]) if isinstance(val, _NUMERIC) else formatted.ljust(widths[i]))
        print(f"  {' | '.join(cells)}")
    if len(rows) > max_rows:
        print(f"\n  ... 외 {len(rows) - max_rows}건")


EXPORT_COMMANDS = {"저장", "export", "보고서", "word", "docx", "excel", "xlsx", "text", "txt", "csv", "내보내기", "파일"}


def _parse_export_format(cmd: str) -> str | None:
    cmd_lower = cmd.lower().strip()
    if cmd_lower in ("word", "docx"):
        return "word"
    elif cmd_lower in ("excel", "xlsx"):
        return "excel"
    elif cmd_lower in ("text", "txt"):
        return "text"
    elif cmd_lower == "csv":
        return "csv"
    elif cmd_lower in ("저장", "export", "보고서", "내보내기", "파일"):
        return "all"
    return None


def main():
    print("=" * 60)
    print(" KB카드 법인영업 Text2SQL Agent (vLLM) v10")
    print(" - Tool 기반 확정 SQL + Verified Query + SQL 자동생성")
    print(" - 모든 경로에서 파라미터 부족 시 사용자 입력 요청")
    print(" - 보고서 내보내기 (Word/Excel/Text)")
    print("=" * 60)
    print(f" LLM: {LLM_MODEL} @ {LLM_BASE_URL}")
    # 기존에는 workflow.EMBEDDINGS_AVAILABLE를 import 시점에 복사해 항상 OFF로
    # 표시됐다. 배너는 설정값 기준으로 안내하고, 실제 가용성은 첫 질문 처리 시
    # 사전계산 결과에 따라 결정된다.
    embed_status = "ON (첫 질문 시 사전계산)" if ENABLE_EMBEDDING_PRECOMPUTE else "OFF (규칙/LLM 폴백)"
    print(f" Embedding: {embed_status}")
    print(f" 등록된 Tool: {len(TOOLS)}개")
    for t in TOOLS:
        req_count = sum(1 for p in t["parameters"] if p.get("required"))
        print(f"   - {t['name']}: {t['description'][:45]}... (필수 {req_count}개)")
    print(f" 엑셀 출력: {BAD_DEBT_OUTPUT_DIR}")
    print(" 내보내기: 조회 후 '저장' | 'word' | 'excel' | 'text' 입력")
    print(" 종료: quit | exit | q")
    print("=" * 60)

    last_result = None

    while True:
        question = input("\n질문: ").strip()
        if not question:
            continue
        if question.lower() in ("quit", "exit", "q"):
            print("종료합니다.")
            break

        if question.lower().split()[0] in EXPORT_COMMANDS or question.lower() in EXPORT_COMMANDS:
            if last_result is None:
                print("저장할 조회 결과가 없습니다. 먼저 질문을 입력해주세요.")
                continue
            fmt = _parse_export_format(question.lower().split()[0] if question.lower().split() else question.lower())
            if fmt is None:
                fmt = "all"
            print("\n보고서 생성 중...")
            try:
                export_result = prepare_export_result(last_result) if fmt in {"all", "excel", "csv", "text"} else last_result
                if fmt == "all":
                    paths = export_all(export_result)
                    print("\n[보고서 저장 완료]")
                    for ftype, fpath in paths.items():
                        print(f"  {ftype.upper()}: {fpath}")
                elif fmt == "word":
                    path = export_to_word(export_result)
                    print(f"\n[Word 저장 완료] {path}")
                elif fmt == "excel":
                    path = export_to_excel(export_result)
                    print(f"\n[Excel 저장 완료] {path}")
                elif fmt == "text":
                    path = export_to_text(export_result)
                    print(f"\n[Text 저장 완료] {path}")
                elif fmt == "csv":
                    path = export_to_csv(export_result)
                    print(f"\n[CSV 저장 완료] {path}")
            except ImportError:
                print("python-docx 패키지가 필요합니다: pip install python-docx")
            except Exception as e:
                print(f"보고서 생성 실패: {e}")
            print("\n" + "-" * 60)
            continue

        print("\n처리 중...\n")
        result = run_agent_with_prompts(question)

        qtype = result.get("question_type", "")
        if qtype:
            label = {"need_sql": "SQL 조회", "direct": "직접 답변", "reject": "답변 불가"}.get(qtype, qtype)
            print(f"[분류] {label}")

        if qtype == "need_sql":
            tool = result.get("selected_tool", "")
            matched = result.get("matched_query_name", "")
            if tool:
                print(f"[Tool] {tool}")
                params = result.get("tool_params", {})
                if params:
                    print(f"[Tool 파라미터] {json.dumps(params, ensure_ascii=False)}")
            elif matched:
                print(f"[매칭] 검증된 쿼리: {matched}")
                params = result.get("extracted_params", {})
                if params:
                    print(f"[추출 파라미터] {json.dumps(params, ensure_ascii=False)}")
            else:
                print(f"[경로] SQL 자동 생성")
                user_params = result.get("user_provided_params", {})
                if user_params:
                    print(f"[사용자 입력 파라미터] {json.dumps(user_params, ensure_ascii=False)}")

            tables = result.get("selected_tables", [])
            if tables:
                print(f"[사용 테이블] {', '.join(tables)}")

        excel_path = result.get("bad_debt_excel_path", "")
        if excel_path:
            print(f"[엑셀 파일] {excel_path}")

        last_result = result

        sql = result.get("final_sql", "")
        if sql:
            print(f"\n[실행 SQL]")
            print(sql)

        columns = result.get("query_columns", [])
        rows = result.get("query_rows", [])
        if columns and rows:
            print(f"\n[쿼리 결과] ({len(rows)}건)")
            print_result_table(columns, rows)

        answer = result.get("answer", "")
        if answer:
            print(f"\n[답변]")
            print(answer)

        error = result.get("error_message", "")
        if error:
            print(f"\n[오류]")
            print(error)

        has_data = bool(result.get("query_columns")) and bool(result.get("query_rows"))
        if has_data:
            print(f"\n  * 결과를 파일로 저장하려면: '저장' | 'word' | 'excel' | 'text' 입력")

        print("\n" + "-" * 60)


if __name__ == "__main__":
    main()
