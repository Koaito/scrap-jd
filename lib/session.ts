// lib/session.ts
// Tương đương _auth_tokens_from_session + load_user() bên Flask.
//
// Khác Flask (1 cookie session ký server-side chứa cả access + refresh
// token), ở đây tách thành 2 cookie httpOnly riêng — không dùng
// NEXT_PUBLIC_* hay localStorage cho 2 token này (xem Phần 2 mục 1).
//
// LƯU Ý (Round sửa refresh): tên cookie/maxAge/options nằm ở
// lib/auth-cookies.ts, hàm decode JWT nằm ở lib/jwt.ts — cả 2 tách riêng
// khỏi file này để proxy.ts (không import được file này vì nó đụng
// next/headers) vẫn dùng chung được, không phải viết
// lại 1 bản lệch. Từ giờ file refresh CHỦ ĐỘNG duy nhất khi vào tới
// Server Component gần như luôn thấy access token đã được proxy.ts
// refresh sẵn — nhánh refresh dưới đây trở thành lớp dự phòng (proxy
// bị skip do matcher, hoặc token hết hạn ngay giữa lúc render).

import { cache } from "react";
import { cookies } from "next/headers";
import { getMe, refresh as refreshApi, type BackendUser } from "@/lib/api/auth";
import { ApiError } from "@/lib/api/client";
import { isExpiredSoon } from "@/lib/jwt";
import { sessionEndReasonFromCode, type SessionEndReason } from "@/lib/session-end";
import {
  ACCESS_COOKIE,
  REFRESH_COOKIE,
  ACCESS_MAX_AGE,
  REFRESH_MAX_AGE,
  COOKIE_OPTS,
} from "@/lib/auth-cookies";

export async function getTokens() {
  const jar = await cookies();
  return {
    accessToken: jar.get(ACCESS_COOKIE)?.value ?? null,
    refreshToken: jar.get(REFRESH_COOKIE)?.value ?? null,
  };
}

export async function setAuthCookies(accessToken: string, refreshToken: string) {
  const jar = await cookies();
  jar.set(ACCESS_COOKIE, accessToken, { ...COOKIE_OPTS, maxAge: ACCESS_MAX_AGE });
  jar.set(REFRESH_COOKIE, refreshToken, { ...COOKIE_OPTS, maxAge: REFRESH_MAX_AGE });
}

export async function clearAuthCookies() {
  const jar = await cookies();
  jar.delete(ACCESS_COOKIE);
  jar.delete(REFRESH_COOKIE);
}

/**
 * Trả về 1 access token CÒN DÙNG ĐƯỢC — tự refresh nếu access token đã
 * hết hạn (hoặc sắp hết), nhưng CHỈ gọi /auth/refresh ĐÚNG 1 LẦN cho mỗi
 * lần render trang nhờ bọc bằng React.cache() (Next.js tự dedupe các
 * lệnh gọi hàm giống nhau trong cùng 1 request).
 *
 * Đây là hàm DUY NHẤT trong toàn bộ app xử lý việc refresh chủ động —
 * cả getCurrentUser() lẫn callAuthed() (lib/api/client.ts) đều gọi qua
 * đây. KHÔNG được tự viết thêm 1 bản refresh riêng ở nơi khác.
 *
 * Lý do bắt buộc dedupe bằng cache() ngay từ đầu: backend Scrap_JD coi
 * refresh token là dùng 1 LẦN. Nếu 2 lệnh gọi API cần refresh xảy ra
 * cùng lúc trong cùng 1 lần render (ví dụ layout gọi song song
 * getCurrentUser() + getSavedJobIds() + getUnreadCount()), mà không
 * dedupe, lệnh thứ 2 sẽ gửi lên 1 refresh token đã bị lệnh thứ 1 làm
 * mất hiệu lực -> backend hiểu nhầm là token bị đánh cắp -> đăng xuất
 * TOÀN BỘ tài khoản trên mọi thiết bị. Xem đầy đủ ở Phụ lục A của plan.
 */
