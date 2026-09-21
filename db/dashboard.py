"""
db.dashboard — 8 khối "insight" của trang /dashboard, tính thẳng bằng SQL
(Phần 5 mục 8 của plan migrate Next.js).

TRƯỚC ĐÂY: Flask (mindx-jobs/blueprints/dashboard.py) gọi list_all_jobs()/
list_all_companies()/list_all_contacts() — lặp gọi API theo trang 200
dòng/lần để kéo TOÀN BỘ dữ liệu về, rồi tự tính 8 khối bằng Python. Chi
phí tăng tuyến tính theo tổng số dòng (dự kiến 5.000-7.000). Giờ mỗi hàm
ở đây là 1-2 câu SQL, chi phí không còn phụ thuộc số dòng phải kéo qua
mạng — cùng hướng đã làm với get_job_data_health()/get_company_data_health().

Ánh xạ 8 khối -> hàm (KHỚP đúng tên hàm Python gốc bên Flask, để đối chiếu):
    jd_needing_push            -> get_jobs_needing_push()
    jd_stale                   -> get_stale_jobs()
    top_skills                 -> get_top_skills()
    salary_ranges              -> get_salary_ranges()
    companies_no_contact       -> get_high_potential_companies_without_contact()
    contacts_needing_followup  -> get_contacts_needing_followup()
    companies_expanding/quiet  -> get_company_job_activity()
    monthly_recap              -> get_monthly_recap_counts()  (+ engagement từ
                                  db.stats.get_monthly_engagement_stats, Python
                                  ở router ghép % chênh lệch)

GIỮ NGUYÊN NGƯỠNG/LOGIC của bản Flask (7/14 ngày cho push, 30 ngày cho
stale/top_skills/expanding, 60 ngày cho công ty tiềm năng, 75 ngày cho
quiet, ...). Chỉ đổi NƠI TÍNH, không đổi nghiệp vụ. 4 điểm CỐ Ý khác bản
Flask (đều nhỏ, nêu rõ để không ai tưởng là lỗi lệch):

  1. NGÀY THEO GIỜ VIỆT NAM. "Hôm nay" và ngày thu thập của job/công ty
     tính theo Asia/Ho_Chi_Minh (Phụ lục F của plan). Flask lấy phần ngày
     của chuỗi created_at thô (UTC) so với now_vn() — trộn 2 múi giờ,
     lệch 1 ngày với dữ liệu tạo trong khung 17:00-24:00 UTC. Cột
     job_postings/companies.created_at là TIMESTAMP (không tz) do
     DEFAULT now() ghi theo TimeZone của session — Supabase mặc định UTC,
     cùng giả định mà db.stats.get_monthly_engagement_stats() đã dùng.
     Xem _VN_TODAY / _vn_date() bên dưới.
  2. THỨ TỰ KHI BẰNG ĐIỂM (cùng số đếm / cùng số ngày) là thứ tự tường
     minh theo tên/id, không phụ thuộc thứ tự dict/list Python như Flask
     (Flask ngầm dựa vào thứ tự API trả về, không ổn định giữa các lần).
  3. top_companies của monthly_recap hiện tên công ty thật kể cả công ty
     đã xoá mềm (Flask hiện "—" vì tra trong danh sách công ty CHỈ gồm
     active) — cùng 1 job vẫn được đếm như nhau, chỉ khác nhãn.
  4. Số dòng trả về KHÔNG bị giới hạn (giống Flask — bảng ở UI tự phân
     trang phía client 20 dòng/trang), trừ top_skills (10), top_industries
     (3), top_companies (5), recent_jobs (5) đúng như bản gốc.

Mọi hàm chỉ ĐỌC (SELECT), không commit.
"""

import psycopg2.extras

# "Hôm nay" theo giờ VN, kiểu DATE — dùng chung mọi câu SQL bên dưới.
# now() (timestamptz) AT TIME ZONE '...' -> timestamp naive giờ VN -> ::date.
_VN_TODAY = "(now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date"


