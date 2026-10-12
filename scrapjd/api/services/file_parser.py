"""
File Parser — đọc file CSV/XLSX upload thành pandas DataFrame (cho
import), và ngược lại sinh file CSV/XLSX từ list[dict] query từ DB (cho
export). Xem requirements.md Requirement 1 (Export) + Requirement 2
(Import Upload).

CHỈ lo phần "đọc/ghi file thô" — KHÔNG validate business rule (xem
validation_engine.py), KHÔNG detect conflict (xem conflict_detector.py).
Tách riêng để mỗi module chỉ có 1 lý do để đổi (đổi thư viện xử lý file
không đụng tới rule nghiệp vụ, và ngược lại).

Chống formula injection (E2, 10/2026): tiêu đề job, tên công ty, ghi chú... đến từ web crawl hoặc
nhập tay, nên có thể là chuỗi bắt đầu bằng `=`, `+`, `-`, `@`. Excel/LibreOffice coi ô như vậy là
công thức khi nhân viên mở file export. Chặn ở ĐÚNG chỗ ghi file (generate_export_file), không chặn
ở export_query: bản xem trước (preview) trả JSON cho trang web hiển thị như chữ thường, thêm dấu
chặn ở đó là làm bẩn dữ liệu hiển thị. Cách chặn theo định dạng:
- CSV: thêm dấu `'` vào đầu ô chuỗi bắt đầu bằng ký tự nguy hiểm (chuẩn OWASP).
- XLSX: KHÔNG đổi nội dung; ép ô thành ô chuỗi và bật `quotePrefix` (Excel hiểu như người dùng gõ
  dấu `'` trước nội dung). Ghi chú: openpyxl mặc định lưu chuỗi bắt đầu bằng `=` thành CÔNG THỨC.
- Import file CSV (parse_file) làm ngược lại: bỏ ĐÚNG một dấu `'` do export thêm, để file export sửa
  tay rồi nhập lại không làm dấu `'` lọt vào DB. File XLSX không bỏ gì vì export XLSX không thêm dấu.
"""

import re
from io import BytesIO
from typing import Any

import pandas as pd
from fastapi import UploadFile

MAX_IMPORT_ROWS = 5000

# Ký tự đầu ô khiến Excel/LibreOffice hiểu là công thức (danh sách của OWASP "CSV Injection": = + - @
# cùng tab và xuống dòng kiểu CR). Dùng chung cho export (thêm dấu chặn) và import (bỏ dấu chặn).
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")
_CSV_GUARD_PREFIX = "'"
_TRIGGER_CLASS = "[" + re.escape("".join(_FORMULA_TRIGGER_CHARS)) + "]"
_QUOTE = re.escape(_CSV_GUARD_PREFIX)
# Ô cần chặn: chuỗi bắt đầu bằng ký tự nguy hiểm, HOẶC bằng một hay nhiều dấu `'` rồi tới ký tự nguy
# hiểm (giá trị gốc `'=1+1`). Trường hợp sau phải chặn thêm để export rồi nhập lại không mất dữ liệu:
# `'=1+1` -> `''=1+1` -> (bỏ một dấu) `'=1+1`. Chuỗi như `'90s` hay `'abc` không khớp nên không bị đụng.
_NEEDS_GUARD_RE = re.compile("^" + _QUOTE + "*" + _TRIGGER_CLASS)
# Dấu chặn do export CSV thêm: đúng MỘT `'` ở đầu ô, theo sau là (các `'` rồi) ký tự nguy hiểm.
_CSV_GUARD_RE = re.compile("^" + _QUOTE + "(?=" + _QUOTE + "*" + _TRIGGER_CLASS + ")")
_EXPORT_SHEET_NAME = "Sheet1"


class UnsupportedFileFormatError(ValueError):
    """File không phải .csv/.xlsx/.xls — xem Requirement 10.2, message
    cố định "Unsupported file format. Please upload CSV or XLSX" để
    router trả đúng nguyên văn cho FE hiển thị."""


