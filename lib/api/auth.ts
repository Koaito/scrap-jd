// lib/api/auth.ts
// Các hàm gọi thẳng route /auth/* của Scrap_JD.
//
// LƯU Ý: login() và refresh() KHÔNG đi qua callAuthed() (vì bản thân
// chúng là nơi LẤY token, không phải nơi CẦN token có sẵn) — 2 hàm này
// dùng thẳng fetch để tránh vòng lặp phụ thuộc với lib/session.ts.

import { ApiError, callAuthed } from "@/lib/api/client";
import type { components } from "@/lib/api/types";
import { getClientIpHeaders } from "@/lib/client-ip-server";

/**
 * UserOut (sinh từ OpenAPI) + `is_staff` (role !== "user"). Backend KHÔNG trả `is_staff`, nên chỉ getMe() mới có
 * giá trị này (tự tính khi map response); mọi nơi khác trong app dựa vào cờ đó, không tự suy lại từ role.
 * Hàm khác trả thẳng response của backend thì dùng UserOut/StaffUser, không phải BackendUser.
 */
export type BackendUser = components["schemas"]["UserOut"] & { is_staff: boolean };

/** Response của POST /auth/login (TokenPairOut, có must_change_password). */
export type TokenPair = components["schemas"]["TokenPairOut"];

/** Response của POST /auth/refresh (AccessTokenOut): KHÔNG có must_change_password như TokenPairOut. */
export type RefreshResult = components["schemas"]["AccessTokenOut"];

const BASE_URL = process.env.CRAWLER_API_URL;
const API_KEY = process.env.CRAWLER_API_KEY;

async function authRawFetch<T>(path: string, init: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": API_KEY!,
      // IP thật của người dùng cuối cho rate limit /auth/login, /auth/refresh
      // (plan Phần 1 mục 3.14, xem lib/client-ip.ts).
      ...(await getClientIpHeaders()),
      ...init.headers,
    },
    cache: "no-store",
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = body?.detail;

    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      throw new ApiError(
        detail.message ?? "Lỗi không xác định",
        res.status,
        detail.error_code,
        detail.params,
      );
    }
    if (Array.isArray(detail) && detail.length > 0) {
      throw new ApiError(detail[0]?.msg ?? "Dữ liệu gửi lên không hợp lệ.", res.status);
    }
    throw new ApiError("Lỗi không xác định", res.status);
  }

  return res.json();
}

export const login = (email: string, password: string) =>
  authRawFetch<TokenPair>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });

/**
 * Xoay vòng refresh token — LUÔN nhận lại 1 CẶP token mới. Backend thu
 * hồi token cũ ngay khi hàm này chạy, nên hàm này KHÔNG được gọi trùng
 * lặp cho cùng 1 refresh token trong cùng 1 request (xem lib/session.ts
 * — getValidAccessToken() đã bọc React.cache() để đảm bảo điều này).
 */
export const refresh = (refreshToken: string) =>
  authRawFetch<RefreshResult>("/auth/refresh", {
    method: "POST",
    body: JSON.stringify({ refresh_token: refreshToken }),
  });

/**
 * GET /auth/me — backend trả UserOut (KHÔNG có field `is_staff`, xem
 * BackendUser ở đầu file), nên tự tính thêm ở đây trước khi trả ra
 * ngoài. Mọi nơi khác trong app (session.ts, auth-guard.ts, route
 * handlers...) dựa vào is_staff đã tính sẵn này, không tự suy lại
 * `role !== "user"` rải rác nhiều chỗ.
 */