def _vn_date(column: str) -> str:
    """Biểu thức SQL đổi 1 cột TIMESTAMP (naive, lưu theo UTC) sang NGÀY
    theo giờ VN: gắn nhãn UTC trước ('AT TIME ZONE 'UTC'' -> timestamptz)
    rồi mới quy đổi sang giờ VN ('AT TIME ZONE 'Asia/Ho_Chi_Minh'' ->
    timestamp naive giờ VN) — thiếu bước đầu, Postgres sẽ hiểu cột naive
    theo TimeZone của session thay vì UTC."""
    return f"(({column} AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Ho_Chi_Minh')::date"


# ---------------------------------------------------------------
# Tab "Gợi ý học viên"
# ---------------------------------------------------------------

def get_jobs_needing_push(conn, *, days_min: int = 7, days_max: int = 14) -> list[dict]:
    """jd_needing_push — job OPEN, CHƯA có ai lưu/ứng tuyển, deadline còn
    từ days_min tới days_max ngày (tính cả 2 đầu mút) so với hôm nay (VN).
    Sắp deadline gần nhất trước. Job không có deadline bị bỏ qua."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT jp.job_id, jp.job_title, jp.company_id, c.company_name,
                   jp.deadline, jp.created_at,
                   (jp.deadline - {_VN_TODAY}) AS days_left
            FROM job_postings jp
            JOIN companies c ON c.company_id = jp.company_id
            WHERE jp.job_status = 'OPEN'
              AND jp.deadline IS NOT NULL
              AND (jp.deadline - {_VN_TODAY}) BETWEEN %(days_min)s AND %(days_max)s
              AND NOT EXISTS (SELECT 1 FROM job_applications a WHERE a.job_id = jp.job_id)
              AND NOT EXISTS (SELECT 1 FROM saved_jobs s WHERE s.job_id = jp.job_id)
            ORDER BY days_left ASC, jp.job_title ASC, jp.job_id ASC
            """,
            {"days_min": days_min, "days_max": days_max},
        )
        return cur.fetchall()


def get_stale_jobs(conn, *, min_age_days: int = 30) -> list[dict]:
    """jd_stale ("JD ế") — job OPEN, CHƯA có ai lưu/ứng tuyển, đã thu thập
    (created_at, ngày VN) từ min_age_days ngày trở lên. Cũ nhất trước."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT jp.job_id, jp.job_title, jp.company_id, c.company_name,
                   jp.created_at,
                   ({_VN_TODAY} - {_vn_date('jp.created_at')}) AS age_days
            FROM job_postings jp
            JOIN companies c ON c.company_id = jp.company_id
            WHERE jp.job_status = 'OPEN'
              AND ({_VN_TODAY} - {_vn_date('jp.created_at')}) >= %(min_age_days)s
              AND NOT EXISTS (SELECT 1 FROM job_applications a WHERE a.job_id = jp.job_id)
              AND NOT EXISTS (SELECT 1 FROM saved_jobs s WHERE s.job_id = jp.job_id)
            ORDER BY age_days DESC, jp.job_title ASC, jp.job_id ASC
            """,
            {"min_age_days": min_age_days},
        )
        return cur.fetchall()


def get_top_skills(conn, *, days_recent: int = 30, top_n: int = 10) -> list[dict]:
    """top_skills — kỹ năng xuất hiện nhiều nhất trong job thu thập trong
    days_recent ngày gần đây (MỌI trạng thái job, giống Flask). Đếm theo
    LƯỢT XUẤT HIỆN (1 job ghi trùng 1 kỹ năng 2 lần thì tính 2 — giống
    Flask). Mỗi phần tử required_skills được tách tiếp theo dấu phẩy rồi
    cắt khoảng trắng, y hệt Flask (nối ", " rồi split(",")); phần rỗng bỏ.
    Bằng số đếm thì theo tên A-Z."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT skill, count(*) AS count
            FROM (
                SELECT btrim(part, E' \\t\\r\\n') AS skill
                FROM job_postings jp
                CROSS JOIN LATERAL jsonb_array_elements_text(
                    CASE WHEN jsonb_typeof(jp.parsed_content -> 'required_skills') = 'array'
                         THEN jp.parsed_content -> 'required_skills'
                         ELSE '[]'::jsonb END
                ) AS s(raw)
                CROSS JOIN LATERAL unnest(string_to_array(s.raw, ',')) AS part
                WHERE ({_VN_TODAY} - {_vn_date('jp.created_at')}) <= %(days_recent)s
            ) x
            WHERE skill <> ''
            GROUP BY skill
            ORDER BY count DESC, skill ASC
            LIMIT %(top_n)s
            """,
            {"days_recent": days_recent, "top_n": top_n},
        )
        return cur.fetchall()


