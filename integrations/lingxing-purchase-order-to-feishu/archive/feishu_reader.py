"""飞书多维表格读取模块（方案 A 逆向）。

功能：
    读取「采购单」表(表1)中指定视图（默认「未入库单」）的记录，
    结合「采购单SKU明细」表(表2)的 SKU 数据，
    还原成与 lingxing_collector.A3() 输入结构一致的 order_dict 列表。

凭据（app_id / app_secret / app_token）一律作为入参，不落盘、不读环境变量。
"""

import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BASE = "https://open.feishu.cn/open-apis"
TABLE1_ID = "tblU0rUgG5MkU94G"   # 采购单（主表，订单级）
TABLE2_ID = "tbl4QqMWyPMbMPGb"   # 采购单SKU明细（子表，SKU级）

VIEW_NAME_DEFAULT = "未入库单"

HTTP_TIMEOUT = 30
REQUEST_RETRY = 3

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def log(level, message):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} - {level} - {message}")


# ---------------------------------------------------------------------------
# 基础请求
# ---------------------------------------------------------------------------
def _api_get(url, token, params=None):
    """带重试的 GET 请求，返回解析后的 JSON。"""
    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}?{query}"
    for attempt in range(REQUEST_RETRY):
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            }, method="GET")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except Exception as e:
            log("WARNING", f"GET {url} 第{attempt + 1}次失败: {e}")
    raise Exception(f"GET {url} 三次重试均失败")


def get_tenant_access_token(app_id, app_secret):
    """获取 tenant_access_token（内部应用鉴权）。"""
    url = f"{BASE}/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json; charset=utf-8",
    }, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("code") != 0:
        raise Exception(f"获取 token 失败: {result}")
    return result["tenant_access_token"]


# ---------------------------------------------------------------------------
# 分页读取
# ---------------------------------------------------------------------------
def list_all_records(app_token, table_id, token, view_id=None, page_size=500):
    """分页读取某表全部记录（可选按视图过滤）。

    返回 list[dict]，每项形如 {"record_id": "...", "fields": {...}}。
    注意：page_size 最大 500。
    """
    records = []
    page_token = None
    while True:
        params = {"page_size": page_size}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token
        url = f"{BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        data = _api_get(url, token, params)
        if data.get("code") != 0:
            raise Exception(f"读取记录失败: {data}")
        items = data.get("data", {}).get("items", [])
        records.extend(items)
        if data.get("data", {}).get("has_more") and data.get("data", {}).get("page_token"):
            page_token = data["data"]["page_token"]
        else:
            break
    return records


