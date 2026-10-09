"""采购时效跟踪项目 - 单文件版

完整流程：
    A2 获取采购单 -> A3 并发解析 -> A4 更新飞书多维表格
    -> B1 读取未入库单 -> A3 并发解析 -> A4 更新多维表格

输入参数（全部作为入参，不落盘）：
    - 领星 cookies（Name/Value 列表）
    - 飞书 app_id / app_secret
    - 多维表格 URL（或 app_token）
    - 表1 / 表2 的 table_id 与名称
    - 未入库单视图名
    - A2 查询日期范围 start_date / end_date（格式 "YYYY-MM-DD"）

配置来源：
    全部配置项集中在 config.json（由 config.example.json 复制而来，已被 .gitignore 忽略）。
    程序入口在 __main__ 中读取一次 config.json，作为入参传给主逻辑方法
    run_and_save_result(config)，全程只调用一次。

使用示例（库调用，显式传参）：
    from purchase_tracking import run_purchase_tracking

    result = run_purchase_tracking(
        cookies=cookies,
        app_id="cli_xxxxxxxxxxxxxxxx",
        app_secret="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        feishu_url="https://xxx.feishu.cn/base/xxxxxxxxxxxxxxxxxxxxxx",
        table1_id="tblXXXXXXXXXXXXXXXX",
        table2_id="tblXXXXXXXXXXXXXXXX",
        start_date="2026-08-01",
        end_date="2026-08-28",
    )

使用示例（配置驱动）：
    from purchase_tracking import load_config, run_and_save_result

    run_and_save_result(load_config())

注意事项：
    1. 表2「采购单SKU明细」含 plan_sn（文本）与加工日期（日期）两个 SKU 级字段：
       plan_sn 是回跑时查领星加工单的钥匙，加工日期是该 SKU 对应加工单的完成时间。
       run_purchase_tracking 会自动确保两字段存在（幂等，已存在则跳过）。
       加工日期按 plan_sn 精确匹配才写入（PP→加工单一对一），同批加工的 SKU 行
       加工日期相同属正常；表1 不再存加工日期，需要时从表2 按单聚合。
    2. B1 从飞书还原的 status_text 使用默认值「待到货」，非原始爬虫值。
    3. A4 采用「部分更新」：值为 None 的字段不写入，避免回跑时把已有数据抹掉。
"""

import json
import os
import re
import time
import threading
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# 配置文件名（相对本脚本所在目录解析，任意 cwd 下均可运行）
CONFIG_FILE = "config.json"
CONFIG_EXAMPLE_FILE = "config.example.json"
DEFAULT_OUTPUT_FILE = "purchase_tracking_result.json"   # config.OUTPUT_FILE 缺省时的落盘文件名
DEFAULT_DEBUG_FILE = "concurrent_debug.jsonl"          # DEBUG_CONCURRENT 开启时的请求记录文件

# 并发调试状态：布尔开关 + 线程安全锁 + 文件路径
_CONCURRENT_DEBUG = {
    "enabled": False,
    "path": DEFAULT_DEBUG_FILE,
    "lock": threading.Lock(),
}


def set_concurrent_debug(enabled, path=None):
    """开启/关闭并发调试记录。

    开启后，所有多线程请求（send_lingxing_request 的每次调用，含加工/物流/采购）
    的入参与返回结果都会以 JSONL 格式追加写入本地文件，供排查多线程串号、返回
    异常、字段为空等问题。开启瞬间会清空旧文件，避免多次运行记录混杂。
    关闭后不再写入（已生成的文件保留）。
    """
    _CONCURRENT_DEBUG["enabled"] = bool(enabled)
    if path:
        _CONCURRENT_DEBUG["path"] = path
    if enabled:
        try:
            with _CONCURRENT_DEBUG["lock"]:
                open(_CONCURRENT_DEBUG["path"], "w", encoding="utf-8").close()
        except OSError as e:
            log("WARNING", f"清空并发调试文件失败: {e}")


def _record_concurrent_request(request_desc, url, post_dict, resp, error):
    """并发调试：把一次请求的关键信息追加写入 JSONL（仅开启时执行，带锁保证线程安全）。

    只记录 url / post / 返回结果 / 异常，不记录 headers（避免 auth-token 等凭据外泄）。
    """
    if not _CONCURRENT_DEBUG["enabled"]:
        return
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "thread": threading.current_thread().name,
        "desc": request_desc,
        "url": url,
        "post": post_dict,
        "resp": resp,
        "error": error,
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _CONCURRENT_DEBUG["lock"]:
        with open(_CONCURRENT_DEBUG["path"], "a", encoding="utf-8") as f:
            f.write(line + "\n")

# =============================================================================
# 全局配置
# =============================================================================
FEISHU_HOST = "https://open.feishu.cn"
LINGXING_HOST = "https://maique.lingxing.com"

# 默认表/视图（可通过入参覆盖）
TABLE1_ID_DEFAULT = "tblU0rUgG5MkU94G"      # 采购单（订单级）
TABLE2_ID_DEFAULT = "tbl4QqMWyPMbMPGb"      # 采购单SKU明细（SKU级）
VIEW_NAME_DEFAULT = "未入库单"
VIEW_ID_DEFAULT = "vewS7mTeVc"                # 未入库单视图 ID（可通过入参覆盖）

REQUEST_TIMEOUT = 30          # HTTP 超时（秒）
MAX_RETRIES = 3               # 失败重试次数
RETRY_BACKOFF = 1.0           # 重试退避基数（秒），按 2^attempt 指数退避
ORDER_WORKERS = 10            # A3 订单并发数
HTTP_WORKERS = 20            # A3 子请求并发数（与订单池独立，避免死锁）
FEISHU_BATCH_SIZE = 500       # 飞书批量写入单次上限
FEISHU_PAGE_SIZE = 500        # 飞书读取分页大小
CLEANUP_THRESHOLD = 15000     # C5 清理触发阈值：表1/表2 任一记录数超过该值才执行删除
BEIJING = timezone(timedelta(hours=8))

