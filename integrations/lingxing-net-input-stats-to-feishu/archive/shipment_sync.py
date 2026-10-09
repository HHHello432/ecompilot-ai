# -*- coding: utf-8 -*-
"""发货单数据同步：领星发货单 -> 飞书在线表格（单文件版）

流程：
    1) 分页拉取领星发货单列表（showShipmentListV2），得到全部发货单号
    2) 并发调用发货单详情（showShipmentItemListBySn），取商品级成本字段
    3) 字段映射：英文变量 -> 中文表头（19 列）
    4) 写入飞书在线表格（Sheets API），整体覆盖刷新

配置来源：config.json（由 config.example.json 复制而来，已被 .gitignore 忽略）。
入口在 __main__ 中读取一次 config.json，作为入参传给主逻辑方法 run_and_save_result(config)。

使用示例（库调用，显式传参）：
    from shipment_sync import run_sync

    result = run_sync(
        cookies=cookies,
        app_id="cli_xxx",
        app_secret="xxx",
        feishu_url="",
        start_date="2026-08-01",
        end_date="2026-08-31",
    )
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

# =============================================================================
# 全局配置
# =============================================================================
CONFIG_FILE = "config.json"
CONFIG_EXAMPLE_FILE = "config.example.json"
DEFAULT_OUTPUT_FILE = "sync_result.json"

LINGXING_HOST = "https://maique.lingxing.com"
FEISHU_HOST = "https://open.feishu.cn"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0
PAGE_SIZE = 200            # 领星列表分页大小
DETAIL_WORKERS = 10        # 详情接口并发数
FEISHU_BATCH_SIZE = 500    # 飞书批量写入单次上限

# 表头（与用户提供一致，顺序即列顺序）
HEADERS = [
    "发货时间", "SKU", "店铺", "货件单号", "发货量",
    "单位FBA仓入库成本", "单位辅料费用", "取值来源",
    "单位头程费用", "单位出库头程费用", "单位税费", "单位出库费用",
    "采购单价", "采购单价(自定义)", "单位出库费用(自定义)",
    "单位辅料费用(自定义)", "单位出库头程(自定义)", "单位FBA仓入库成本(自定义)",
    "发货仓库店铺",
]

# 中文列名 -> 详情接口 items 字段
COLUMN_MAPPING = {
    "发货时间":             "shipment_time",
    "SKU":                 "sku",
    "店铺":                 "sname",
    "货件单号":             "shipment_id",      # FBA 货件号（表格既有口径，如 FBA15M8H9W6D）
    "发货量":               "quantity_shipped",
    "单位FBA仓入库成本":      "fba_stock_cost",
    "单位辅料费用":          "aux_cost",
    "取值来源":             "cost_source",
    "单位头程费用":          "transport_cost",
    "单位出库头程费用":       "outbound_head_cost_unit",
    "单位税费":             "tax_unit",
    "单位出库费用":          "outbound_cost_unit",
    "采购单价":             "purchase_price_unit",
    "采购单价(自定义)":       "custom_purchase_price_unit",
    "单位出库费用(自定义)":    "custom_outbound_cost_unit",
    "单位辅料费用(自定义)":    "custom_aux_cost",
    "单位出库头程(自定义)":    "custom_outbound_head_cost_unit",
    "单位FBA仓入库成本(自定义)": "custom_fba_inbound_cost_unit",
    "发货仓库店铺":          "seller_name",
}

# 第 20 列「店铺(不含站点)」公式模板（与表格既有公式一致，{row} 为行号）
SHOP_SHORT_FORMULA = ('IFERROR(LEFT(C{row},FIND("~",SUBSTITUTE(C{row},"-","~",'
                      'LEN(C{row})-LEN(SUBSTITUTE(C{row},"-",""))))-1),C{row})')


# =============================================================================
# 日志
# =============================================================================
def log(level, message):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {level} - {message}")


# =============================================================================
# 领星请求
# =============================================================================
def build_lingxing_headers(cookies):
    """根据 cookies（Name/Value 列表）构造领星请求头。照 purchase_tracking 模式。"""
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
        "Referer": "https://maique.lingxing.com/erp/msupply/DeliveryOrderDetail",
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


def build_list_body(start_date, end_date, offset, length=PAGE_SIZE):
    return {
        "search_field": "shipment_sn", "sids": "", "mids": "",
        "logistics_provider_ids": "", "logistics_type": "", "status": "",
        "pay_status": "", "print_status": "", "pick_status": "",
        "third_party_order_status": "", "is_exist_declaration": "",
        "is_exist_clearance": "", "relate_shipment_status": "",
        "transportation_cost_status": "", "other_cost_status": "",
        "predicted_transportation_cost_status": "",
        "predicted_other_cost_status": "", "is_relate_aux": "",
        "logistics_status": "", "head_fee_status": "", "is_custom_cost": "",
        "is_relate_packing_task_sn": "", "ship_mode": "",
        "shipment_status": [], "time_type": 2,
        "start_date": start_date, "end_date": end_date,
        "seniorSearchList": [], "is_awd": "", "is_urgent": "",
        "head_fee_type": "", "principal_uid": [], "audit_uids": [],
        "multi_status": [], "sort_field": "gmt_create", "sort_type": "desc",
        "offset": offset, "length": length,
        "req_time_sequence": "/api/shipment/showShipmentListV2$$10",
    }


def send_lingxing_request(url, post_dict, base_headers, method="POST",
                          request_desc="request"):
    """发送领星请求（POST/GET），带超时与指数退避重试，失败返回 None。"""
    result, error = None, None
    for attempt in range(MAX_RETRIES):
        try:
            headers = base_headers.copy()
            headers["X-AK-Request-Id"] = str(uuid.uuid4())
            data = json.dumps(post_dict).encode("utf-8") if post_dict is not None else None
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                if response.status != 200:
                    raise Exception(f"HTTP {response.status}")
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


def fetch_shipment_sns(cookies, start_date, end_date):
    """分页拉取发货单列表，返回 list[{"shipment_sn":..., "shipment_time":...}]。"""
    base_headers = build_lingxing_headers(cookies)
    all_sns = []
    offset = 0
    total = None
    while total is None or offset <= total:
        body = build_list_body(start_date, end_date, offset)
        resp = send_lingxing_request(
            f"{LINGXING_HOST}/api/shipment/showShipmentListV2",
            body, base_headers, request_desc=f"发货单列表 offset={offset}")
        if not resp or resp.get("code") != 1:
            log("ERROR", f"列表接口返回异常: code={resp.get('code') if resp else None} msg={resp.get('msg') if resp else None}")
            break
        data = resp.get("data") or {}
        total = data.get("total", total or 0)
        lst = data.get("list") or []
        if not lst:
            break
        for s in lst:
            sn = s.get("shipment_sn")
            if sn:
                all_sns.append({"shipment_sn": sn, "shipment_time": s.get("shipment_time", "")})
        offset += PAGE_SIZE
        log("DEBUG", f"分页进度: {len(all_sns)}/{total}，offset={offset}")
        if offset > total:
            break
    log("INFO", f"列表共获取 {len(all_sns)} 条发货单")
    return all_sns


def fetch_shipment_detail(cookies, shipment_sn):
    """调详情接口取单个发货单的商品明细（items）。失败返回 None。"""
    base_headers = build_lingxing_headers(cookies)
    url = (f"{LINGXING_HOST}/api/shipment/showShipmentItemListBySn"
           f"?sort_field=fnsku&sort_type=&id={urllib.parse.quote(shipment_sn)}"
           f"&req_time_sequence=%2Fapi%2Fshipment%2FshowShipmentItemListBySn%24%241")
    resp = send_lingxing_request(url, None, base_headers, method="GET",
                                 request_desc=f"详情[{shipment_sn}]")
    if not resp or resp.get("code") != 1:
        log("ERROR", f"详情[{shipment_sn}] 返回异常: code={resp.get('code') if resp else None}")
        return None
    return resp.get("data")


def fetch_all_details(cookies, sn_list):
    """并发拉取全部发货单详情，返回 {shipment_sn: data}（失败的记录在 errors）。"""
    base_headers = build_lingxing_headers(cookies)
    results = {}
    errors = []
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as executor:
        futures = {}
        for item in sn_list:
            sn = item["shipment_sn"]
            fut = executor.submit(_detail_worker, cookies, sn)
            futures[fut] = sn
        completed = 0
        for fut in as_completed(futures):
            sn = futures[fut]
            try:
                data = fut.result()
                if data:
                    results[sn] = data
                else:
                    errors.append(sn)
            except Exception as e:
                errors.append(sn)
                log("ERROR", f"详情[{sn}] 未捕获异常: {e}")
            completed += 1
            if completed % 50 == 0 or completed == len(futures):
                log("INFO", f"详情进度: {completed}/{len(futures)}（成功 {len(results)}，失败 {len(errors)}）")
    if errors:
        log("WARNING", f"详情获取失败 {len(errors)} 单: {errors[:20]}{'...' if len(errors) > 20 else ''}")
    return results, errors


def _detail_worker(cookies, shipment_sn):
    return fetch_shipment_detail(cookies, shipment_sn)


# =============================================================================
# 字段映射
# =============================================================================
def _fmt_shipment_time(value):
    """发货时间统一为 'YYYY-MM-DD HH:MM:SS'（接口返回可能是字符串或秒级时间戳）。"""
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    try:
        v = int(value)
        if v > 1e12:          # 毫秒
            v = v / 1000
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
    except (TypeError, ValueError):
        return "" if value is None else str(value)


def map_item(item):
    """把详情接口的一条商品明细映射为表头行。"""
    row = []
    for col in HEADERS:
        key = COLUMN_MAPPING[col]
        value = item.get(key) if isinstance(item, dict) else None
        if col == "发货时间":
            value = _fmt_shipment_time(value)
        elif col == "发货仓库店铺" and value in (None, ""):
            # seller_name 未维护时回退到店铺名（数据中二者一致）
            value = item.get("sname") if isinstance(item, dict) else None
        row.append("" if value is None else value)
    return row


def expand_details(details, sn_list):
    """把详情数据展开为行数据（每条商品一行），保留列表顺序。"""
    rows = []
    for item in sn_list:
        sn = item["shipment_sn"]
        data = details.get(sn)
        if data is None:
            log("WARNING", f"发货单 {sn} 详情缺失，跳过其商品行")
            continue
        items = data.get("items") or []
        for it in items:
            rows.append(map_item(it))
    return rows


# =============================================================================
# 飞书在线表格
# =============================================================================
def _feishu_req(method, url, token=None, body=None, params=None, retries=MAX_RETRIES):
    """飞书 API 统一调用，带超时与指数退避重试。token 为 None 时不带鉴权头。"""
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
    """1 -> A, 26 -> Z, 27 -> AA"""
    result = []
    while n:
        n, rem = divmod(n - 1, 26)
        result.append(chr(ord("A") + rem))
    return "".join(reversed(result))


def resolve_spreadsheet(feishu_url, token):
    """解析飞书 URL -> (spreadsheet_token, sheet_id)。

    支持两种 URL：
      - wiki 链接：https://xxx.feishu.cn/wiki/<node>?sheet=<sheet_id>
      - 表格直链：https://xxx.feishu.cn/sheets/<spreadsheet_token>?sheet=<sheet_id>
    """
    m = re.search(r"/wiki/([a-zA-Z0-9]+)", feishu_url)
    if m:
        node_token = m.group(1)
        data = _feishu_req("GET", f"{FEISHU_HOST}/open-apis/wiki/v2/spaces/get_node",
                           token, params={"token": node_token})
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

    # sheet_id：URL 参数优先；否则取第一个 sheet
    sheet_param = None
    qm = re.search(r"[?&]sheet=([a-zA-Z0-9]+)", feishu_url)
    if qm:
        sheet_param = qm.group(1)

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
            raise RuntimeError(f"URL 指定的 sheet {sheet_param} 不存在，可选: {[s['sheet_id'] for s in sheets]}")
    else:
        sheet_id = sheets[0]["sheet_id"]
    return spreadsheet_token, sheet_id


def clear_sheet_range(spreadsheet_token, sheet_id, token, end_col="Z", end_row=4000):
    """清空指定范围内容（保留样式），用于清除旧数据。"""
    range_str = f"{sheet_id}!A2:{end_col}{end_row}"
    data = _feishu_req("POST",
                       f"{FEISHU_HOST}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_clear",
                       token, body={"range": range_str})
    if data.get("code") != 0:
        raise RuntimeError(f"清空表格范围失败: {data}")
    return data


def write_sheet(spreadsheet_token, sheet_id, token, values):
    """整体写入（覆盖从 A1 开始的范围）。values: 二维列表，首行为表头。

    飞书 v2 接口：PUT /spreadsheets/{token}/values，range 放在 body 的 valueRange 中。
    """
    nrows = len(values)
    ncols = max((len(r) for r in values), default=len(HEADERS))
    end_col = _col_letters(ncols)
    range_str = f"{sheet_id}!A1:{end_col}{nrows}"
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


def write_formula_column(spreadsheet_token, sheet_id, token, nrows, formula_col="T",
                         template=SHOP_SHORT_FORMULA):
    """向第 formula_col 列写入逐行公式（保持表格「店铺(不含站点)」公式行为）。

    公式从第 2 行（数据首行）写到第 nrows 行。
    """
    if nrows < 1:
        return None
    start_row = 2
    end_row = nrows + 1
    formulas = [[{"type": "formula", "text": f"={template.format(row=r)}"}] for r in range(start_row, end_row + 1)]
    range_str = f"{sheet_id}!{formula_col}{start_row}:{formula_col}{end_row}"
    body = {
        "valueRange": {"range": range_str, "values": formulas},
        "valueInputOption": "USER_INPUT",
    }
    data = _feishu_req("PUT",
                       f"{FEISHU_HOST}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                       token, body=body)
    if data.get("code") != 0:
        raise RuntimeError(f"写入公式列失败: {data}")
    return data


# =============================================================================
# 主流程
# =============================================================================
def run_sync(cookies, app_id, app_secret, feishu_url, start_date, end_date,
             dry_run=False):
    """完整流程：列表枚举 -> 详情取成本 -> 映射 -> 写入飞书在线表格。

    dry_run=True 时不写飞书，仅返回映射后的数据（用于验证）。
    返回 {"rows": n, "shipments": n, "columns": {...映射统计}}
    """
    # 1) 列表枚举发货单
    sn_list = fetch_shipment_sns(cookies, start_date, end_date)
    if not sn_list:
        raise RuntimeError("未获取到任何发货单，请检查日期范围与 cookies")

    # 2) 并发取详情
    details, errors = fetch_all_details(cookies, sn_list)

    # 3) 展开映射
    rows = expand_details(details, sn_list)
    log("INFO", f"展开为 {len(rows)} 行商品数据（{len(sn_list)} 条货件）")

    # 4) 列填充统计
    col_stats = {}
    for i, col in enumerate(HEADERS):
        filled = sum(1 for r in rows if r[i] not in ("", None))
        col_stats[col] = {"filled": filled, "total": len(rows),
                          "source": COLUMN_MAPPING.get(col)}
        log("DEBUG", f"  {col}: {filled}/{len(rows)} 有值 | 字段 {COLUMN_MAPPING.get(col)}")

    if dry_run:
        return {"rows": len(rows), "shipments": len(sn_list), "detail_errors": len(errors),
                "data": rows, "columns": col_stats}

    # 5) 写飞书：直接整体覆盖（新数据行数 >= 旧数据，无需单独清空）
    token = get_tenant_access_token(app_id, app_secret)
    spreadsheet_token, sheet_id = resolve_spreadsheet(feishu_url, token)
    values = [HEADERS] + rows
    write_sheet(spreadsheet_token, sheet_id, token, values)
    write_formula_column(spreadsheet_token, sheet_id, token, len(rows))
    log("INFO", f"已写入飞书表格 {spreadsheet_token}/{sheet_id}: {len(values)} 行（含表头）+ 公式列")
    return {"rows": len(rows), "shipments": len(sn_list), "detail_errors": len(errors),
            "columns": col_stats}


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
        raise SystemExit(
            f"缺少配置文件 {path} —— 请先复制模板并填写真实配置：\n"
            f"    copy {CONFIG_EXAMPLE_FILE} {CONFIG_FILE}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(f"配置文件 {path} 不是合法 JSON: {e}")
    if not isinstance(cfg, dict):
        raise SystemExit(f"配置文件 {path} 顶层必须是 JSON 对象")
    required = ["APP_ID", "APP_SECRET", "FEISHU_URL", "COOKIES"]
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
        feishu_url=config["FEISHU_URL"],
        start_date=config.get("START_DATE", ""),
        end_date=config.get("END_DATE", ""),
        dry_run=dry_run,
    )


def run_and_save_result(config):
    result = run_from_config(config)
    save_result(result, config.get("OUTPUT_FILE") or DEFAULT_OUTPUT_FILE)
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="领星发货单 -> 飞书在线表格")
    parser.add_argument("--dry-run", action="store_true",
                        help="只拉取并映射，不写飞书（数据写入 sync_result.json）")
    args = parser.parse_args()

    config = load_config(CONFIG_FILE)
    if args.dry_run:
        result = run_from_config(config, dry_run=True)
        save_result(result, "sync_result.json")
    else:
        run_and_save_result(config)
