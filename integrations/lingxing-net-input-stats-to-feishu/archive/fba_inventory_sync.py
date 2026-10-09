# -*- coding: utf-8 -*-
"""FBA 库存同步：领星 FBA 库存列表 -> 飞书在线表格（单文件版）

流程：
    1) 分页拉取领星 FBA 库存列表（/api/storage/fbaLists，is_cost_page=1 成本视图）
    2) 字段映射：英文变量 -> 中文表头（36 列）
    3) 分批整体覆盖写入飞书在线表格（Sheets）

配置来源：config.json（与发货单/应收报告共用一套飞书凭据/领星 cookies）。
"""

import ast
import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid

# =============================================================================
# 全局配置
# =============================================================================
CONFIG_FILE = "config.json"
CONFIG_EXAMPLE_FILE = "config.example.json"
DEFAULT_OUTPUT_FILE = "fba_inventory_result.json"

LINGXING_HOST = "https://maique.lingxing.com"
FEISHU_HOST = "https://open.feishu.cn"
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0
PAGE_SIZE = 200            # FBA 库存分页大小
WRITE_BATCH = 1000         # 飞书写入分批行数（单次写入过大易失败）

# 目标表格：与发货单/应收同一 wiki，sheet 4DjziB
FEISHU_URL_DEFAULT = ""

# 表头（36 列）
HEADERS = [
    "父ASIN", "所属仓库", "店铺", "ASIN", "MSKU", "FNSKU", "SKU", "品名",
    "SPU", "款名", "属性", "一级分类", "二级分类", "三级分类", "品牌", "负责人",
    "FBA总库存(成本)", "FBA可用库存(成本)", "FBA可售(成本)", "FBM可售(成本)",
    "FBA预留(成本)", "FBA待调仓(成本)", "FBA调仓中(成本)", "FBA待发货(成本)",
    "FBA计划入库(成本)", "FBA标发在途(成本)", "FBA实际在途(成本)", "FBA入库中(成本)",
    "FBA不可售(成本)", "FBA调查中(成本)",
    "单位采购成本", "单位头程费用", "FBA总货值", "FBA总库存成本",
    "仓储类型", "配送方式",
]

# 中文列名 -> 接口字段（FBA实际在途无金额字段，用数量 real_transit_quantity）
COLUMN_MAPPING = {
    "父ASIN":           "parent_asin_real",
    "所属仓库":          "name",
    "店铺":              "seller_name",
    "ASIN":             "asin",
    "MSKU":             "seller_sku",
    "FNSKU":            "fnsku",
    "SKU":              "sku",
    "品名":              "product_name",
    "SPU":              "spu",
    "款名":              "spu_name",
    "属性":              "attribute",
    "一级分类":          "category_level1",
    "二级分类":          "category_level2",
    "三级分类":          "category_level3",
    "品牌":              "product_brand_text",
    "负责人":            "asin_principal_list",
    "FBA总库存(成本)":     "total_price",
    "FBA可用库存(成本)":   "available_total_price",
    "FBA可售(成本)":      "afn_fulfillable_quantity_price",
    "FBM可售(成本)":      "quantity_price",
    "FBA预留(成本)":      "afn_reserved_quantity_price",
    "FBA待调仓(成本)":    "reserved_fc_transfers_price",
    "FBA调仓中(成本)":    "reserved_fc_processing_price",
    "FBA待发货(成本)":    "reserved_customerorders_price",
    "FBA计划入库(成本)":  "afn_inbound_working_quantity_price",
    "FBA标发在途(成本)":  "afn_inbound_shipped_quantity_price",
    "FBA实际在途(成本)":  "real_transit_quantity",   # 接口无金额字段，写数量
    "FBA入库中(成本)":    "afn_inbound_receiving_quantity_price",
    "FBA不可售(成本)":    "afn_unsellable_quantity_price",
    "FBA调查中(成本)":    "afn_researching_quantity_price",
    "单位采购成本":       "cg_price",
    "单位头程费用":       "cg_transport_costs",
    "FBA总货值":         "total_amount",
    "FBA总库存成本":      "total_cost",
    "仓储类型":          "storage_type_name",
    "配送方式":          "fulfillment_channel_name",
}


