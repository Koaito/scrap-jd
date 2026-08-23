"""
Import Executor — nhận preview_data (đã lưu trong import_previews) +
resolution map từ staff (Skip/Update/Create mỗi dòng, company_id đã chọn
cho dòng "pending_company_resolution", xác nhận reactivate cho dòng
"conflict_inactive") -> thực thi INSERT/UPDATE thật trong 1 DATABASE
TRANSACTION (Requirement 6.1: tất cả hoặc không gì cả).

KHÔNG tự conn.commit()/rollback() ở đây — router (nơi có transaction
boundary rõ ràng, giống mọi route ghi khác trong codebase) chịu trách
nhiệm commit sau khi execute_import() trả về thành công, hoặc để
exception tự nổi lên cho router rollback + trả lỗi (Requirement 6.2,
6.8: giữ nguyên Preview_Session nếu fail để retry).
"""

from dataclasses import dataclass
from typing import Optional

import db as db_module
from api.services import company_resolver, conflict_detector
from api.services.entity_specs import get_spec
from api.services.validation_engine import validate_single_field
from constants import LEVEL_CODE_VALUES


BATCH_PROPAGATING_ACTIONS: dict[str, dict[str, str]] = {
    # "keep_this"   : giữ dòng đang gửi resolution, bỏ dòng kia (2 dòng
    #                 là CÙNG 1 người, dòng này là bản đúng).
    # "keep_other"  : ngược lại — bỏ dòng đang gửi, giữ dòng kia.
    # "import_both" : xác nhận 2 dòng là 2 người KHÁC NHAU, giữ cả 2 (match
    #                 mờ chỉ là trùng ngẫu nhiên 1 vài field, không phải
    #                 trùng người).
    #
    # THIẾT KẾ MỞ RỘNG (thêm 08/2026 — xem _expand_conflict_in_batch_
    # resolutions() bên dưới): đây là NGUỒN SỰ THẬT DUY NHẤT khai báo mọi
    # action lan truyền + hiệu ứng của nó lên dòng đang gửi ("self") và
    # dòng bị match ("other") trong 1 cặp conflict_in_batch. Muốn thêm 1
    # action lan truyền mới trong tương lai (vd 1 action "cả 2 là chi
    # nhánh của cùng 1 công ty, gộp thông tin") — CHỈ cần thêm 1 entry
    # {"self": <skip|create>, "other": <skip|create>} vào dict này, KHÔNG
    # cần sửa bất kỳ nhánh if/elif nào ở _expand_conflict_in_batch_
    # resolutions() hay _apply_conflict_action(): 2 hàm đó chỉ đọc action
    # THUẦN (skip/create/update) đã được chuẩn hoá, không bao giờ tự biết
    # tên action lan truyền là gì. "self"/"other" hiện chỉ nhận "skip"
    # hoặc "create" — "update" KHÔNG hợp lệ ở đây vì conflict_in_batch
    # không có existing_record thật (xem check action=="update" phía
    # dưới trong execute_import).
    "keep_this": {"self": "create", "other": "skip"},
    "keep_other": {"self": "skip", "other": "create"},
    "import_both": {"self": "create", "other": "create"},
}


class RowResolutionError(Exception):
    """1 dòng thiếu thông tin bắt buộc để thực thi (vd action=Update cho
    dòng pending_company_resolution mà staff chưa chọn company_id nào,
    hoặc action=Update cho dòng conflict_inactive mà chưa xác nhận
    confirm_reactivate, hoặc action != skip cho dòng needs_field_fix mà
    staff chưa gửi đủ/đúng field_fixes — xem _apply_field_fixes() bên
    dưới) — router bắt exception này, trả 422 rõ nguyên nhân TRƯỚC KHI
    chạm transaction (thực ra được raise NGAY BÊN TRONG transaction nên
    vẫn rollback sạch, không cần router tự kiểm tra trước — chỉ cần bắt
    và convert sang HTTPException)."""


@dataclass
class ImportSummary:
    created: int = 0
    updated: int = 0
    skipped: int = 0


