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

import math
import re
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any
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
# 数值允许科学计数法（如 "1e-5 mg/L"）；全程用 Decimal 解析，避免二进制浮点丢精度。
_RESULT_RE = re.compile(
    r"""^\s*
    (?P<flag><|≤|＜|<=|＜=)?\s*
    (?P<num>(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?)
    \s*(?P<unit>mg/l|μg/l|µg/l|ug/l|毫克/升|微克/升)
    \s*$""",
    re.IGNORECASE | re.VERBOSE,
)

_UNIT_FACTORS = {
    "mg/l": Decimal("1"),
    "毫克/升": Decimal("1"),
    "μg/l": Decimal("0.001"),
    "ug/l": Decimal("0.001"),
    "微克/升": Decimal("0.001"),
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


class Numeric(float):
    """精确十进制数值在 JSON 响应中的载体。

    继承 ``float``，因此在 FastAPI/pydantic 的编码器与 ``json.dumps`` 中都被当作
    JSON number（而不是字符串）；同时保留原始 ``Decimal`` 作为数值真值。比较、
    格式化均走 Decimal，绝不经过二进制浮点，故 0.0100000001 与 0.01 可严格区分。
    响应序列化时由 API 层优先读取 ``decimal``，从而输出完整的十进制位。
    """

    __slots__ = ("_decimal",)

    def __new__(cls, value: Decimal | str | int | float | "Numeric") -> "Numeric":
        if isinstance(value, Numeric):
            dec = value._decimal
        elif isinstance(value, Decimal):
            dec = value
        else:
            dec = Decimal(str(value))
        obj = float.__new__(cls, float(dec))
        obj._decimal = dec
        return obj

    @property
    def decimal(self) -> Decimal:
        return self._decimal

    @staticmethod
    def _other_dec(other: object) -> object:
        """把参与比较的另一操作数转换为 Decimal，非数值原样返回。"""
        if isinstance(other, Numeric):
            return other._decimal
        if isinstance(other, Decimal):
            return other
        if isinstance(other, float):
            return None if math.isnan(other) or math.isinf(other) else Decimal(str(other))
        if isinstance(other, int) and not isinstance(other, bool):
            return Decimal(other)
        return None

    def __repr__(self) -> str:
        return format(self._decimal, "f")

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        dec = self._other_dec(other)
        if dec is None:
            return False if isinstance(other, (float, int, Decimal)) else NotImplemented
        return self._decimal == dec

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:
        return hash(self._decimal)

    def __lt__(self, other: object) -> bool:
        return self._decimal < _exact(other)

    def __le__(self, other: object) -> bool:
        return self._decimal <= _exact(other)

    def __gt__(self, other: object) -> bool:
        return self._decimal > _exact(other)

    def __ge__(self, other: object) -> bool:
        return self._decimal >= _exact(other)


def _exact(value: Any) -> Decimal:
    """把限值等输入转换为 Decimal，避免二进制浮点丢精度。"""
    if isinstance(value, Numeric):
        return value.decimal
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"限值必须是有限数值: {value!r}")
    return Decimal(str(value))


def _exact_scale(value: Decimal, factor: Decimal) -> Decimal:
    """精确乘以 10 的整数次幂因子，不受默认 Decimal 精度（28 位）截断。"""
    digits = max(len(value.as_tuple().digits), 1)
    places = max(-factor.as_tuple().exponent, 0)
    with localcontext() as ctx:
        ctx.prec = digits + places + 10
        return value * factor


def _decimal_number(value: Decimal) -> Numeric:
    """把精确 Decimal 包装成响应用的 JSON number。"""
    return Numeric(value)


def _plain_text(value: Decimal) -> str:
    """Decimal 的完整十进制文本；不用科学计数法，不丢任何有效位。

    极端量级（展开后极长）改用科学计数法，避免生成超长响应；JSON 数字允许
    指数形式，数值精度不受影响。
    """
    if value.is_zero():
        return "0"
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int):
        digits = len(value.as_tuple().digits)
        plain_length = digits + max(-exponent, 0) + 2  # 小数点 + 可能的符号
        if plain_length > 128:
            return str(value.normalize())
    return format(value, "f")


def json_number_text(value: Decimal | Numeric) -> str:
    """把精确十进制数值渲染成 JSON number 文本（不含引号）。"""
    dec = value.decimal if isinstance(value, Numeric) else value
    return _plain_text(dec)


@dataclass
class ArsenicReading:
    kind: str            # "value" | "below_detection_limit"
    mg_l: Numeric        # 换算为 mg/L 后的精确数值（数值结果或检出限）
    raw: str

    def __post_init__(self) -> None:
        # 兼容直接以 float/Decimal 构造的调用方：统一保留精确十进制真值。
        if not isinstance(self.mg_l, Numeric):
            self.mg_l = Numeric(_exact(self.mg_l))


def parse_arsenic(text: str | None) -> ArsenicReading | None:
    """解析砷结果文本，返回 None 表示无法解析。全程使用 Decimal 精确换算。"""
    m = _RESULT_RE.match(text or "")
    if not m:
        return None
    unit = m.group("unit").lower().replace("µ", "μ")
    value = _exact_scale(Decimal(m.group("num")), _UNIT_FACTORS[unit])
    kind = "below_detection_limit" if m.group("flag") else "value"
    return ArsenicReading(kind=kind, mg_l=value, raw=(text or "").strip())


def classify(reading: ArsenicReading, limit_mg_l: Decimal | float | str | Numeric) -> str:
    """按限值判定单样本状态（Decimal 全程比较，无浮点误差）。"""
    return classify_decimals(reading, _exact(limit_mg_l))


def classify_decimals(reading: ArsenicReading, limit: Decimal) -> str:
    """同 classify，但限值已转换为 Decimal，避免重复转换。

    - 数值结果：> 限值 → 超限；≤ 限值（含等于）→ 未超限。
    - "<检出限"：检出限 > 限值 → 无法判定；检出限 ≤ 限值 → 未超限。
    """
    observed = _exact(reading.mg_l)
    if reading.kind == "value":
        return STATUS_EXCEEDS if observed > limit else STATUS_WITHIN
    if observed > limit:
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


def collect(
    list_url: str,
    sampling_point: str,
    limit_mg_l: Decimal | float | str | Numeric,
) -> dict:
    """从分页列表 URL 出发，采集指定取样点的砷检测记录并逐样本判定。

    ``limit_mg_l`` 以 Decimal 参与全部比较；响应中的 mg/L 数值以 ``Numeric``
    返回，保留完整十进制位，足以复核判定。
    """
    limit = _exact(limit_mg_l)
    if not limit.is_finite():
        raise ValueError(f"无效的限值: {limit_mg_l!r}")
    if limit < 0:
        raise ValueError(f"限值不能为负数: {limit_mg_l!r}")

    list_url = _normalize(list_url)
    site_netloc = urlsplit(list_url).netloc.lower()
    if not site_netloc:
        raise ValueError(f"无效的列表 URL: {list_url}")

    result: dict = {
        "sampling_point": sampling_point,
        "limit_mg_l": _decimal_number(limit),
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
                mg_l = reading.mg_l
                sample["arsenic_mg_l"] = _decimal_number(mg_l)
                sample["status"] = classify_decimals(reading, limit)
                if reading.kind == "below_detection_limit":
                    sample["note"] = (
                        f"结果低于检出限 {_plain_text(_exact(mg_l))} mg/L"
                    )
            result["samples"].append(sample)

    result["incomplete"] = bool(result["failed_details"])
    return result
