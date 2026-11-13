"""供水厂质控：实验室网站砷检测记录核查 API。"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from scraper import Numeric, collect, json_number_text

app = FastAPI(
    title="砷检测记录核查 API",
    description=(
        "接收分页列表 URL、取样点编号和 mg/L 限值，沿同站分页读取各样本详情，"
        "逐样本返回 超限 / 未超限 / 无法判定 及来源链接。"
    ),
    version="1.0.0",
)


class CheckRequest(BaseModel):
    list_url: str = Field(..., description="分页列表第一页的 URL", examples=["http://sample-site/list/page1.html"])
    sampling_point: str = Field(..., description="取样点编号，如 SP-01", examples=["SP-01"])
    limit_mg_l: Decimal = Field(..., description="砷限值，单位 mg/L（十进制精确）", examples=[0.01])

    @field_validator("limit_mg_l")
    @classmethod
    def _check_limit(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("限值必须是有限数值")
        if value < 0:
            raise ValueError("限值不能为负数")
        return value


def _run(list_url: str, sampling_point: str, limit_mg_l: object) -> dict:
    try:
        return collect(list_url, sampling_point.strip(), limit_mg_l)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"列表页读取失败: {exc}"
        ) from exc


def _exact_limit_from_body(body: bytes) -> Decimal | None:
    """从原始请求体读取限值的十进制文本，避免 JSON 浮点解析丢精度。

    FastAPI 默认用 ``json.loads`` 把请求体数字解析为 float；限值位数较多时
    二进制浮点会失真。这里用 Decimal 直接解析原始数字文本。返回 None 表示
    无需/无法使用更精确的值（回退到模型解析结果）；合法性与范围由
    ``CheckRequest`` 校验，非法值仍得到标准 422。
    """
    try:
        payload = json.loads(body, parse_float=Decimal, parse_int=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or "limit_mg_l" not in payload:
        return None
    raw = payload["limit_mg_l"]
    if not isinstance(raw, Decimal):
        return None
    return raw if raw.is_finite() and raw >= 0 else None


def _render_json(obj: Any) -> str:
    """序列化响应；Decimal/Numeric 输出为 JSON number，保留全部十进制位。"""
    if isinstance(obj, Numeric):
        return json_number_text(obj)
    if isinstance(obj, Decimal):
        return json_number_text(obj)
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        if not obj == obj or obj in (float("inf"), float("-inf")):
            raise ValueError("输出包含非有限浮点数")
        return repr(obj)
    if isinstance(obj, dict):
        items = ", ".join(
            f"{json.dumps(str(key), ensure_ascii=False)}: {_render_json(value)}"
            for key, value in obj.items()
        )
        return "{" + items + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ", ".join(_render_json(value) for value in obj) + "]"
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class ExactJSONResponse(JSONResponse):
    """JSON 响应：mg/L 等十进制字段仍为数值类型，且不丢十进制位。"""

    def render(self, content: Any) -> bytes:
        return _render_json(content).encode("utf-8")


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> ExactJSONResponse:
    # 默认处理器直接 json.dumps 原始输入；输入含 NaN/Infinity、或 ctx 里带异常对象
    # 时会二次崩溃，这里先清洗成可严格序列化的形式。
    def clean(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return repr(value)
        if isinstance(value, (ValueError, Decimal)):
            return str(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", "backslashreplace")
        if isinstance(value, dict):
            return {key: clean(val) for key, val in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(val) for val in value]
        return value

    errors = []
    for error in exc.errors():
        cleaned = dict(error)
        if "input" in cleaned:
            cleaned["input"] = clean(cleaned["input"])
        if isinstance(cleaned.get("ctx"), dict):
            cleaned["ctx"] = clean(cleaned["ctx"])
        errors.append(cleaned)
    return ExactJSONResponse({"detail": errors}, status_code=422)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/check")
async def check_post(request: Request, req: CheckRequest) -> ExactJSONResponse:
    # Starlette 会缓存请求体，此处可再读一次；仅在能精确还原时采用。
    exact_limit = _exact_limit_from_body(await request.body())
    limit = exact_limit if exact_limit is not None else req.limit_mg_l
    return ExactJSONResponse(_run(req.list_url, req.sampling_point, limit))


@app.get("/check")
def check_get(
    list_url: str = Query(..., description="分页列表第一页的 URL"),
    sampling_point: str = Query(..., description="取样点编号"),
    limit_mg_l: Decimal = Query(..., ge=0, description="砷限值，单位 mg/L"),
) -> ExactJSONResponse:
    return ExactJSONResponse(_run(list_url, sampling_point, limit_mg_l))