def execute_import(
    conn,
    *,
    entity_type: str,
    preview_rows: list[dict],
    resolutions: dict[str, dict],
    actor_id: str,
) -> ImportSummary:
    """resolutions: {str(row_index): {"action": "skip"|"update"|"create"
    (dòng conflict_status="conflict_in_batch" nhận thêm 3 giá trị action
    LAN TRUYỀN — xem BATCH_PROPAGATING_ACTIONS +
    _expand_conflict_in_batch_resolutions() bên dưới),
    "company_id": "<uuid>" (optional, cho dòng pending_company_resolution),
    "confirm_reactivate": bool (optional, cho dòng conflict_inactive),
    "level_code": "<Intern|Fresher|...|Manager>" (optional, BẮT BUỘC nếu
    dòng needs_level_resolve=true và action != "skip" — xem check ngay
    đầu vòng lặp bên dưới)}}.

    Requirement 5.6: dòng conflict KHÔNG có resolution -> mặc định Skip.
    Dòng no_conflict KHÔNG cần resolution -> LUÔN tạo mới (Requirement 6.3).
    """
    # conflict_in_batch — action lan truyền (thêm 08/2026): giãn resolutions
    # TRƯỚC KHI vào vòng lặp chính, để mọi logic bên dưới (check "thiếu
    # resolution", check action=="update", _apply_conflict_action, ...)
    # KHÔNG cần biết gì về action lan truyền — chúng chỉ thấy action
    # skip/create/update thuần, y hệt trước khi có tính năng này. Xem
    # docstring _expand_conflict_in_batch_resolutions() để hiểu đầy đủ.
    resolutions = _expand_conflict_in_batch_resolutions(preview_rows, resolutions)

    summary = ImportSummary()

    for row in preview_rows:
        row_index = row["row_index"]
        data = dict(row["data"])
        status = row["conflict_status"]
        resolution = resolutions.get(str(row_index), {})
        action = resolution.get("action", "skip")

        # conflict_in_batch (08/2026, hiện chỉ Contact — xem preview_
        # manager.apply_field_fix() + conflict_detector.
        # find_duplicate_rows_in_batch()): dòng KHÔNG trùng gì trong DB
        # nhưng trùng với 1 dòng KHÁC trong CHÍNH file import. KHÁC mọi
        # status khác ở trên — KHÔNG cho phép mặc định Skip âm thầm khi
        # thiếu resolution (default action="skip" ở dòng trên vốn hợp lý
        # cho "conflict" thường theo Requirement 5.6, nhưng ở đây im
        # lặng bỏ qua rất dễ làm mất 1 trong 2 dòng dữ liệu thật mà staff
        # không hề hay biết) -> resolutions BẮT BUỘC có entry TƯỜNG MINH
        # cho row_index này, dù ý staff có là Skip đi nữa.
        #
        # Có 2 CÁCH để entry đó tồn tại lúc chạy tới đây (đã bị
        # _expand_conflict_in_batch_resolutions() ở đầu hàm chuẩn hoá hết
        # về skip/create thuần trước khi tới vòng lặp này):
        #   (1) staff/FE tự gửi resolution RIÊNG cho CẢ 2 dòng trong cặp
        #       (action "skip" hoặc "create" cho từng dòng độc lập) —
        #       cách gốc, vẫn được hỗ trợ nguyên vẹn.
        #   (2) staff/FE chỉ gửi 1 resolution DUY NHẤT dùng action LAN
        #       TRUYỀN (BATCH_PROPAGATING_ACTIONS: "keep_this"/
        #       "keep_other"/"import_both") cho 1 trong 2 dòng — backend
        #       tự suy ra + điền resolution cho dòng kia (lấy dòng kia từ
        #       duplicate_in_batch.other_row_index). Nếu CẢ 2 dòng cùng
        #       được gửi resolution (dù thuần hay lan truyền) mà mâu
        #       thuẫn nhau (vd dòng này "keep_this" nhưng dòng kia lại
        #       "create" — tức cả 2 cùng đòi giữ) thì
        #       _expand_conflict_in_batch_resolutions() đã raise
        #       RowResolutionError từ trước khi tới được đây.
        #
        # action hợp lệ ở TẦNG NÀY (sau khi đã giãn) chỉ còn "skip" (dòng
        # này là dòng trùng, bỏ qua) hoặc "create" (giữ dòng này, chạy
        # BÌNH THƯỜNG qua _apply_conflict_action() ở dưới, vì case này
        # KHÔNG có existing_record thật để tạo xung đột gì thêm). "update"
        # KHÔNG hợp lệ — không có existing_record (dòng "kia" chỉ là 1
        # dòng khác trong file, không phải record đã có trong DB, không
        # có gì để update) — action lan truyền không bao giờ tự sinh ra
        # "update" (xem BATCH_PROPAGATING_ACTIONS, chỉ map về skip/create),
        # nên check bên dưới chỉ còn bắt được trường hợp staff GỬI TAY
        # action="update" trực tiếp.
        if status == "conflict_in_batch":
            if str(row_index) not in resolutions:
                other_index = (row.get("duplicate_in_batch") or {}).get("other_row_index")
                other_label = f"dòng {other_index + 1}" if isinstance(other_index, int) else "1 dòng khác"
                raise RowResolutionError(
                    f"Dòng {row_index + 1}: phát hiện trùng với {other_label} trong "
                    f"cùng file import — cần staff tự chọn rõ ràng (Skip nếu đây là "
                    f"dòng trùng lặp, Create nếu xác nhận đây là người khác, hoặc dùng "
                    f"1 action lan truyền 'keep_this'/'keep_other'/'import_both' cho "
                    f"CẢ CẶP qua resolution của 1 trong 2 dòng) trước khi xác nhận, "
                    f"không được để mặc định."
                )
            if action == "update":
                raise RowResolutionError(
                    f"Dòng {row_index + 1}: action 'update' không hợp lệ cho dòng "
                    f"conflict_in_batch — dòng trùng nằm trong CHÍNH file import, "
                    f"không phải record đã có trong DB, không có gì để update."
                )

        # needs_level_resolve (chỉ Job, 08/2026 — xem preview_manager.py::
        # build_preview): level_code trong file không khớp danh sách hợp
        # lệ (dù đã chuẩn hoá hoa/thường) -> validation_engine.py đã set
        # data["level_code"] = None ngay từ preview. Bắt buộc staff chọn
        # lại qua resolution["level_code"] TRƯỚC KHI tạo/sửa job — action
        # "skip" thì bỏ qua check này (đằng nào cũng không ghi gì).
        # KHÔNG default âm thầm về None/1 giá trị nào đó: level sai/thiếu
        # là lỗi dữ liệu staff cần xác nhận tay, khác company (không chọn
        # gì -> tự hiểu "tạo company mới theo tên trong file", vì company
        # LUÔN có thể tạo mới hợp lệ; còn level chỉ có đúng 7 giá trị cố
        # định, "level rỗng" không phải 1 lựa chọn nghiệp vụ hợp lệ).
        if entity_type == "job" and row.get("needs_level_resolve") and action != "skip":
            chosen_level = resolution.get("level_code")
            if not chosen_level or chosen_level not in LEVEL_CODE_VALUES:
                raise RowResolutionError(
                    f"Dòng {row_index + 1}: level_code trong file "
                    f"({row.get('level_code_raw')!r}) không khớp danh sách hợp lệ "
                    f"{LEVEL_CODE_VALUES} — cần chọn lại 1 giá trị trước khi xác nhận."
                )
            data["level_code"] = chosen_level

        # needs_field_fix (08/2026, mọi entity — xem preview_manager.py::
        # build_preview): dòng có field lỗi type/required/business-rule
        # lúc build preview (data[field] đã bị set None cho field lỗi).
        # Bắt buộc staff gửi field_fixes cho MỌI field còn lỗi trước khi
        # tạo/sửa (action "skip" thì bỏ qua, đằng nào cũng không ghi gì) —
        # re-validate lại bằng đúng validate_single_field() dùng lúc build
        # preview, không tin ngầm dữ liệu FE gửi lên.
        if row.get("needs_field_fix") and action != "skip":
            _apply_field_fixes(entity_type, row, data, resolution)

        # Job/Contact: company đã resolve XONG lúc build preview (mọi
        # status TRỪ "pending_company_resolution") -> gắn company_id đã
        # biết vào data ngay từ đây, để _create_row/_update_row dùng
        # thẳng thay vì phải đọc lại company_resolution mỗi nơi.
        if entity_type in ("job", "contact") and status != "pending_company_resolution":
            company_resolution = row.get("company_resolution") or {}
            if company_resolution.get("company_id"):
                data["company_id"] = company_resolution["company_id"]

        if status == "no_conflict":
            _create_row(conn, entity_type, data, row, resolution, actor_id)
            summary.created += 1
            continue

        if status == "pending_company_resolution":
            # Staff đã chọn 1 company trong danh sách gợi ý -> dùng
            # company_id đó. Không chọn gì (bỏ trống) -> hiểu là "không
            # công ty gợi ý nào đúng, tạo company mới theo company_name
            # trong file" (theo đúng quyết định thiết kế), KHÔNG chặn
            # lại bắt staff phải chọn.
            company_id = resolution.get("company_id") or _resolve_company_id_for_create(
                conn, data, actor_id
            )
            data["company_id"] = company_id
            # Re-check conflict NGAY LÚC NÀY với company_id thật vừa chọn
            # (lúc build preview chưa biết company_id nên chưa detect được
            # — xem docstring conflict_detector.py).
            real_status, existing = _recheck_conflict(conn, entity_type, data)
            if real_status == "no_conflict":
                _create_row(conn, entity_type, data, row, resolution, actor_id)
                summary.created += 1
            else:
                _apply_conflict_action(
                    conn, entity_type, data, existing, real_status,
                    action, resolution, actor_id, summary,
                )
            continue

        # status in ("conflict", "conflict_inactive", "conflict_in_batch")
        # — conflict_in_batch không có existing_record thật (luôn None,
        # xem preview_manager.apply_field_fix()), nhưng vẫn chạy qua
        # ĐÚNG _apply_conflict_action() vì action skip/create không đụng
        # existing_record; action=update đã bị chặn ở check phía trên.
        existing = row.get("existing_record")
        _apply_conflict_action(
            conn, entity_type, data, existing, status, action, resolution, actor_id, summary,
        )

    return summary


