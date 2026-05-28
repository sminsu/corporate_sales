"""Report/export helpers for completed query results."""

import csv
import re
from datetime import datetime
from decimal import Decimal

from .config import REPORT_DIR

# ---------------------------------------------------------------------------
# 7. 보고서 내보내기
# ---------------------------------------------------------------------------


def _ensure_report_dir():
    REPORT_DIR.mkdir(exist_ok=True)


def _make_filename(question: str, ext: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w가-힣]", "_", question)[:30].strip("_")
    return f"{ts}_{safe}.{ext}"


def format_number_for_report(val) -> str:
    if isinstance(val, (int, float, Decimal)):
        val = float(val)
        if abs(val) >= 100_000_000:
            return f"{val / 100_000_000:,.1f}억"
        elif abs(val) >= 10_000:
            return f"{val / 10_000:,.0f}만"
        else:
            return f"{val:,}"
    return str(val)


def export_to_word(result: dict) -> str:
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    _ensure_report_dir()
    question = result.get("question", "조회결과")
    filepath = REPORT_DIR / _make_filename(question, "docx")
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "맑은 고딕"
    style.font.size = Pt(10)
    doc.add_heading("KB카드 법인영업 데이터 분석 보고서", level=0)
    info_table = doc.add_table(rows=3, cols=2)
    info_table.style = "Light List"
    for i, (label, value) in enumerate([
        ("작성일시", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("질문", question),
        ("분석 경로", _get_source_label(result)),
    ]):
        info_table.rows[i].cells[0].text = label
        info_table.rows[i].cells[1].text = value
    doc.add_paragraph("")
    answer = result.get("answer", "")
    if answer:
        doc.add_heading("분석 결과", level=1)
        doc.add_paragraph(answer)
    columns = result.get("query_columns", [])
    rows = result.get("query_rows", [])
    if columns and rows:
        doc.add_heading("상세 데이터", level=1)
        doc.add_paragraph(f"총 {len(rows)}건 조회됨")
        display_rows = rows[:200]
        table = doc.add_table(rows=1 + len(display_rows), cols=len(columns))
        table.style = "Light Grid Accent 1"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for j, col_name in enumerate(columns):
            cell = table.rows[0].cells[j]
            cell.text = col_name
            for paragraph in cell.paragraphs:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in paragraph.runs:
                    run.bold = True
                    run.font.size = Pt(9)
        for i, row in enumerate(display_rows):
            for j, val in enumerate(row):
                cell = table.rows[i + 1].cells[j]
                cell.text = format_number_for_report(val)
                for paragraph in cell.paragraphs:
                    if isinstance(val, (int, float)):
                        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                    if cell.paragraphs[0].runs:
                        cell.paragraphs[0].runs[0].font.size = Pt(9)
        if len(rows) > 200:
            doc.add_paragraph(f"* 전체 {len(rows)}건 중 상위 200건만 표시")
    sql = result.get("final_sql", "")
    if sql:
        doc.add_heading("실행 SQL", level=1)
        sql_para = doc.add_paragraph()
        sql_run = sql_para.add_run(sql)
        sql_run.font.size = Pt(8)
        sql_run.font.name = "Consolas"
        sql_run.font.color.rgb = RGBColor(80, 80, 80)
    doc.add_paragraph("")
    footer = doc.add_paragraph("KB카드 법인영업 Text2SQL Agent v10 자동생성 보고서")
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in footer.runs:
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(150, 150, 150)
    doc.save(str(filepath))
    return str(filepath)


def export_to_text(result: dict) -> str:
    _ensure_report_dir()
    question = result.get("question", "조회결과")
    filepath = REPORT_DIR / _make_filename(question, "txt")
    lines = ["=" * 70, "  KB카드 법인영업 데이터 분석 보고서", "=" * 70,
             f"  작성일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             f"  질문: {question}", f"  분석 경로: {_get_source_label(result)}", "=" * 70, ""]
    answer = result.get("answer", "")
    if answer:
        lines.extend(["[분석 결과]", "-" * 70, answer, ""])
    columns = result.get("query_columns", [])
    rows = result.get("query_rows", [])
    if columns and rows:
        lines.extend([f"[상세 데이터] (총 {len(rows)}건)", "-" * 70])
        display_rows = rows[:200]
        col_widths = [max(len(c), 8) for c in columns]
        for row in display_rows:
            for j, val in enumerate(row):
                col_widths[j] = max(col_widths[j], len(format_number_for_report(val)))
        lines.append(" | ".join(c.ljust(col_widths[j]) for j, c in enumerate(columns)))
        lines.append("-+-".join("-" * w for w in col_widths))
        for row in display_rows:
            cells = []
            for j, val in enumerate(row):
                formatted = format_number_for_report(val)
                cells.append(formatted.rjust(col_widths[j]) if isinstance(val, (int, float)) else formatted.ljust(col_widths[j]))
            lines.append(" | ".join(cells))
        if len(rows) > 200:
            lines.append(f"\n* 전체 {len(rows)}건 중 상위 200건만 표시")
        lines.append("")
    sql = result.get("final_sql", "")
    if sql:
        lines.extend(["[실행 SQL]", "-" * 70, sql, ""])
    lines.extend(["=" * 70, "  KB카드 법인영업 Text2SQL Agent v10 자동생성 보고서", "=" * 70])
    filepath.write_text("\n".join(lines), encoding="utf-8")
    return str(filepath)


def export_to_csv(result: dict) -> str:
    _ensure_report_dir()
    question = result.get("question", "조회결과")
    filepath = REPORT_DIR / _make_filename(question, "csv")
    columns = result.get("query_columns", [])
    rows = result.get("query_rows", [])
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if columns:
            writer.writerow(columns)
        for row in rows:
            writer.writerow(row)
    return str(filepath)


def export_all(result: dict) -> dict[str, str]:
    paths = {}
    try:
        paths["word"] = export_to_word(result)
    except ImportError:
        paths["word"] = "(python-docx 패키지 미설치 - pip install python-docx)"
    except Exception as e:
        paths["word"] = f"(Word 생성 실패: {e})"
    paths["text"] = export_to_text(result)
    paths["csv"] = export_to_csv(result)
    return paths


def _get_source_label(result: dict) -> str:
    tool = result.get("selected_tool", "")
    matched = result.get("matched_query_name", "")
    if tool:
        return f"Tool: {tool}"
    elif matched:
        return f"검증된 쿼리: {matched}"
    elif result.get("question_type") == "direct":
        return "직접 답변"
    else:
        return "SQL 자동 생성"
