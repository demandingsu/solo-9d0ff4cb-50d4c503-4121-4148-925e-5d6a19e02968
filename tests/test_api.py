"""API 层测试：限值接收、JSON 响应数值精度、GET/POST 一致性。"""
import functools
import http.server
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

DETAIL_VALUES = {
    "d001": "0.0100000001 mg/L",
    "d002": "10.0000001 μg/L",
    "d003": "&lt;0.0100000001 mg/L",
    "d004": "&lt;10.0000001 μg/L",
    "d005": "0.01 mg/L",
    "d006": "&lt;0.01 mg/L",
}

EXPECTED = {
    "AS-0001": "超限",
    "AS-0002": "超限",
    "AS-0003": "无法判定",
    "AS-0004": "无法判定",
    "AS-0005": "未超限",
    "AS-0006": "未超限",
}


@pytest.fixture(scope="module")
def precision_site(tmp_path_factory):
    site = tmp_path_factory.mktemp("precision_site")
    (site / "list").mkdir()
    (site / "detail").mkdir()
    rows = []
    for i, (name, value) in enumerate(DETAIL_VALUES.items(), 1):
        (site / "detail" / f"{name}.html").write_text(
            "<html><body><table>"
            f"<tr><th>样本编号</th><td>AS-{i:04d}</td></tr>"
            "<tr><th>取样点</th><td>SP-01</td></tr>"
            "<tr><th>采样时间</th><td>2026-11-13 08:00</td></tr>"
            f"<tr><th>砷结果</th><td>{value}</td></tr>"
            "</table></body></html>",
            encoding="utf-8",
        )
        rows.append(f'<tr><td><a class="detail-link" href="/detail/{name}.html">详情</a></td></tr>')
    (site / "list" / "page1.html").write_text(
        "<html><body><table>" + "".join(rows) + "</table></body></html>",
        encoding="utf-8",
    )

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _samples_by_id(response):
    return {s["sample_id"]: s for s in response.json()["samples"]}


def test_post_limit_0_01_flags_just_above_values(client, precision_site):
    response = client.post(
        "/check",
        json={
            "list_url": f"{precision_site}/list/page1.html",
            "sampling_point": "SP-01",
            "limit_mg_l": 0.01,
        },
    )
    assert response.status_code == 200
    by_id = _samples_by_id(response)
    for sample_id, status in EXPECTED.items():
        assert by_id[sample_id]["status"] == status, sample_id

    # mg/L 字段仍是 JSON number，并保留足够十进制位
    body = response.text
    assert '"arsenic_mg_l": 0.0100000001' in body
    assert '"limit_mg_l": 0.01' in body
    assert json.loads(body)["samples"][0]["arsenic_mg_l"] == 0.0100000001


def test_get_is_consistent_with_post(client, precision_site):
    params = {
        "list_url": f"{precision_site}/list/page1.html",
        "sampling_point": "SP-01",
        "limit_mg_l": "0.0100000001",
    }
    response = client.get("/check", params=params)
    assert response.status_code == 200
    assert '"limit_mg_l": 0.0100000001' in response.text
    by_id = _samples_by_id(response)
    # 限值与结果相等：数值结果不算超限，检出限等于限值也不算无法判定
    assert by_id["AS-0001"]["status"] == "未超限"
    assert by_id["AS-0003"]["status"] == "未超限"


def test_post_keeps_more_limit_decimals_than_float(client, precision_site):
    """请求体的限值用 Decimal 解析，位数超出 float 表示能力也不失真。"""
    raw = (
        '{"list_url": "%s/list/page1.html", "sampling_point": "SP-01", '
        '"limit_mg_l": 0.010000000100000000000000000001}'
    ) % precision_site
    response = client.post(
        "/check", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert '"limit_mg_l": 0.010000000100000000000000000001' in response.text
    # 限值 > 0.0100000001：先前“超限”的样本此时都不超限
    by_id = _samples_by_id(response)
    assert by_id["AS-0001"]["status"] == "未超限"
    assert by_id["AS-0003"]["status"] == "未超限"


@pytest.mark.parametrize("bad_limit", [-1, -0.0000000001, "NaN", "abc"])
def test_invalid_limits_rejected(client, precision_site, bad_limit):
    response = client.post(
        "/check",
        json={
            "list_url": f"{precision_site}/list/page1.html",
            "sampling_point": "SP-01",
            "limit_mg_l": bad_limit,
        },
    )
    assert response.status_code == 422
