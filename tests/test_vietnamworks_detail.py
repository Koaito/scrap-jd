"""
Test trang chi tiết VietnamWorks (đợt B/VNW, 10/2026) — KHÔNG cần database,
KHÔNG cần internet:
  - adapters/vietnamworks_detail.parse_detail_page() (giải mã luồng Next.js),
  - luồng tải chi tiết của VietnamWorksAdapter.fetch_jobs() (level, JD đầy đủ,
    lỗi tải, không giải mã được, job đã biết).

Vì sao có luồng này: API search cắt jobDescription/jobRequirement và luôn trả
yearsOfExperience=0, nên level của VietnamWorks gần như luôn rơi về "Junior"
(891/1291 job trong DB thật, 0 job Middle).
"""

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import normalize
from adapters.base import CrawlBlockedError
from adapters.vietnamworks import (
    ANOMALY_DETAIL_UNPARSABLE,
    VietnamWorksAdapter,
    _level_hint_from_job_level,
)
from adapters.vietnamworks_detail import is_gone_page, parse_detail_page
from snapshots import SnapshotRecorder
from vnw_page_builder import build_detail_html

HERE = os.path.dirname(__file__)
CATEGORY = "data-analyst"
URL = "https://www.vietnamworks.com/data-engineer-2109772-jv"


def _read(name: str) -> str:
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def _listing_job(**over) -> dict:
    """Job kiểu response search: yêu cầu BỊ CẮT và yearsOfExperience=0, đúng như
    API thật."""
    job = {
        "jobId": 2109772,
        "jobTitle": "Data Engineer",
        "jobUrl": URL,
        "companyName": "Trường Đại học VinUni",
        "companyId": 298856,
        "approvedOnText": "Đăng 12 ngày trước",
        "expiredOn": "2026-10-21T23:59:59+07:00",
        "typeWorkingId": 1,
        "yearsOfExperience": 0,
        "jobLevel": "Experienced (non-manager)",
        "jobDescription": "<p>We are looking for a Data Engineer...</p>",
        "jobRequirement": "<p>- Bachelor degree. - 3+ years of experience in Data Eng...</p>",
        "skills": [{"skillName": "PYTHON"}],
        "benefits": [],
        "workingLocations": [{"cityNameVI": "Hà Nội"}],
    }
    job.update(over)
    return job


def _detail_job(**over) -> dict:
    """Cùng job nhưng như trang chi tiết: đầy đủ + yearsOfExperience thật."""
    job = _listing_job(
        yearsOfExperience=3,
        jobDescription="<p>Thiết kế và vận hành data pipeline cho trường đại học.</p>",
        jobRequirement=(
            "<p><strong>Yêu cầu: </strong></p><p>- Có 3+ năm kinh nghiệm Data Engineering. </p>"
            "<p>- Thành thạo Python, SQL, Airflow, Kafka. </p>"
        ),
        skills=[{"skillName": "PYTHON", "skillId": 1}, {"skillName": "Data Engineering", "skillId": 2}],
        benefits=[{"benefitNameVI": "Đào tạo", "benefitValue": "Truy cập tài nguyên học tập"}],
    )
    job.update(over)
    return job


def _adapter(pages: dict, listing_jobs: list, recorder=None):
    """Adapter với search trả `listing_jobs` (1 trang) và GET trả `pages[url]`
    (str = HTML; None = tải thất bại). Ghi lại các URL đã GET vào adapter.requested."""
    adapter = VietnamWorksAdapter()
    adapter.requested = []
    adapter._post_json = lambda url, body, **kw: {"meta": {"nbPages": 1}, "data": listing_jobs}

    def fake_fetch(url, max_retries=3):
        adapter.requested.append(url)
        return pages[url]

    adapter._fetch_html = fake_fetch
    if recorder is not None:
        adapter.set_snapshot_recorder(recorder)
    return adapter


# ----------------------------------------------------------------------
# parse_detail_page — trang thật
# ----------------------------------------------------------------------
def test_parse_real_detail_page():
    """Trang thật job 2109772 (đã ẩn email): bản đầy đủ + yearsOfExperience."""
    job = parse_detail_page(_read("fixture_vietnamworks_detail.html"))

    assert job is not None
    assert job["jobId"] == 2109772
    assert job["jobTitle"] == "Data Engineer"
    assert job["yearsOfExperience"] == 3          # API search luôn trả 0
    assert job["jobLevel"] == "Experienced (non-manager)"
    assert job["approvedOnText"].startswith("Đăng ")
    # Yêu cầu ĐẦY ĐỦ (API search chỉ trả ~300 ký tự rồi "...")
    assert len(job["jobRequirement"]) > 1500
    assert not job["jobRequirement"].rstrip().endswith("...")
    assert "3+ years of experience" in job["jobRequirement"]
    assert len(job["jobDescription"]) > 1000
    assert [s["skillName"] for s in job["skills"]][:2] == ["PYTHON", "Data Engineer"]
    assert len(job["benefits"]) == 2
    assert "$" not in job["jobRequirement"][:3]    # tham chiếu "$25" đã được giải


