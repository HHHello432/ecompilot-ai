# -*- coding: utf-8 -*-
"""领星 ERP 数据同步 -> 飞书在线表格（单文件版，4 任务整合）

用法：
    python lingxing_sync.py shipment [--dry-run]   # 任务1：发货单明细
    python lingxing_sync.py receivable [--dry-run] # 任务2：月度应收报告
    python lingxing_sync.py fba [--dry-run]        # 任务3：FBA 在售库存
    python lingxing_sync.py stock [--dry-run]      # 任务4：广州仓本地库存
    python lingxing_sync.py all                    # 全部任务按顺序执行

--dry-run 只拉取并映射、不写飞书，结果写入对应 *_result.json。

配置来源：config.json（分组配置，见 config.example.json），已被 .gitignore 忽略。
所有任务共用同一套领星 cookies 与飞书凭据；各任务独立配置表格 URL 与筛选参数。

四个任务：
    1. shipment  发货单明细（含成本）   -> sheet 2hshkO「发货单详情」
    2. receivable 月度应收报告          -> sheet 3WpisA「应收结算报告」
    3. fba       FBA 在售库存（含成本） -> sheet 4DjziB「FBA在售库存详情」
    4. stock     广州仓本地库存         -> sheet 5vpUvJ「广州仓库存」

纯 Python 标准库实现，零第三方依赖。
"""

import argparse
import ast
import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

# =============================================================================
# 全局常量
# =============================================================================
CONFIG_FILE = "config.json"
CONFIG_EXAMPLE_FILE = "config.example.json"

LINGXING_HOST = "https://maique.lingxing.com"
LINGXING_GW_HOST = "https://gw.lingxingerp.com"
FEISHU_HOST = "https://open.feishu.cn"

REQUEST_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0
PAGE_SIZE = 200            # 领星列表分页大小（应收 100）
DETAIL_WORKERS = 10        # 发货单详情并发数
WRITE_BATCH = 1000         # 飞书写入分批行数

# 各任务默认目标表格（同一 wiki 下的不同 sheet）
DEFAULT_URLS = {
    "shipment":   "",
    "receivable": "",
    "fba":        "",
    "stock":      "",
}


def log(level, message):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {level} - {message}")


# =============================================================================
# 公共：领星请求
# =============================================================================
def build_lingxing_headers(cookies):
    """根据 cookies（Name/Value 列表）构造领星请求头。"""
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
        "Referer": "https://maique.lingxing.com/erp/msupply/deliveryOrder",
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


def send_request(url, post_dict, base_headers, request_desc="request", method="POST"):
    """发送领星请求（POST/GET），带超时与指数退避重试，失败返回 None。"""
    result, error = None, None
    for attempt in range(MAX_RETRIES):
        try:
            headers = base_headers.copy()
            headers["X-AK-Request-Id"] = str(uuid.uuid4())
            data = json.dumps(post_dict).encode("utf-8") if post_dict is not None else None
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
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


def fetch_paged(url, body_tpl, base_headers, list_path, total_path=None,
                desc="分页", page_size=PAGE_SIZE):
    """通用分页拉取。

    list_path:  返回结构中 list 的取值路径，如 ("data", "list") 或 ("list",)（顶层）。
    total_path: 返回结构中 total 的取值路径（None 表示与 list 同级取 "total"）。
    返回 (records, total)。
    """
    all_records = []
    offset = 0
    total = None
    while total is None or offset < total:
        body = dict(body_tpl)
        body["offset"] = offset
        body["length"] = page_size
        resp = send_request(url, body, base_headers, f"{desc} offset={offset}")
        if not resp or resp.get("code") != 1:
            log("ERROR", f"接口返回异常: code={resp.get('code') if resp else None}")
            break
        node = resp
        for key in list_path:
            node = (node or {}).get(key)
        total_node = resp
        for key in (total_path if total_path else list_path[:-1] + ("total",)):
            total_node = (total_node or {}).get(key)
        total = total_node if isinstance(total_node, int) else (total or 0)
        lst = node if isinstance(node, list) else []
        if not lst:
            break
        all_records.extend(lst)
        offset += page_size
        log("DEBUG", f"{desc}进度: {len(all_records)}/{total}")
    log("INFO", f"{desc}完成，共 {len(all_records)} 条")
    return all_records, total


# =============================================================================
# 公共：字段解析辅助
# =============================================================================
def join_names(value):
    """列表字段（['张泽荣'] / \"['张泽荣']\" / []）-> 逗号分隔文本；空 -> ''。"""
    if value in (None, "", [], "[]", "null"):
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text in ("[]", "null"):
            return ""
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
        if isinstance(parsed, list):
            return ", ".join(str(x) for x in parsed if x)
        return text
    if isinstance(value, list):
        return ", ".join(str(x) for x in value if x)
    return "" if value is None else str(value)