class FileTooLargeError(ValueError):
    """File vượt quá MAX_IMPORT_ROWS dòng — xem Requirement 2.3, message
    cố định "File exceeds maximum of 5000 rows"."""

    def __init__(self, row_count: int):
        self.row_count = row_count
        super().__init__(f"File exceeds maximum of 5000 rows (found {row_count})")


def parse_file(file: UploadFile, raw_bytes: bytes) -> pd.DataFrame:
    """Parse file CSV/XLSX upload thành DataFrame.

    file: dùng để đọc `filename` (quyết định CSV hay XLSX theo đuôi file).
    raw_bytes: nội dung file đã đọc sẵn.

    **STRATEGY: Read everything as strings** — Best practice từ production
    CSV import systems (xem pandas docs + CSVBox/Dromo architecture):
    
    - dtype=object: Đọc mọi cell thành string (không để pandas infer type)
    - keep_default_na=False: Empty cell → empty string "", KHÔNG phải NaN
    - Validation engine sẽ tự parse string → type đúng (int/date/email...)
    
    Lý do: Pandas infer type không đáng tin (mixed types, locale-dependent
    date parsing, float precision loss...). Control tốt nhất là đọc raw
    string + validate/convert trong code của mình.

    Raises:
        UnsupportedFileFormatError: đuôi file không phải csv/xlsx/xls.
        FileTooLargeError: số dòng dữ liệu > 5000.
    """
    filename = (file.filename or "").lower()

    if filename.endswith(".csv"):
        # dtype=object + keep_default_na=False: mọi cell thành string,
        # empty cell thành "" (không phải np.nan). Đơn giản và predictable.
        df = pd.read_csv(
            BytesIO(raw_bytes),
            dtype=object,
            keep_default_na=False,
            encoding="utf-8"
        )
    elif filename.endswith(".xlsx") or filename.endswith(".xls"):
        # Excel: pandas vẫn tự infer type (không có dtype= cho read_excel),
        # nhưng sẽ convert về string ở bước sau.
        df = pd.read_excel(BytesIO(raw_bytes), engine="openpyxl")
        # Convert mọi cell thành string, NaN thành empty string.
        df = df.astype(object).fillna("")
    else:
        raise UnsupportedFileFormatError(
            "Unsupported file format. Please upload CSV or XLSX"
        )

    # Chuẩn hoá header: strip khoảng trắng.
    df.columns = [str(c).strip() for c in df.columns]

    if len(df) > MAX_IMPORT_ROWS:
        raise FileTooLargeError(len(df))

    # Convert mọi cell thành string và strip whitespace.
    # Empty string sẽ được validation_engine convert thành None.
    # File CSV: bỏ dấu chặn formula do export thêm (E2) TRƯỚC khi strip, để giá trị ra đúng như trong DB:
    # giá trị gốc `\tabc` được export thành `'\tabc`, và nhập lại thì strip như mọi ô khác. Chỉ CSV: export
    # XLSX không thêm dấu nên một ô XLSX bắt đầu bằng `'` là dữ liệu thật.
    is_csv = filename.endswith(".csv")
    for col in df.columns:
        text = df[col].astype(str)
        if is_csv:
            text = text.str.replace(_CSV_GUARD_RE, "", regex=True)
        df[col] = text.str.strip()

    return df


def neutralize_formula_text(value: Any) -> Any:
    """Giá trị 1 ô của file CSV export, đã chặn formula injection (E2).

    Chỉ đụng chuỗi khớp _NEEDS_GUARD_RE (bắt đầu bằng ký tự trong _FORMULA_TRIGGER_CHARS, có thể sau
    vài dấu `'`): thêm `'` vào đầu. Số, None, ngày (đã thành chuỗi bắt đầu bằng chữ số) và chuỗi thường
    giữ nguyên. Số âm là kiểu số chứ không phải chuỗi nên `-5` ở cột lương không bị thêm dấu."""
    if isinstance(value, str) and _NEEDS_GUARD_RE.match(value):
        return _CSV_GUARD_PREFIX + value
    return value