def get_salary_ranges(conn) -> list[dict]:
    """salary_ranges — mức lương trung bình theo (ngành, level), CHỈ tính
    job lương tháng (MONTH) bằng VNĐ và có ít nhất 1 trong 2 đầu lương > 0
    (0/NULL = "Thoả thuận", không tính). avg_min chỉ tính trên các job có
    salary_min > 0, avg_max tương tự; sample_size = số job có salary_min
    > 0, nếu không có job nào thì = số job có salary_max > 0 (đúng Flask).
    Ngành/level rỗng gộp vào "Khác". Sắp theo mức lương cao (avg_max, rơi
    về avg_min) giảm dần."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                COALESCE(NULLIF(jp.matching_industry, ''), 'Khác') AS industry,
                COALESCE(NULLIF(l.level_code, ''), 'Khác') AS level,
                (avg(jp.salary_min) FILTER (WHERE jp.salary_min > 0))::float8 AS avg_min,
                (avg(jp.salary_max) FILTER (WHERE jp.salary_max > 0))::float8 AS avg_max,
                COALESCE(
                    NULLIF(count(*) FILTER (WHERE jp.salary_min > 0), 0),
                    count(*) FILTER (WHERE jp.salary_max > 0)
                ) AS sample_size
            FROM job_postings jp
            LEFT JOIN levels l ON l.level_id = jp.level_id
            WHERE COALESCE(NULLIF(jp.currency, ''), 'VNĐ') = 'VNĐ'
              AND jp.salary_period = 'MONTH'
              AND (jp.salary_min > 0 OR jp.salary_max > 0)
            GROUP BY 1, 2
            ORDER BY COALESCE(
                         avg(jp.salary_max) FILTER (WHERE jp.salary_max > 0),
                         avg(jp.salary_min) FILTER (WHERE jp.salary_min > 0),
                         0
                     ) DESC,
                     1 ASC, 2 ASC
            """
        )
        return cur.fetchall()


# ---------------------------------------------------------------
# Tab "Doanh nghiệp"
# ---------------------------------------------------------------

def get_high_potential_companies_without_contact(conn, *, quiet_days: int = 60) -> list[dict]:
    """companies_no_contact — công ty đang active, tiềm năng hợp tác CAO
    (HIGH), mà: (a) chưa có contact active nào (reason='no_contact'), hoặc
    (b) có contact nhưng không contact nào được liên hệ trong quiet_days
    ngày gần đây — 'contact_gone_cold' nếu từng liên hệ (last_contacted =
    lần gần nhất), 'never_contacted' nếu chưa lần nào. Chưa từng liên
    hệ (NULL) xếp TRƯỚC, sau đó lâu nhất trước.

    reason là MÃ (không phải câu tiếng Việt như Flask) — FE tự map nhãn:
    no_contact = "Chưa có contact nào", contact_gone_cold = "Contact đã
    nguội", never_contacted = "Có contact nhưng chưa từng liên hệ"."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT company_id, company_name, city, last_contacted,
                   CASE WHEN contact_count = 0 THEN 'no_contact'
                        WHEN last_contacted IS NOT NULL THEN 'contact_gone_cold'
                        ELSE 'never_contacted' END AS reason
            FROM (
                SELECT c.company_id, c.company_name,
                       p.province_name AS city,
                       count(cc.contact_id) AS contact_count,
                       max(cc.last_contacted_date) AS last_contacted,
                       COALESCE(bool_or(
                           cc.last_contacted_date IS NOT NULL
                           AND ({_VN_TODAY} - cc.last_contacted_date) < %(quiet_days)s
                       ), false) AS has_recent_contact
                FROM companies c
                LEFT JOIN provinces p ON p.province_id = c.province_id
                LEFT JOIN company_contacts cc
                       ON cc.company_id = c.company_id AND cc.is_active = true
                WHERE c.is_active = true
                  AND c.partnership_potential = 'HIGH'
                GROUP BY c.company_id, c.company_name, p.province_name
            ) agg
            WHERE contact_count = 0 OR NOT has_recent_contact
            ORDER BY last_contacted ASC NULLS FIRST, company_name ASC, company_id ASC
            """,
            {"quiet_days": quiet_days},
        )
        return cur.fetchall()