# 表1 字段（加工日期已移入表2 SKU 级，表1 需要时从表2 聚合）
TABLE1_FIELDS = ["采购单号", "1688订单号", "是否完成下单",
                 "下单日期", "发货日期", "签收日期", "入库日期", "备注"]
TABLE1_DATE_FIELDS = {"下单日期", "发货日期", "签收日期", "入库日期"}


# =============================================================================
# 日志
# =============================================================================
def log(level, message):
    """统一日志输出：时间 - 线程名 - 级别 - 消息。"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    thread_name = threading.current_thread().name
    print(f"{timestamp} - {thread_name} - {level} - {message}")


def _as_dict(value):
    """把接口返回的 object 字段规整为 dict。

    dict.get(key, default) 的默认值只在「键不存在」时生效；接口显式返回
    null（如飞书 records 接口某页 items 为 null）时会原样拿到 None，
    后续 .get() 就会抛 AttributeError。这里统一兜底。
    """
    return value if isinstance(value, dict) else {}


def _as_list(value):
    """把接口返回的 array 字段规整为 list，理由同 _as_dict。"""
    return value if isinstance(value, list) else []


# =============================================================================
# A2：获取领星采购单列表
# =============================================================================
def build_lingxing_headers(cookies):
    """根据 cookies 一次性构造领星请求头模板（不含 X-AK-Request-Id）。"""
    cookie_pairs = []
    auth_token_encoded = None
    company_id = env_key = uid = zid = None

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
    cookie_header = "; ".join(cookie_pairs)

    return {
        "AK-Client-Type": "web",
        "AK-Origin": "https://maique.lingxing.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh-TW;q=0.9,zh;q=0.8,en-US;q=0.7,en;q=0.6",
        "Connection": "keep-alive",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://maique.lingxing.com",
        "Referer": "https://maique.lingxing.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Storage-Access": "active",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "X-AK-Company-Id": company_id,
        "X-AK-ENV-KEY": env_key,
        "X-AK-Language": "zh",
        "X-AK-PLATFORM": "1",
        "X-AK-Request-Source": "erp",
        "X-AK-Uid": uid,
        "X-AK-Version": "3.8.4.3.0.009",
        "X-AK-Zid": zid,
        "auth-token": auth_token,
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Cookie": cookie_header,
    }


def http_request(cookie_list, start_date, end_date, offset, search_options=None, time_field="create_time"):
    """A2 单页请求：获取领星采购单列表。

    参数：
        cookie_list    : 领星 cookies（Name/Value 列表）
        start_date     : 开始日期，如 "2026-07-18"
        end_date       : 结束日期，如 "2026-07-18"
        offset         : 分页偏移量
        search_options : 领星接口的 search_options 参数，默认空列表
        time_field     : 按哪个时间字段过滤，默认 create_time（可配置为 order_time）
    """
    headers = build_lingxing_headers(cookie_list)
    headers["X-AK-Request-Id"] = str(uuid.uuid4())

    post_dict = {
        "offset": offset,
        "length": 200,
        "expect_arrive_time_status": "",
        "sort_field": time_field,
        "sort_type": "desc",
        "status_shipped": [],
        "status": "",
        "pay_status": [],
        "search_field_time": time_field,
        "start_date": start_date,
        "end_date": end_date,
        "search_field": "order_sn",
        "search_value": "",
        "wid": [],
        "sid": [],
        "gtag_ids": "",
        "permission_uid_list": [],
        "senior_search_list": [],
        "is_urgent": "",
        "change_order_status": "",
        "is_bad": "",
        "is_tax": "",
        "is_logistics": "",
        "is_associate_return": 0,
        "is_associate_exchange": 0,
        "supplier_ids": [],
        "bids": [],
        "cids": [],
        "is_transparency": "",
        "record_tag_ids": [],
        "search_options": search_options if search_options is not None else [],
        "max_item_number": 5,
        "req_time_sequence": "/api/purchase/orderListsV2"
    }

    url = f"{LINGXING_HOST}/api/purchase/orderListsV2"
    data = json.dumps(post_dict).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
        response_body = response.read().decode("utf-8")
        return json.loads(response_body)


def a2_fetch_orders(cookies, start_date, end_date, search_options=None, page_size=200, time_field="create_time"):
    """A2 分页获取全部采购单，返回 list[order_dict]。

    分页逻辑与八爪鱼流程图保持一致：offset 从 0 开始，每次 +200，
    当 offset <= total 时继续（同时以返回 list 为空作为兜底退出条件）。
    """
    all_orders = []
    offset = 0
    total = None

    while total is None or offset <= total:
        resp = http_request(cookies, start_date, end_date, offset, search_options, time_field=time_field)
        data = _as_dict((resp or {}).get("data"))
        total = data.get("total", total or 0)
        orders = _as_list(data.get("list"))

        if not orders:
            break

        all_orders.extend(orders)
        offset += page_size
        log("DEBUG", f"A2 分页进度: 已获取 {len(all_orders)}/{total} 条，当前 offset={offset}")

    log("INFO", f"A2 获取到今日订单数据: 共 {len(all_orders)} 条")
    return all_orders


# =============================================================================
# A3：并发解析采购单
# =============================================================================
def send_lingxing_request(url, post_dict, base_headers, request_desc="request"):
    """发送领星 POST 请求，带超时与指数退避重试，失败返回 None。

    并发调试：当全局开关 DEBUG_CONCURRENT 开启时，每次调用（无论成功失败）
    都会把入参与返回结果追加写入本地 JSONL 文件，供排查多线程请求问题。
    """
    result, error = None, None
    for attempt in range(MAX_RETRIES):
        try:
            headers = base_headers.copy()
            headers["X-AK-Request-Id"] = str(uuid.uuid4())
            data = json.dumps(post_dict).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                if response.status != 200:
                    raise Exception(f"HTTP {response.status}")
                body = response.read().decode("utf-8")
                log("DEBUG", f"{request_desc} 第{attempt+1}次请求成功")
                result = json.loads(body)
                break
        except Exception as e:
            error = str(e)
            log("WARNING", f"{request_desc} 第{attempt+1}次请求失败: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    if result is None:
        error = error or "三次重试全部失败，放弃"
        log("ERROR", f"{request_desc} {error}")
    _record_concurrent_request(request_desc, url, post_dict, result, error if result is None else None)
    return result


def _extract_logistics(resp2):
    """从物流接口返回提取发货/签收时间。防御性排序，避免接口返回顺序不确定导致对调。"""
    start_time = end_time = None
    if not resp2:
        return start_time, end_time
    log_list = _as_list(_as_dict(resp2.get("data")).get("list"))
    if not log_list:
        return start_time, end_time
    trace_info = log_list[0].get("trace_info")
    if not trace_info:
        return start_time, end_time
    sorted_trace = sorted(trace_info, key=lambda t: t.get("accept_time") or "")
    start_time = sorted_trace[0].get("accept_time")
    if log_list[0].get("status_text") == "已签收":
        end_time = sorted_trace[-1].get("accept_time")
    return start_time, end_time


def _extract_logistics_order_no(resp2):
    """从物流接口返回提取物流单号 data.list[].logistics_order_no。

    取第一条非空值；领星在「不需要物流」的订单上会用该字段占位标记，
    调用方据此判断是否需要在备注中打标。取不到返回 None。
    """
    if not resp2:
        return None
    for item in _as_list(_as_dict(resp2.get("data")).get("list")):
        if not isinstance(item, dict):
            continue
        value = item.get("logistics_order_no")
        if value:
            return value
    return None


def _extract_finish_time(resp, plan_sn):
    """从加工单接口返回中提取指定 plan_sn 的完成时间。

    必须精确命中该 plan_sn 才返回；plan_sn 为空、未命中或无完成时间返回 None。
    防呆：plan_sn 缺失时空查询会命中「全局最新加工单」，导致大量订单拿到
    同一个加工时间（记录被"串"），因此宁可缺失也不允许取错。
    """
    if not plan_sn or not isinstance(resp, dict):
        return None
    lst = _as_list(resp.get("list"))
    if not lst:
        return None
    if any("plan_sn" in r for r in lst):
        match = next((r for r in lst if str(r.get("plan_sn", "")) == str(plan_sn)), None)
        if match is None:
            return None
    else:
        match = lst[0]  # 响应不含 plan_sn 字段：视为服务端已按 search_value 过滤
    fin = match.get("finish_time")
    return fin if fin != "-" else None


def process_order(order_dict, base_headers, http_executor):
    """处理单个订单，并发获取加工/物流/采购信息。

    返回 {"order": 订单级记录或 None, "items": [SKU级记录, ...]}。
    HTTP 子请求提交到外部传入的 http_executor，订单池与 HTTP 池相互独立，避免嵌套死锁。
    """
    order_sn = order_dict.get("order_sn", "未知")
    log("INFO", f"开始处理订单 [{order_sn}]")

    status_text = order_dict.get("status_text", None)
    if status_text is None:
        log("WARNING", f"订单 [{order_sn}] status_text 为空，跳过")
        return {"order": None, "items": []}
    if status_text == "已作废":
        log("DEBUG", f"订单 [{order_sn}] 已作废，跳过")
        return {"order": None, "items": []}

    sn_1688 = order_dict.get("alibaba_order_sn", None)
    sn = order_dict.get("order_sn", None)
    item_list = order_dict.get("item_list", None)
    if not item_list:
        log("WARNING", f"订单 [{order_sn}] item_list 为空，跳过")
        return {"order": None, "items": []}
    # 收集本单所有非空 plan_sn（去重保序）：plan_sn 是 SKU 级，PP→加工单一对一，
    # 每个 SKU 的加工完成时间各自查询，不再用「第一个 plan_sn」近似整单
    plan_sns = []
    _seen = set()
    for it in item_list:
        p = it.get("plan_sn")
        if p and p not in _seen:
            _seen.add(p)
            plan_sns.append(p)

    # 注意：字段名是「是否完成下单」，但这里判断的是履约状态
    is_order_placed = (status_text == "待到货" or status_text == "已完成")
    order_time = order_dict.get("order_time", None)

    url1 = f"{LINGXING_HOST}/api/storage_process/lists"
    post1_tpl = {
        "sort_field": "create_time", "sort_type": "desc", "wids": "",
        "seller_id": [], "status": "", "gtag_ids": "", "type": "",
        "search_field_time": "create_time", "search_field": "plan_sn",
        "search_value": None, "start_date": "", "end_date": "",
        "offset": 0, "length": 20,
        "req_time_sequence": "/api/storage_process/lists"
    }
    url2 = f"{LINGXING_HOST}/api/purchase_logistics/getLogistics"
    post2 = {"order_sn": sn, "req_time_sequence": "/api/purchase_logistics/getLogistics"}
    url3 = f"{LINGXING_HOST}/api/purchase/orderInfo"
    post3 = {"order_sn": sn, "req_time_sequence": "/api/purchase/orderInfo"}

    log("DEBUG", f"订单 [{order_sn}] 开始并发请求加工({len(plan_sns)}个plan_sn)、物流、采购信息")

    plan_futures = {
        p: http_executor.submit(send_lingxing_request, url1, dict(post1_tpl, search_value=p),
                                base_headers, f"订单[{order_sn}]加工单[{p}]")
        for p in plan_sns
    }
    f2 = http_executor.submit(send_lingxing_request, url2, post2, base_headers, f"订单[{order_sn}]物流请求")
    f3 = http_executor.submit(send_lingxing_request, url3, post3, base_headers, f"订单[{order_sn}]采购单请求")

    # 加工完成时间：逐 plan_sn 精确命中才写入（见 _extract_finish_time 防呆说明）
    plan_finish = {}
    for p, fut in plan_futures.items():
        try:
            fin = _extract_finish_time(fut.result(), p)
            if fin:
                plan_finish[p] = fin
                log("DEBUG", f"订单 [{order_sn}] plan_sn={p} 加工完成时间: {fin}")
            else:
                log("DEBUG", f"订单 [{order_sn}] plan_sn={p} 无加工记录，不写入加工日期")
        except Exception as e:
            log("ERROR", f"订单 [{order_sn}] 提取加工时间异常(plan_sn={p}): {e}")
    resp2, resp3 = f2.result(), f3.result()

    # 物流发货/签收时间
    start_time, end_time = _extract_logistics(resp2)
    log("DEBUG", f"订单 [{order_sn}] 物流: 发货={start_time}, 签收={end_time}")

    # 物流单号为「不需要物流」时，在表1 备注字段打标
    remark = None
    if _extract_logistics_order_no(resp2) == "不需要物流":
        remark = "不需要物流"
        log("DEBUG", f"订单 [{order_sn}] 物流单号标记「不需要物流」，写入表1 备注")

    # 入库时间
    in_time = None
    if resp3:
        try:
            receive = _as_list(_as_dict(resp3.get("data")).get("receive"))
            if receive:
                in_time = receive[0]
                log("DEBUG", f"订单 [{order_sn}] 入库时间: {in_time}")
        except Exception as e:
            log("ERROR", f"订单 [{order_sn}] 提取入库信息异常: {e}")

    order_record = {
        "采购单号": sn,
        "1688订单号": sn_1688,
        "是否完成下单": is_order_placed,
        "下单日期": order_time,
        "发货日期": start_time,
        "签收日期": end_time,
        "入库日期": in_time,
        "备注": remark,
    }

    # SKU 级：带上 plan_sn 与各自加工日期（回跑时按 plan_sn 还原查询；空值不写入）
    item_records = [
        {"采购单号": sn, "SKU": item.get("sku"), "plan_sn": item.get("plan_sn"),
         "加工日期": plan_finish.get(item.get("plan_sn"))}
        for item in item_list
    ]

    log("INFO", f"订单 [{order_sn}] 处理完成，生成 1 条订单记录 + {len(item_records)} 条SKU记录")
    return {"order": order_record, "items": item_records}


def A3(input_list, cookies):
    """A3 主入口：并发处理所有订单。

    返回结构化结果：{"orders": [...], "items": [...]}
    单个订单异常不再拖垮整体（失败的订单会被记录到日志）。
    """
    if input_list is None:
        log("WARNING", "输入列表为 None，返回 None")
        return None

    log("INFO", f"开始处理 {len(input_list)} 个订单")
    base_headers = build_lingxing_headers(cookies)

    orders, items, failed = [], [], []

    with ThreadPoolExecutor(max_workers=ORDER_WORKERS) as order_executor, \
         ThreadPoolExecutor(max_workers=HTTP_WORKERS) as http_executor:
        future_to_sn = {
            order_executor.submit(process_order, od, base_headers, http_executor): od.get("order_sn", "未知")
            for od in input_list
        }

        completed = 0
        total = len(input_list)
        for future in as_completed(future_to_sn):
            sn = future_to_sn[future]
            try:
                result = future.result()
                if result["order"] is not None:
                    orders.append(result["order"])
                items.extend(result["items"])
            except Exception as e:
                failed.append(sn)
                log("ERROR", f"订单 [{sn}] 处理出现未捕获异常: {e}")
            completed += 1
            log("INFO", f"总体进度: {completed}/{total} 订单处理完毕")

    log("INFO", f"全部订单处理完成：订单记录 {len(orders)} 条，SKU记录 {len(items)} 条"
               + (f"，失败 {len(failed)} 条: {failed}" if failed else ""))
    return {"orders": orders, "items": items}


# =============================================================================
# 飞书通用请求
# =============================================================================
def _feishu_req(method, url, token=None, body=None, params=None, retries=MAX_RETRIES):
    """飞书 API 统一调用，带超时与指数退避重试。token 为 None 时不带鉴权头。"""
    if params:                      # GET 查询串（page_size / page_token / view_id 等）
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
        except Exception as e:
            last = e
            log("WARNING", f"{method} {url} 第{attempt+1}次失败: {e}")
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise RuntimeError(f"{method} {url} 重试{retries}次仍失败: {last}")


def get_tenant_access_token(app_id, app_secret):
    """获取 tenant_access_token（内部应用鉴权）。"""
    url = f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal"
    data = _feishu_req("POST", url, token=None, body={"app_id": app_id, "app_secret": app_secret})
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
    return data["tenant_access_token"]


def list_all_records(app_token, table_id, token, view_id=None):
    """分页读取某表全部记录，返回 [{"record_id":..., "fields":{...}}, ...]。"""
    out = []
    page_token = None
    while True:
        params = {"page_size": FEISHU_PAGE_SIZE}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token
        url = f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        data = _feishu_req("GET", url, token, params=params)
        if data.get("code") != 0:
            raise RuntimeError(f"读取表 {table_id} 失败: {data}")
        d = _as_dict(data.get("data"))
        out.extend(_as_list(d.get("items")))
        if d.get("has_more") and d.get("page_token"):
            page_token = d["page_token"]
        else:
            break
    return out


def batch_create_records(app_token, table_id, token, records):
    """批量新增记录。records: [{"fields": {...}}, ...]，返回 data 部分。"""
    url = f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
    data = _feishu_req("POST", url, token, body={"records": records})
    if data.get("code") != 0:
        raise RuntimeError(f"batch_create 失败: {data}")
    return _as_dict(data.get("data"))


def batch_update_records(app_token, table_id, token, records):
    """批量更新记录。records: [{"record_id":..., "fields": {...}}, ...]。"""
    url = f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
    data = _feishu_req("POST", url, token, body={"records": records})
    if data.get("code") != 0:
        raise RuntimeError(f"batch_update 失败: {data}")
    return _as_dict(data.get("data"))


def batch_delete_records(app_token, table_id, token, record_ids):
    """批量删除记录。record_ids: [str, ...]，每批 500 条上限由调用方分块。"""
    url = f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete"
    data = _feishu_req("DELETE", url, token, body={"records": record_ids})
    if data.get("code") != 0:
        raise RuntimeError(f"batch_delete 失败: {data}")
    return _as_dict(data.get("data"))


# =============================================================================
# 飞书字段管理（用于一次性补齐 plan_sn 字段）
# =============================================================================
def list_fields(app_token, table_id, token):
    """列出表的所有字段，返回 [{"field_id":..., "field_name":..., "type":...}, ...]。"""
    url = f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    out = []
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        data = _feishu_req("GET", url, token, params=params)
        if data.get("code") != 0:
            raise RuntimeError(f"读取字段列表失败: {data}")
        d = _as_dict(data.get("data"))
        out.extend(_as_list(d.get("items")))
        if d.get("has_more") and d.get("page_token"):
            page_token = d["page_token"]
        else:
            break
    return out


def create_field(app_token, table_id, token, field_name, field_type=1):
    """新增字段。field_type=1 为文本类型。"""
    url = f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    data = _feishu_req("POST", url, token, body={"field_name": field_name, "type": field_type})
    if data.get("code") != 0:
        raise RuntimeError(f"新增字段 {field_name} 失败: {data}")
    return _as_dict(data.get("data"))


# 表2 附加字段：回跑钥匙 plan_sn（文本，type=1）+ SKU 级加工日期（日期，type=5）
TABLE2_EXTRA_FIELDS = {"plan_sn": 1, "加工日期": 5}


def ensure_table2_fields(app_id, app_secret, app_token, table_id):
    """确保表2 存在 plan_sn / 加工日期 字段（幂等：缺失才创建）。

    加工日期放表2 SKU 级后，与 plan_sn 一起随回跑刷新；plan_sn 为文本、
    加工日期为日期字段（写入毫秒时间戳），与飞书表实际类型保持一致。
    """
    token = get_tenant_access_token(app_id, app_secret)
    existing = {f.get("field_name") for f in list_fields(app_token, table_id, token)}
    created = False
    for name, ftype in TABLE2_EXTRA_FIELDS.items():
        if name in existing:
            log("INFO", f"表2 字段「{name}」已存在，跳过创建")
            continue
        create_field(app_token, table_id, token, name, ftype)
        log("INFO", f"已在表 {table_id} 创建字段「{name}」(type={ftype})")
        created = True
    return created


def _to_date_ms(value):
    """把日期值转成飞书日期字段接受的 13 位毫秒时间戳（整数），无法解析返回 None。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v) if v > 1e12 else int(v * 1000)
    s = str(value).strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=BEIJING)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    try:
        v = float(s)
        return int(v) if v > 1e12 else int(v * 1000)
    except ValueError:
        log("WARNING", f"无法解析日期值: {value}")
        return None


