import urllib.parse
import urllib.request
import json
import uuid
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---- 可调参数 ----
REQUEST_TIMEOUT = 30    # 单请求超时（秒）—— 原代码缺失，已补
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0     # 重试间隔基数（秒），按 2^attempt 指数退避
ORDER_WORKERS = 10      # 并发处理订单数
HTTP_WORKERS = 20       # 并发 HTTP 请求数（与订单池相互独立，避免死锁）


def log(level, message):
    """统一日志输出，包含时间、线程名、级别"""
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    thread_name = threading.current_thread().name
    print(f"{timestamp} - {thread_name} - {level} - {message}")


def build_headers(cookies):
    """一次性解析 cookies 构建请求头模板（不含 X-AK-Request-Id）"""
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
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


def send_request(url, post_dict, base_headers, request_desc="request"):
    """发送 POST 请求，带超时与重试退避，返回解析后的 JSON，失败返回 None"""
    for attempt in range(MAX_RETRIES):
        try:
            headers = base_headers.copy()
            headers["X-AK-Request-Id"] = str(uuid.uuid4())
            data = json.dumps(post_dict).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            # 关键修复：加 timeout，避免某个接口卡死导致线程永久阻塞
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                if response.status != 200:
                    raise Exception(f"HTTP {response.status}")
                body = response.read().decode("utf-8")
                log("DEBUG", f"{request_desc} 第{attempt+1}次请求成功")
                return json.loads(body)
        except Exception as e:
            log("WARNING", f"{request_desc} 第{attempt+1}次请求失败: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))  # 指数退避
    log("ERROR", f"{request_desc} 三次重试全部失败，放弃")
    return None


def _extract_logistics(resp2):
    """从物流接口返回提取发货/签收时间。防御性排序，避免接口返回顺序不确定导致对调。"""
    start_time = end_time = None
    if not resp2:
        return start_time, end_time
    log_list = resp2.get("data", {}).get("list")
    if not log_list:
        return start_time, end_time
    trace_info = log_list[0].get("trace_info")
    if not trace_info:
        return start_time, end_time
    # 假设 accept_time 为 "yyyy/MM/dd HH:mm"（零填充），字符串排序即可
    sorted_trace = sorted(trace_info, key=lambda t: t.get("accept_time") or "")
    start_time = sorted_trace[0].get("accept_time")          # 发货 = 最早一条
    if log_list[0].get("status_text") == "已签收":
        end_time = sorted_trace[-1].get("accept_time")       # 签收 = 最新一条
    return start_time, end_time


def process_order(order_dict, base_headers, http_executor):
    """
    处理单个订单，并发获取加工/物流/采购信息。
    返回 {"order": 订单级记录 或 None, "items": [SKU级记录, ...]}。
    注意：HTTP 子请求提交到外部传入的 http_executor，订单池与 HTTP 池相互独立，避免嵌套死锁。
    """
    order_sn = order_dict.get('order_sn', '未知')
    log("INFO", f"开始处理订单 [{order_sn}]")

    status_text = order_dict.get('status_text', None)
    if status_text is None:
        log("WARNING", f"订单 [{order_sn}] status_text 为空，跳过")
        return {"order": None, "items": []}
    if status_text == "已作废":
        log("DEBUG", f"订单 [{order_sn}] 已作废，跳过")
        return {"order": None, "items": []}

    sn_1688 = order_dict.get('alibaba_order_sn', None)
    sn = order_dict.get('order_sn', None)
    item_list = order_dict.get('item_list', None)
    if not item_list:
        log("WARNING", f"订单 [{order_sn}] item_list 为空，跳过")
        return {"order": None, "items": []}
    sn_plan = item_list[0].get('plan_sn', None)

    # ⚠️ 语义待确认：字段名"是否完成下单"，但此处判断的是履约状态
    # （status_text 为"待到货"/"已完成"）。若本意是"已成功提交下单"，
    # 此判断可能不准确，请按业务实际修改。
    is_order_placed = (status_text == "待到货" or status_text == "已完成")
    order_time = order_dict.get('order_time', None)

    url1 = "https://maique.lingxing.com/api/storage_process/lists"
    post1 = {
        "sort_field": "create_time", "sort_type": "desc", "wids": "",
        "seller_id": [], "status": "", "gtag_ids": "", "type": "",
        "search_field_time": "create_time", "search_field": "plan_sn",
        "search_value": sn_plan, "start_date": "", "end_date": "",
        "offset": 0, "length": 20,
        "req_time_sequence": "/api/storage_process/lists"
    }
    url2 = "https://maique.lingxing.com/api/purchase_logistics/getLogistics"
    post2 = {"order_sn": sn, "req_time_sequence": "/api/purchase_logistics/getLogistics"}
    url3 = "https://maique.lingxing.com/api/purchase/orderInfo"
    post3 = {"order_sn": sn, "req_time_sequence": "/api/purchase/orderInfo"}

    log("DEBUG", f"订单 [{order_sn}] 开始并发请求加工、物流、采购信息")

    # 复用外部 HTTP 线程池，提交三个子请求（不下新建池）
    f1 = http_executor.submit(send_request, url1, post1, base_headers, f"订单[{order_sn}]加工单请求")
    f2 = http_executor.submit(send_request, url2, post2, base_headers, f"订单[{order_sn}]物流请求")
    f3 = http_executor.submit(send_request, url3, post3, base_headers, f"订单[{order_sn}]采购单请求")
    resp1, resp2, resp3 = f1.result(), f2.result(), f3.result()

    # 加工完成时间（订单级）
    time_finish = None
    if resp1:
        try:
            lst = resp1.get("list")
            if lst and lst[0].get("finish_time") != "-":
                time_finish = lst[0].get("finish_time")
                log("DEBUG", f"订单 [{order_sn}] 加工完成时间: {time_finish}")
        except Exception as e:
            log("ERROR", f"订单 [{order_sn}] 提取加工时间异常: {e}")

    # 物流发货/签收时间（订单级）
    start_time, end_time = _extract_logistics(resp2)
    log("DEBUG", f"订单 [{order_sn}] 物流: 发货={start_time}, 签收={end_time}")

    # 入库时间（订单级）
    in_time = None
    if resp3:
        try:
            receive = resp3.get("data", {}).get("receive")
            if receive:
                in_time = receive[0]
                log("DEBUG", f"订单 [{order_sn}] 入库时间: {in_time}")
        except Exception as e:
            log("ERROR", f"订单 [{order_sn}] 提取入库时间异常: {e}")

    # 组装订单级记录（对应飞书表1「采购单」）
    order_record = {
        "采购单号": sn,
        "1688订单号": sn_1688,
        "是否完成下单": is_order_placed,
        "下单日期": order_time,
        "发货日期": start_time,
        "签收日期": end_time,
        "入库日期": in_time,
        "加工日期": time_finish,
    }

    # 组装 SKU 级记录（对应飞书表2「采购单SKU明细」）
    # 表2 的"采购单号"关联字段需填表1 的 record_id，上传时再回填；此处只带采购单号与 SKU
    item_records = [{"采购单号": sn, "SKU": item.get("sku")} for item in item_list]

    log("INFO", f"订单 [{order_sn}] 处理完成，生成 1 条订单记录 + {len(item_records)} 条SKU记录")
    return {"order": order_record, "items": item_records}


def A3(input_list, cookies):
    """
    主入口：并发处理所有订单，返回结构化结果：
    {
        "orders": [ 订单级记录（对应飞书表1「采购单」） ],
        "items":  [ SKU级记录（对应飞书表2「采购单SKU明细」） ]
    }
    单次订单异常不会再拖垮整体结果（原代码 raise 会丢掉已完成数据，已修复）。
    """
    if input_list is None:
        log("WARNING", "输入列表为 None，返回 None")
        return None

    log("INFO", f"开始处理 {len(input_list)} 个订单")
    base_headers = build_headers(cookies)

    orders, items, failed = [], [], []

    # 两个独立线程池：订单池驱动 process_order，HTTP 池承载子请求，互不阻塞
    with ThreadPoolExecutor(max_workers=ORDER_WORKERS) as order_executor, \
         ThreadPoolExecutor(max_workers=HTTP_WORKERS) as http_executor:
        future_to_sn = {
            order_executor.submit(process_order, od, base_headers, http_executor): od.get('order_sn', '未知')
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
