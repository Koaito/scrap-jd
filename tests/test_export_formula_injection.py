"""
E2: file export (CSV/XLSX) chống formula injection.

Tiêu đề job, tên công ty, ghi chú... đến từ web crawl hoặc nhập tay. Ô bắt đầu bằng `=`, `+`, `-`, `@`
(cùng tab và CR) bị Excel/LibreOffice hiểu là công thức khi nhân viên mở file. Cách chặn:
- CSV: thêm `'` vào đầu ô chuỗi nguy hiểm.
- XLSX: không đổi nội dung, ép thành ô chuỗi và bật quotePrefix (openpyxl mặc định lưu chuỗi bắt đầu
  bằng `=` thành ô công thức).
- Import file CSV (parse_file) bỏ đúng một dấu `'` do export thêm, để export rồi nhập lại không làm dấu
  `'` lọt vào DB. File XLSX không bỏ gì (export XLSX không thêm dấu).
Không cần DB: chỉ chạy hàm thuần của file_parser.
"""
import csv
import io
import zipfile
from typing import Any

import openpyxl
import pytest
from fastapi import UploadFile

from scrapjd.api.services import file_parser
from scrapjd.api.services.export_query import _format_rows

_COLUMNS = ["job_id", "job_title", "ss_team_notes", "salary_min"]

# (giá trị gốc trong DB, ô trong file CSV). Tab và CR đứng đầu cũng là ký tự nguy hiểm.
_DANGEROUS = [
    ("=HYPERLINK(\"http://evil.example\",\"x\")", "'=HYPERLINK(\"http://evil.example\",\"x\")"),
    ("=1+1", "'=1+1"),
    ("+84912345678", "'+84912345678"),
    ("-Senior Dev", "'-Senior Dev"),
    ("@SUM(1+1)", "'@SUM(1+1)"),
    ("\tabc", "'\tabc"),
    ("\rabc", "'\rabc"),
    # Giá trị gốc đã có dấu nháy đầu rồi tới ký tự nguy hiểm: cũng chặn thêm một dấu để nhập lại không mất.
    ("'=1+1", "''=1+1"),
    ("''@x", "'''@x"),
]


# Với XLSX chỉ ca bắt đầu thẳng bằng ký tự nguy hiểm mới cần ép chuỗi (ca `'=1+1` là chuỗi thường).
_DANGEROUS_XLSX = [d for d in _DANGEROUS if not d[0].startswith("'")]


def _upload(filename: str) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO())


def _read_csv_cells(buffer: io.BytesIO) -> list[list[str]]:
    # utf-8-sig bỏ BOM; newline="" để ô có tab/CR không bị tách dòng sai.
    text = buffer.getvalue().decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline="")))


def _rows(title: Any) -> list[dict[str, Any]]:
    return [{"job_id": "id-1", "job_title": title, "ss_team_notes": None, "salary_min": -5}]


# ---------------------------------------------------------------------------
# neutralize_formula_text
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", _DANGEROUS)
def test_neutralize_prefixes_every_dangerous_start(raw, expected):
    assert file_parser.neutralize_formula_text(raw) == expected


@pytest.mark.parametrize("value", [
    "Data Analyst", "", " =1+1", "Việt Nam", "'90s", "'abc", "'", "1+1=2", "a=b", "90s", "2026-10-12",
    None, 0, -5, 3.5, True,
])
def test_neutralize_leaves_everything_else_alone(value):
    # Chuỗi không bắt đầu bằng ký tự nguy hiểm, và mọi giá trị không phải chuỗi (số âm là SỐ, không phải
    # chuỗi "-5") giữ nguyên; kể cả đúng object (None, True...).
    got = file_parser.neutralize_formula_text(value)
    assert got == value and type(got) is type(value)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", _DANGEROUS)
def test_csv_export_guards_dangerous_cells(raw, expected):
    cells = _read_csv_cells(file_parser.generate_export_file(_rows(raw), _COLUMNS, "csv"))
    assert cells[0] == _COLUMNS  # tiêu đề cột không bị đụng
    assert cells[1] == ["id-1", expected, "", "-5"]


