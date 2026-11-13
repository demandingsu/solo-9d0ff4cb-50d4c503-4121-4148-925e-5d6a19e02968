"""scraper 单元测试 + 针对样例站点的端到端采集测试。"""
import functools
import http.server
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from scraper import (  # noqa: E402
    STATUS_EXCEEDS,
    STATUS_UNDETERMINABLE,
    STATUS_WITHIN,
    ArsenicReading,
    classify,
    collect,
    parse_arsenic,
)

LIMIT = 0.01  # mg/L


# ---------- parse_arsenic ----------

@pytest.mark.parametrize(
    "text, kind, mg_l",
    [
        ("0.004 mg/L", "value", 0.004),
        ("12 μg/L", "value", 0.012),
        ("10.1 µg/L", "value", 0.0101),
        ("0.5 毫克/升", "value", 0.5),
        ("<0.05 mg/L", "below_detection_limit", 0.05),
        ("<5 μg/L", "below_detection_limit", 0.005),
        ("＜0.01 mg/L", "below_detection_limit", 0.01),
        ("≤2 微克/升", "below_detection_limit", 0.002),
    ],
)
def test_parse_arsenic(text, kind, mg_l):
    r = parse_arsenic(text)
    assert r is not None
    assert r.kind == kind
    assert r.mg_l == pytest.approx(mg_l)


@pytest.mark.parametrize("text", ["", None, "未检出", "abc", "0.01", "mg/L"])
def test_parse_arsenic_unparseable(text):
    assert parse_arsenic(text) is None


# ---------- classify ----------

def test_value_above_limit_exceeds():
    assert classify(ArsenicReading("value", 0.0101, ""), LIMIT) == STATUS_EXCEEDS


def test_value_equal_limit_not_exceeds():
    assert classify(ArsenicReading("value", 0.01, ""), LIMIT) == STATUS_WITHIN


def test_value_below_limit_not_exceeds():
    assert classify(ArsenicReading("value", 0.009, ""), LIMIT) == STATUS_WITHIN


def test_dl_above_limit_undeterminable():
    assert classify(ArsenicReading("below_detection_limit", 0.05, ""), LIMIT) == STATUS_UNDETERMINABLE


def test_dl_equal_limit_not_exceeds():
    # 真值严格小于检出限，检出限等于限值 ⇒ 真值 < 限值
    assert classify(ArsenicReading("below_detection_limit", 0.01, ""), LIMIT) == STATUS_WITHIN


def test_dl_below_limit_not_exceeds():
    assert classify(ArsenicReading("below_detection_limit", 0.005, ""), LIMIT) == STATUS_WITHIN


# ---------- 端到端：对样例站点采集 ----------

@pytest.fixture(scope="module")
def site_base():
    site_dir = os.path.join(os.path.dirname(__file__), "..", "sample_site", "html")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=site_dir)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_collect_end_to_end(site_base):
    result = collect(f"{site_base}/list/page1.html", "SP-01", LIMIT)

    # 同站分页：3 个列表页全部访问，外站“下一页”被忽略
    assert result["pages_visited"] == [
        f"{site_base}/list/page1.html",
        f"{site_base}/list/page2.html",
        f"{site_base}/list/page3.html",
    ]

    # 去重：d002 出现两次只计一次；外站链接被忽略
    assert result["duplicates_skipped"] == 1
    assert result["details_found"] == 10

    # 失效详情 d999 进入失败列表，结果标记不完整
    assert result["incomplete"] is True
    assert [f["url"] for f in result["failed_details"]] == [f"{site_base}/detail/d999.html"]

    # 仅返回 SP-01 的样本（SP-02 的 d005 被过滤）
    by_id = {s["sample_id"]: s for s in result["samples"]}
    assert set(by_id) == {
        "AS-2026-1101", "AS-2026-1102", "AS-2026-1103", "AS-2026-1104",
        "AS-2026-1106", "AS-2026-1107", "AS-2026-1108", "AS-2026-1109",
    }

    expected = {
        "AS-2026-1101": (STATUS_WITHIN, 0.004),        # 0.004 mg/L
        "AS-2026-1102": (STATUS_EXCEEDS, 0.012),       # 12 μg/L
        "AS-2026-1103": (STATUS_UNDETERMINABLE, 0.05), # <0.05 mg/L，检出限 > 限值
        "AS-2026-1104": (STATUS_WITHIN, 0.005),        # <5 μg/L
        "AS-2026-1106": (STATUS_WITHIN, 0.01),         # 等于限值不算超限
        "AS-2026-1107": (STATUS_WITHIN, 0.0098),       # 9.8 μg/L
        "AS-2026-1108": (STATUS_EXCEEDS, 0.0101),      # 10.1 μg/L
        "AS-2026-1109": (STATUS_WITHIN, 0.01),         # <0.01 mg/L，检出限 = 限值
    }
    for sample_id, (status, mg_l) in expected.items():
        sample = by_id[sample_id]
        assert sample["status"] == status, sample_id
        assert sample["arsenic_mg_l"] == pytest.approx(mg_l), sample_id
        assert sample["source_url"].startswith(site_base), sample_id
        assert sample["sampling_point"] == "SP-01"
        assert sample["sampled_at"]