def log(level, message):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {level} - {message}")


# =============================================================================
# 领星请求
# =============================================================================
def build_lingxing_headers(cookies):
    cookie_pairs = []
    auth_token_encoded = company_id = env_key = uid = zid = None
    for c in cookies:
        name = c.get("Name", "")
        value = c.get("Value", "")
        cookie_pairs.append(f"{name}={value}")
        if name == "auth-token":
            auth_token_encoded = value
        elif name == "company_id":
            company_id = value
        elif name == "env_key":
            env_key = value
        elif name == "uid":
            uid = value
        elif name == "zid":
            zid = value
    if not auth_token_encoded:
        raise ValueError("auth-token not found in cookies")
    auth_token = urllib.parse.unquote(auth_token_encoded)
    return {
        "AK-Client-Type": "web",
        "AK-Origin": "https://maique.lingxing.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh-TW;q=0.9,zh;q=0.8,en-US;q=0.7,en;q=0.6",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://maique.lingxing.com",
        "Referer": "https://maique.lingxing.com/erp/msupply/fbaInventory",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "X-AK-Company-Id": company_id,
        "X-AK-ENV-KEY": env_key,
        "X-AK-Language": "zh",
        "X-AK-PLATFORM": "1",
        "X-AK-Request-Source": "erp",
        "X-AK-Uid": uid,
        "X-AK-Version": "3.9.0.3.0.037",
        "X-AK-Zid": zid,
        "auth-token": auth_token,
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Cookie": "; ".join(cookie_pairs),
    }


def send_request(url, post_dict, base_headers, request_desc="request"):
    result, error = None, None
    for attempt in range(MAX_RETRIES):
        try:
            headers = base_headers.copy()
            headers["X-AK-Request-Id"] = str(uuid.uuid4())
            data = json.dumps(post_dict).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))
                break
        except Exception as e:
            error = str(e)
            log("WARNING", f"{request_desc} 第{attempt+1}次请求失败: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    if result is None:
        log("ERROR", f"{request_desc} 重试{MAX_RETRIES}次仍失败: {error}")
    return result


def fetch_fba_inventory(cookies):
    """分页拉取 FBA 库存全量，返回 list[record]。"""
    base_headers = build_lingxing_headers(cookies)
    url = f"{LINGXING_HOST}/api/storage/fbaLists"
    all_records = []
    offset = 0
    total = None
    while total is None or offset < total:
        body = {
            "cid": "", "bid": "", "attribute": "", "asin_principal": "",
            "search_field": "sku", "is_cost_page": 1, "status": "",
            "senior_search_list": "[]", "offset": offset, "length": PAGE_SIZE,
            "fulfillment_channel_type": "FBA", "is_hide_zero_stock": "0",
            "is_parant_asin_merge": "0",
            "req_time_sequence": "/api/storage/fbaLists$$2",
        }
        resp = send_request(url, body, base_headers, f"FBA库存 offset={offset}")
        if not resp or resp.get("code") != 1:
            log("ERROR", f"接口返回异常: code={resp.get('code') if resp else None}")
            break
        total = resp.get("total", total or 0)
        lst = resp.get("list") or []        # 注意：list 在顶层，不是 data.list
        if not lst:
            break
        all_records.extend(lst)
        offset += PAGE_SIZE
        log("DEBUG", f"分页进度: {len(all_records)}/{total}")
    log("INFO", f"共获取 {len(all_records)} 条 FBA 库存记录")
    return all_records


# =============================================================================
# 字段映射
# =============================================================================
def _parse_name_list(value):
    """负责人字段：['纪泳君'] / \"['纪泳君']\" / [] -> '纪泳君'（逗号分隔）。"""
    if value in (None, "", "[]", "[] ", "null"):
        return ""
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            # 非标准字符串，尝试提取引号内容
            parsed = re.findall(r"'([^']*)'|\"([^\"]*)\"", text)
            parsed = [a or b for a, b in parsed]
        if isinstance(parsed, list):
            return ", ".join(str(x) for x in parsed if x)
        return str(parsed)
    if isinstance(value, list):
        return ", ".join(str(x) for x in value if x)
    return "" if value is None else str(value)


def _parse_attr(value):
    """属性字段：'[]' / '[\"红色\"]' -> 逗号分隔文本。"""
    if value in (None, "", "[]"):
        return ""
    text = str(value).strip()
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, list):
            return ", ".join(str(x) for x in parsed if x)
        return text
    except (ValueError, SyntaxError):
        return text


