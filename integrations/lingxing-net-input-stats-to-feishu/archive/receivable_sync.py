# -*- coding: utf-8 -*-
"""月度应收报告同步：领星应收报告 -> 飞书在线表格（单文件版）

流程：
    1) 分页拉取领星月度应收报告（gw.lingxingerp.com/bd/sp/api/monthly/receivable/report/list）
    2) 字段映射：英文变量 -> 中文表头（17 列数据 + 公式列「店铺」 + 日期列「月份」）
    3) 整体覆盖写入飞书在线表格（Sheets）

配置来源：config.json（与发货单同步共用一套飞书凭据/领星 cookies）。
入口在 __main__ 中读取一次 config.json，作为入参传给主逻辑方法。
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date

# =============================================================================
# 全局配置
# =============================================================================
CONFIG_FILE = "config.json"
CONFIG_EXAMPLE_FILE = "config.example.json"
DEFAULT_OUTPUT_FILE = "receivable_result.json"

LINGXING_GW_HOST = "https://gw.lingxingerp.com"
FEISHU_HOST = "https://open.feishu.cn"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0
PAGE_SIZE = 100            # 应收报告分页大小

# 目标表格：与发货单同一 wiki，sheet 3WpisA
FEISHU_URL_DEFAULT = ""

# 表头（17 列数据列；R 列「店铺」为公式列、S 列「月份」为日期列，由脚本单独处理）
HEADERS = [
    "店铺", "站点", "结算月", "对账结果", "币种",
    "期初余额", "收入", "退款", "支出", "其他",
    "信用卡", "其他项目", "转账成功", "到账成功", "转账失败",
    "期末余额", "备注",
]

# 中文列名 -> 接口返回字段
COLUMN_MAPPING = {
    "店铺":   "storeName",
    "站点":   "country",
    "结算月": "settlementDate",
    "对账结果": "archiveStatusName",
    "币种":   "currencyCode",
    "期初余额": "beginningBalanceCurrencyAmount",
    "收入":   "incomeAmount",
    "退款":   "refundAmount",
    "支出":   "spendAmount",
    "其他":   "other",
    "信用卡": "card",
    "其他项目": "otherItem",
    "转账成功": "convertedSuccessAmount",
    "到账成功": "receivedAmount",
    "转账失败": "convertedFailedAmount",
    "期末余额": "endingBalance",
    "备注":   "remark",
}

# R 列「店铺」公式（从 A 列店铺提取不含站点，与表格既有公式一致）
SHOP_SHORT_FORMULA = ('IFERROR(LEFT(A{row},FIND("~",SUBSTITUTE(A{row},"-","~",'
                      'LEN(A{row})-LEN(SUBSTITUTE(A{row},"-",""))))-1),A{row})')


def log(level, message):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {level} - {message}")


# =============================================================================
# 领星请求
# =============================================================================
def build_lingxing_headers(cookies):
    """照发货单同步：cookies（Name/Value）构造请求头。"""
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
        "Referer": "https://maique.lingxing.com/",
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


def fetch_receivable(cookies, settle_month, sids):
    """分页拉取月度应收报告，返回 list[record]。"""
    base_headers = build_lingxing_headers(cookies)
    url = f"{LINGXING_GW_HOST}/bd/sp/api/monthly/receivable/report/list"
    all_records = []
    offset = 0
    total = None
    while total is None or offset < total:
        body = {
            "settleMonth": settle_month, "archiveStatus": "", "currencyCode": "CNY",
            "sortField": "endingBalance", "sortType": "desc", "receivedState": "",
            "offset": offset, "length": PAGE_SIZE, "sids": sids, "mids": [],
            "req_time_sequence": "/bd/sp/api/monthly/receivable/report/list$$3",
        }
        resp = send_request(url, body, base_headers, f"应收报告 offset={offset}")
        if not resp or resp.get("code") != 1:
            log("ERROR", f"接口返回异常: {resp}")
            break
        d = resp.get("data") or {}
        total = d.get("total", total or 0)
        records = d.get("records") or []
        if not records:
            break
        all_records.extend(records)
        offset += PAGE_SIZE
        log("DEBUG", f"分页进度: {len(all_records)}/{total}")
    log("INFO", f"共获取 {len(all_records)} 条应收记录")
    return all_records


# =============================================================================
# 字段映射
# =============================================================================
def _month_excel_serial(settlement_date):
    """结算月 'YYYY-MM' -> 该月 1 号的 Excel 序列号（与表格既有「月份」列一致）。

    Excel 序列号基准：1900-01-01 = 1（即 1899-12-30 起算）。
    """
    try:
        y, m = (int(x) for x in settlement_date.split("-")[:2])
        d = date(y, m, 1)
        base = date(1899, 12, 30)
        return (d - base).days
    except (ValueError, AttributeError):
        return settlement_date


def map_record(record):
    """把一条应收记录映射为 17 列数据行。"""
    row = []
    for col in HEADERS:
        key = COLUMN_MAPPING[col]
        value = record.get(key) if isinstance(record, dict) else None
        row.append("" if value is None else value)
    return row


def build_rows(records):
    """展开为 19 列完整行：[17 数据列, R列公式, S列月份]（供飞书写入）。"""
    rows = []
    for i, record in enumerate(records):
        row = map_record(record)
        # R 列（第18列）公式：从 A 列店铺提取不含站点
        row.append({"type": "formula", "text": f"={SHOP_SHORT_FORMULA.format(row=i + 2)}"})
        # S 列（第19列）月份：结算月 1 号的 Excel 序列号
        row.append(_month_excel_serial(record.get("settlementDate")))
        rows.append(row)
    return rows


# =============================================================================
# 飞书在线表格（与发货单同步共用 v2 读写）
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


def write_sheet(spreadsheet_token, sheet_id, token, values):
    """整体覆盖写入（含公式单元格）。values: 二维列表，首行为表头。"""
    nrows = len(values)
    ncols = max((len(r) for r in values), default=19)
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


# =============================================================================
# 主流程
# =============================================================================
def run_sync(cookies, app_id, app_secret, feishu_url, settle_month, sids,
             dry_run=False):
    records = fetch_receivable(cookies, settle_month, sids)
    if not records:
        raise RuntimeError("未获取到任何应收记录，请检查结算月与 cookies")

    rows = build_rows(records)
    log("INFO", f"展开为 {len(rows)} 行应收数据")

    # 列填充统计
    col_stats = {}
    for i, col in enumerate(HEADERS):
        filled = sum(1 for r in rows if r[i] not in ("", None))
        col_stats[col] = f"{filled}/{len(rows)}"

    if dry_run:
        return {"rows": len(rows), "records": len(records), "data": rows, "columns": col_stats}

    token = get_tenant_access_token(app_id, app_secret)
    spreadsheet_token, sheet_id = resolve_spreadsheet(feishu_url, token)
    header_row = HEADERS + ["店铺", "月份"]          # 19 列表头（R 列店铺、S 列月份）
    values = [header_row] + rows
    write_sheet(spreadsheet_token, sheet_id, token, values)
    log("INFO", f"已写入飞书表格 {spreadsheet_token}/{sheet_id}: {len(values)} 行（含表头）")
    return {"rows": len(rows), "records": len(records), "columns": col_stats}


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
        feishu_url=config.get("RECEIVABLE_FEISHU_URL", FEISHU_URL_DEFAULT),
        settle_month=config.get("SETTLE_MONTH", ""),
        sids=config.get("RECEIVABLE_SIDS", []),
        dry_run=dry_run,
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="领星月度应收报告 -> 飞书在线表格")
    parser.add_argument("--dry-run", action="store_true",
                        help="只拉取并映射，不写飞书（数据写入 receivable_result.json）")
    args = parser.parse_args()

    config = load_config(CONFIG_FILE)
    if args.dry_run:
        result = run_from_config(config, dry_run=True)
        save_result(result, "receivable_result.json")
    else:
        result = run_from_config(config)
        save_result(result, config.get("OUTPUT_FILE") or DEFAULT_OUTPUT_FILE)