export const getValidAccessToken = cache(async (): Promise<string | null> => {
  const { accessToken, refreshToken } = await getTokens();
  if (!accessToken) return null;
  if (!isExpiredSoon(accessToken)) return accessToken;
  if (!refreshToken) return null;

  try {
    const pair = await refreshApi(refreshToken);
    await setAuthCookies(pair.access_token, pair.refresh_token);
    return pair.access_token;
  } catch {
    // Refresh thất bại (hết hạn/đã bị thu hồi/không hợp lệ) -> dọn
    // cookie, trả null. Nơi gọi (getCurrentUser/callAuthed) tự quyết
    // định bước tiếp theo (redirect lặng lẽ hay toast rõ ràng theo
    // error_code cụ thể — xem Phụ lục C).
    await clearAuthCookies();
    return null;
  }
});

/**
 * Refresh PHẢN ỨNG — dùng trong callAuthed() khi vẫn dính 401 sau khi
 * đã gọi getValidAccessToken() (lệch giờ, token bị thu hồi ngoài dự
 * kiến, hoặc trúng đúng khoảnh khắc hết hạn). KHÔNG dùng cache() ở đây
 * vì đây vốn đã là nhánh dự phòng hiếm khi chạy, không cần dedupe thêm
 * — nhưng lưu ý: nếu forceRefreshAccessToken() được gọi nhiều lần
 * trong cùng 1 request (nhiều lệnh cùng 401 cùng lúc), mỗi lần gọi vẫn
 * tự gửi request /auth/refresh riêng, có thể trúng đúng race condition
 * ở Phụ lục A. Ở mức migrate đầu, chấp nhận rủi ro này cho nhánh phản
 * ứng (xác suất thấp hơn nhiều so với nhánh chủ động), theo đúng
 * hướng A đã chốt trong Phụ lục A — không thêm Redis lock.
 */
export async function forceRefreshAccessToken(): Promise<string | null> {
  return (await forceRefreshWithReason()).token;
}

/**
 * Cùng việc với forceRefreshAccessToken() nhưng khi refresh THẤT BẠI còn
 * trả kèm `errorCode` của lỗi refresh — để callAuthed() (lib/api/client.ts)
 * phân biệt được "refresh token đã bị thu hồi vì đăng nhập nơi khác"
 * (auth_refresh_token_thu_hoi_truoc, Phụ lục C) với hết hạn thường, thay vì
 * chỉ biết chung chung là refresh hỏng. Vẫn là bản refresh phản ứng DUY NHẤT
 * — forceRefreshAccessToken() chỉ là vỏ mỏng bọc lại hàm này.
 */
export async function forceRefreshWithReason(): Promise<{
  token: string | null;
  errorCode?: string;
}> {
  const { refreshToken } = await getTokens();
  if (!refreshToken) return { token: null };

  try {
    const pair = await refreshApi(refreshToken);
    await setAuthCookies(pair.access_token, pair.refresh_token);
    return { token: pair.access_token };
  } catch (err) {
    await clearAuthCookies();
    return { token: null, errorCode: err instanceof ApiError ? err.errorCode : undefined };
  }
}

/**
 * Trạng thái phiên của request hiện tại: người dùng (nếu còn hợp lệ) và,
 * khi không còn, LÝ DO nếu là loại cần báo cho người dùng biết (Phụ lục C:
 * bị đăng nhập nơi khác thay / phiên bị thu hồi do đăng xuất-đổi mật khẩu).
 * Hết hạn thường hoặc chưa đăng nhập -> endReason = null (im lặng).
 *
 * Cố ý KHÔNG bọc React.cache(): sau Server Action có revalidatePath(), phần
 * render lại nằm cùng request — cache sẽ giữ thông tin người dùng cũ (vd tên
 * vừa đổi ở /profile không hiện ngay). Số lần gọi /auth/me không đổi so với
 * trước: layout và requireUser() vẫn mỗi nơi gọi đúng như cũ.
 */
export async function getSessionState(): Promise<{
  user: BackendUser | null;
  endReason: SessionEndReason | null;
}> {
  const accessToken = await getValidAccessToken();
  if (!accessToken) return { user: null, endReason: null };

  try {
    return { user: await getMe(accessToken), endReason: null };
  } catch (err) {
    return {
      user: null,
      endReason: err instanceof ApiError ? sessionEndReasonFromCode(err.errorCode) : null,
    };
  }
}

export async function getCurrentUser(): Promise<BackendUser | null> {
  return (await getSessionState()).user;
}