def _chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# =============================================================================
# A4：上传/更新飞书多维表格
# =============================================================================
def a4_upload_to_feishu(data, app_id, app_secret, app_token,
                        table1_id=TABLE1_ID_DEFAULT, table2_id=TABLE2_ID_DEFAULT):
    """把 A3 产出的 {orders, items} 上传到飞书双表（方案A：先全量读字典，再分流 upsert）。

    参数：
        data        : A3 返回值，形如 {"orders":[...], "items":[...]}
        app_id      : 飞书应用 ID（入参，不写死）
        app_secret  : 飞书应用密钥（入参，不写死）
        app_token   : 多维表格 app_token
        table1_id   : 表1 数据表 ID
        table2_id   : 表2 数据表 ID

    返回：
        {"table1": {"created": n, "updated": n}, "table2": {...}} 统计信息
    """
    token = get_tenant_access_token(app_id, app_secret)
    orders = data.get("orders", []) or []
    items = data.get("items", []) or []

    # ========== 表1 采购单（订单级，先写，因为表2 关联要它的 record_id） ==========
    existing1 = {}  # 采购单号 -> record_id
    for rec in list_all_records(app_token, table1_id, token):
        key = _as_dict(rec.get("fields")).get("采购单号")
        if key:
            existing1[key] = rec["record_id"]

    to_create1, to_update1 = [], []
    for o in orders:
        key = o.get("采购单号")
        if not key:
            log("WARNING", f"订单记录缺少采购单号，跳过: {o}")
            continue

        raw = {k: o.get(k) for k in TABLE1_FIELDS}
        for dk in TABLE1_DATE_FIELDS:
            raw[dk] = _to_date_ms(raw[dk])

        # 关键：只写入非空字段，避免用 None 覆盖飞书里已有的值。
        # 回跑（B1->A3->A4）时加工日期因缺少 plan_sn 会是 None，
        # 若整行全字段覆盖，会把首轮全量跑存下来的加工日期抹掉。
        fields = {k: v for k, v in raw.items() if v is not None}

        if key in existing1:
            to_update1.append({"record_id": existing1[key], "fields": fields})
        else:
            to_create1.append({"fields": fields})

    for chunk in _chunk(to_create1, FEISHU_BATCH_SIZE):
        resp = batch_create_records(app_token, table1_id, token, chunk)
        for r in _as_list(resp.get("records")):
            k = _as_dict(r.get("fields")).get("采购单号")
            if k:
                existing1[k] = r["record_id"]
    for chunk in _chunk(to_update1, FEISHU_BATCH_SIZE):
        batch_update_records(app_token, table1_id, token, chunk)

    # ========== 表2 采购单SKU明细（SKU级，关联回表1） ==========
    existing2 = {}  # 采购单号#SKU -> record_id
    for rec in list_all_records(app_token, table2_id, token):
        key = _as_dict(rec.get("fields")).get("采购单号_SKU")
        if key:
            existing2[key] = rec["record_id"]

    to_create2, to_update2 = [], []
    for it in items:
        po = it.get("采购单号")
        sku = it.get("SKU")
        if not po or not sku:
            log("WARNING", f"表2 项缺少采购单号或 SKU，跳过: {it}")
            continue
        composite = f"{po}#{sku}"
        rid = existing1.get(po)
        if rid is None:
            log("WARNING", f"表2 项 {composite} 在表1 找不到对应采购单记录，跳过")
            continue
        fields = {
            "采购单号_SKU": composite,
            "SKU": sku,
            "采购单号": [rid],  # 单向关联字段：填被关联记录的 record_id 数组
        }
        plan_sn = it.get("plan_sn")
        if plan_sn:            # 同样遵循「空值不覆盖」原则
            fields["plan_sn"] = plan_sn
        fin_ms = _to_date_ms(it.get("加工日期"))
        if fin_ms is not None:  # SKU 级加工日期(日期字段需毫秒时间戳)：空值/无法解析不覆盖，回跑时才可被正确刷新
            fields["加工日期"] = fin_ms
        if composite in existing2:
            to_update2.append({"record_id": existing2[composite], "fields": fields})
        else:
            to_create2.append({"fields": fields})

    for chunk in _chunk(to_create2, FEISHU_BATCH_SIZE):
        batch_create_records(app_token, table2_id, token, chunk)
    for chunk in _chunk(to_update2, FEISHU_BATCH_SIZE):
        batch_update_records(app_token, table2_id, token, chunk)

    summary = {
        "table1": {"created": len(to_create1), "updated": len(to_update1)},
        "table2": {"created": len(to_create2), "updated": len(to_update2)},
    }
    log("INFO", f"A4 上传完成: 表1 新增{summary['table1']['created']}/更新{summary['table1']['updated']}"
                f"，表2 新增{summary['table2']['created']}/更新{summary['table2']['updated']}")
    return summary


