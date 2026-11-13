"""供水厂质控：实验室网站砷检测记录核查 API。"""
from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from scraper import collect

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
    limit_mg_l: float = Field(..., ge=0, description="砷限值，单位 mg/L", examples=[0.01])


def _run(list_url: str, sampling_point: str, limit_mg_l: float) -> dict:
    try:
        return collect(list_url, sampling_point.strip(), limit_mg_l)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"列表页读取失败: {exc}"
        ) from exc


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/check")
def check_post(req: CheckRequest) -> dict:
    return _run(req.list_url, req.sampling_point, req.limit_mg_l)


@app.get("/check")
def check_get(
    list_url: str = Query(..., description="分页列表第一页的 URL"),
    sampling_point: str = Query(..., description="取样点编号"),
    limit_mg_l: float = Query(..., ge=0, description="砷限值，单位 mg/L"),
) -> dict:
    return _run(list_url, sampling_point, limit_mg_l)
