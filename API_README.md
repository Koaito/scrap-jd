# API Layer (FastAPI) — hướng dẫn chạy

Lớp API này **bọc ngoài** codebase crawler hiện có (`adapters/`, `normalize.py`,
`db.py`, `pipeline.py`) — không sửa gì các file đó ngoại trừ thêm 1 nhóm
hàm query mới cuối `db.py` (mục "QUERY LAYER CHO API"). `main.py` (CLI
crawl cũ) giữ nguyên 100%, chạy song song không xung đột với API.

## Cài đặt thêm

```bash
pip install -r requirements.txt   # đã thêm fastapi + uvicorn
```

## Chạy

```bash
uvicorn api.app:app --reload --port 8000
```

Mở trình duyệt: `http://localhost:8000/docs` — Swagger UI tự sinh, bấm
"Try it out" thử ngay từng endpoint, không cần Postman.

## Cấu trúc mới thêm

```
api/
  app.py              <- entry point FastAPI, đăng ký router + CORS
  deps.py             <- get_db(): mở/đóng connection Postgres theo từng request
  schemas.py           <- Pydantic models (request/response JSON)
  crawl_runner.py      <- chạy pipeline crawl ở nền, theo dõi qua run_id
  routers/
    jobs.py             <- GET /jobs, GET /jobs/{id}
    companies.py         <- GET /companies, GET /companies/{id}
    crawl.py              <- POST /crawl, GET /crawl/{run_id}
    meta.py                <- GET /stats, GET /sources, GET /health
```

## Endpoints hiện có

| Method | Path | Việc |
|---|---|---|
| GET | `/jobs?industry=&province=&level=&work_type=&status=&keyword=&limit=&offset=` | List job, filter + phân trang |
| GET | `/jobs/{job_id}` | Chi tiết 1 job (kèm parsed_content) |
| GET | `/companies?keyword=&province=&has_social=&limit=&offset=` | List công ty, filter + phân trang |
| GET | `/companies/{company_id}` | Chi tiết công ty (kèm danh sách job) |
| POST | `/crawl` | Kích hoạt crawl nền — body `{"source": "topcv", "category": "data-analyst", "pages": 3}`, trả `run_id` ngay |
| GET | `/crawl/{run_id}` | Theo dõi tiến độ/kết quả 1 lượt crawl |
| GET | `/stats` | Tổng job/công ty, tỷ lệ có social, phân bố ngành/nguồn |
| GET | `/sources` | Danh sách source/category có sẵn (đọc từ `config.py`) — frontend render dropdown |
| GET | `/health` | Health check đơn giản |

## Giới hạn đã biết (khung sườn ban đầu — xem thêm docstring trong `crawl_runner.py`)

- **Trạng thái crawl lưu trong RAM** (dict `_RUNS`), mất khi restart server,
  không đồng bộ nếu chạy `uvicorn --workers > 1`. Đủ dùng cho quy mô hiện
  tại (dashboard nội bộ, ít người). Cần chạy nhiều worker/persistent thì
  nâng cấp sang Celery + Redis hoặc RQ sau.
- **CORS đang mở `allow_origins=["*"]`** để dễ test — SIẾT LẠI đúng domain
  frontend thật trước khi deploy production (sửa trong `api/app.py`).
- **Chưa có auth** — mọi endpoint hiện public. Cần thêm trước khi expose
  ra ngoài internet (API key đơn giản, hoặc OAuth2 nếu cần phân quyền
  theo `ss_team_members` đã có sẵn trong schema).
- **Connection Postgres mở/đóng mỗi request** (không dùng pool) — đủ cho
  traffic thấp. Nếu nhiều người dùng dashboard cùng lúc, cân nhắc đổi
  sang connection pool.

## Việc CHƯA làm (để team quyết định có cần không)

- Endpoint PATCH/PUT để sửa `ss_team_notes`, `contact_status`... (hiện
  API chỉ đọc — read-only). Thêm dễ dàng nếu cần, tái dùng `db.update_*`
  đã có sẵn.
- Auth/phân quyền.
- Rate limiting.