def map_record(record):
    row = []
    for col in HEADERS:
        key = COLUMN_MAPPING[col]
        value = record.get(key) if isinstance(record, dict) else None
        if col == "负责人":
            value = _parse_name_list(value)
        elif col == "属性":
            value = _parse_attr(value)
        row.append("" if value is None else value)
    return row


def build_rows(records):
    return [map_record(r) for r in records]


# =============================================================================
# 飞书在线表格（v2 读写）
# =============================================================================
def _feishu_req(method, url, token=None, body=None, params=None, retries=MAX_RETRIES):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last = None
    for attempt in range(retries):
        try:
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = ""
            log("WARNING", f"{method} {url} 第{attempt+1}次失败: HTTP {e.code} {err_body[:200]}")
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
        except Exception as e:
            last = e
            log("WARNING", f"{method} {url} 第{attempt+1}次失败: {e}")
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise RuntimeError(f"{method} {url} 重试{retries}次仍失败: {last}")


def get_tenant_access_token(app_id, app_secret):
    url = f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal"
    data = _feishu_req("POST", url, token=None, body={"app_id": app_id, "app_secret": app_secret})
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
    return data["tenant_access_token"]


def _col_letters(n):
    result = []
    while n:
        n, rem = divmod(n - 1, 26)
        result.append(chr(ord("A") + rem))
    return "".join(reversed(result))


def resolve_spreadsheet(feishu_url, token):
    m = re.search(r"/wiki/([a-zA-Z0-9]+)", feishu_url)
    if m:
        data = _feishu_req("GET", f"{FEISHU_HOST}/open-apis/wiki/v2/spaces/get_node",
                           token, params={"token": m.group(1)})
        if data.get("code") != 0:
            raise RuntimeError(f"解析 wiki 节点失败: {data}")
        obj_token = (data.get("data") or {}).get("node", {}).get("obj_token")
        if not obj_token:
            raise RuntimeError(f"wiki 节点未关联对象: {data}")
        spreadsheet_token = obj_token
    else:
        m = re.search(r"/sheets/([a-zA-Z0-9]+)", feishu_url)
        if not m:
            raise RuntimeError(f"无法从 URL 解析 spreadsheet_token: {feishu_url}")
        spreadsheet_token = m.group(1)

    qm = re.search(r"[?&]sheet=([a-zA-Z0-9]+)", feishu_url)
    sheet_param = qm.group(1) if qm else None
    data = _feishu_req("GET",
                       f"{FEISHU_HOST}/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
                       token)
    if data.get("code") != 0:
        raise RuntimeError(f"查询表格 sheet 列表失败: {data}")
    sheets = (data.get("data") or {}).get("sheets") or []
    if not sheets:
        raise RuntimeError("表格中没有 sheet")
    if sheet_param:
        sheet_id = next((s["sheet_id"] for s in sheets if s.get("sheet_id") == sheet_param), None)
        if sheet_id is None:
            raise RuntimeError(f"URL 指定的 sheet {sheet_param} 不存在")
    else:
        sheet_id = sheets[0]["sheet_id"]
    return spreadsheet_token, sheet_id