def _expand_conflict_in_batch_resolutions(
    preview_rows: list[dict], resolutions: dict[str, dict],
) -> dict[str, dict]:
    """Giãn (expand) resolutions gốc từ staff/FE: mọi entry dùng 1 action
    LAN TRUYỀN trong BATCH_PROPAGATING_ACTIONS ("keep_this"/"keep_other"/
    "import_both") cho 1 dòng conflict_status="conflict_in_batch" sẽ được:

    1. Chuẩn hoá action của CHÍNH dòng đó về "skip"/"create" thuần (theo
       effect["self"]), để mọi logic execute_import() phía sau (kể cả
       _apply_conflict_action()) không cần biết action lan truyền là gì.
    2. Tự tạo (hoặc đối chiếu, nếu đã có sẵn) resolution cho dòng KIA
       (lấy từ row["duplicate_in_batch"]["other_row_index"]) theo
       effect["other"] — staff KHÔNG BẮT BUỘC phải tự gửi resolution cho
       dòng kia nữa (khác hành vi gốc trước 08/2026, vốn bắt buộc gửi đủ
       2 entry riêng — cách đó VẪN được hỗ trợ song song, xem bên dưới).

    Thuần Python, KHÔNG đụng DB (giống find_duplicate_rows_in_batch()) —
    chỉ đọc preview_rows (đã có sẵn duplicate_in_batch từ preview_manager)
    + resolutions do caller truyền vào, KHÔNG mutate resolutions gốc (trả
    dict MỚI) để an toàn nếu caller còn dùng resolutions gốc cho việc
    khác (vd log/audit nguyên văn request staff gửi).

    Quy tắc AN TOÀN (ưu tiên báo lỗi rõ ràng hơn đoán sai dữ liệu):
    - Action lan truyền CHỈ hợp lệ cho dòng đang ở đúng conflict_status=
      "conflict_in_batch" và có duplicate_in_batch.other_row_index trỏ
      tới 1 dòng CÒN TỒN TẠI trong preview_rows.
    - Liên kết PHẢI mutual (dòng kia cũng đang trỏ ngược lại đúng dòng
      này) — bảo vệ case 3+ dòng cùng match nhau (A-B-C), vốn CHƯA được
      xử lý triệt để ở tầng detect (xem conflict_detector.
      find_duplicate_rows_in_batch()/preview_manager._clear_batch_link()
      docstring). Không mutual -> raise, bắt staff resolve riêng từng
      dòng bằng skip/create thuần (an toàn hơn tự suy luận sai).
    - Nếu dòng kia CŨNG có resolution riêng (staff gửi đủ cả 2, theo
      cách gốc, hoặc gửi cả 2 dưới dạng action lan truyền) mà HIỆU LỰC
      (effective action skip/create thật sự, sau khi tự quy đổi nếu bản
      thân nó cũng là action lan truyền) mâu thuẫn với action lan truyền
      của dòng đang xét -> raise RowResolutionError, KHÔNG tự chọn bên
      nào đúng.
    - Kết quả nhất quán dù duyệt theo thứ tự nào (idempotent): nếu CẢ 2
      dòng trong 1 cặp đều gửi action lan truyền tương thích nhau (vd
      dòng A "keep_this" và dòng B "keep_other" — cùng ngụ ý A=create,
      B=skip), hàm này không báo lỗi, dù dòng nào được xử lý trước.
    """
    rows_by_index = {r["row_index"]: r for r in preview_rows}
    expanded = {key: dict(value) for key, value in resolutions.items()}

    def _effective_self_action(raw_action) -> Optional[str]:
        """Quy action bất kỳ (thuần hoặc lan truyền) về hiệu lực THẬT SỰ
        của nó lên chính dòng sở hữu action đó — dùng để so sánh 2 phía
        của 1 cặp có mâu thuẫn hay không. Trả None nếu raw_action không
        nhận diện được (vd giá trị rác/sai chính tả) — để lại cho check
        action hợp lệ thông thường ở execute_import() xử lý, hàm này
        không cần tự báo lỗi thay."""
        if raw_action in ("skip", "create", "update"):
            return raw_action
        if raw_action in BATCH_PROPAGATING_ACTIONS:
            return BATCH_PROPAGATING_ACTIONS[raw_action]["self"]
        return None

    for row_key, resolution in resolutions.items():
        action = resolution.get("action")
        if action not in BATCH_PROPAGATING_ACTIONS:
            continue

        try:
            row_index = int(row_key)
        except (TypeError, ValueError):
            raise RowResolutionError(
                f"Resolution key '{row_key}' không phải row_index hợp lệ."
            )

        row = rows_by_index.get(row_index)
        if row is None:
            raise RowResolutionError(
                f"Dòng {row_key}: không tồn tại trong preview này (dữ liệu "
                f"preview có thể đã lệch, hãy tải lại)."
            )
        row_label = row_index + 1

        if row.get("conflict_status") != "conflict_in_batch":
            raise RowResolutionError(
                f"Dòng {row_label}: action '{action}' chỉ áp dụng cho dòng có "
                f"conflict_status='conflict_in_batch' (trùng với 1 dòng khác "
                f"trong CHÍNH file import) — dòng này đang ở trạng thái "
                f"'{row.get('conflict_status')}', dùng action skip/update/"
                f"create như bình thường."
            )

        link = row.get("duplicate_in_batch") or {}
        other_index = link.get("other_row_index")
        if other_index is None:
            raise RowResolutionError(
                f"Dòng {row_label}: thiếu duplicate_in_batch.other_row_index — "
                f"không xác định được dòng kia để áp dụng action lan truyền "
                f"'{action}'."
            )

        other_row = rows_by_index.get(other_index)
        other_label = other_index + 1
        if other_row is None:
            raise RowResolutionError(
                f"Dòng {row_label}: dòng liên kết (dòng {other_label}) không "
                f"tồn tại trong preview này — dữ liệu preview có thể đã lệch, "
                f"hãy tải lại."
            )

        other_link = other_row.get("duplicate_in_batch") or {}
        mutual = (
            other_row.get("conflict_status") == "conflict_in_batch"
            and other_link.get("other_row_index") == row_index
        )
        if not mutual:
            raise RowResolutionError(
                f"Dòng {row_label}: liên kết trùng-trong-batch với dòng "
                f"{other_label} không còn khớp 2 chiều (có thể dòng "
                f"{other_label} đã đổi sang match 1 dòng khác từ 1 lần sửa "
                f"field khác) — không thể tự áp dụng action lan truyền "
                f"'{action}' một cách an toàn. Hãy resolve riêng từng dòng "
                f"bằng action skip/create."
            )

        effect = BATCH_PROPAGATING_ACTIONS[action]
        self_action = effect["self"]
        other_action = effect["other"]

        # (1) Chuẩn hoá entry của CHÍNH dòng này về action thuần.
        expanded[row_key] = {**resolution, "action": self_action}

        # (2) Đối chiếu/điền resolution cho dòng KIA.
        other_key = str(other_index)
        other_original = resolutions.get(other_key)
        if other_original is not None:
            other_original_action = other_original.get("action")
            other_effective = _effective_self_action(other_original_action)
            if other_effective is not None and other_effective != other_action:
                raise RowResolutionError(
                    f"Dòng {row_label} và dòng {other_label} đang trùng nhau "
                    f"trong CHÍNH file import, nhưng resolution mâu thuẫn "
                    f"nhau: dòng {row_label} chọn action '{action}' (ngụ ý "
                    f"dòng {other_label} phải là '{other_action}'), trong khi "
                    f"dòng {other_label} lại được gửi resolution action="
                    f"'{other_original_action}' (hiệu lực '{other_effective}'). "
                    f"Sửa lại cho khớp nhau, hoặc chỉ gửi resolution cho 1 "
                    f"trong 2 dòng và để backend tự áp dụng cho dòng còn lại."
                )
            # Khớp nhau (hoặc other_original_action không nhận diện được,
            # để check action hợp lệ thông thường phía dưới tự bắt lỗi) —
            # chuẩn hoá luôn entry dòng kia về action thuần, GIỮ nguyên
            # các field khác staff đã gửi riêng cho dòng đó (vd field_fixes
            # nếu dòng kia cũng đang needs_field_fix).
            expanded[other_key] = {
                **other_original,
                "action": other_effective or other_original_action,
            }
        else:
            # Dòng kia KHÔNG có resolution riêng -> tự suy ra + điền vào,
            # đúng tinh thần action lan truyền: staff chỉ cần 1 lựa chọn
            # cho cả cặp. "_propagated_from" chỉ để debug/audit (vd log
            # lỗi RowResolutionError sau đó, như _apply_field_fixes() nếu
            # dòng kia vẫn còn needs_field_fix chưa được sửa) — KHÔNG có ý
            # nghĩa gì với logic thực thi phía dưới, an toàn nếu bị bỏ qua.
            expanded[other_key] = {"action": other_action, "_propagated_from": row_index}

    return expanded