# ----------------------------------------------------------------------
# parse_detail_page — trang dựng bằng builder (khoá từng đặc điểm định dạng)
# ----------------------------------------------------------------------
def test_parse_roundtrip_with_multibyte_text():
    """Độ dài dòng T tính theo BYTE: tiếng Việt có dấu mà dùng số ký tự sẽ cắt
    sai và làm lệch mọi dòng phía sau."""
    job = _detail_job(jobRequirement="<p>Yêu cầu: Tốt nghiệp Đại học, có ‘3 năm’ kinh nghiệm ✓</p>" * 5)
    parsed = parse_detail_page(build_detail_html(job))

    assert parsed["jobRequirement"] == job["jobRequirement"]
    assert parsed["jobDescription"] == job["jobDescription"]
    assert parsed["skills"] == job["skills"]
    assert parsed["benefits"] == job["benefits"]
    assert parsed["workingLocations"] == job["workingLocations"]


@pytest.mark.parametrize("split_into", [1, 2, 7, 40])
def test_parse_joins_any_number_of_push_chunks(split_into):
    job = _detail_job()
    parsed = parse_detail_page(build_detail_html(job, split_into=split_into))
    assert parsed["jobRequirement"] == job["jobRequirement"]


def test_parse_returns_none_when_page_is_not_a_job_page():
    assert parse_detail_page("") is None
    assert parse_detail_page(None) is None
    assert parse_detail_page("<html><body>Access denied</body></html>") is None
    # có luồng nhưng không có object job
    assert parse_detail_page(build_detail_html({"foo": "bar"}, decoy_rows=True)) is None


def test_parse_returns_none_on_corrupt_stream():
    html = '<script>self.__next_f.push([1,"abc:{\\"jobId\\":1,'  # chuỗi JS cụt
    assert parse_detail_page(html) is None


def test_parse_escaped_dollar_and_dangling_reference():
    """"$$" là dấu "$" đã escape; "$ff" trỏ tới dòng không tồn tại thì để nguyên."""
    job = _detail_job(jobTitle="$$5 bonus", companyName="$ff")
    parsed = parse_detail_page(build_detail_html(job))
    assert parsed["jobTitle"] == "$5 bonus"
    assert parsed["companyName"] == "$ff"


# ----------------------------------------------------------------------
# _level_hint_from_job_level
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "label, expected",
    [
        ("Fresher/Entry level", "Fresher"),
        ("Manager", "Manager"),
        ("Director and above", "Manager"),
        ("Intern/Student", "Intern"),
        # BẪY: chứa chữ "manager" nhưng là nhân viên thường
        ("Experienced (non-manager)", ""),
        ("", ""),
        (None, ""),
        ("Nhãn lạ", ""),
    ],
)
def test_level_hint_from_job_level(label, expected):
    assert _level_hint_from_job_level(label) == expected


# ----------------------------------------------------------------------
# fetch_jobs: tải chi tiết cho job mới
# ----------------------------------------------------------------------
def test_new_job_uses_detail_years_and_full_text():
    listing = _listing_job()
    adapter = _adapter({URL: build_detail_html(_detail_job())}, [listing])

    (rec,) = list(adapter.fetch_jobs(CATEGORY, max_pages=1))

    assert adapter.requested == [URL]
    assert rec.experience_text == "3 năm"                  # từ yearsOfExperience của trang chi tiết
    assert normalize.infer_level(rec.experience_text, rec.job_title, rec.level_hint) == "Middle"
    assert rec.posted_text == "Đăng 12 ngày trước"
    # JD đầy đủ nằm trong cache cho pipeline (không còn bản cắt "...")
    detail = adapter.fetch_job_full_detail(URL)
    assert "Airflow, Kafka" in detail["requirements"]
    assert not detail["requirements"].rstrip().endswith("...")
    assert detail["job_description"] == "Thiết kế và vận hành data pipeline cho trường đại học."
    assert detail["required_skills"] == ["PYTHON", "Data Engineering"]
    assert detail["perks"] == "Đào tạo: Truy cập tài nguyên học tập"