def get_contacts_needing_followup(conn, *, quiet_days: int = 14) -> list[dict]:
    """contacts_needing_followup — contact active, CHƯA 'IN_PARTNERSHIP',
    im lặng >= quiet_days ngày tính từ last_contacted_date, hoặc từ
    collected_date nếu chưa từng liên hệ (never_contacted=true). Cả 2 ngày
    đều NULL (dữ liệu thiếu) thì bỏ qua. Im lặng lâu nhất trước.

    Giống Flask, KHÔNG lọc theo trạng thái active của CÔNG TY (contact còn
    active của công ty đã xoá mềm vẫn hiện)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT cc.contact_id, cc.contact_name, cc.job_title,
                   cc.contact_status, cc.company_id, c.company_name,
                   cc.last_contacted_date, cc.collected_date,
                   ({_VN_TODAY} - COALESCE(cc.last_contacted_date, cc.collected_date)) AS quiet_days,
                   (cc.last_contacted_date IS NULL) AS never_contacted
            FROM company_contacts cc
            JOIN companies c ON c.company_id = cc.company_id
            WHERE cc.is_active = true
              AND cc.contact_status <> 'IN_PARTNERSHIP'
              AND COALESCE(cc.last_contacted_date, cc.collected_date) IS NOT NULL
              AND ({_VN_TODAY} - COALESCE(cc.last_contacted_date, cc.collected_date)) >= %(quiet_days)s
            ORDER BY quiet_days DESC, cc.contact_name ASC, cc.contact_id ASC
            """,
            {"quiet_days": quiet_days},
        )
        return cur.fetchall()


