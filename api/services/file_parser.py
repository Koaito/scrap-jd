"""
File Parser — đọc file CSV/XLSX upload thành pandas DataFrame (cho
import), và ngược lại sinh file CSV/XLSX từ list[dict] query từ DB (cho
export). Xem requirements.md Requirement 1 (Export) + Requirement 2
(Import Upload).

CHỈ lo phần "đọc/ghi file thô" — KHÔNG validate business rule (xem
validation_engine.py), KHÔNG detect conflict (xem conflict_detector.py).
Tách riêng để mỗi module chỉ có 1 lý do để đổi (đổi thư viện xử lý file
không đụng tới rule nghiệp vụ, và ngược lại).
"""

from io import BytesIO
from typing import Optional

import pandas as pd
from fastapi import UploadFile

MAX_IMPORT_ROWS = 5000


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
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()

    return df


def generate_export_file(rows: list[dict], columns: list[str], file_format: str) -> BytesIO:
    """Sinh file CSV/XLSX từ list[dict] record đã query từ DB.

    columns: thứ tự cột mong muốn trong file xuất ra (theo đúng tên cột
    DB — Requirement 11.1/11.2/11.3), truyền tường minh thay vì để
    pandas tự suy ra từ dict đầu tiên, vì dict có thể thiếu key ở 1 vài
    row (field NULL) khiến thứ tự cột không ổn định giữa các lần export.

    Trả BytesIO đã seek(0) về đầu, sẵn sàng cho router trả về qua
    StreamingResponse/Response.
    """
    df = pd.DataFrame(rows, columns=columns)

    buffer = BytesIO()
    if file_format == "csv":
        df.to_csv(buffer, index=False, encoding="utf-8")
    elif file_format == "xlsx":
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Sheet1")
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
