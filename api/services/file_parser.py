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

    file: dùng để đọc `filename` (quyết định CSV hay XLSX theo đuôi file
    — KHÔNG dựa vào content_type vì trình duyệt/OS gửi content_type CSV
    không đồng nhất giữa các hệ điều hành, đuôi file đáng tin hơn).
    raw_bytes: nội dung file đã đọc sẵn (router đọc 1 lần qua
    `await file.read()` rồi truyền vào đây — UploadFile là file thực
    async, không tiện gọi trực tiếp trong hàm sync này).

    Để pandas tự infer type (không dùng dtype=str) — số đọc thành
    float64/int64, date string đọc thành object/string, empty cell đọc
    thành np.nan. Validation Engine (validation_engine.py) sẽ xử lý
    mixed types (int/float/str/None) và convert đúng schema từng entity.

    Empty cell -> np.nan -> convert về None ngay tại đây, để tầng sau
    (validation/conflict/insert) chỉ cần check `is None` một kiểu duy
    nhất, không phải vừa check None vừa check pd.isna()/np.nan.

    Raises:
        UnsupportedFileFormatError: đuôi file không phải csv/xlsx/xls.
        FileTooLargeError: số dòng dữ liệu (không tính header) > 5000.
    """
    filename = (file.filename or "").lower()

    if filename.endswith(".csv"):
        # KHÔNG dùng dtype=str — nếu dùng, pandas convert NaN thành string
        # literal "nan" thay vì giữ np.nan object, làm pd.notnull() không
        # nhận ra missing value, và validation_engine nhận raw_val="nan"
        # khi xử lý number field → int("nan") crash. Để pandas tự đọc type
        # tự nhiên (number → float64, date → object/string, empty → np.nan),
        # sau đó chuẩn hoá về None ở bước `df.where(pd.notnull(df), None)`
        # bên dưới — validation_engine.py mới là nơi convert đúng type theo
        # schema từng entity.
        df = pd.read_csv(BytesIO(raw_bytes), keep_default_na=True, encoding="utf-8")
    elif filename.endswith(".xlsx") or filename.endswith(".xls"):
        df = pd.read_excel(BytesIO(raw_bytes), engine="openpyxl")
    else:
        raise UnsupportedFileFormatError(
            "Unsupported file format. Please upload CSV or XLSX"
        )

    # Chuẩn hoá header: bỏ khoảng trắng thừa 2 đầu — Requirement 11.4
    # (chấp nhận header ở bất kỳ thứ tự nào) không nói gì về khoảng
    # trắng thừa, nhưng đây là lỗi copy-paste rất hay gặp thực tế (vd
    # " job_title" thay vì "job_title") nên chuẩn hoá luôn ở đây.
    df.columns = [str(c).strip() for c in df.columns]

    if len(df) > MAX_IMPORT_ROWS:
        raise FileTooLargeError(len(df))

    # NaN -> None: pandas dùng np.nan (float NaN) để biểu diễn missing
    # value, nhưng tầng sau (validation/conflict/insert) không muốn
    # phải import numpy và dùng pd.isna() mỗi lần check — chuẩn hoá hết
    # về None (Python native) ngay đây. where(pd.notnull(df), None) thay
    # thế mọi NaN bằng None.
    df = df.where(pd.notnull(df), None)

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
