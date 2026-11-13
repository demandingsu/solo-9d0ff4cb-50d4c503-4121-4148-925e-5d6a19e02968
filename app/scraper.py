"""沿同站分页采集实验室网页上的砷检测记录。

规则约定（与 README 一致）：
- 列表页中的详情链接：优先取 ``<a class="detail-link">``，若页面没有则回退到
  href 中包含 ``detail`` 的链接；只接受与列表 URL 同站（同一 host[:port]）的链接。
- 下一页：优先 ``<a rel="next">``，回退到链接文本为 “下一页 / 下页 / next” 的链接，
  同样只跟随同站链接。
- 详情页字段从 ``th→td`` 或 ``dt→dd`` 键值对中提取，按中文标签模糊匹配。
- 重复详情链接只处理一次；详情读取失败记入 failed_details 并将 incomplete 置为 True。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

MAX_LIST_PAGES = 50      # 防止分页失控
MAX_DETAIL_PAGES = 500   # 防止详情抓取失控
REQUEST_TIMEOUT = 10.0
USER_AGENT = "arsenic-qc-collector/1.0"

STATUS_EXCEEDS = "超限"
STATUS_WITHIN = "未超限"
STATUS_UNDETERMINABLE = "无法判定"

# 形如 "0.004 mg/L"、"<0.05 mg/L"、"<5 μg/L"、"≤1 微克/升"
_RESULT_RE = re.compile(
    r"""^\s*
    (?P<flag><|≤|＜|<=|＜=)?\s*
    (?P<num>\d+(?:\.\d+)?)
    \s*(?P<unit>mg/l|μg/l|µg/l|ug/l|毫克/升|微克/升)
    \s*$""",
    re.IGNORECASE | re.VERBOSE,
)

_UNIT_FACTORS = {
    "mg/l": 1.0,
    "毫克/升": 1.0,
    "μg/l": 0.001,
    "ug/l": 0.001,
    "微克/升": 0.001,
}

# 字段中文标签（按优先级排列，包含匹配）
_FIELD_LABELS = {
    "sample_id": ("样本编号", "样品编号", "样本号", "样品号"),
    "sampling_point": ("取样点编号", "取样点", "采样点编号", "采样点"),
    "sampled_at": ("采样时间", "取样时间", "采样日期"),
    "arsenic": ("砷结果", "砷含量", "砷"),
}

_NEXT_TEXTS = {"下一页", "下页", "next"}


class DetailParseError(Exception):
    """详情页结构不符合预期（缺少必填字段）。"""


@dataclass
class ArsenicReading:
    kind: str      # "value" | "below_detection_limit"
    mg_l: float    # 换算为 mg/L 后的数值（数值结果或检出限）
    raw: str


def parse_arsenic(text: str | None) -> ArsenicReading | None:
    """解析砷结果文本，返回 None 表示无法解析。"""
    m = _RESULT_RE.match(text or "")
    if not m:
        return None
    unit = m.group("unit").lower().replace("µ", "μ")
    # round 避免 9.8 * 0.001 = 0.009800000000000001 之类的浮点噪声
    value = round(float(m.group("num")) * _UNIT_FACTORS[unit], 9)
    kind = "below_detection_limit" if m.group("flag") else "value"
    return ArsenicReading(kind=kind, mg_l=value, raw=(text or "").strip())


def classify(reading: ArsenicReading, limit_mg_l: float) -> str:
    """按限值判定单样本状态。

    - 数值结果：> 限值 → 超限；≤ 限值（含等于）→ 未超限。
    - "<检出限"：检出限 > 限值 → 无法判定；检出限 ≤ 限值 → 未超限。
    """
    if reading.kind == "value":
        return STATUS_EXCEEDS if reading.mg_l > limit_mg_l else STATUS_WITHIN
    if reading.mg_l > limit_mg_l:
        return STATUS_UNDETERMINABLE
    return STATUS_WITHIN


def _normalize(url: str, base: str | None = None) -> str:
    absolute = urljoin(base, url) if base else url
    return urldefrag(absolute)[0]


def _same_site(url: str, site_netloc: str) -> bool:
    return urlsplit(url).netloc.lower() == site_netloc


def _extract_detail_links(soup: BeautifulSoup, page_url: str, site_netloc: str) -> list[str]:
    anchors = soup.select("a.detail-link[href]")
    if not anchors:
        anchors = soup.select('a[href*="detail"]')
    links = []
    for a in anchors:
        url = _normalize(a["href"], page_url)
        if _same_site(url, site_netloc):
            links.append(url)
    return links


def _extract_next(soup: BeautifulSoup, page_url: str, site_netloc: str) -> str | None:
    anchor = soup.select_one('a[rel~="next"][href]')
    if anchor is None:
        for cand in soup.find_all("a", href=True):
            if cand.get_text(strip=True).lower() in _NEXT_TEXTS:
                anchor = cand
                break
    if anchor is None:
        return None
    url = _normalize(anchor["href"], page_url)
    return url if _same_site(url, site_netloc) else None


def _extract_fields(soup: BeautifulSoup) -> dict[str, str]:
    fields: dict[str, str] = {}
    for th in soup.find_all("th"):
        td = th.find_next_sibling("td")
        if td:
            fields[th.get_text(strip=True)] = td.get_text(" ", strip=True)
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            fields[dt.get_text(strip=True)] = dd.get_text(" ", strip=True)
    return fields


def _pick(fields: dict[str, str], labels: tuple[str, ...]) -> str | None:
    for label in labels:
        for key, value in fields.items():
            if label in key:
                return value
    return None


def _parse_detail(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    fields = _extract_fields(soup)
    record = {
        "sample_id": _pick(fields, _FIELD_LABELS["sample_id"]),
        "sampling_point": _pick(fields, _FIELD_LABELS["sampling_point"]),
        "sampled_at": _pick(fields, _FIELD_LABELS["sampled_at"]),
        "arsenic_raw": _pick(fields, _FIELD_LABELS["arsenic"]),
        "source_url": url,
    }
    missing = [
        name
        for name in ("sample_id", "sampling_point", "sampled_at", "arsenic_raw")
        if not record[name]
    ]
    if missing:
        raise DetailParseError(f"详情页缺少字段: {', '.join(missing)}")
    record["sampling_point"] = record["sampling_point"].strip()
    return record


def collect(list_url: str, sampling_point: str, limit_mg_l: float) -> dict:
    """从分页列表 URL 出发，采集指定取样点的砷检测记录并逐样本判定。"""
    list_url = _normalize(list_url)
    site_netloc = urlsplit(list_url).netloc.lower()
    if not site_netloc:
        raise ValueError(f"无效的列表 URL: {list_url}")

    result: dict = {
        "sampling_point": sampling_point,
        "limit_mg_l": limit_mg_l,
        "incomplete": False,
        "failed_details": [],
        "pages_visited": [],
        "details_found": 0,
        "duplicates_skipped": 0,
        "samples": [],
    }

    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(
        follow_redirects=True, timeout=REQUEST_TIMEOUT, headers=headers
    ) as client:
        # 1) 沿同站分页收集详情链接（去重，保持首次出现顺序）
        queue = [list_url]
        seen_lists: set[str] = set()
        detail_urls: list[str] = []
        seen_details: set[str] = set()
        while queue and len(seen_lists) < MAX_LIST_PAGES:
            page = queue.pop(0)
            if page in seen_lists:
                continue
            seen_lists.add(page)
            resp = client.get(page)
            resp.raise_for_status()
            result["pages_visited"].append(page)
            soup = BeautifulSoup(resp.text, "html.parser")
            for url in _extract_detail_links(soup, page, site_netloc):
                if url in seen_details:
                    result["duplicates_skipped"] += 1
                    continue
                seen_details.add(url)
                detail_urls.append(url)
            nxt = _extract_next(soup, page, site_netloc)
            if nxt and nxt not in seen_lists and nxt not in queue:
                queue.append(nxt)
        result["details_found"] = len(detail_urls)

        # 2) 逐个读取详情页，失败记录地址并继续
        for url in detail_urls[:MAX_DETAIL_PAGES]:
            try:
                resp = client.get(url)
                resp.raise_for_status()
                record = _parse_detail(resp.text, url)
            except Exception as exc:  # 网络错误 / HTTP 错误 / 解析错误
                result["failed_details"].append(
                    {"url": url, "error": str(exc) or type(exc).__name__}
                )
                continue
            if record["sampling_point"] != sampling_point:
                continue
            reading = parse_arsenic(record["arsenic_raw"])
            sample = {**record, "arsenic_mg_l": None, "status": None, "note": None}
            if reading is None:
                sample["status"] = STATUS_UNDETERMINABLE
                sample["note"] = "砷结果格式无法解析"
            else:
                sample["arsenic_mg_l"] = reading.mg_l
                sample["status"] = classify(reading, limit_mg_l)
                if reading.kind == "below_detection_limit":
                    sample["note"] = f"结果低于检出限 {reading.mg_l:g} mg/L"
            result["samples"].append(sample)

    result["incomplete"] = bool(result["failed_details"])
    return result