def get_company_job_activity(
    conn,
    *,
    expanding_days: int = 30,
    expanding_min_jobs: int = 2,
    quiet_days: int = 75,
    recent_jobs_shown: int = 5,
) -> dict:
    """companies_expanding / companies_quiet — trả {"expanding": [...],
    "quiet": [...]}. Chỉ xét công ty active, dựa trên MỌI job (mọi trạng
    thái) của công ty đó, theo ngày thu thập (VN).

    expanding: có >= expanding_min_jobs job thu thập trong expanding_days
    ngày gần đây; kèm recent_jobs = tối đa recent_jobs_shown tên job mới
    nhất trong khoảng đó (mới nhất trước). Nhiều job nhất trước.
    quiet: job MỚI NHẤT của công ty đã cách hôm nay >= quiet_days ngày.
    Im lặng lâu nhất trước."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            WITH jd AS (
                SELECT jp.company_id, {_vn_date('jp.created_at')} AS d
                FROM job_postings jp
                JOIN companies c ON c.company_id = jp.company_id AND c.is_active = true
            ),
            per_company AS (
                SELECT company_id,
                       max(d) AS last_job_date,
                       count(*) FILTER (WHERE ({_VN_TODAY} - d) <= %(expanding_days)s) AS recent_job_count
                FROM jd
                GROUP BY company_id
            )
            SELECT pc.company_id, c.company_name, p.province_name AS city,
                   pc.recent_job_count, pc.last_job_date,
                   ({_VN_TODAY} - pc.last_job_date) AS quiet_days
            FROM per_company pc
            JOIN companies c ON c.company_id = pc.company_id
            LEFT JOIN provinces p ON p.province_id = c.province_id
            WHERE pc.recent_job_count >= %(expanding_min_jobs)s
               OR ({_VN_TODAY} - pc.last_job_date) >= %(quiet_days)s
            """,
            {
                "expanding_days": expanding_days,
                "expanding_min_jobs": expanding_min_jobs,
                "quiet_days": quiet_days,
            },
        )
        rows = cur.fetchall()

        expanding = [r for r in rows if r["recent_job_count"] >= expanding_min_jobs]
        quiet = [r for r in rows if r["quiet_days"] >= quiet_days]

        titles_by_company: dict = {}
        if expanding:
            cur.execute(
                f"""
                SELECT company_id, job_title
                FROM (
                    SELECT jp.company_id, jp.job_title,
                           row_number() OVER (
                               PARTITION BY jp.company_id
                               ORDER BY {_vn_date('jp.created_at')} DESC,
                                        jp.created_at DESC, jp.job_id DESC
                           ) AS rn
                    FROM job_postings jp
                    WHERE jp.company_id = ANY(%(ids)s::uuid[])
                      AND ({_VN_TODAY} - {_vn_date('jp.created_at')}) <= %(expanding_days)s
                ) x
                WHERE rn <= %(shown)s
                ORDER BY company_id, rn
                """,
                {
                    "ids": [r["company_id"] for r in expanding],
                    "expanding_days": expanding_days,
                    "shown": recent_jobs_shown,
                },
            )
            for row in cur.fetchall():
                # Flask lấy 5 job mới nhất RỒI mới bỏ tên rỗng (không lấy bù
                # thêm cho đủ 5) — lọc sau khi đã cắt top N cho khớp.
                if row["job_title"]:
                    titles_by_company.setdefault(row["company_id"], []).append(row["job_title"])

    for r in expanding:
        r["recent_jobs"] = titles_by_company.get(r["company_id"], [])
    expanding.sort(key=lambda r: (-r["recent_job_count"], r["company_name"], r["company_id"]))
    quiet.sort(key=lambda r: (-r["quiet_days"], r["company_name"], r["company_id"]))
    # Trường không thuộc từng nhóm bị bỏ để response gọn, đúng shape Flask.
    expanding = [
        {k: r[k] for k in ("company_id", "company_name", "city", "recent_job_count", "recent_jobs")}
        for r in expanding
    ]
    quiet = [
        {k: r[k] for k in ("company_id", "company_name", "city", "quiet_days", "last_job_date")}
        for r in quiet
    ]
    return {"expanding": expanding, "quiet": quiet}


# ---------------------------------------------------------------
# Tab "Báo cáo tháng"
# ---------------------------------------------------------------

