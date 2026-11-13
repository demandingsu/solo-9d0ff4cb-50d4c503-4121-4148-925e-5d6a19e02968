# 供水厂砷检测记录核查采集 API

供水质控员核查实验室网站上的砷检测记录：给定分页列表 URL、取样点编号和 mg/L 限值，
API 沿同站分页读取各样本详情（样本编号、取样点、采样时间、砷结果），换算单位后逐样本
返回 **超限 / 未超限 / 无法判定** 及来源链接，辅助决定哪些样本需要复查。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

启动两个服务：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| `api` | http://localhost:8000 | FastAPI 采集 API（文档见 `/docs`） |
| `sample-site` | http://localhost:8080 | 可离线访问的分页样例站点（nginx 静态页） |

调用示例（在 compose 网络内，API 通过服务名 `sample-site` 访问样例站点）：

```bash
curl -s -X POST http://localhost:8000/check \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://sample-site/list/page1.html",
       "sampling_point": "SP-01",
       "limit_mg_l": 0.01}'
```

也支持 GET：`GET /check?list_url=...&sampling_point=SP-01&limit_mg_l=0.01`。

> 若从宿主机直接访问样例站点做联调，把 `list_url` 换成
> `http://localhost:8080/list/page1.html` 即可（此时浏览器/curl 在宿主机解析）。

## 判定规则

- 结果形如 `0.004 mg/L`、`12 μg/L`（数值）或 `<0.05 mg/L`（低于检出限）；
  单位支持 `mg/L`、`μg/L`（及 `µg/L`、`ug/L`、`毫克/升`、`微克/升`），统一换算为 mg/L（1 μg/L = 0.001 mg/L）。
- **数值结果**：`值 > 限值` → 超限；`值 ≤ 限值` → 未超限（**等于限值不算超限**）。
- **`<检出限`**：`检出限 > 限值` → **无法判定**（真值可能高于限值）；
  `检出限 ≤ 限值` → 未超限（真值严格小于限值）。
- 结果文本无法解析时，该样本标记为无法判定并附 `note` 说明。
- **十进制精度**：限值、详情数值与单位换算全程按十进制精确计算，不经过二进制浮点，
  因此 `0.0100000001 mg/L`、`10.0000001 μg/L` 在限值 0.01 mg/L 时严格判为**超限**，
  `<0.0100000001 mg/L` 判为**无法判定**；限值自身也可带更多小数（如 `0.0100000001`），
  此时与限值相等的结果仍按未超限处理。响应中的 `limit_mg_l` / `arsenic_mg_l` 保持
  数值类型（JSON number），并保留完整的十进制位供复核。

## 采集行为约定

- **同站分页**：从列表 URL 出发，只跟随同一 host[:port] 的“下一页”链接
  （优先 `<a rel="next">`，回退链接文本“下一页/下页/next”）；外站链接一律忽略。
- **详情链接**：优先取 `<a class="detail-link">`，页面没有时回退到 href 含 `detail` 的链接。
- **去重**：重复出现的详情链接只读取、只统计一次（`duplicates_skipped` 记录跳过数）。
- **失败处理**：详情页请求失败（如 404）或字段缺失时，该地址记入 `failed_details`，
  其余样本照常返回，且响应中 `incomplete: true` 标明本次结果不完整。
- **安全上限**：最多遍历 50 个列表页、500 个详情页，防止异常站点导致失控。

## 响应格式

```json
{
  "sampling_point": "SP-01",
  "limit_mg_l": 0.01,
  "incomplete": true,
  "failed_details": [{"url": "http://sample-site/detail/d999.html", "error": "..."}],
  "pages_visited": ["http://sample-site/list/page1.html", "..."],
  "details_found": 14,
  "duplicates_skipped": 1,
  "samples": [
    {
      "sample_id": "AS-2026-1111",
      "sampling_point": "SP-01",
      "sampled_at": "2026-11-08 08:20",
      "arsenic_raw": "0.0100000001 mg/L",
      "arsenic_mg_l": 0.0100000001,
      "status": "超限",
      "note": null,
      "source_url": "http://sample-site/detail/d010.html"
    }
  ]
}
```

`limit_mg_l` 与 `arsenic_mg_l` 均为 JSON 数值（number，不是字符串），并保留完整的
十进制位，可直接用响应中的数值复核判定。


## 样例站点数据（限值 0.01 mg/L 时的预期判定）

| 样本 | 取样点 | 砷结果 | 判定 |
| --- | --- | --- | --- |
| AS-2026-1101 | SP-01 | 0.004 mg/L | 未超限 |
| AS-2026-1102 | SP-01 | 12 μg/L | 超限 |
| AS-2026-1103 | SP-01 | <0.05 mg/L | 无法判定（检出限高于限值） |
| AS-2026-1104 | SP-01 | <5 μg/L | 未超限 |
| AS-2026-1105 | SP-02 | 0.02 mg/L | （非目标取样点，被过滤） |
| AS-2026-1106 | SP-01 | 0.01 mg/L | 未超限（等于限值） |
| AS-2026-1107 | SP-01 | 9.8 μg/L | 未超限 |
| AS-2026-1108 | SP-01 | 10.1 μg/L | 超限 |
| AS-2026-1109 | SP-01 | <0.01 mg/L | 未超限（检出限等于限值） |
| AS-2026-1111 | SP-01 | 0.0100000001 mg/L | 超限（严格高于限值 0.01） |
| AS-2026-1112 | SP-01 | 10.0000001 μg/L | 超限（换算后严格高于限值 0.01） |
| AS-2026-1113 | SP-01 | <0.0100000001 mg/L | 无法判定（检出限高于限值） |
| AS-2026-1114 | SP-01 | <10.0000001 μg/L | 无法判定（检出限高于限值） |
| AS-2026-1110 | SP-01 | 链接已失效 | 计入 failed_details，incomplete=true |

## 本地开发（不用 Docker）

```bash
pip install -r app/requirements.txt
python -m http.server 8080 --directory sample_site/html &   # 样例站点
cd app && uvicorn main:app --port 8000                      # API
pytest tests/                                               # 测试
```