export const getMe = async (accessToken: string): Promise<BackendUser> => {
  const user = await authRawFetch<components["schemas"]["UserOut"]>("/auth/me", {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  return { ...user, is_staff: user.role !== "user" };
};

/**
 * PATCH /auth/me — cho user tự sửa full_name/phone/track của chính
 * mình. Backend TỰ ÉP phone/track về NULL cho mọi tài khoản không phải
 * role "user" (staff) — form Profile nên ẩn hẳn 2 field này khi user
 * hiện tại là staff, không hiện ô nhập rồi âm thầm không lưu được.
 * (Phần 2 mục 3 của plan)
 */
export const updateMe = (data: { full_name: string; phone?: string; track?: string }) =>
  callAuthed<components["schemas"]["UserOut"]>("/auth/me", {
    method: "PATCH",
    body: JSON.stringify(data),
  });

export const logout = () => callAuthed<void>("/auth/logout", { method: "POST" });

/**
 * POST /auth/change-password — CẦN auth (đi qua callAuthed, tự refresh
 * token nếu cần). `old_password` chỉ optional khi must_change_password
 * đang true (Route Handler không tự đoán trước, để backend quyết định
 * và trả lỗi `AUTH_OLD_PASSWORD_INCORRECT` (401) nếu thiếu/sai khi thật
 * ra bắt buộc — xem api/routers/auth_session.py::change_password() bên
 * Scrap_JD). Backend tự thu hồi TOÀN BỘ refresh token + clear
 * active_session_id sau khi đổi thành công — Route Handler gọi hàm này
 * (app/api/auth/change-password/route.ts) có trách nhiệm tự xoá cookie
 * phiên NGAY sau khi gọi thành công, không đợi request kế tiếp tự phát
 * hiện phiên đã chết.
 */
export const changePassword = (data: { old_password?: string; new_password: string }) =>
  callAuthed<components["schemas"]["UserOut"]>("/auth/change-password", {
    method: "POST",
    body: JSON.stringify(data),
  });

/**
 * GET /auth/users — toàn bộ tài khoản, ss_team trở lên gọi được. Trả về
 * CHỈ nhân sự team SS (role ss_team/admin), khớp `staff_members` ở
 * blueprints/contacts.py bên Flask — dùng để map `assigned_ss_user` ->
 * tên người phụ trách (và sau này làm dropdown gán phụ trách, Phần 2
 * mục 3). Học viên (role "user") bị lọc bỏ: backend cũng không cho gán
 * contact cho role này (CONTACT_ASSIGNED_USER_ROLE_INVALID).
 */
export type StaffUser = components["schemas"]["UserOut"];

export async function listStaffUsers(): Promise<StaffUser[]> {
  const all = await callAuthed<StaffUser[]>("/auth/users");
  return all.filter((u) => u.role === "ss_team" || u.role === "admin");
}

/**
 * GET /auth/users — TOÀN BỘ tài khoản, KHÔNG lọc role (khác
 * listStaffUsers() ở trên). Dùng cho /staff-accounts (Nhóm 3, Đợt 3.2,
 * Phần 1/4): khớp accounts() bên Flask (blueprints/staff.py) — trang này
 * cố ý liệt kê cả học viên tự đăng ký (role "user"), không chỉ nhân sự
 * team SS, để admin thấy/khoá được cả tài khoản học viên nếu cần.
 */
export async function listAllUsers(): Promise<StaffUser[]> {
  return callAuthed<StaffUser[]>("/auth/users");
}

export type UserCreatedOut = components["schemas"]["UserCreatedOut"];

/**
 * POST /auth/users — CHỈ admin (require_admin ở backend). Trả về
 * UserCreatedOut kèm `temp_password`: mật khẩu tạm do backend tự sinh, CHỈ
 * xuất hiện trong response này (backend chỉ lưu hash, không endpoint nào
 * lấy lại được). Nơi gọi tuyệt đối KHÔNG được log/cache/lưu lại giá trị
 * này ở đâu ngoài state client của trang /staff-accounts/add (plan Nhóm 3).
 * Nơi gọi tự requireAdmin() + tự chuẩn hoá đầu vào (trim/lowercase) trước.
 */
export async function createUser(data: {
  full_name: string;
  email: string;
  role: string;
}): Promise<UserCreatedOut> {
  return callAuthed<UserCreatedOut>("/auth/users", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/**
 * PATCH /auth/users/{id}/role — CHỈ admin. Backend trả 400 nếu id trùng
 * chính admin đang gọi (chặn tự đổi role mình) — UI vẫn disable sẵn ở
 * đúng dòng đó để khỏi round-trip vô ích (plan Nhóm 3). Nơi gọi tự
 * requireAdmin() trước. Role có hiệu lực NGAY (get_current_user() ở
 * backend đọc role mới nhất từ DB, không còn dựa vào JWT cũ).
 */
export async function updateUserRole(ssUserId: string, role: string): Promise<StaffUser> {
  return callAuthed<StaffUser>(`/auth/users/${encodeURIComponent(ssUserId)}/role`, {
    method: "PATCH",
    body: JSON.stringify({ role }),
  });
}

/**
 * PATCH /auth/users/{id}/active-status — CHỈ admin. `isActive=false` là
 * khoá VĨNH VIỄN (khác locked_until — khoá tạm do sai mật khẩu, tự hết
 * hạn). Backend trả 400 nếu id trùng chính admin đang gọi. Nơi gọi tự
 * requireAdmin() trước.
 *
 * Hiệu lực: get_current_user() ở backend đã đọc is_active mới nhất từ DB
 * và trả 403 AUTH_ACCOUNT_INACTIVE ngay ở request kế tiếp của người bị
 * khoá (mục 3.10 của plan đã sửa ở api/deps.py) — KHÔNG còn trễ tối đa 30
 * phút như docstring cũ của db/auth.py::update_user_active_status().
 */
export async function updateUserActiveStatus(ssUserId: string, isActive: boolean): Promise<StaffUser> {
  return callAuthed<StaffUser>(`/auth/users/${encodeURIComponent(ssUserId)}/active-status`, {
    method: "PATCH",
    body: JSON.stringify({ is_active: isActive }),
  });
}
