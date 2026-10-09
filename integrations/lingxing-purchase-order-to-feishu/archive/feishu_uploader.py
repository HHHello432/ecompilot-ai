import urllib.request
import urllib.parse
import json
import time
import threading
from datetime import datetime, timezone, timedelta

# ---- 配置 ----
FEISHU_HOST = "https://open.feishu.cn"
TABLE1_ID = "tblU0rUgG5MkU94G"   # 表1 采购单（订单级）
TABLE2_ID = "tbl4QqMWyPMbMPGb"   # 表2 采购单SKU明细（SKU级，含关联字段）
BATCH_SIZE = 500                 # 飞书批量接口单次上限
BEIJING = timezone(timedelta(hours=8))

# 表1 字段顺序（写入时固定字段集合，避免混入多余键）
TABLE1_FIELDS = ["采购单号", "1688订单号", "是否完成下单",
                 "下单日期", "发货日期", "签收日期", "入库日期", "加工日期"]
# 表1 中属于「日期」类型的字段，需要转成 13 位毫秒时间戳
DATE_FIELDS_T1 = {"下单日期", "发货日期", "签收日期", "入库日期", "加工日期"}


def log(level, message):
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    thread_name = threading.current_thread().name
    print(f"{timestamp} - {thread_name} - {level} - {message}")


def _req(method, url, token, body=None, retries=3):
    """统一 HTTP 调用，带超时与指数退避重试。token 为 None 时不带鉴权头。"""
    last = None
    for attempt in range(retries):
        try:
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last = e
            log("WARNING", f"{method} {url} 第{attempt+1}次失败: {e}")
            time.sleep(1 * (2 ** attempt))
    raise RuntimeError(f"{method} {url} 重试{retries}次仍失败: {last}")


def get_tenant_access_token(app_id, app_secret):
    """获取 tenant_access_token（内部应用鉴权）。"""
    url = f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal"
    data = _req("POST", url, None, {"app_id": app_id, "app_secret": app_secret})
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
    return data["tenant_access_token"]


def list_all_records(app_token, table_id, token):
    """分页读取整张表，返回 [{"record_id":..., "fields":{...}}, ...]。"""
    out = []
    page_token = None
    while True:
        url = (f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}"
               f"/tables/{table_id}/records?page_size=100")
        if page_token:
            url += f"&page_token={urllib.parse.quote(page_token)}"
        data = _req("GET", url, token)
        if data.get("code") != 0:
            raise RuntimeError(f"读取表 {table_id} 失败: {data}")
        d = data.get("data", {})
        out.extend(d.get("items", []))
        if d.get("has_more") and d.get("page_token"):
            page_token = d["page_token"]
        else:
            break
    return out


def batch_create(app_token, table_id, token, records):
    """批量新增。records: [{"fields": {...}}, ...]，返回 data.records（含新建 record_id）。"""
    url = (f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}"
           f"/tables/{table_id}/records/batch_create")
    data = _req("POST", url, token, {"records": records})
    if data.get("code") != 0:
        raise RuntimeError(f"batch_create 失败: {data}")
    return data.get("data", {})


def batch_update(app_token, table_id, token, records):
    """批量更新。records: [{"record_id":..., "fields": {...}}, ...]。"""
    url = (f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}"
           f"/tables/{table_id}/records/batch_update")
    data = _req("POST", url, token, {"records": records})
    if data.get("code") != 0:
        raise RuntimeError(f"batch_update 失败: {data}")
    return data.get("data", {})