def test_csv_export_guards_every_column_not_just_the_title():
    rows = [{"job_id": "=1", "job_title": "+2", "ss_team_notes": "-3", "salary_min": 7}]
    cells = _read_csv_cells(file_parser.generate_export_file(rows, _COLUMNS, "csv"))
    assert cells[1] == ["'=1", "'+2", "'-3", "7"]


def test_csv_export_keeps_normal_values_bom_and_vietnamese():
    buf = file_parser.generate_export_file(_rows("Nhân viên Kinh doanh"), _COLUMNS, "csv")
    assert buf.getvalue().startswith(b"\xef\xbb\xbf")
    assert _read_csv_cells(buf)[1] == ["id-1", "Nhân viên Kinh doanh", "", "-5"]


def test_csv_export_does_not_mutate_the_input_rows():
    rows = _rows("=1+1")
    file_parser.generate_export_file(rows, _COLUMNS, "csv")
    assert rows[0]["job_title"] == "=1+1"


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------

def _xlsx_sheet(buffer: io.BytesIO) -> Any:
    return openpyxl.load_workbook(io.BytesIO(buffer.getvalue())).active


@pytest.mark.parametrize("raw,_csv", _DANGEROUS_XLSX)
def test_xlsx_export_keeps_text_but_makes_it_a_quoted_string_cell(raw, _csv):
    cell = _xlsx_sheet(file_parser.generate_export_file(_rows(raw), _COLUMNS, "xlsx"))["B2"]
    # Nội dung KHÔNG đổi (khác CSV). Riêng CR: openpyxl ghi CR thô vào XML nên đọc lại thành LF (chuẩn hoá
    # xuống dòng của XML, có từ trước E2); bảo vệ vẫn đúng, nên so sau khi quy CR về LF.
    assert cell.value == raw.replace("\r", "\n")
    assert cell.data_type == "s"  # không phải "f" (công thức)
    assert cell.quotePrefix is True


def test_xlsx_export_leaves_a_leading_quote_value_as_a_plain_string():
    cell = _xlsx_sheet(file_parser.generate_export_file(_rows("'=1+1"), _COLUMNS, "xlsx"))["B2"]
    assert cell.value == "'=1+1" and cell.data_type == "s" and cell.quotePrefix is False


def test_xlsx_export_has_no_formula_element_in_sheet_xml():
    rows = [{"job_id": "=1+1", "job_title": "=cmd|' /C calc'!A0", "ss_team_notes": "@x", "salary_min": 1}]
    buf = file_parser.generate_export_file(rows, _COLUMNS, "xlsx")
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        sheet_xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "<f>" not in sheet_xml and "<f " not in sheet_xml


def test_xlsx_export_leaves_normal_cells_numbers_and_header_alone():
    sheet = _xlsx_sheet(file_parser.generate_export_file(_rows("Data Analyst"), _COLUMNS, "xlsx"))
    assert [c.value for c in sheet[1]] == _COLUMNS
    assert [c.quotePrefix for c in sheet[1]] == [False] * 4
    assert sheet["B2"].value == "Data Analyst" and sheet["B2"].quotePrefix is False
    assert sheet["D2"].value == -5 and sheet["D2"].data_type == "n"  # số âm vẫn là số


def test_unsupported_format_still_raises():
    with pytest.raises(file_parser.UnsupportedFileFormatError):
        file_parser.generate_export_file(_rows("x"), _COLUMNS, "pdf")


# ---------------------------------------------------------------------------
# Import: bỏ dấu chặn
# ---------------------------------------------------------------------------

def _parse_csv_text(text: str):
    return file_parser.parse_file(_upload("in.csv"), text.encode("utf-8"))