# =============================================================================
# C5：按需清理已结束跟踪的记录（删除）
# =============================================================================
def cleanup_finished_records(app_id, app_secret, app_token,
                             table1_id=TABLE1_ID_DEFAULT,
                             table2_id=TABLE2_ID_DEFAULT,
                             threshold=CLEANUP_THRESHOLD):
    """按需删除已结束跟踪的记录，保持双表只留仍在流程中的订单。

    触发条件（破坏性操作，由 config 的 CLEANUP_FINISHED 开关控制）：
        表1 或表2 任一记录数超过 threshold（默认 15000）条才执行，否则跳过。

    删除规则：
        - 表1「采购单」：删除「加工日期」不为空 或「备注」不为空的记录
          （加工日期非空 = 已加工完成；备注非空 = 如标记「不需要物流」）；
        - 表2「采购单SKU明细」：删除引用（关联）到被删表1 记录的行，
          避免残留指向已删记录的孤儿关联。

    返回 {"triggered": bool, "table1_deleted": n, "table2_deleted": n}。
    """
    token = get_tenant_access_token(app_id, app_secret)

    recs1 = list_all_records(app_token, table1_id, token)
    recs2 = list_all_records(app_token, table2_id, token)
    if len(recs1) <= threshold and len(recs2) <= threshold:
        log("INFO", f"C5 跳过清理：表1 {len(recs1)} 条 / 表2 {len(recs2)} 条，均未超过阈值 {threshold}")
        return {"triggered": False, "table1_deleted": 0, "table2_deleted": 0}

    # 1) 表1：加工日期非空 或 备注非空 -> 待删
    to_delete1, deleted_ids1, orphan_orders = [], set(), set()
    for rec in recs1:
        fields = _as_dict(rec.get("fields"))
        if fields.get("加工日期") or fields.get("备注"):
            to_delete1.append(rec["record_id"])
            deleted_ids1.add(rec["record_id"])
            po = fields.get("采购单号")
            if po:
                orphan_orders.add(po)

    # 2) 表2：引用到被删表1 记录的行 -> 待删
    to_delete2 = []
    for rec in recs2:
        fields = _as_dict(rec.get("fields"))
        # 关联字段「采购单号」读回可能是 [record_id, ...] 或单个 record_id
        refs = fields.get("采购单号")
        ref_ids = _as_list(refs) if isinstance(refs, list) else ([refs] if refs else [])
        if any(rid in deleted_ids1 for rid in ref_ids):
            to_delete2.append(rec["record_id"])
            continue
        # 兜底：按组合键「采购单号#SKU」的订单号前缀匹配
        combo = fields.get("采购单号_SKU")
        if combo and isinstance(combo, str) and combo.partition("#")[0] in orphan_orders:
            to_delete2.append(rec["record_id"])

    # 3) 先删表2（子表），再删表1（主表）
    for chunk in _chunk(to_delete2, FEISHU_BATCH_SIZE):
        batch_delete_records(app_token, table2_id, token, chunk)
    for chunk in _chunk(to_delete1, FEISHU_BATCH_SIZE):
        batch_delete_records(app_token, table1_id, token, chunk)

    log("INFO", f"C5 清理完成（表1 {len(recs1)} 条 / 表2 {len(recs2)} 条，超过阈值 {threshold} 触发）: "
                f"表1 删除 {len(to_delete1)} 条（加工日期非空或备注非空），"
                f"表2 删除 {len(to_delete2)} 条（引用被删记录）")
    return {"triggered": True, "table1_deleted": len(to_delete1), "table2_deleted": len(to_delete2)}