def _protect_formula_cells(worksheet: Any) -> None:
    """Ép các ô chuỗi bắt đầu bằng ký tự nguy hiểm thành ô chuỗi thật có `quotePrefix` (E2).

    Không đổi nội dung ô. openpyxl lưu chuỗi bắt đầu bằng `=` thành ô công thức (data_type "f"), nên
    phải đặt lại thành "s". `quotePrefix` giữ ô là chữ cả khi nhân viên bấm sửa ô rồi Enter (Excel
    không tự dịch lại thành công thức). Bỏ qua dòng 1 (tiêu đề cột là tên cố định của mình).

    worksheet khai `Any` vì openpyxl không có type stubs trong requirements-dev."""
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith(_FORMULA_TRIGGER_CHARS):
                cell.data_type = "s"
                cell.quotePrefix = True


def generate_export_file(rows: list[dict[str, Any]], columns: list[str], file_format: str) -> BytesIO:
    """Sinh file CSV/XLSX từ list[dict] record đã query từ DB.

    columns: thứ tự cột mong muốn trong file xuất ra (theo đúng tên cột
    DB — Requirement 11.1/11.2/11.3), truyền tường minh thay vì để
    pandas tự suy ra từ dict đầu tiên, vì dict có thể thiếu key ở 1 vài
    row (field NULL) khiến thứ tự cột không ổn định giữa các lần export.

    Ô chuỗi bắt đầu bằng `=`, `+`, `-`, `@`, tab, CR được chặn formula injection (xem docstring đầu
    module): CSV thêm `'` vào đầu ô, XLSX ép thành ô chuỗi có quotePrefix.

    Trả BytesIO đã seek(0) về đầu, sẵn sàng cho router trả về qua
    StreamingResponse/Response.
    """
    buffer = BytesIO()
    if file_format == "csv":
        safe_rows = [{key: neutralize_formula_text(val) for key, val in row.items()} for row in rows]
        df = pd.DataFrame(safe_rows, columns=columns)
        # utf-8-sig (KHÔNG phải "utf-8" trần) — thêm BOM (Byte Order Mark,
        # 3 byte EF BB BF) ở đầu file CSV. Thiếu BOM, file CSV UTF-8 vẫn
        # ĐÚNG về mặt byte, nhưng Excel (mặc định trên máy Windows/macOS
        # tiếng Việt) không tự nhận ra là UTF-8 khi mở .csv — nó đoán theo
        # bảng mã ANSI/Windows-1252 của hệ thống, khiến mọi ký tự tiếng
        # Việt (chiếm 2-3 byte trong UTF-8) bị tách lẻ từng byte rồi map
        # sai sang ký tự Latin-1/cp1252, ra chữ rác kiểu "Việt Nam" ->
        # "Viá»‡t Nam" (staff báo lỗi 08/2026, xem ảnh chụp export CSV).
        # Có BOM, Excel tự động nhận diện UTF-8 và hiển thị đúng ngay khi
        # double-click mở file — không cần import CSV thủ công/tự chọn
        # encoding UTF-8 mỗi lần. XLSX (nhánh dưới) KHÔNG bị lỗi này —
        # openpyxl lưu text trong XML Unicode chuẩn (<sharedStrings.xml>),
        # không phụ thuộc Excel đoán bảng mã, nên không cần sửa gì.
        df.to_csv(buffer, index=False, encoding="utf-8-sig")
    elif file_format == "xlsx":
        df = pd.DataFrame(rows, columns=columns)
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name=_EXPORT_SHEET_NAME)
            _protect_formula_cells(writer.sheets[_EXPORT_SHEET_NAME])
    else:
        raise UnsupportedFileFormatError(
            "Unsupported file format. Please upload CSV or XLSX"
        )
    buffer.seek(0)
    return buffer


def content_type_for_format(file_format: str) -> str:
    return {
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }[file_format]