def to_number(value):
    """纯数字字符串 -> int/float（让飞书存为数值而非文本）；其余原样返回。

    日期、SKU、店铺名等非数值字符串 float 解析失败自动跳过；
    超过 15 位的纯数字串（如公司 ID）不转换，防止误转。
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s or len(s) > 15:
        return value
    try:
        f = float(s)
    except ValueError:
        return value
    if f.is_integer() and "." not in s and "e" not in s.lower():
        return int(f)
    return f


def fmt_shipment_time(value):
    """发货时间统一为 'YYYY-MM-DD HH:MM:SS'（接口可能是字符串或秒级时间戳）。"""
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    try:
        v = int(value)
        if v > 1e12:
            v = v / 1000
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
    except (TypeError, ValueError):
        return "" if value is None else str(value)


def month_excel_serial(settlement_date):
    """结算月 'YYYY-MM' -> 该月 1 号的 Excel 序列号（基准 1899-12-30）。

    无效输入（如 'auto' 未被填充、空值）直接报错——绝不让脏值写进表格。
    """
    text = str(settlement_date or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})", text)
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"无效的月份值: {settlement_date!r}（应为 YYYY-MM；"
                         f"config 设 auto 时需经 load_config 填充后使用）")
    y, mo = int(m.group(1)), int(m.group(2))
    return (date(y, mo, 1) - date(1899, 12, 30)).days


# =============================================================================
# 公共：飞书在线表格（v2 读写）
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
    """解析飞书 URL -> (spreadsheet_token, sheet_id)。支持 wiki 链接与表格直链。"""
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
    """整体覆盖写入：表头 + 数据（分批防单次过大）。返回总行数（含表头）。

    数据行按表头列数自动补齐/截断，保证每行列数与写入范围一致。
    """
    ncols = len(header_row)
    rows = [list(r) + [""] * (ncols - len(r)) if len(r) < ncols
            else list(r[:ncols]) for r in rows]
    end_col = _col_letters(ncols)
    write_sheet_range(spreadsheet_token, sheet_id, token, f"{sheet_id}!A1:{end_col}1", [header_row])
    start_row = 2
    for i in range(0, len(rows), WRITE_BATCH):
        batch = rows[i:i + WRITE_BATCH]
        r0 = start_row + i
        r1 = r0 + len(batch) - 1
        write_sheet_range(spreadsheet_token, sheet_id, token,
                          f"{sheet_id}!A{r0}:{end_col}{r1}", batch)
        log("DEBUG", f"写入批次: 行 {r0}-{r1}（{len(batch)} 行）")
    return len(rows) + 1


def detect_last_data_row(spreadsheet_token, sheet_id, token, probe_cols=10):
    """探测最后一行非空数据行号（读前 probe_cols 列，1000 行分块，连续 2 空块提前停）。

    probe_cols 默认 10：广州仓表 A-C 列（店铺/分类）经常整行为空，只看前 3 列会漏判，
    探测列太少会导致旧数据末行被低估、残留清不干净。
    """
    last, empty_streak, start, chunk = 1, 0, 1, 1000
    while empty_streak < 2 and start <= 100000:
        rng = f"{sheet_id}!A{start}:{_col_letters(probe_cols)}{start + chunk - 1}"
        data = _feishu_req("GET",
                           f"{FEISHU_HOST}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{rng}",
                           token)
        rows = ((data.get("data") or {}).get("valueRange") or {}).get("values") or []
        block_last = None
        for i, r in enumerate(rows):
            if any(str(c).strip() not in ("", "None") for c in r):
                block_last = start + i
        if block_last:
            last = max(last, block_last)
            empty_streak = 0
        else:
            empty_streak += 1
        start += chunk
    return last


def clear_rows(spreadsheet_token, sheet_id, token, ncols, row_start, row_end):
    """把 [row_start, row_end] 区域写空（清理旧数据残留行）。"""
    if row_end < row_start:
        return
    col_end = _col_letters(ncols)
    for s in range(row_start, row_end + 1, WRITE_BATCH):
        e = min(s + WRITE_BATCH - 1, row_end)
        empty = [[""] * ncols for _ in range(e - s + 1)]
        write_sheet_range(spreadsheet_token, sheet_id, token,
                          f"{sheet_id}!A{s}:{col_end}{e}", empty)
        log("DEBUG", f"清理残留行: {s}-{e}")


def upload_sheet(feishu_url, app_id, app_secret, header_row, rows, extra_columns=None):
    """完整写入流程：探测旧数据 -> 写表头+数据（分批）-> 附加列 -> 清理残留行。

    extra_columns: [(列字母, "formula"|"value", 值)]
      formula: 值为公式模板（含 {row} 占位符），逐行写入
      value:   固定值（如月份 Excel 序列号），整列写入
    返回写入后的表格总行数（含表头）。
    """
    token = get_tenant_access_token(app_id, app_secret)
    spreadsheet_token, sheet_id = resolve_spreadsheet(feishu_url, token)
    ncols = len(header_row)
    old_last = detect_last_data_row(spreadsheet_token, sheet_id, token)
    write_all(spreadsheet_token, sheet_id, token, header_row, rows)
    for col, kind, val in extra_columns or []:
        if kind == "formula":
            write_formula_column(spreadsheet_token, sheet_id, token, len(rows), col, val)
        else:
            write_value_column(spreadsheet_token, sheet_id, token, len(rows), col, val)
    new_last = len(rows) + 1
    if old_last > new_last:
        clear_rows(spreadsheet_token, sheet_id, token, ncols, new_last + 1, old_last)
    log("INFO", f"已写入飞书表格 {spreadsheet_token}/{sheet_id}: {new_last} 行（含表头），旧数据末行 {old_last}")
    return new_last


def write_formula_column(spreadsheet_token, sheet_id, token, nrows, formula_col,
                         template, ref_col="C"):
    """向指定列写入逐行公式（模板含 {row} 占位符）。"""
    if nrows < 1:
        return None
    start_row, end_row = 2, nrows + 1
    formulas = [[{"type": "formula", "text": f"={template.format(row=r)}"}]
                for r in range(start_row, end_row + 1)]
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


def write_value_column(spreadsheet_token, sheet_id, token, nrows, col, value):
    """向指定列写入固定值（如月份 Excel 序列号）。"""
    if nrows < 1:
        return None
    start_row, end_row = 2, nrows + 1
    values = [[value] for _ in range(start_row, end_row + 1)]
    range_str = f"{sheet_id}!{col}{start_row}:{col}{end_row}"
    body = {
        "valueRange": {"range": range_str, "values": values},
        "valueInputOption": "USER_INPUT",
    }
    data = _feishu_req("PUT",
                       f"{FEISHU_HOST}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                       token, body=body)
    if data.get("code") != 0:
        raise RuntimeError(f"写入值列失败: {data}")
    return data


def upload_rows(feishu_url, app_id, app_secret, header_row, rows,
                formula_col=None, formula_template=None):
    """（兼容保留）通用上传：解析表格 -> 写表头+数据 ->（可选）写公式列。"""
    extra = [(formula_col, "formula", formula_template)] if formula_col and formula_template else None
    return upload_sheet(feishu_url, app_id, app_secret, header_row, rows, extra_columns=extra)


# =============================================================================
# 任务1：发货单明细（含成本）
# =============================================================================
SHIPMENT_HEADERS = [
    "发货时间", "SKU", "店铺", "货件单号", "发货量",
    "单位FBA仓入库成本", "单位辅料费用", "取值来源",
    "单位头程费用", "单位出库头程费用", "单位税费", "单位出库费用",
    "采购单价", "采购单价(自定义)", "单位出库费用(自定义)",
    "单位辅料费用(自定义)", "单位出库头程(自定义)", "单位FBA仓入库成本(自定义)",
    "发货仓库店铺",
]
SHIPMENT_COLUMNS = {
    "发货时间": "shipment_time", "SKU": "sku", "店铺": "sname",
    "货件单号": "shipment_id", "发货量": "quantity_shipped",
    "单位FBA仓入库成本": "fba_stock_cost", "单位辅料费用": "aux_cost",
    "取值来源": "cost_source", "单位头程费用": "transport_cost",
    "单位出库头程费用": "outbound_head_cost_unit", "单位税费": "tax_unit",
    "单位出库费用": "outbound_cost_unit", "采购单价": "purchase_price_unit",
    "采购单价(自定义)": "custom_purchase_price_unit",
    "单位出库费用(自定义)": "custom_outbound_cost_unit",
    "单位辅料费用(自定义)": "custom_aux_cost",
    "单位出库头程(自定义)": "custom_outbound_head_cost_unit",
    "单位FBA仓入库成本(自定义)": "custom_fba_inbound_cost_unit",
    "发货仓库店铺": "seller_name",
}
# 表格第 20 列「店铺(不含站点)」公式（引用 C 列店铺）
SHIPMENT_SHOP_FORMULA = ('IFERROR(LEFT(C{row},FIND("~",SUBSTITUTE(C{row},"-","~",'
                         'LEN(C{row})-LEN(SUBSTITUTE(C{row},"-",""))))-1),C{row})')
# 不做数字转换的列（SKU 等标识符可能是纯数字，转了会丢前导零/变科学计数法）
SHIPMENT_NO_CONVERT = {"SKU"}


def shipment_list_body(start_date, end_date, offset, length):
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


def fetch_shipment_sns(cookies, start_date, end_date):
    """分页拿发货单号列表。"""
    base_headers = build_lingxing_headers(cookies)
    url = f"{LINGXING_HOST}/api/shipment/showShipmentListV2"
    sn_list = []
    offset, total = 0, None
    while total is None or offset < total:
        resp = send_request(url, shipment_list_body(start_date, end_date, offset, PAGE_SIZE),
                            base_headers, f"发货单列表 offset={offset}")
        if not resp or resp.get("code") != 1:
            break
        d = resp.get("data") or {}
        total = d.get("total", total or 0)
        for s in d.get("list") or []:
            sn = s.get("shipment_sn")
            if sn:
                sn_list.append({"shipment_sn": sn, "shipment_time": s.get("shipment_time", "")})
        offset += PAGE_SIZE
        log("DEBUG", f"发货单列表: {len(sn_list)}/{total}")
    log("INFO", f"发货单列表共 {len(sn_list)} 条")
    return sn_list


def fetch_shipment_detail(cookies, shipment_sn):
    base_headers = build_lingxing_headers(cookies)
    url = (f"{LINGXING_HOST}/api/shipment/showShipmentItemListBySn"
           f"?sort_field=fnsku&sort_type=&id={urllib.parse.quote(shipment_sn)}"
           f"&req_time_sequence=%2Fapi%2Fshipment%2FshowShipmentItemListBySn%24%241")
    resp = send_request(url, None, base_headers, f"详情[{shipment_sn}]", method="GET")
    if not resp or resp.get("code") != 1:
        return None
    return resp.get("data")


def fetch_seller_ids(cookies, exclude_keywords=None):
    """调 /api/my/sellers 获取全部店铺 sid，排除名称含指定关键词的店铺（如「某店铺」）。

    返回排序后的 sid 列表；接口失败时返回空列表（调用方回退 config 中的 SIDS）。
    """
    exclude_keywords = exclude_keywords or []
    base_headers = build_lingxing_headers(cookies)
    url = f"{LINGXING_HOST}/api/my/sellers?req_time_sequence=%2Fapi%2Fmy%2Fsellers%24%241"
    resp = send_request(url, None, base_headers, "店铺列表", method="GET")
    if not resp or resp.get("code") != 1:
        log("ERROR", f"获取店铺列表失败: code={resp.get('code') if resp else None}")
        return []
    sids = []
    for s in resp.get("list") or []:
        name = str(s.get("name") or "")
        if any(kw and kw in name for kw in exclude_keywords):
            log("DEBUG", f"排除店铺: {name} (sid={s.get('id')})")
            continue
        if s.get("id"):
            sids.append(s["id"])
    log("INFO", f"店铺列表: 共 {len(resp.get('list') or [])} 个，排除后 {len(sids)} 个")
    return sorted(sids)


def shipment_map_item(item):
    row = []
    for col in SHIPMENT_HEADERS:
        key = SHIPMENT_COLUMNS[col]
        value = item.get(key) if isinstance(item, dict) else None
        if col == "发货时间":
            value = fmt_shipment_time(value)
        elif col == "发货仓库店铺" and value in (None, ""):
            value = item.get("sname") if isinstance(item, dict) else None
        elif col not in SHIPMENT_NO_CONVERT:
            value = to_number(value)
        row.append("" if value is None else value)
    return row


def run_shipment(cookies, app_id, app_secret, cfg_task, dry_run=False):
    """任务1：发货单明细 -> 2hshkO（含 T 列公式、U 列月份）。"""
    start_date = cfg_task.get("START_DATE", "")
    end_date = cfg_task.get("END_DATE", "")
    for label, val in (("START_DATE", start_date), ("END_DATE", end_date)):
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(val or "")):
            raise SystemExit(f"SHIPMENT.{label} 无效: {val!r}"
                             f"（应为 YYYY-MM-DD；config 设 auto 时须经 load_config 填充）")
    feishu_url = cfg_task.get("FEISHU_URL", DEFAULT_URLS["shipment"])

    sn_list = fetch_shipment_sns(cookies, start_date, end_date)
    details = {}
    errors = []
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as executor:
        futures = {executor.submit(fetch_shipment_detail, cookies, it["shipment_sn"]): it["shipment_sn"]
                   for it in sn_list}
        done = 0
        for fut in as_completed(futures):
            sn = futures[fut]
            try:
                data = fut.result()
                if data:
                    details[sn] = data
                else:
                    errors.append(sn)
            except Exception:
                errors.append(sn)
            done += 1
            if done % 100 == 0 or done == len(futures):
                log("INFO", f"详情进度: {done}/{len(futures)}（成功 {len(details)}，失败 {len(errors)}）")
    if errors:
        log("WARNING", f"详情失败 {len(errors)} 单: {errors[:10]}")

    rows = []
    for it in sn_list:
        data = details.get(it["shipment_sn"])
        if not data:
            continue
        for item in data.get("items") or []:
            rows.append(shipment_map_item(item))
    log("INFO", f"发货单展开 {len(rows)} 行商品数据")

    if dry_run:
        return {"rows": len(rows), "shipments": len(sn_list), "detail_errors": len(errors), "data": rows}

    # A1:S 数据 -> T 列「店铺(不含站点)」公式 -> U 列「时间」月份（当月 1 号 Excel 序列号）
    upload_sheet(feishu_url, app_id, app_secret, SHIPMENT_HEADERS, rows, extra_columns=[
        ("T", "formula", SHIPMENT_SHOP_FORMULA),
        ("U", "value", month_excel_serial(start_date)),
    ])
    return {"rows": len(rows), "shipments": len(sn_list), "detail_errors": len(errors)}


# =============================================================================
# 任务2：月度应收报告
# =============================================================================
RECEIVABLE_HEADERS = [
    "店铺", "站点", "结算月", "对账结果", "币种",
    "期初余额", "收入", "退款", "支出", "其他",
    "信用卡", "其他项目", "转账成功", "到账成功", "转账失败",
    "期末余额", "备注",
]
RECEIVABLE_COLUMNS = {
    "店铺": "storeName", "站点": "country", "结算月": "settlementDate",
    "对账结果": "archiveStatusName", "币种": "currencyCode",
    "期初余额": "beginningBalanceCurrencyAmount", "收入": "incomeAmount",
    "退款": "refundAmount", "支出": "spendAmount", "其他": "other",
    "信用卡": "card", "其他项目": "otherItem",
    "转账成功": "convertedSuccessAmount", "到账成功": "receivedAmount",
    "转账失败": "convertedFailedAmount", "期末余额": "endingBalance",
    "备注": "remark",
}
# R 列「店铺」公式（引用 A 列店铺提取不含站点）
RECEIVABLE_SHOP_FORMULA = ('IFERROR(LEFT(A{row},FIND("~",SUBSTITUTE(A{row},"-","~",'
                           'LEN(A{row})-LEN(SUBSTITUTE(A{row},"-",""))))-1),A{row})')


def receivable_body(settle_month, sids, offset, length):
    return {
        "settleMonth": settle_month, "archiveStatus": "", "currencyCode": "CNY",
        "sortField": "endingBalance", "sortType": "desc", "receivedState": "",
        "offset": offset, "length": length, "sids": sids, "mids": [],
        "req_time_sequence": "/bd/sp/api/monthly/receivable/report/list$$3",
    }


def receivable_map(record):
    row = []
    for col in RECEIVABLE_HEADERS:
        value = record.get(RECEIVABLE_COLUMNS[col]) if isinstance(record, dict) else None
        if col not in ("结算月", "备注"):
            value = to_number(value)
        row.append("" if value is None else value)
    return row


def run_receivable(cookies, app_id, app_secret, cfg_task, dry_run=False):
    """任务2：应收报告 -> 3WpisA（R 列公式 + S 列月份）。

    筛选条件：结算月 = 当月（config SETTLE_MONTH，auto 由 load_config 填充）；
    币种 CNY；店铺 = 全部店铺排除名称含 EXCLUDE_SELLERS 关键词的（默认「某店铺」）。
    """
    settle_month = cfg_task.get("SETTLE_MONTH", "")
    if not re.match(r"^\d{4}-\d{2}$", str(settle_month or "")):
        raise SystemExit(f"RECEIVABLE.SETTLE_MONTH 无效: {settle_month!r}"
                         f"（应为 YYYY-MM；config 设 auto 时须经 load_config 填充）")
    feishu_url = cfg_task.get("FEISHU_URL", DEFAULT_URLS["receivable"])

    # 店铺：优先动态获取（排除某店铺等），SIDS 显式配置时用配置值
    sids = cfg_task.get("SIDS") or []
    if not sids:
        sids = fetch_seller_ids(cookies, cfg_task.get("EXCLUDE_SELLERS", []))
    if not sids:
        raise RuntimeError("应收任务没有可用店铺 sid（动态获取失败且未配置 SIDS）")

    base_headers = build_lingxing_headers(cookies)
    url = f"{LINGXING_GW_HOST}/bd/sp/api/monthly/receivable/report/list"
    records, _ = fetch_paged(url, receivable_body(settle_month, sids, 0, 100),
                             base_headers, ("data", "records"), ("data", "total"),
                             "应收报告", page_size=100)
    rows = [receivable_map(r) for r in records]

    if dry_run:
        return {"rows": len(rows), "sids": len(sids), "settle_month": settle_month, "data": rows}

    # 表头 19 列：17 数据列 + R「店铺」公式 + S「月份」（当月 1 号 Excel 序列号）
    header_row = RECEIVABLE_HEADERS + ["店铺", "月份"]
    upload_sheet(feishu_url, app_id, app_secret, header_row, rows, extra_columns=[
        ("R", "formula", RECEIVABLE_SHOP_FORMULA),
        ("S", "value", month_excel_serial(settle_month)),
    ])
    return {"rows": len(rows), "sids": len(sids)}


# =============================================================================
# 任务3：FBA 在售库存
# =============================================================================
FBA_HEADERS = [
    "父ASIN", "所属仓库", "店铺", "ASIN", "MSKU", "FNSKU", "SKU", "品名",
    "SPU", "款名", "属性", "一级分类", "二级分类", "三级分类", "品牌", "负责人",
    "FBA总库存(成本)", "FBA可用库存(成本)", "FBA可售(成本)", "FBM可售(成本)",
    "FBA预留(成本)", "FBA待调仓(成本)", "FBA调仓中(成本)", "FBA待发货(成本)",
    "FBA计划入库(成本)", "FBA标发在途(成本)", "FBA实际在途(成本)", "FBA入库中(成本)",
    "FBA不可售(成本)", "FBA调查中(成本)",
    "单位采购成本", "单位头程费用", "FBA总货值", "FBA总库存成本",
    "仓储类型", "配送方式",
]
FBA_COLUMNS = {
    "父ASIN": "parent_asin_real", "所属仓库": "name", "店铺": "seller_name",
    "ASIN": "asin", "MSKU": "seller_sku", "FNSKU": "fnsku", "SKU": "sku",
    "品名": "product_name", "SPU": "spu", "款名": "spu_name",
    "属性": "attribute", "一级分类": "category_level1",
    "二级分类": "category_level2", "三级分类": "category_level3",
    "品牌": "product_brand_text", "负责人": "asin_principal_list",
    "FBA总库存(成本)": "total_price", "FBA可用库存(成本)": "available_total_price",
    "FBA可售(成本)": "afn_fulfillable_quantity_price", "FBM可售(成本)": "quantity_price",
    "FBA预留(成本)": "afn_reserved_quantity_price",
    "FBA待调仓(成本)": "reserved_fc_transfers_price",
    "FBA调仓中(成本)": "reserved_fc_processing_price",
    "FBA待发货(成本)": "reserved_customerorders_price",
    "FBA计划入库(成本)": "afn_inbound_working_quantity_price",
    "FBA标发在途(成本)": "afn_inbound_shipped_quantity_price",
    "FBA实际在途(成本)": "real_transit_quantity",   # 无金额字段，写数量
    "FBA入库中(成本)": "afn_inbound_receiving_quantity_price",
    "FBA不可售(成本)": "afn_unsellable_quantity_price",
    "FBA调查中(成本)": "afn_researching_quantity_price",
    "单位采购成本": "cg_price", "单位头程费用": "cg_transport_costs",
    "FBA总货值": "total_amount", "FBA总库存成本": "total_cost",
    "仓储类型": "storage_type_name", "配送方式": "fulfillment_channel_name",
}
# 不做数字转换的列（标识符可能是纯数字，防止丢前导零）
FBA_NO_CONVERT = {"父ASIN", "ASIN", "MSKU", "FNSKU", "SKU", "SPU"}


def fba_body(offset, length):
    return {
        "cid": "", "bid": "", "attribute": "", "asin_principal": "",
        "search_field": "sku", "is_cost_page": 1, "status": "",
        "senior_search_list": "[]", "offset": offset, "length": length,
        "fulfillment_channel_type": "FBA", "is_hide_zero_stock": "0",
        "is_parant_asin_merge": "0",
        "req_time_sequence": "/api/storage/fbaLists$$2",
    }


def fba_map(record):
    row = []
    for col in FBA_HEADERS:
        key = FBA_COLUMNS[col]
        value = record.get(key) if isinstance(record, dict) else None
        if col in ("负责人", "属性"):
            value = join_names(value)   # attribute 为 JSON 数组字符串，同样解析
        elif col not in FBA_NO_CONVERT:
            value = to_number(value)
        row.append("" if value is None else value)
    return row


def run_fba(cookies, app_id, app_secret, cfg_task, dry_run=False):
    """任务3：FBA 库存 -> 4DjziB（36 列，分批写入）。"""
    feishu_url = cfg_task.get("FEISHU_URL", DEFAULT_URLS["fba"])
    base_headers = build_lingxing_headers(cookies)
    url = f"{LINGXING_HOST}/api/storage/fbaLists"
    # 注意：fbaLists 的 list/total 在顶层（不在 data 里）
    records, _ = fetch_paged(url, fba_body(0, PAGE_SIZE), base_headers,
                             ("list",), ("total",), "FBA库存", page_size=PAGE_SIZE)
    rows = [fba_map(r) for r in records]
    log("INFO", f"FBA 库存映射完成 {len(rows)} 行")

    if dry_run:
        return {"rows": len(rows), "data": rows}

    upload_rows(feishu_url, app_id, app_secret, FBA_HEADERS, rows)
    return {"rows": len(rows)}


# =============================================================================
# 任务4：广州仓本地库存
# =============================================================================
STOCK_HEADERS = [
    "店铺", "一级分类", "二级分类", "三级分类", "品牌", "Listing负责人",
    "产品状态", "可用量", "次品量", "FBA发货单待配货量",
    "实际总量", "预计总量", "安全库存量",
    "采购单价", "单位费用", "单位库存成本", "货值", "费用", "预警状态", "库存成本",
    "产品负责人", "备注", "平均库龄", "头程", "单位头程",
    "271-360天库龄", "361天以上库龄",
]
STOCK_COLUMNS = {
    "店铺": "store_name_list", "一级分类": "category_name",
    "二级分类": None, "三级分类": None, "品牌": "brand_name",
    "Listing负责人": None, "产品状态": "product_status_name",
    "可用量": "good_num", "次品量": "bad_num",
    "FBA发货单待配货量": "pending_num", "实际总量": "total",
    "预计总量": "pre_total", "安全库存量": "storage_safe_num",
    "采购单价": "purchase_price", "单位费用": "price",
    "单位库存成本": "stock_price", "货值": "amount", "费用": "fee",
    "预警状态": "warn_status_name", "库存成本": "stock_cost",
    "产品负责人": "principal_name_list", "备注": "remark",
    "平均库龄": "average_age", "头程": "head_stock_cost",
    "单位头程": "head_stock_price",
    "271-360天库龄": "section7", "361天以上库龄": "section8",
}
STOCK_NO_CONVERT = {"备注"}


def stock_body(offset, length):
    return {
        "wid_list": "3275", "mid_list": "", "sid_list": "", "inventoryOwnership": -1,
        "cid_list": "", "bid_list": "", "principal_list": "", "product_type_list": "",
        "product_attribute": "", "product_status": "", "search_field": "sku",
        "search_value": "", "is_sku_merge_show": 0, "is_hide_zero_stock": 0,
        "offset": offset, "length": length, "sort_field": "total", "sort_type": "desc",
        "gtag_ids": "", "senior_search_list": "[]", "permission_uid_list": "",
        "country_code_list": "", "has_statistic": False,
        "req_time_sequence": "/api/storage/lists$$3",
    }


def stock_map(record):
    row = []
    for col in STOCK_HEADERS:
        key = STOCK_COLUMNS[col]
        value = record.get(key) if (key and isinstance(record, dict)) else None
        if col in ("店铺", "产品负责人"):
            value = join_names(value)
        elif col not in STOCK_NO_CONVERT:
            value = to_number(value)
        row.append("" if value is None else value)
    return row


def run_stock(cookies, app_id, app_secret, cfg_task, dry_run=False):
    """任务4：广州仓库存 -> 5vpUvJ（27 列，分批写入）。"""
    feishu_url = cfg_task.get("FEISHU_URL", DEFAULT_URLS["stock"])
    base_headers = build_lingxing_headers(cookies)
    url = f"{LINGXING_HOST}/api/storage/lists"
    records, _ = fetch_paged(url, stock_body(0, PAGE_SIZE), base_headers,
                             ("data", "list"), ("data", "total"), "仓库库存", page_size=PAGE_SIZE)
    rows = [stock_map(r) for r in records]
    log("INFO", f"仓库库存映射完成 {len(rows)} 行")

    if dry_run:
        return {"rows": len(rows), "data": rows}

    upload_rows(feishu_url, app_id, app_secret, STOCK_HEADERS, rows)
    return {"rows": len(rows)}


# =============================================================================
# 配置驱动
# =============================================================================
def current_month_range():
    """当月第一天与最后一天 -> (YYYY-MM-DD, YYYY-MM-DD)。

    发货单任务按「当月所有已发货」统计，日期范围动态取当前自然月。
    """
    today = date.today()
    first_day = today.replace(day=1)
    if today.month == 12:
        last_day = date(today.year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(today.year, today.month + 1, 1) - timedelta(days=1)
    return first_day.isoformat(), last_day.isoformat()


def current_settle_month():
    """当前月份 -> 'YYYY-MM'（应收任务的结算月）。"""
    return date.today().strftime("%Y-%m")


def default_config(cookie_list):
    """生成配置骨架，日期动态取当前月份（与 config.json 同构，可直接写盘）。

    - 发货单：当月 1 号 ~ 当月最后一天（统计当月所有已发货）
    - 应收：结算月 = 当月
    - FBA / 广州仓：无日期筛选，始终全量替换

    cookie_list: 领星 cookies（Name/Value 列表）。

    安全约定：APP_ID / APP_SECRET / FEISHU_URL / EXCLUDE_SELLERS 属于凭据与客户
    私有配置，一律不写入代码。首次运行生成的 config.json 需要手动补全这几项。
    """
    start_date, end_date = current_month_range()
    return {
        "APP_ID": "",
        "APP_SECRET": "",
        "COOKIES": cookie_list,
        "SHIPMENT": {
            "START_DATE": start_date,
            "END_DATE": end_date,
            "FEISHU_URL": "",
        },
        "RECEIVABLE": {
            "SETTLE_MONTH": current_settle_month(),
            "SIDS": [],
            "FEISHU_URL": "",
            "EXCLUDE_SELLERS": [],
        },
        "FBA": {
            "FEISHU_URL": "",
        },
        "STOCK": {
            "FEISHU_URL": "",
        },
    }


def _resolve_path(path):
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), path)


def load_config(config_path=CONFIG_FILE):
    path = _resolve_path(config_path)
    if not os.path.exists(path):
        raise SystemExit(f"缺少配置文件 {path} —— 请复制 {CONFIG_EXAMPLE_FILE} 为 {CONFIG_FILE}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("APP_ID") or not cfg.get("APP_SECRET") or not cfg.get("COOKIES"):
        raise SystemExit("config.json 缺少 APP_ID / APP_SECRET / COOKIES")
    # 动态日期：发货单日期/应收结算月 缺失或为 "auto" 时，自动取当前月份。
    # 这样每月运行无需改 config.json，始终统计当月数据。
    start_date, end_date = current_month_range()
    sh = cfg.setdefault("SHIPMENT", {})
    if str(sh.get("START_DATE") or "").strip().lower() in ("", "auto"):
        sh["START_DATE"] = start_date
    if str(sh.get("END_DATE") or "").strip().lower() in ("", "auto"):
        sh["END_DATE"] = end_date
    rv = cfg.setdefault("RECEIVABLE", {})
    if str(rv.get("SETTLE_MONTH") or "").strip().lower() in ("", "auto"):
        rv["SETTLE_MONTH"] = current_settle_month()
    return cfg


def save_result(result, out_path):
    out_path = _resolve_path(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    log("INFO", f"结果已写入 {out_path}")
    return out_path


def run_task(name, config, dry_run=False):
    """按任务名执行，返回结果 dict。"""
    cfg_task = config.get(name.upper(), {})
    if name == "shipment":
        result = run_shipment(config["COOKIES"], config["APP_ID"], config["APP_SECRET"],
                              cfg_task, dry_run=dry_run)
    elif name == "receivable":
        result = run_receivable(config["COOKIES"], config["APP_ID"], config["APP_SECRET"],
                                cfg_task, dry_run=dry_run)
    elif name == "fba":
        result = run_fba(config["COOKIES"], config["APP_ID"], config["APP_SECRET"],
                         cfg_task, dry_run=dry_run)
    elif name == "stock":
        result = run_stock(config["COOKIES"], config["APP_ID"], config["APP_SECRET"],
                           cfg_task, dry_run=dry_run)
    else:
        raise SystemExit(f"未知任务: {name}")
    return result


def main(task, config, dry_run=False):
    """按任务名执行。config: 配置字典（由 __main__ 从本地文件加载后传入）。"""
    tasks = ["shipment", "receivable", "fba", "stock"] if task == "all" else [task]
    for name in tasks:
        log("INFO", f"===== 开始任务: {name} =====")
        result = run_task(name, config, dry_run=dry_run)
        if dry_run:
            save_result(result, os.path.join("output", f"{name}_result.json"))
        log("INFO", f"===== 任务 {name} 完成: {result} =====")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="领星 ERP 数据同步到飞书在线表格（4 任务）")
    parser.add_argument("task", choices=["shipment", "receivable", "fba", "stock", "all"],
                        help="要执行的任务（all 表示全部按顺序执行）")
    parser.add_argument("--dry-run", action="store_true", help="只拉取并映射，不写飞书")
    args = parser.parse_args()

    # 读取本地配置文件（仅在此处读文件；main / run_* 均只接收配置字典）
    config = load_config(CONFIG_FILE)
    main(args.task, config, dry_run=args.dry_run)