def _apply_conflict_action(conn, entity_type, data, existing, status, action, resolution, actor_id, summary):
    if action == "skip":
        summary.skipped += 1
        return

    if action == "create":
        _create_row(conn, entity_type, data, {"row_index": None}, resolution, actor_id)
        summary.created += 1
        return

    if action == "update":
        if status == "conflict_inactive" and not resolution.get("confirm_reactivate"):
            raise RowResolutionError(
                "Dòng trùng với record đang ở trạng thái ngừng hoạt động (inactive) — "
                "cần xác nhận confirm_reactivate=true để ghi đè và kích hoạt lại."
            )
        _update_row(conn, entity_type, data, existing, resolution, actor_id,
                    reactivate=(status == "conflict_inactive"))
        summary.updated += 1
        return

    raise RowResolutionError(f"action '{action}' không hợp lệ (chỉ nhận skip/update/create)")


def _apply_field_fixes(entity_type: str, row: dict, data: dict, resolution: dict) -> None:
    """Re-validate resolution["field_fixes"] (giá trị staff sửa trực tiếp
    trên bảng preview cho dòng needs_field_fix) TRƯỚC KHI ghi vào `data`
    dùng để tạo/sửa record thật — KHÔNG tin ngầm FE đã validate đúng (dù
    FE cũng validate phía client, request có thể bị sửa tay/replay tới
    thẳng API). Dùng lại validate_single_field() Y HỆT hàm
    validate_dataframe() dùng lúc build preview, để 2 nơi không lệch
    logic convert theo type.

    Raise RowResolutionError (router convert sang 422) nếu thiếu field
    trong field_fixes, để trống, hoặc giá trị vẫn không hợp lệ sau khi
    sửa — GIỐNG cách needs_level_resolve chặn confirm phía trên."""
    field_errors = row.get("field_errors") or {}
    if not field_errors:
        return

    row_label = row["row_index"] + 1
    spec = get_spec(entity_type)
    field_fixes = resolution.get("field_fixes") or {}

    for fname in field_errors:
        if fname not in field_fixes:
            raise RowResolutionError(
                f"Dòng {row_label}: cột '{fname}' vẫn còn lỗi "
                f"({field_errors[fname]['message']}) — cần điền field_fixes['{fname}'] "
                f"trước khi xác nhận import."
            )
        raw = str(field_fixes[fname]).strip()
        if raw == "":
            raise RowResolutionError(
                f"Dòng {row_label}: cột '{fname}' là bắt buộc, không được để trống."
            )
        value, err = validate_single_field(spec, fname, raw)
        if err is not None:
            raise RowResolutionError(
                f"Dòng {row_label}, cột '{fname}': {err['message']}"
            )
        data[fname] = value

    # Business rule liên trường (Job: salary_min/salary_max) có thể bị vi
    # phạm LẠI sau khi áp field_fixes (vd staff sửa salary_min lớn hơn
    # salary_max cũ vẫn giữ nguyên) dù từng field riêng lẻ giờ hợp lệ về
    # type — re-check để không lọt 1 job có salary_max < salary_min vào DB.
    if entity_type == "job":
        salary_min = data.get("salary_min")
        salary_max = data.get("salary_max")
        if salary_min is not None and salary_min < 0:
            raise RowResolutionError(
                f"Dòng {row_label}, cột 'salary_min': phải >= 0, nhận được {salary_min}"
            )
        if salary_min is not None and salary_max is not None and salary_max < salary_min:
            raise RowResolutionError(
                f"Dòng {row_label}: salary_max ({salary_max}) phải >= salary_min ({salary_min})"
            )


