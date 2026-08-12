# API Layer (FastAPI) — hướng dẫn chạy

Lớp API này **bọc ngoài** codebase crawler hiện có (`adapters/`, `normalize.py`,
`db.py`, `pipeline.py`) — không sửa gì các file đó ngoại trừ thêm 1 nhóm
hàm query mới cuối `db.py` (mục "QUERY LAYER CHO API"). `main.py` (CLI
crawl cũ) giữ nguyên 100%, chạy song song không xung đột với API.

## Cài đặt thêm

```bash
pip install -r requirements.txt   # đã thêm fastapi + uvicorn
```

Điền thêm `API_KEY` và `ALLOWED_ORIGINS` trong `.env` (bắt buộc — xem
mục "Bảo mật" bên dưới, xem `.env.example` để biết cách tạo `API_KEY`).

## Chạy

```bash
uvicorn api.app:app --reload --port 8000
```

Swagger UI (`/docs`) **mặc định TẮT** (xem mục "Bảo mật"). Set
`ENABLE_DOCS=true` trong `.env` để bật lúc dev local, rồi mở
`http://localhost:8000/docs` — nhớ bấm nút khoá (🔒) góc trên bên phải,
nhập `API_KEY`, hoặc thêm header thủ công khi thử "Try it out".

## Bảo mật (thêm 08/2026)

- **API key** — mọi endpoint (kể cả `/health`) yêu cầu header
  `X-API-Key` đúng giá trị `API_KEY` trong `.env`. Fail-closed: quên
  cấu hình `API_KEY` → server tự chặn hết (500), không âm thầm mở toang.
  Chi tiết: `api/auth.py`.
- **CORS** — chỉ domain liệt kê trong `ALLOWED_ORIGINS` (`.env`, phân
  tách bằng dấu phẩy) mới gọi được từ trình duyệt. Để trống → không
  domain nào gọi được (fail-closed, giống `API_KEY`).
- **Swagger/ReDoc/openapi.json mặc định TẮT** — đây là 3 route duy nhất
  KHÔNG đi qua được lớp `API_KEY` (giới hạn kỹ thuật của FastAPI, route
  Starlette thuần, không phải path operation thường). Vì vậy mặc định
  tắt hẳn (fail-closed) để không lộ cấu trúc API ra ngoài; chỉ bật bằng
  `ENABLE_DOCS=true` lúc dev local, tránh bật trên môi trường public.
  Xem chi tiết trong docstring `api/app.py`.

## Deploy production

Đã deploy thật lên **Render** (Web Service, kết nối GitHub private repo
`Koaito/scrap-jd`), khai báo đủ 10 biến môi trường (Postgres, `API_KEY`,
`ALLOWED_ORIGINS`, Tavily/Gemini key). URL public:
`https://scrap-jd-api.onrender.com`.

Khi làm frontend và deploy lên Vercel: quay lại Render, sửa
`ALLOWED_ORIGINS` cho khớp domain Vercel thật, KHÔNG quên bước này
(thiếu → frontend gọi API bị chặn bởi CORS dù key đúng).

## Cấu trúc

```
api/
  app.py              <- entry point FastAPI, đăng ký auth + CORS + router
  auth.py              <- kiểm tra X-API-Key, fail-closed nếu thiếu cấu hình
  deps.py               <- get_db(): mở/đóng connection Postgres theo từng request
  schemas.py             <- Pydantic models (request/response JSON)
  crawl_runner.py         <- chạy pipeline crawl ở nền, theo dõi qua run_id
  routers/
    jobs.py                <- GET /jobs, GET /jobs/{id}
    companies.py             <- GET /companies, GET /companies/{id}
    crawl.py                  <- POST /crawl, GET /crawl/{run_id}
    meta.py                    <- GET /stats, GET /sources, GET /health
```

## Endpoints hiện có

| Method | Path | Việc |
|---|---|---|
| GET | `/jobs?industry=&province=&level=&work_type=&status=&keyword=&limit=&offset=` | List job, filter + phân trang |
| GET | `/jobs/{job_id}` | Chi tiết 1 job (kèm parsed_content) |
| GET | `/companies?keyword=&province=&has_social=&limit=&offset=` | List công ty, filter + phân trang |
| GET | `/companies/{company_id}` | Chi tiết công ty (kèm danh sách job) |
| POST | `/crawl` | Kích hoạt crawl nền — body `{"source", "category", "pages"?, "max_jobs"?}`, trả `run_id` ngay |
| GET | `/crawl/{run_id}` | Theo dõi tiến độ/kết quả 1 lượt crawl |
| GET | `/stats` | Tổng job/công ty, tỷ lệ có social, phân bố ngành/nguồn |
| GET | `/sources` | Danh sách source/category có sẵn (đọc từ `config.py`) — frontend render dropdown |
| GET | `/health` | Health check (vẫn yêu cầu API key — xem mục Bảo mật) |

Mọi request phía trên đều cần header `X-API-Key: <giá trị API_KEY>`.

### `POST /crawl` — giới hạn theo trang hoặc theo số lượng JD

Khớp 1-1 với `--pages`/`--max-jobs` đã có ở CLI (`main.py`) — không còn
gap giữa API và CLI như bản trước:

```json
// Giới hạn theo trang (như cũ)
{"source": "topcv", "category": "data-analyst", "pages": 3}

// Giới hạn theo TỔNG SỐ JD, không quan tâm bao nhiêu trang
{"source": "topcv", "category": "data-analyst", "max_jobs": 20}

// Kết hợp cả 2 -> dừng ở điều kiện nào tới trước
{"source": "topcv", "category": "data-analyst", "pages": 5, "max_jobs": 50}

// Không truyền cả 2 -> dùng DEFAULT_MAX_PAGES (config.py) như trước giờ
{"source": "topcv", "category": "data-analyst"}
```

`GET /crawl/{run_id}` trả thêm field `max_jobs` (null nếu không giới
hạn theo số lượng) bên cạnh `pages` đã có.

## Giới hạn đã biết

- **Trạng thái crawl lưu trong RAM** (dict `_RUNS`), mất khi restart server,
  không đồng bộ nếu chạy `uvicorn --workers > 1`. Đủ dùng cho quy mô hiện
  tại (dashboard nội bộ, ít người). Cần chạy nhiều worker/persistent thì
  nâng cấp sang Celery + Redis hoặc RQ sau.
- **Auth là 1 API key tĩnh dùng chung**, không phân quyền theo từng
  người trong `ss_team_members` — đủ cho quy mô hiện tại (team 2 người).
  Xem mục "NÂNG CẤP SAU" trong docstring `api/auth.py` nếu cần nhiều key.
- **Connection Postgres mở/đóng mỗi request** (không dùng pool) — đủ cho
  traffic thấp. Nếu nhiều người dùng dashboard cùng lúc, cân nhắc đổi
  sang connection pool.
- **`POST /crawl` không giới hạn số lượt chạy song song** — gọi dồn dập
  nhiều lần có thể có nhiều pipeline chạy cùng lúc. Nên tự giới hạn ở
  phía frontend (disable nút "Crawl" khi đang chạy).

## Việc CHƯA làm (để team quyết định có cần không)

- Endpoint PATCH/PUT để sửa `ss_team_notes`, `contact_status`... (hiện
  API chỉ đọc — read-only, trừ `POST /crawl`). Thêm dễ dàng nếu cần,
  tái dùng `db.update_*` đã có sẵn.
- Phân quyền theo người dùng (hiện chỉ 1 API key dùng chung).
- Rate limiting.
- Frontend — chưa bắt đầu.