def test_years_read_from_requirement_text_when_structured_years_is_zero():
    page = build_detail_html(_detail_job(
        yearsOfExperience=0,
        jobRequirement="<p>- Có từ 5 năm kinh nghiệm trong Marketing, Growth.</p>",
    ))
    (rec,) = list(_adapter({URL: page}, [_listing_job()]).fetch_jobs(CATEGORY, max_pages=1))
    assert rec.experience_text == "5 năm"


def test_range_in_requirement_takes_minimum():
    page = build_detail_html(_detail_job(
        yearsOfExperience=0,
        jobRequirement="<p>- Có 3–5 năm kinh nghiệm về Data Architect.</p>",
    ))
    (rec,) = list(_adapter({URL: page}, [_listing_job()]).fetch_jobs(CATEGORY, max_pages=1))
    assert rec.experience_text == "3 năm"
    assert normalize.infer_level(rec.experience_text, rec.job_title, rec.level_hint) == "Middle"


def test_no_experience_required_maps_to_fresher():
    page = build_detail_html(_detail_job(
        yearsOfExperience=0, jobRequirement="<p>- Không yêu cầu kinh nghiệm, sẽ được đào tạo.</p>",
    ))
    (rec,) = list(_adapter({URL: page}, [_listing_job()]).fetch_jobs(CATEGORY, max_pages=1))
    assert normalize.infer_level(rec.experience_text, rec.job_title, rec.level_hint) == "Fresher"


def test_job_level_is_fallback_only_when_no_years_found():
    """Không có số năm ở đâu cả -> jobLevel quyết định; có số năm -> bỏ qua jobLevel."""
    no_years = build_detail_html(_detail_job(
        yearsOfExperience=0, jobRequirement="<p>- Tốt nghiệp đại học.</p>",
        jobLevel="Manager", jobTitle="Quản lý vận hành",
    ))
    (rec,) = list(_adapter({URL: no_years}, [_listing_job(jobTitle="Quản lý vận hành")]).fetch_jobs(CATEGORY, 1))
    assert rec.experience_text == ""
    assert rec.level_hint == "Manager"
    assert normalize.infer_level(rec.experience_text, rec.job_title, rec.level_hint) == "Manager"

    with_years = build_detail_html(_detail_job(jobLevel="Manager", jobTitle="Data Modeler"))
    (rec,) = list(_adapter({URL: with_years}, [_listing_job(jobTitle="Data Modeler")]).fetch_jobs(CATEGORY, 1))
    assert normalize.infer_level(rec.experience_text, rec.job_title, rec.level_hint) == "Middle"


def test_non_manager_label_does_not_become_manager():
    page = build_detail_html(_detail_job(yearsOfExperience=0, jobRequirement="<p>Tốt nghiệp đại học</p>"))
    (rec,) = list(_adapter({URL: page}, [_listing_job()]).fetch_jobs(CATEGORY, max_pages=1))
    assert rec.level_hint == ""
    assert normalize.infer_level(rec.experience_text, rec.job_title, rec.level_hint) == "Junior"


def test_detail_values_override_search_but_blank_detail_keeps_search_value():
    detail = _detail_job(approvedOnText="")        # trang chi tiết để trống posted
    (rec,) = list(_adapter({URL: build_detail_html(detail)}, [_listing_job()]).fetch_jobs(CATEGORY, 1))
    assert rec.posted_text == "Đăng 12 ngày trước"  # giữ giá trị từ search


# ----------------------------------------------------------------------
# fetch_jobs: lỗi tải / không giải mã được / job đã biết
# ----------------------------------------------------------------------
def test_job_skipped_when_detail_page_cannot_be_fetched():
    adapter = _adapter({URL: None}, [_listing_job()])
    # Không yield -> pipeline không thể lưu bản JD cắt ngắn một cách âm thầm;
    # job chưa insert nên lượt sau vẫn được nhặt lại. Tải lỗi KHÔNG phải bất thường cấu trúc.
    assert list(adapter.fetch_jobs(CATEGORY, max_pages=1)) == []
    assert adapter.listing_anomalies == []


def test_one_failed_job_does_not_stop_the_others():
    url2 = "https://www.vietnamworks.com/data-analyst-2222-jv"
    listing = [_listing_job(), _listing_job(jobId=2222, jobUrl=url2, jobTitle="Data Analyst")]
    pages = {URL: None, url2: build_detail_html(_detail_job(jobId=2222, jobUrl=url2, jobTitle="Data Analyst"))}
    recs = list(_adapter(pages, listing).fetch_jobs(CATEGORY, max_pages=1))
    assert [r.source_url for r in recs] == [url2]