def _recheck_conflict(conn, entity_type, data) -> tuple[str, Optional[dict]]:
    if entity_type == "job":
        # Dùng TÊN CÔNG TY THẬT (tra theo company_id staff vừa chọn ở
        # bước preview, xem preview_manager.resolve_company_selection()),
        # KHÔNG dùng data["company_name"] gốc trong file — detect_job_
        # conflict() match theo company_name dạng text (JOIN companies rồi
        # so lower(company_name), không dùng company_id trực tiếp — xem
        # conflict_detector.py), nên nếu staff chọn 1 company có tên hơi
        # khác tên gốc trong file (vd file ghi "FPT" nhưng chọn "FPT
        # Software") thì check bằng company_name gốc sẽ SAI — kết quả
        # confirm-time lệch với kết quả preview-time (staff thấy
        # "no_conflict" lúc preview nhưng conflict lúc confirm, hoặc ngược
        # lại). company_id["company_id"] LUÔN có ở đây (đã gán ngay trước
        # khi gọi hàm này, xem execute_import() nhánh
        # pending_company_resolution) — company mới tạo (company_id chưa
        # từng tồn tại trước đó) thì get_company() trả None, fallback về
        # company_name gốc (company mới tạo chắc chắn no_conflict với
        # bất kỳ tên nào, nên fallback này không ảnh hưởng kết quả).
        company_name = data.get("company_name")
        company_id = data.get("company_id")
        if company_id:
            company = company_resolver.get_company(conn, company_id)
            if company is not None:
                company_name = company["company_name"]
        result = conflict_detector.detect_job_conflict(
            conn, company_name, data.get("job_title"), data.get("deadline")
        )
    elif entity_type == "contact":
        result = conflict_detector.detect_contact_conflict(
            conn, data.get("company_id"), data.get("contact_name"), data.get("work_email")
        )
    else:
        result = {"conflict_status": "no_conflict"}
    return result["conflict_status"], result.get("existing_record")