def write_sheet_range(spreadsheet_token, sheet_id, token, range_str, values):
    body = {
        "valueRange": {"range": range_str, "values": values},
        "valueInputOption": "USER_INPUT",
    }
    data = _feishu_req("PUT",
                       f"{FEISHU_HOST}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                       token, body=body)
    if data.get("code") != 0:
        raise RuntimeError(f"写入表格失败: {data}")
    return data


def write_all(spreadsheet_token, sheet_id, token, header_row, rows):
    """分批整体覆盖写入：表头 + 数据（分批防单次过大）。"""
    ncols = len(header_row)
    end_col = _col_letters(ncols)
    # 表头
    write_sheet_range(spreadsheet_token, sheet_id, token, f"{sheet_id}!A1:{end_col}1", [header_row])
    # 数据分批
    start_row = 2
    for i in range(0, len(rows), WRITE_BATCH):
        batch = rows[i:i + WRITE_BATCH]
        r0 = start_row + i
        r1 = r0 + len(batch) - 1
        write_sheet_range(spreadsheet_token, sheet_id, token,
                          f"{sheet_id}!A{r0}:{end_col}{r1}", batch)
        log("DEBUG", f"写入批次: 行 {r0}-{r1}（{len(batch)} 行）")
    return len(rows) + 1


# =============================================================================
# 主流程
# =============================================================================
def run_sync(cookies, app_id, app_secret, feishu_url, dry_run=False):
    records = fetch_fba_inventory(cookies)
    if not records:
        raise RuntimeError("未获取到任何 FBA 库存记录，请检查 cookies")

    rows = build_rows(records)
    log("INFO", f"映射完成 {len(rows)} 行")

    col_stats = {}
    for i, col in enumerate(HEADERS):
        filled = sum(1 for r in rows if r[i] not in ("", None))
        col_stats[col] = f"{filled}/{len(rows)}"

    if dry_run:
        return {"rows": len(rows), "data": rows, "columns": col_stats}

    token = get_tenant_access_token(app_id, app_secret)
    spreadsheet_token, sheet_id = resolve_spreadsheet(feishu_url, token)
    nrows = write_all(spreadsheet_token, sheet_id, token, HEADERS, rows)
    log("INFO", f"已写入飞书表格 {spreadsheet_token}/{sheet_id}: {nrows} 行（含表头）")
    return {"rows": len(rows), "columns": col_stats}


# =============================================================================
# 配置驱动
# =============================================================================
def _resolve_path(path):
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), path)


def load_config(config_path=CONFIG_FILE):
    path = _resolve_path(config_path)
    if not os.path.exists(path):
        raise SystemExit(f"缺少配置文件 {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    required = ["APP_ID", "APP_SECRET", "COOKIES"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise SystemExit(f"config.json 中以下配置为空: {', '.join(missing)}")
    return cfg


def save_result(result, out_path):
    out_path = _resolve_path(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    log("INFO", f"结果已写入 {out_path}")
    return out_path


def run_from_config(config, dry_run=False):
    return run_sync(
        cookies=config["COOKIES"],
        app_id=config["APP_ID"],
        app_secret=config["APP_SECRET"],
        feishu_url=config.get("FBA_FEISHU_URL", FEISHU_URL_DEFAULT),
        dry_run=dry_run,
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="领星 FBA 库存 -> 飞书在线表格")
    parser.add_argument("--dry-run", action="store_true",
                        help="只拉取并映射，不写飞书（数据写入 fba_inventory_result.json）")
    args = parser.parse_args()

    config = load_config(CONFIG_FILE)
    if args.dry_run:
        result = run_from_config(config, dry_run=True)
        save_result(result, "fba_inventory_result.json")
    else:
        result = run_from_config(config)
        save_result(result, DEFAULT_OUTPUT_FILE)