def find_view_id(app_token, table_id, token, view_name):
    """按视图名在指定表中查找 view_id。"""
    url = f"{BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/views"
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        data = _api_get(url, token, params)
        if data.get("code") != 0:
            raise Exception(f"读取视图列表失败: {data}")
        for v in data.get("data", {}).get("items", []):
            if v.get("view_name") == view_name:
                return v.get("view_id")
        if data.get("data", {}).get("has_more") and data.get("data", {}).get("page_token"):
            page_token = data["data"]["page_token"]
        else:
            break
    raise ValueError(f"未找到名为「{view_name}」的视图")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _ms_to_str(value):
    """飞书日期字段读回的是 13 位毫秒时间戳（int）。转成北京时间字符串。

    若已是字符串或无法解析，则原样返回。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return value
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------
def read_unstocked_orders(app_id, app_secret, app_token,
                          table1_id=TABLE1_ID, table2_id=TABLE2_ID,
                          view_name=VIEW_NAME_DEFAULT, view_id=None,
                          default_status_text="待到货"):
    """读取「未入库单」视图，输出与 A3 输入结构一致的数据。

    参数：
        app_id, app_secret : 飞书机器人凭据
        app_token          : 多维表格 app_token
        table1_id          : 采购单表 id（默认已建好的 tblU0rUgG5MkU94G）
        table2_id          : 采购单SKU明细表 id（默认已建好的 tbl4QqMWyPMbMPGb）
        view_name          : 要读取的视图名（默认「未入库单」）
        view_id            : 若已知视图 id 可直接传入，跳过按名查找
        default_status_text: 重建 order_dict 时填入的 status_text。
                             原字段来自爬虫，飞书只存了「是否完成下单」布尔值，
                             无法完全还原；视图是未入库单，默认填「待到货」。

    返回：
        list[dict]，每项结构等同 A3 的 input_list 元素，例如：
        {
            "order_sn": "PO-001",
            "alibaba_order_sn": "...",
            "status_text": "待到货",
            "order_time": "2024-01-02 10:30:00",
            "item_list": [{"sku": "ABC", "plan_sn": None}, ...]
        }

    注意（数据缺口，务必知悉）：
        1. plan_sn 未存储在飞书两张表里，这里只能填 None。
           A3 里用 item_list[0].plan_sn 去查「加工单」(storage_process/lists)，
           因此回跑时「加工日期」将无法刷新（A3 内部已 try/except，不会崩，
           只是 time_finish 为 None）。如需恢复，需给表2 增加 plan_sn 字段
           并在上传时写入。
        2. status_text 用 default_status_text 兜底，非原始爬虫值。
    """
    token = get_tenant_access_token(app_id, app_secret)

    if not view_id:
        log("INFO", f"按视图名「{view_name}」查找 view_id")
        view_id = find_view_id(app_token, table1_id, token, view_name)
        log("INFO", f"找到视图 view_id={view_id}")
    else:
        log("INFO", f"使用传入 view_id={view_id}")

    # 1) 读表1（按视图过滤）
    log("INFO", "读取采购单表（未入库单视图）")
    order_records = list_all_records(app_token, table1_id, token, view_id=view_id)
    log("INFO", f"视图内订单数: {len(order_records)}")

    # 2) 读表2 全量（关联字段不能做筛选，只能全量读回再分组）
    log("INFO", "读取采购单SKU明细表（全量）")
    sku_records = list_all_records(app_token, table2_id, token)
    log("INFO", f"明细表记录数: {len(sku_records)}")

    # 3) 由「采购单号_SKU」拆出 采购单号 -> [SKU]
    sku_map = {}
    for r in sku_records:
        fields = r.get("fields", {})
        combo = fields.get("采购单号_SKU")
        if not combo or not isinstance(combo, str):
            continue
        order_sn, _, sku_tail = combo.rpartition("#")
        if not order_sn:
            continue
        sku_val = fields.get("SKU") or (sku_tail if sku_tail else None)
        if not sku_val:
            continue
        sku_map.setdefault(order_sn, [])
        if sku_val not in sku_map[order_sn]:
            sku_map[order_sn].append(sku_val)

    # 4) 组装 order_dict
    result = []
    for r in order_records:
        fields = r.get("fields", {})
        order_sn = fields.get("采购单号")
        if not order_sn:
            continue
        item_list = [{"sku": sku, "plan_sn": None} for sku in sku_map.get(order_sn, [])]
        order_dict = {
            "order_sn": order_sn,
            "alibaba_order_sn": fields.get("1688订单号") or "",
            "status_text": default_status_text,
            "order_time": _ms_to_str(fields.get("下单日期")),
            "item_list": item_list,
        }
        if not item_list:
            log("WARNING", f"订单 [{order_sn}] 在明细表中无 SKU，A3 将跳过该订单")
        result.append(order_dict)

    log("INFO", f"重建完成，共 {len(result)} 个订单（含 SKU 明细）")
    return result


# ---------------------------------------------------------------------------
# 独立运行（可选）：python feishu_reader.py
#   凭据可从环境变量读取，便于快速验证（不强制）。
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import argparse

    parser = argparse.ArgumentParser(description="读取飞书未入库单视图，还原 A3 输入结构")
    parser.add_argument("--app_id", default=os.environ.get("FEISHU_APP_ID"))
    parser.add_argument("--app_secret", default=os.environ.get("FEISHU_APP_SECRET"))
    parser.add_argument("--app_token", default=os.environ.get("FEISHU_APP_TOKEN"))
    parser.add_argument("--table1_id", default=TABLE1_ID)
    parser.add_argument("--table2_id", default=TABLE2_ID)
    parser.add_argument("--view_name", default=VIEW_NAME_DEFAULT)
    parser.add_argument("--view_id", default=None)
    parser.add_argument("--out", default="unstocked_orders.json", help="输出 JSON 路径")
    args = parser.parse_args()

    if not (args.app_id and args.app_secret and args.app_token):
        raise SystemExit("缺少凭据：请通过参数或环境变量 FEISHU_APP_ID/SECRET/TOKEN 提供")

    data = read_unstocked_orders(
        args.app_id, args.app_secret, args.app_token,
        table1_id=args.table1_id, table2_id=args.table2_id,
        view_name=args.view_name, view_id=args.view_id,
    )
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已写入 {args.out}，共 {len(data)} 个订单")