def _create_row(conn, entity_type, data, row, resolution, actor_id):
    if entity_type == "company":
        province_id = (
            db_module.get_or_create_province(conn, data["province_name"])
            if data.get("province_name") else None
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO companies
                    (company_name, tax_id, website, industry, company_size, address,
                     province_id, fanpage_url, linkedin_url, partnership_potential,
                     created_by, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING company_id
                """,
                (
                    data["company_name"], data.get("tax_id"), data.get("website"),
                    data.get("industry"), data.get("company_size"), data.get("address"),
                    province_id, data.get("fanpage_url"), data.get("linkedin_url"),
                    data.get("partnership_potential") or "UNVERIFIED",
                    actor_id, actor_id,
                ),
            )
            return str(cur.fetchone()[0])

    if entity_type == "job":
        company_id = data.get("company_id") or _resolve_company_id_for_create(conn, data, actor_id)
        level_id = db_module.get_level_id(conn, data["level_code"]) if data.get("level_code") else None
        province_id = (
            db_module.get_or_create_province(conn, data["province_name"])
            if data.get("province_name") else None
        )
        return db_module.create_manual_job(
            conn,
            job_title=data["job_title"],
            company_id=company_id,
            matching_industry=data.get("matching_industry") or "",
            level_id=level_id,
            province_id=province_id,
            work_type=data.get("work_type"),
            currency=data.get("currency") or "VNĐ",
            salary_min=data.get("salary_min"),
            salary_max=data.get("salary_max"),
            salary_type=data.get("salary_type") or "NEGOTIABLE",
            salary_period=data.get("salary_period") or "MONTH",
            deadline=data.get("deadline"),
            created_by=actor_id,
        )

    if entity_type == "contact":
        company_id = data.get("company_id") or _resolve_company_id_for_create(conn, data, actor_id)
        return db_module.create_company_contact(
            conn,
            company_id=company_id,
            contact_name=data["contact_name"],
            job_title=data.get("job_title"),
            work_email=data.get("work_email"),
            social_link=data.get("social_link"),
            phone_number=data.get("phone_number"),
            found_source=data.get("found_source") or "IMPORT",
            created_by=actor_id,
        )

    raise ValueError(f"entity_type không hợp lệ: {entity_type!r}")


def _resolve_company_id_for_create(conn, data, actor_id) -> str:
    """Dòng Job/Contact ở trạng thái no_conflict — company đã resolved
    (exact match tax_id/tên) lúc build preview, company_id nằm trong
    company_resolution chứ không phải data['company_id'] trực tiếp (xem
    preview_manager.build_preview) — hàm này KHÔNG được gọi tới nếu
    router truyền company_id đúng cách; giữ lại làm lớp phòng thủ, tự
    tạo company mới theo company_name nếu vì lý do gì đó vẫn thiếu."""
    return db_module.get_or_create_company_by_profile(
        conn, data.get("company_name", ""), province_id=None, created_by=actor_id,
    )


def _update_row(conn, entity_type, data, existing, resolution, actor_id, *, reactivate: bool):
    if entity_type == "company":
        company_id = existing[get_spec(entity_type).id_field]
        province_id = (
            db_module.get_or_create_province(conn, data["province_name"])
            if data.get("province_name") else None
        )
        db_module.patch_company_profile(
            conn, company_id,
            company_name=data.get("company_name"),
            tax_id=data.get("tax_id"),
            website=data.get("website"),
            industry=data.get("industry"),
            company_size=data.get("company_size"),
            address=data.get("address"),
            province_id=province_id,
            fanpage_url=data.get("fanpage_url"),
            linkedin_url=data.get("linkedin_url"),
            partnership_potential=data.get("partnership_potential"),
            updated_by=actor_id,
        )
        if reactivate:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE companies SET is_active = true WHERE company_id = %s",
                    (company_id,),
                )
        return company_id

    if entity_type == "job":
        job_id = existing[get_spec(entity_type).id_field]
        level_id = db_module.get_level_id(conn, data["level_code"]) if data.get("level_code") else None
        province_id = (
            db_module.get_or_create_province(conn, data["province_name"])
            if data.get("province_name") else None
        )
        db_module.update_job(
            conn, job_id,
            job_title=data.get("job_title"),
            matching_industry=data.get("matching_industry"),
            level_id=level_id,
            province_id=province_id,
            work_type=data.get("work_type"),
            currency=data.get("currency"),
            salary_min=data.get("salary_min"),
            salary_max=data.get("salary_max"),
            salary_type=data.get("salary_type"),
            salary_period=data.get("salary_period"),
            deadline=data.get("deadline"),
            # reactivate: job cũ CLOSED/EXPIRED -> mở lại OPEN, KỂ CẢ KHI
            # file import không có cột job_status (mặc định OPEN vì đây
            # là job "còn hiệu lực" theo file mới import).
            job_status=(data.get("job_status") or ("OPEN" if reactivate else None)),
            updated_by=actor_id,
        )
        return job_id

    if entity_type == "contact":
        contact_id = existing[get_spec(entity_type).id_field]
        db_module.update_company_contact(
            conn, contact_id,
            contact_name=data.get("contact_name"),
            job_title=data.get("job_title"),
            work_email=data.get("work_email"),
            social_link=data.get("social_link"),
            phone_number=data.get("phone_number"),
            updated_by=actor_id,
        )
        if reactivate:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE company_contacts SET is_active = true WHERE contact_id = %s",
                    (contact_id,),
                )
        return contact_id

    raise ValueError(f"entity_type không hợp lệ: {entity_type!r}")