def test_unparsable_detail_falls_back_to_search_data_and_flags_degraded():
    rec_snap = SnapshotRecorder()
    adapter = _adapter({URL: "<html><body>trang đổi cấu trúc</body></html>"}, [_listing_job()], rec_snap)

    (rec,) = list(adapter.fetch_jobs(CATEGORY, max_pages=1))

    # Vẫn yield (như hành vi trước khi có luồng chi tiết) nhưng báo bất thường
    assert rec.source_url == URL
    assert adapter.listing_anomalies == [ANOMALY_DETAIL_UNPARSABLE]
    assert ("detail", "detail_unparsable") in [(i.kind, i.reason) for i in rec_snap.items]
    # yêu cầu trong cache là bản cắt từ search; số năm vẫn đọc được từ đoạn còn lại
    assert rec.experience_text == "3 năm"
    assert adapter.fetch_job_full_detail(URL)["requirements"].startswith("- Bachelor degree")


def test_detail_page_of_a_different_job_is_not_trusted():
    other = build_detail_html(_detail_job(jobId=999, yearsOfExperience=9))
    adapter = _adapter({URL: other}, [_listing_job()])
    (rec,) = list(adapter.fetch_jobs(CATEGORY, max_pages=1))
    assert rec.experience_text != "9 năm"
    assert adapter.listing_anomalies == [ANOMALY_DETAIL_UNPARSABLE]


def test_known_url_is_skipped_before_any_request():
    """Job đã có trong DB và không cần vá -> không tải trang chi tiết (tiết kiệm request)."""
    adapter = _adapter({}, [_listing_job()])          # pages rỗng: tải bất kỳ URL nào sẽ KeyError
    adapter.set_known_url_checker(lambda url: True)

    assert list(adapter.fetch_jobs(CATEGORY, max_pages=1)) == []
    assert adapter.requested == []
    assert adapter.skipped_known_count == 1


def test_job_needing_patch_still_goes_through():
    adapter = _adapter({URL: build_detail_html(_detail_job())}, [_listing_job()])
    adapter.set_known_url_checker(lambda url: False)
    assert len(list(adapter.fetch_jobs(CATEGORY, max_pages=1))) == 1
    assert adapter.skipped_known_count == 0


def test_circuit_breaker_still_trips_through_detail_requests():
    """Lỗi tải trang chi tiết liên tiếp phải ngắt mạch như các request khác."""
    adapter = VietnamWorksAdapter()
    adapter._post_json = lambda url, body, **kw: {"meta": {"nbPages": 1}, "data": [_listing_job()]}

    def blocked(url, max_retries=3):
        adapter._note_fetch_failure(url, "HTTP 403")
        return None

    adapter._fetch_html = blocked
    adapter._block_threshold = 1
    with pytest.raises(CrawlBlockedError):
        list(adapter.fetch_jobs(CATEGORY, max_pages=1))


def test_snapshot_keeps_one_detail_sample():
    rec_snap = SnapshotRecorder()
    adapter = _adapter({URL: build_detail_html(_detail_job())}, [_listing_job()], rec_snap)
    list(adapter.fetch_jobs(CATEGORY, max_pages=1))
    assert [(i.kind, i.reason) for i in rec_snap.items] == [("listing", "sample"), ("detail", "sample")]


def test_search_payload_is_not_mutated():
    listing = _listing_job()
    before = copy.deepcopy(listing)
    list(_adapter({URL: build_detail_html(_detail_job())}, [listing]).fetch_jobs(CATEGORY, max_pages=1))
    assert listing == before
    json.dumps(listing)  # vẫn serialize được (snapshot dùng JSON)


# ----------------------------------------------------------------------
# Trang "tin đã bị gỡ"
# ----------------------------------------------------------------------
def test_gone_page_real_file_is_detected_and_not_parsed():
    """File thật job 2089555: HTTP 200, không có dữ liệu job, chỉ có lệnh
    chuyển hướng NEXT_REDIRECT tới /410."""
    html = _read("fixture_vietnamworks_gone.html")
    assert parse_detail_page(html) is None
    assert is_gone_page(html) is True


def test_real_job_page_is_not_mistaken_for_gone():
    html = _read("fixture_vietnamworks_detail.html")
    assert parse_detail_page(html) is not None
    assert is_gone_page(html) is False


def test_is_gone_page_ignores_other_redirects_and_empty():
    assert is_gone_page("") is False
    assert is_gone_page(None) is False
    assert is_gone_page('b:E{"digest":"NEXT_REDIRECT;replace;https://www.vietnamworks.com/login;307;"}') is False