# =============================================================================
# B1：读取未入库单视图
# =============================================================================
def find_view_id(app_token, table_id, token, view_name):
    """按视图名在指定表中查找 view_id。"""
    url = f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/views"
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        data = _feishu_req("GET", url, token, params=params)
        if data.get("code") != 0:
            raise RuntimeError(f"读取视图列表失败: {data}")
        d = _as_dict(data.get("data"))
        for v in _as_list(d.get("items")):
            if v.get("view_name") == view_name:
                return v.get("view_id")
        if d.get("has_more") and d.get("page_token"):
            page_token = d["page_token"]
        else:
            break
    raise ValueError(f"未找到名为「{view_name}」的视图")


def _ms_to_str(value):
    """飞书日期字段读回的是 13 位毫秒时间戳（int），转成北京时间字符串。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        ms = int(value)
        dt = datetime.fromtimestamp(ms / 1000, tz=BEIJING)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value


def b1_read_unstocked_orders(app_id, app_secret, app_token,
                             table1_id=TABLE1_ID_DEFAULT,
                             table2_id=TABLE2_ID_DEFAULT,
                             view_name=VIEW_NAME_DEFAULT,
                             view_id=VIEW_ID_DEFAULT,
                             default_status_text="待到货"):
    """B1：读取「未入库单」视图，输出与 A3 输入结构一致的数据。

    说明：
        - plan_sn 已随表2 的 plan_sn 字段存储，这里会还原回去，
          因此回跑时表2 的 SKU 级「加工日期」也能按 plan_sn 重新查询刷新
          （前提是该行有 plan_sn）。
        - status_text 使用 default_status_text 兜底，默认「待到货」，非原始爬虫值。
    """
    token = get_tenant_access_token(app_id, app_secret)

    if not view_id:
        log("INFO", f"B1 按视图名「{view_name}」查找 view_id")
        view_id = find_view_id(app_token, table1_id, token, view_name)
        log("INFO", f"B1 找到视图 view_id={view_id}")
    else:
        log("INFO", f"B1 使用传入 view_id={view_id}")

    log("INFO", "B1 读取采购单表（未入库单视图）")
    order_records = list_all_records(app_token, table1_id, token, view_id=view_id)
    log("INFO", f"B1 视图内订单数: {len(order_records)}")

    log("INFO", "B1 读取采购单SKU明细表（全量）")
    sku_records = list_all_records(app_token, table2_id, token)
    log("INFO", f"B1 明细表记录数: {len(sku_records)}")

    # 由组合键拆出 采购单号 -> [{"sku":..., "plan_sn":...}]
    sku_map = {}
    for r in sku_records:
        fields = _as_dict(r.get("fields"))
        combo = fields.get("采购单号_SKU")
        if not combo or not isinstance(combo, str):
            continue
        order_sn, _, sku_tail = combo.rpartition("#")
        if not order_sn:
            continue
        sku_val = fields.get("SKU") or (sku_tail if sku_tail else None)
        if not sku_val:
            continue
        entry = {"sku": sku_val, "plan_sn": fields.get("plan_sn")}
        sku_map.setdefault(order_sn, [])
        if entry not in sku_map[order_sn]:
            sku_map[order_sn].append(entry)

    result = []
    for r in order_records:
        fields = _as_dict(r.get("fields"))
        order_sn = fields.get("采购单号")
        if not order_sn:
            continue
        # 还原 plan_sn，回跑时 A3 才能用它去查加工单（storage_process/lists）
        item_list = [
            {"sku": e["sku"], "plan_sn": e.get("plan_sn")}
            for e in sku_map.get(order_sn, [])
        ]
        order_dict = {
            "order_sn": order_sn,
            "alibaba_order_sn": fields.get("1688订单号") or "",
            "status_text": default_status_text,
            "order_time": _ms_to_str(fields.get("下单日期")),
            "item_list": item_list,
        }
        if not item_list:
            log("WARNING", f"B1 订单 [{order_sn}] 在明细表中无 SKU，A3 将跳过该订单")
        result.append(order_dict)

    log("INFO", f"B1 重建完成，共 {len(result)} 个订单")
    return result


# =============================================================================
# 主控流程
# =============================================================================
def extract_app_token(feishu_url):
    """从多维表格 URL 中提取 app_token。

    支持形如：
        https://xxx.feishu.cn/base/xxxxxxxxxxxxxxxxxxxxxx
    """
    m = re.search(r"/base/([a-zA-Z0-9]+)", feishu_url)
    if not m:
        raise ValueError(f"无法从多维表格 URL 提取 app_token: {feishu_url}")
    return m.group(1)


def run_purchase_tracking(cookies,
                          app_id, app_secret,
                          feishu_url,
                          start_date, end_date,
                          table1_id=TABLE1_ID_DEFAULT,
                          table2_id=TABLE2_ID_DEFAULT,
                          table1_name="采购单",
                          table2_name="采购单SKU明细",
                          view_name=VIEW_NAME_DEFAULT,
                          view_id=VIEW_ID_DEFAULT,
                          search_options=None,
                          default_status_text="待到货",
                          auto_migrate=True,
                          a2_time_field="create_time",
                          cleanup_finished=True,
                          cleanup_threshold=CLEANUP_THRESHOLD):
    """采购时效跟踪完整流程入口。

    流程：
        1) A2 按日期范围获取采购单
        2) A3 并发解析采购单
        3) A4 更新飞书多维表格
        4) B1 读取未入库单视图
        5) A3 再次解析未入库单
        6) A4 再次更新多维表格
        7) C5 清理已结束跟踪的记录（破坏性，cleanup_finished=False 可关闭）

    a2_time_field: A2 拉单按哪个时间字段过滤，create_time（创建）或 order_time（下单）。
    cleanup_finished: 完成后是否执行清理，默认 True。
    cleanup_threshold: 表1/表2 任一记录数超过该值才执行清理，默认 15000。

    返回：
        {"phase1": {...}, "phase2": {...},
         "cleanup": {"triggered": bool, "table1_deleted": n, "table2_deleted": n}}
    """
    app_token = extract_app_token(feishu_url)
    log("INFO", f"开始采购时效跟踪流程：base={app_token}, 表1={table1_name}({table1_id}), 表2={table2_name}({table2_id})")

    # 幂等：确保表2 有 plan_sn / 加工日期 字段（缺失才创建）
    if auto_migrate:
        ensure_table2_fields(app_id, app_secret, app_token, table2_id)

    # ---- Phase 1：A2 -> A3 -> A4 ----
    log("INFO", f"A2 获取采购单: {start_date} ~ {end_date}（按 {a2_time_field} 过滤）")
    raw_orders = a2_fetch_orders(cookies, start_date, end_date,
                                 search_options=search_options, time_field=a2_time_field)
    log("INFO", f"A2 共获取 {len(raw_orders)} 条采购单")

    log("INFO", "A3 解析采购单...")
    data1 = A3(raw_orders, cookies)
    log("INFO", f"A3 产出：订单记录 {len(data1['orders'])}，SKU记录 {len(data1['items'])}")

    log("INFO", "A4 更新多维表格（Phase 1）...")
    summary1 = a4_upload_to_feishu(data1, app_id, app_secret, app_token, table1_id, table2_id)

    # ---- Phase 2：B1 -> A3 -> A4 ----
    log("INFO", f"B1 读取未入库单视图「{view_name}」...")
    unstocked = b1_read_unstocked_orders(
        app_id, app_secret, app_token,
        table1_id, table2_id, view_name=view_name,
        view_id=view_id,
        default_status_text=default_status_text,
    )
    log("INFO", f"B1 共 {len(unstocked)} 条未入库单")

    if unstocked:
        log("INFO", "A3 解析未入库单...")
        data2 = A3(unstocked, cookies)
        log("INFO", f"A3 产出：订单记录 {len(data2['orders'])}，SKU记录 {len(data2['items'])}")
        log("INFO", "A4 更新多维表格（Phase 2）...")
        summary2 = a4_upload_to_feishu(data2, app_id, app_secret, app_token, table1_id, table2_id)
    else:
        summary2 = {"table1": {"created": 0, "updated": 0}, "table2": {"created": 0, "updated": 0}}
        log("INFO", "没有未入库单需要回跑")

    result = {"phase1": summary1, "phase2": summary2}

    # ---- Phase 3：C5 按需清理已结束跟踪的记录（破坏性，可关闭）----
    if cleanup_finished:
        log("INFO", f"C5 按需清理（表1/表2 任一超过 {cleanup_threshold} 条时删除表1 加工日期非空或备注非空、表2 对应引用记录）...")
        result["cleanup"] = cleanup_finished_records(app_id, app_secret, app_token,
                                                     table1_id, table2_id, threshold=cleanup_threshold)
    else:
        result["cleanup"] = {"triggered": False, "table1_deleted": 0, "table2_deleted": 0}
        log("INFO", "C5 清理已关闭（cleanup_finished=False），跳过")

    log("INFO", f"全部流程完成: {result}")
    return result


# =============================================================================
# 配置驱动入口
#   所有隐私/环境相关配置统一放在 config.json（已被 .gitignore 忽略，不会入库）。
#   首次使用：复制 config.example.json 为 config.json，然后填写真实值。
#     cp config.example.json config.json
#
#   配置在 __main__ 中只读取一次，随后作为入参整体传给主逻辑方法，
#   主逻辑方法全程只被调用一次；结果落盘等附加逻辑另行封装。
# =============================================================================
def _resolve_path(path):
    """相对路径按脚本所在目录解析；绝对路径原样返回。"""
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), path)


def load_config(config_path=CONFIG_FILE):
    """加载 config.json 配置，返回 dict；缺失或字段不全时给出明确提示。"""
    path = _resolve_path(config_path)
    if not os.path.exists(path):
        raise SystemExit(
            f"缺少配置文件 {path} —— 请先复制模板并填写真实配置：\n"
            f"    copy {CONFIG_EXAMPLE_FILE} {CONFIG_FILE}"
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(f"配置文件 {path} 不是合法 JSON: {e}")
    if not isinstance(cfg, dict):
        raise SystemExit(f"配置文件 {path} 顶层必须是 JSON 对象")

    required = ["APP_ID", "APP_SECRET", "FEISHU_URL", "TABLE1_ID", "TABLE2_ID"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise SystemExit(f"config.json 中以下配置为空: {', '.join(missing)}")
    return cfg


def load_cookies(cfg):
    """读取领星 cookies：优先用 config 中的 COOKIES 列表，否则从 COOKIES_PATH 读 JSON。"""
    cookies = cfg.get("COOKIES")
    if cookies:
        return cookies
    path = cfg.get("COOKIES_PATH")
    if not path:
        raise SystemExit("config.json 需要设置 COOKIES（列表）或 COOKIES_PATH（JSON 文件路径）之一")
    path = _resolve_path(path)
    if not os.path.exists(path):
        raise SystemExit(f"cookies 文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_from_config(config, start_date=None, end_date=None, cookies=None, **overrides):
    """主逻辑方法：把 config.json 读出的配置字典作为参数，执行完整流程。"""
    # 并发调试开关：从 config 读取布尔值，统一在此设置全局状态
    if config.get("DEBUG_CONCURRENT"):
        set_concurrent_debug(True, config.get("DEBUG_FILE", DEFAULT_DEBUG_FILE))
    else:
        set_concurrent_debug(False)

    if cookies is None:
        cookies = load_cookies(config)
    return run_purchase_tracking(
        cookies=cookies,
        app_id=config["APP_ID"],
        app_secret=config["APP_SECRET"],
        feishu_url=config["FEISHU_URL"],
        start_date=start_date or config.get("START_DATE"),
        end_date=end_date or config.get("END_DATE"),
        table1_id=config.get("TABLE1_ID", TABLE1_ID_DEFAULT),
        table2_id=config.get("TABLE2_ID", TABLE2_ID_DEFAULT),
        table1_name=config.get("TABLE1_NAME", "采购单"),
        table2_name=config.get("TABLE2_NAME", "采购单SKU明细"),
        view_name=config.get("VIEW_NAME", VIEW_NAME_DEFAULT),
        view_id=config.get("VIEW_ID", VIEW_ID_DEFAULT),
        a2_time_field=config.get("A2_TIME_FIELD", "create_time"),
        cleanup_finished=config.get("CLEANUP_FINISHED", True),
        cleanup_threshold=config.get("CLEANUP_THRESHOLD", CLEANUP_THRESHOLD),
        **overrides,
    )


def save_result(result, out_path):
    """附加逻辑：把执行结果写入 JSON 文件。"""
    out_path = _resolve_path(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log("INFO", f"结果已写入 {out_path}")
    return out_path


def run_and_save_result(config):
    """封装方法：执行主流程并把结果落盘（供 __main__ 单次调用）。

    全部入参取自 config：
      - START_DATE / END_DATE  日期范围
      - OUTPUT_FILE            结果落盘文件名（缺省 purchase_tracking_result.json）
    需要临时覆盖时，请先改 config 再调用本方法。
    """
    result = run_from_config(config)
    save_result(result, config.get("OUTPUT_FILE") or DEFAULT_OUTPUT_FILE)
    return result


if __name__ == "__main__":
    config = load_config(CONFIG_FILE)
    run_and_save_result(config)