@pytest.mark.parametrize("guarded,original", [
    ("'=1+1", "=1+1"),
    ("'+84912345678", "+84912345678"),
    ("'-Senior", "-Senior"),
    ("'@x", "@x"),
    ("''=1+1", "'=1+1"),  # chỉ bỏ đúng MỘT dấu
    ("'''@x", "''@x"),
])
def test_parse_file_strips_one_guard_quote(guarded, original):
    df = _parse_csv_text(f"job_title\n{guarded}\n")
    assert df["job_title"].tolist() == [original]


@pytest.mark.parametrize("value", ["'90s", "'", "'abc", "abc'", "a'=b", "Data Analyst"])
def test_parse_file_keeps_quotes_that_are_not_a_guard(value):
    df = _parse_csv_text(f'job_title\n"{value}"\n')
    assert df["job_title"].tolist() == [value]


def test_parse_file_strips_guard_before_whitespace_strip():
    # Giá trị gốc "\tabc" được export thành "'\tabc"; nhập lại phải ra "abc" như mọi ô được strip.
    df = _parse_csv_text("job_title\n\"'\tabc\"\n")
    assert df["job_title"].tolist() == ["abc"]


def test_parse_xlsx_does_not_strip_a_leading_quote():
    # Export XLSX không thêm dấu chặn, nên dấu nháy đầu trong file XLSX là dữ liệu thật.
    rows = [{"job_id": "id-1", "job_title": "'=1+1", "ss_team_notes": None, "salary_min": 1}]
    buf = file_parser.generate_export_file(rows, _COLUMNS, "xlsx")
    df = file_parser.parse_file(_upload("job_export.xlsx"), buf.getvalue())
    assert df["job_title"].tolist() == ["'=1+1"]


def test_parse_file_does_not_touch_header_names():
    df = _parse_csv_text("'=weird_header,job_title\n1,x\n")
    assert "'=weird_header" in df.columns


# ---------------------------------------------------------------------------
# Vòng export -> import
# ---------------------------------------------------------------------------

_ROUND_TRIP_TITLES = [
    "=HYPERLINK(\"http://evil.example\",\"x\")", "+84912345678", "-Senior Dev", "@handle",
    "'90s style", "'=1+1", "''@x", "Data Analyst", "Việt Nam",
]


def test_csv_round_trip_gives_back_the_original_values():
    rows = [{"job_id": f"id-{i}", "job_title": t, "ss_team_notes": None, "salary_min": i}
            for i, t in enumerate(_ROUND_TRIP_TITLES)]
    buf = file_parser.generate_export_file(rows, _COLUMNS, "csv")
    df = file_parser.parse_file(_upload("job_export.csv"), buf.getvalue())
    assert list(df.columns) == _COLUMNS  # BOM của utf-8-sig không dính vào tên cột đầu
    assert df["job_title"].tolist() == _ROUND_TRIP_TITLES


def test_xlsx_round_trip_gives_back_the_original_values():
    # Không dùng số điện thoại: pandas.read_excel tự đổi chuỗi toàn chữ số thành số (có từ trước E2).
    titles = [t for t in _ROUND_TRIP_TITLES if t != "+84912345678"]
    rows = [{"job_id": f"id-{i}", "job_title": t, "ss_team_notes": None, "salary_min": i}
            for i, t in enumerate(titles)]
    buf = file_parser.generate_export_file(rows, _COLUMNS, "xlsx")
    df = file_parser.parse_file(_upload("job_export.xlsx"), buf.getvalue())
    assert df["job_title"].tolist() == titles


# ---------------------------------------------------------------------------
# Preview KHÔNG được thêm dấu chặn (chỉ file mới cần)
# ---------------------------------------------------------------------------

def test_export_preview_rows_are_not_guarded():
    # Bản xem trước trả JSON cho trang web hiển thị như chữ thường; chặn formula chỉ thuộc về bước ghi
    # file (file_parser.generate_export_file), không thuộc export_query.
    rows = [{"job_id": "id-1", "job_title": "=1+1", "company_name": "+Acme", "ss_team_notes": "-x"}]
    out = _format_rows(rows, "job")
    assert out[0]["job_title"] == "=1+1"
    assert out[0]["company_name"] == "+Acme"
    assert out[0]["ss_team_notes"] == "-x"