def _to_date_ms(value):
    """把日期值转成飞书日期字段接受的 13 位毫秒时间戳（整数）。无法解析则返回 None。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v) if v > 1e12 else int(v * 1000)   # 已是时间戳：>1e12 视为毫秒
    s = str(value).strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=BEIJING)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    try:                                       # 纯数字字符串
        v = float(s)
        return int(v) if v > 1e12 else int(v * 1000)
    except ValueError:
        log("WARNING", f"无法解析日期值: {value}")
        return None


def _chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def upload_to_feishu(data, app_id, app_secret, app_token,
                     table1_id=TABLE1_ID, table2_id=TABLE2_ID):
    """
    把 A3 产出的 {orders, items} 上传到飞书双表（方案A：先全量读字典，再分流 upsert）。

    参数:
        data        : A3 的返回值，形如 {"orders":[...], "items":[...]}
        app_id      : 飞书应用 ID（入参，不写死）
        app_secret  : 飞书应用密钥（入参，不写死）
        app_token   : 多维表格 app_token（即 base URL 中 /base/ 后的串，入参）
        table1_id   : 表1 数据表 ID，默认用已建好的 tblU0rUgG5MkU94G
        table2_id   : 表2 数据表 ID，默认用已建好的 tbl4QqMWyPMbMPGb

    返回:
        {"table1": {...}, "table2": {...}} 统计信息
    """
    token = get_tenant_access_token(app_id, app_secret)
    orders = data.get("orders", []) or []
    items = data.get("items", []) or []

    # ============ 表1 采购单（订单级，先写，因为表2 关联要它的 record_id） ============
    existing1 = {}  # 采购单号 -> record_id
    for rec in list_all_records(app_token, table1_id, token):
        key = rec.get("fields", {}).get("采购单号")
        if key:
            existing1[key] = rec["record_id"]

    to_create1, to_update1 = [], []
    for o in orders:
        fields = {k: o.get(k) for k in TABLE1_FIELDS}
        for dk in DATE_FIELDS_T1:                      # 日期字段转毫秒时间戳
            fields[dk] = _to_date_ms(fields[dk])
        key = o.get("采购单号")
        if key in existing1:
            to_update1.append({"record_id": existing1[key], "fields": fields})
        else:
            to_create1.append({"fields": fields})

    for chunk in _chunk(to_create1, BATCH_SIZE):
        resp = batch_create(app_token, table1_id, token, chunk)
        for r in resp.get("records", []):             # 把新建的 record_id 也记回字典
            k = r.get("fields", {}).get("采购单号")
            if k:
                existing1[k] = r["record_id"]
    for chunk in _chunk(to_update1, BATCH_SIZE):
        batch_update(app_token, table1_id, token, chunk)

    # ============ 表2 采购单SKU明细（SKU级，关联回表1） ============
    existing2 = {}  # 采购单号#SKU -> record_id
    for rec in list_all_records(app_token, table2_id, token):
        key = rec.get("fields", {}).get("采购单号_SKU")
        if key:
            existing2[key] = rec["record_id"]

    to_create2, to_update2 = [], []
    for it in items:
        po = it.get("采购单号")
        sku = it.get("SKU")
        composite = f"{po}#{sku}"
        rid = existing1.get(po)                        # 表1 的 record_id
        if rid is None:
            log("WARNING", f"表2 项 {composite} 在表1 找不到对应采购单记录，跳过")
            continue
        fields = {
            "采购单号_SKU": composite,
            "SKU": sku,
            "采购单号": [rid],                         # 单向关联字段：填被关联记录的 record_id 数组
        }
        if composite in existing2:
            to_update2.append({"record_id": existing2[composite], "fields": fields})
        else:
            to_create2.append({"fields": fields})

    for chunk in _chunk(to_create2, BATCH_SIZE):
        batch_create(app_token, table2_id, token, chunk)
    for chunk in _chunk(to_update2, BATCH_SIZE):
        batch_update(app_token, table2_id, token, chunk)

    summary = {
        "table1": {"created": len(to_create1), "updated": len(to_update1)},
        "table2": {"created": len(to_create2), "updated": len(to_update2)},
    }
    log("INFO", f"上传完成: 表1 新增{summary['table1']['created']}/更新{summary['table1']['updated']}"
                f"，表2 新增{summary['table2']['created']}/更新{summary['table2']['updated']}")
    return summary


def run_pipeline(input_list, cookies, app_id, app_secret, app_token,
                 table1_id=TABLE1_ID, table2_id=TABLE2_ID):
    """
    一键流水线：领星抓取(A3) -> 飞书双表上传。
    cookies 用于领星抓取；app_id/app_secret/app_token 用于飞书写入（均为入参）。
    """
    from lingxing_collector import A3
    data = A3(input_list, cookies)
    return upload_to_feishu(data, app_id, app_secret, app_token, table1_id, table2_id)