def get_monthly_recap_counts(conn) -> dict:
    """monthly_recap — phần đếm từ job/công ty (phần ứng tuyển/lưu job lấy
    riêng từ db.stats.get_monthly_engagement_stats() rồi ghép ở router).
    "Tháng này"/"tháng trước" theo lịch, giờ VN. Trả số THÔ (this/last),
    router tự tính % chênh lệch (làm tròn kiểu Python round() như Flask).

    jobs_expired: job có deadline RƠI TRONG tháng này VÀ đã qua (deadline <
    hôm nay), mọi trạng thái job. Công ty chỉ đếm công ty active."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            WITH b AS (
                SELECT {_VN_TODAY} AS today,
                       date_trunc('month', {_VN_TODAY}::timestamp)::date AS this_start,
                       (date_trunc('month', {_VN_TODAY}::timestamp) - interval '1 month')::date AS last_start,
                       (date_trunc('month', {_VN_TODAY}::timestamp) + interval '1 month')::date AS next_start
            )
            SELECT b.this_start, b.last_start,
                   count(*) FILTER (WHERE {_vn_date('jp.created_at')} >= b.this_start
                                      AND {_vn_date('jp.created_at')} <  b.next_start) AS jobs_this,
                   count(*) FILTER (WHERE {_vn_date('jp.created_at')} >= b.last_start
                                      AND {_vn_date('jp.created_at')} <  b.this_start) AS jobs_last,
                   count(*) FILTER (WHERE jp.deadline >= b.this_start
                                      AND jp.deadline <  b.next_start
                                      AND jp.deadline <  b.today) AS jobs_expired
            FROM b LEFT JOIN job_postings jp ON true
            GROUP BY b.this_start, b.last_start
            """
        )
        jobs = cur.fetchone()

        cur.execute(
            f"""
            WITH b AS (
                SELECT date_trunc('month', {_VN_TODAY}::timestamp)::date AS this_start,
                       (date_trunc('month', {_VN_TODAY}::timestamp) - interval '1 month')::date AS last_start,
                       (date_trunc('month', {_VN_TODAY}::timestamp) + interval '1 month')::date AS next_start
            )
            SELECT count(*) FILTER (WHERE {_vn_date('c.created_at')} >= b.this_start
                                      AND {_vn_date('c.created_at')} <  b.next_start) AS companies_this,
                   count(*) FILTER (WHERE {_vn_date('c.created_at')} >= b.last_start
                                      AND {_vn_date('c.created_at')} <  b.this_start) AS companies_last
            FROM b LEFT JOIN companies c ON c.is_active = true
            GROUP BY b.this_start, b.last_start
            """
        )
        companies = cur.fetchone()

        cur.execute(
            f"""
            WITH b AS (
                SELECT date_trunc('month', {_VN_TODAY}::timestamp)::date AS this_start,
                       (date_trunc('month', {_VN_TODAY}::timestamp) - interval '1 month')::date AS last_start,
                       (date_trunc('month', {_VN_TODAY}::timestamp) + interval '1 month')::date AS next_start
            )
            SELECT COALESCE(NULLIF(jp.matching_industry, ''), 'Khác') AS industry,
                   count(*) FILTER (WHERE {_vn_date('jp.created_at')} >= b.this_start
                                      AND {_vn_date('jp.created_at')} <  b.next_start) AS this_count,
                   count(*) FILTER (WHERE {_vn_date('jp.created_at')} >= b.last_start
                                      AND {_vn_date('jp.created_at')} <  b.this_start) AS last_count
            FROM job_postings jp CROSS JOIN b
            GROUP BY 1
            HAVING count(*) FILTER (WHERE {_vn_date('jp.created_at')} >= b.this_start
                                      AND {_vn_date('jp.created_at')} <  b.next_start) > 0
            ORDER BY this_count DESC, industry ASC
            LIMIT 3
            """
        )
        top_industries = cur.fetchall()

        cur.execute(
            f"""
            WITH b AS (
                SELECT date_trunc('month', {_VN_TODAY}::timestamp)::date AS this_start,
                       (date_trunc('month', {_VN_TODAY}::timestamp) + interval '1 month')::date AS next_start
            )
            SELECT jp.company_id, c.company_name, count(*) AS count
            FROM job_postings jp
            JOIN companies c ON c.company_id = jp.company_id
            CROSS JOIN b
            WHERE {_vn_date('jp.created_at')} >= b.this_start
              AND {_vn_date('jp.created_at')} <  b.next_start
            GROUP BY jp.company_id, c.company_name
            ORDER BY count DESC, c.company_name ASC, jp.company_id ASC
            LIMIT 5
            """
        )
        top_companies = cur.fetchall()

    return {
        "this_month_start": jobs["this_start"],
        "last_month_start": jobs["last_start"],
        "jobs_this": jobs["jobs_this"],
        "jobs_last": jobs["jobs_last"],
        "jobs_expired": jobs["jobs_expired"],
        "companies_this": companies["companies_this"],
        "companies_last": companies["companies_last"],
        "top_industries": top_industries,
        "top_companies": top_companies,
    }
