# -*- coding: utf-8 -*-
"""领星 ERP 数据源接入（逆向 Web 会话，非官方 OpenAPI）。

注意事项（详见 docs/领星ERP数据源接入.md）：
  - 不是领星官方 OpenAPI，没有 app_key/app_secret/MD5 签名，没有 OAuth。
  - 通过浏览器 Cookie（含 auth-token/company_id/env_key/uid/zid）逆向 Web 后台接口。
  - 三个域名：
        ERP   https://maique.lingxing.com  (LINGXING_ERP_HOST)
        GW    https://gw.lingxingerp.com   (LINGXING_GW_HOST)
        ADS   https://ads.lingxing.com     (LINGXING_ADS_HOST)
  - X-AK-Version 硬编码默认值，需随领星前端升级而更新。

端点与字段均从参考脚本核对（lingxing_sync.py / main.py / purchase_tracking.py），
详见模块底部注释与接入文档，切勿臆造字段名。
"""
from __future__ import annotations

import ast
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

import pandas as pd

from backend.services.data_cleaner import CANONICAL_COLUMNS

logger = logging.getLogger("lingxing")

# =============================================================================
# 常量（均可用环境变量覆盖）
# =============================================================================
ERP_HOST = os.getenv("LINGXING_ERP_HOST", "https://maique.lingxing.com").rstrip("/")
GW_HOST = os.getenv("LINGXING_GW_HOST", "https://gw.lingxingerp.com").rstrip("/")
ADS_HOST = os.getenv("LINGXING_ADS_HOST", "https://ads.lingxing.com").rstrip("/")

DEFAULT_VERSION = os.getenv("LINGXING_VERSION", "3.9.0.3.0.037")

MAX_RETRIES = 3
RETRY_BACKOFF = 1.0
PAGE_SIZE = 200
REQUEST_TIMEOUT = 60

# 本地仓筛选：原脚本把广州仓 id "3275" 写死（属原作者账号），此处改为可配置，
# 默认空字符串 = 不按仓库过滤（返回账号下全部本地仓）。
WAREHOUSE_IDS = os.getenv("LINGXING_WAREHOUSE_IDS", "")
# marketplace 过滤，沿用原脚本默认值 "1"；多店铺账号可用 LINGXING_MIDS 覆盖。
DEFAULT_MIDS = os.getenv("LINGXING_MIDS", "1")

# X-AK-Request-Id 之外的固定请求头模板（其余字段从 Cookie 动态提取）
_BASE_HEADER_TEMPLATE = {
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
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
    "X-AK-Language": "zh",
    "X-AK-PLATFORM": "1",
    "X-AK-Request-Source": "erp",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


# =============================================================================
# Cookie 解析 + 请求头构造
# =============================================================================
def _normalize_cookie_input(cookie: str | list[dict[str, Any]]) -> list[tuple[str, str]]:
    """把 Cookie 输入统一成 [(name, value), ...]。

    支持两种形态：
      - 字符串： ``"auth-token=..; company_id=..; ..."``  → 按分号拆分首 "=" 取键值
      - 列表：  ``[{"Name": "...", "Value": "..."}]`` 或 ``[{"name":.., "value":..}]``
    """
    pairs: list[tuple[str, str]] = []
    if isinstance(cookie, str):
        for part in cookie.split(";"):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                name, value = part.split("=", 1)
                pairs.append((name.strip(), value.strip()))
            else:
                pairs.append((part, ""))
        return pairs

    if isinstance(cookie, list):
        for item in cookie:
            if not isinstance(item, dict):
                continue
            name = item.get("Name") if "Name" in item else item.get("name")
            value = item.get("Value") if "Value" in item else item.get("value")
            if not name:
                continue
            pairs.append((str(name), "" if value is None else str(value)))
        return pairs

    raise TypeError("cookie 必须是字符串或 [{Name,Value}] 列表")


def build_lingxing_headers(cookie: str | list[dict[str, Any]], version: str | None = None) -> dict[str, str]:
    """根据 Cookie 构造领星请求头。

    参数：
        cookie  : Cookie 字符串或 [{Name,Value}] 列表
        version : X-AK-Version，缺省取 DEFAULT_VERSION（环境变量 LINGXING_VERSION）
    返回：完整请求头 dict（含每次请求需重设的占位 X-AK-Request-Id）
    异常：缺少 auth-token 抛 ValueError。
    """
    pairs = _normalize_cookie_input(cookie)

    cookie_header_parts: list[str] = []
    auth_token_encoded = company_id = env_key = uid = zid = None
    for name, value in pairs:
        cookie_header_parts.append(f"{name}={value}")
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
        raise ValueError("Cookie 中缺少 auth-token，请确认领星登录态是否有效")

    auth_token = urllib.parse.unquote(auth_token_encoded)

    headers = dict(_BASE_HEADER_TEMPLATE)
    headers.update(
        {
            "X-AK-Company-Id": company_id or "",
            "X-AK-ENV-KEY": env_key or "",
            "X-AK-Uid": uid or "",
            "X-AK-Zid": zid or "",
            "X-AK-Version": version or DEFAULT_VERSION,
            "auth-token": auth_token,
            "Cookie": "; ".join(cookie_header_parts),
            # 每次请求由 send_request 覆盖；此处给一个初始值避免缺失
            "X-AK-Request-Id": str(uuid.uuid4()),
        }
    )
    return headers


# =============================================================================
# 通用 HTTP 与分页
# =============================================================================
def send_request(
    url: str,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    desc: str = "request",
    method: str = "POST",
    timeout: int = REQUEST_TIMEOUT,
    retries: int = MAX_RETRIES,
) -> dict[str, Any] | None:
    """发送领星请求（POST/GET），带超时与指数退避重试，失败返回 None。

    每次请求附加随机 X-AK-Request-Id。
    """
    result: dict[str, Any] | None = None
    last_error: str | None = None
    for attempt in range(retries):
        try:
            req_headers = dict(headers)
            req_headers["X-AK-Request-Id"] = str(uuid.uuid4())
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                result = json.loads(body)
                break
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一兜底
            last_error = str(exc)
            logger.warning("%s 第%d次请求失败: %s", desc, attempt + 1, exc)
            if attempt < retries - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    if result is None:
        logger.error("%s 重试%d次仍失败: %s", desc, retries, last_error)
    return result


def fetch_paged(
    url: str,
    body_tpl: dict[str, Any],
    headers: dict[str, str],
    list_path: tuple[str, ...],
    total_path: tuple[str, ...] | None = None,
    desc: str = "分页",
    page_size: int = PAGE_SIZE,
) -> tuple[list[dict[str, Any]], int]:
    """通用分页拉取。

    list_path : 返回结构中 list 的取值路径，如 ("data","list") 或 ("list",)（顶层）。
    total_path: 返回结构中 total 的取值路径（None 表示与 list 同级的 "total"）。
    返回 (records, total)。
    """
    all_records: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        body = dict(body_tpl)
        body["offset"] = offset
        body["length"] = page_size
        resp = send_request(url, body, headers, f"{desc} offset={offset}")
        if not resp or resp.get("code") != 1:
            logger.error("接口返回异常: code=%s", resp.get("code") if resp else None)
            break
        node: Any = resp
        for key in list_path:
            node = (node or {}).get(key)
        total_node: Any = resp
        for key in (total_path if total_path else tuple(list(list_path[:-1]) + ("total",))):
            total_node = (total_node or {}).get(key)
        total = total_node if isinstance(total_node, int) else (total or 0)
        records = node if isinstance(node, list) else []
        if not records:
            break
        all_records.extend(records)
        offset += page_size
        logger.info("%s 进度: %d/%d", desc, len(all_records), total)
    logger.info("%s 完成，共 %d 条", desc, len(all_records))
    return all_records, total or 0


# =============================================================================
# 纯函数：字段解析辅助（照搬自 lingxing_sync.py）
# =============================================================================
def join_names(value: Any) -> str:
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


def to_number(value: Any) -> Any:
    """纯数字字符串 -> int/float；其余原样返回。

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


def fmt_shipment_time(value: Any) -> str:
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


# =============================================================================
# LingxingClient
# =============================================================================
class LingxingClient:
    """领星 ERP 逆向接口客户端（Cookie 会话）。"""

    def __init__(
        self,
        cookie: str | list[dict[str, Any]],
        version: str | None = None,
        erp_host: str | None = None,
        gw_host: str | None = None,
        ads_host: str | None = None,
    ) -> None:
        self.cookie = cookie
        self.version = version
        self.erp_host = (erp_host or ERP_HOST).rstrip("/")
        self.gw_host = (gw_host or GW_HOST).rstrip("/")
        self.ads_host = (ads_host or ADS_HOST).rstrip("/")

    # -- 内部工具 ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return build_lingxing_headers(self.cookie, self.version)

    # -- 店铺 --------------------------------------------------------------
    def fetch_sellers(self) -> list[dict[str, Any]]:
        """店铺列表 GET /api/my/sellers。返回 list（位于顶层，list_path=("list",)）。"""
        url = f"{self.erp_host}/api/my/sellers?req_time_sequence=%2Fapi%2Fmy%2Fsellers%24%241"
        resp = send_request(url, None, self._headers(), "店铺列表", method="GET")
        if not resp or resp.get("code") != 1:
            return []
        return resp.get("list") or []

    # -- 产品表现（主表）---------------------------------------------------
    def fetch_product_performance(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """产品表现-ASIN列表 POST {gw}/bd/productPerformance/asinLists。

        字段（逐一核对自 main.py clean_row）：
            sku/asin/item_name -> 商品名（build_canonical_dataframe 内取）
            volume       销量        -> 下单数 orders
            amount       销售额      -> 支付金额 payment_amount
            spend        广告花费    -> 广告花费 ad_cost
            clicks       点击        -> 点击量 clicks
            ad_sales_amount / acos / acoas / avg_custom_price 等为可选指标
            afn_fulfillable_quantity FBA 可售（用于库存兜底）
        list/total 位于 data 内：list_path=("data","list"), total_path=("data","total")。
        """
        url = f"{self.gw_host}/bd/productPerformance/asinLists"
        body = {
            "sort_field": "volume", "sort_type": "desc",
            "offset": 0, "length": PAGE_SIZE,
            "search_field": "asin", "search_value": [],
            "mids": DEFAULT_MIDS, "sids": "",
            "date_type": "purchase",
            "start_date": start_date, "end_date": end_date,
            "principal_uids": [], "bids": [], "cids": [],
            "extend_search": [], "summary_field": "asin", "purchase_status": 0,
            "currency_code": "CNY", "product_states": [],
            "is_resale": "", "order_types": [], "promotions": [],
            "developers": [], "delivery_methods": [],
            "is_recently_enum": True, "ad_cost_type": "",
            "attr_value_ids": [], "turn_on_summary": 1,
            "summary_field_level1": "asin", "summary_field_level2": "",
            "owner_uids": [], "gtag_ids": [], "auto_tags": [], "regions": [],
            "date_range_type": 0, "only_query_today": False,
            "today_hour": time.strftime("%H"),
            "query_order_profit": True,
            "req_time_sequence": "/bd/productPerformance/asinLists",
        }
        records, _ = fetch_paged(
            url, body, self._headers(), ("data", "list"), ("data", "total"),
            "产品表现", page_size=PAGE_SIZE,
        )
        return records

    # -- 库存 --------------------------------------------------------------
    def fetch_fba_inventory(self) -> list[dict[str, Any]]:
        """FBA 在售库存 POST {erp}/api/storage/fbaLists。

        注意：fbaLists 的 list/total 在**顶层**（不在 data 内）。
        list_path=("list",), total_path=("total",)。
        可用库存量字段：afn_fulfillable_quantity（FBA 可售）。
        """
        url = f"{self.erp_host}/api/storage/fbaLists"
        body = {
            "cid": "", "bid": "", "attribute": "", "asin_principal": "",
            "search_field": "sku", "is_cost_page": 1, "status": "",
            "senior_search_list": "[]", "offset": 0, "length": PAGE_SIZE,
            "fulfillment_channel_type": "FBA", "is_hide_zero_stock": "0",
            "is_parant_asin_merge": "0",
            "req_time_sequence": "/api/storage/fbaLists$$2",
        }
        records, _ = fetch_paged(
            url, body, self._headers(), ("list",), ("total",),
            "FBA库存", page_size=PAGE_SIZE,
        )
        return records

    def fetch_stock(self) -> list[dict[str, Any]]:
        """本地仓库存 POST {erp}/api/storage/lists。

        list/total 位于 data 内：list_path=("data","list"), total_path=("data","total")。
        可用量字段：good_num（可用量）、total（实际总量）。
        仓库范围由 LINGXING_WAREHOUSE_IDS 控制（原脚本写死为作者账号的广州仓 id）。
        """
        url = f"{self.erp_host}/api/storage/lists"
        body = {
            "wid_list": WAREHOUSE_IDS, "mid_list": "", "sid_list": "", "inventoryOwnership": -1,
            "cid_list": "", "bid_list": "", "principal_list": "", "product_type_list": "",
            "product_attribute": "", "product_status": "", "search_field": "sku",
            "search_value": "", "is_sku_merge_show": 0, "is_hide_zero_stock": 0,
            "offset": 0, "length": PAGE_SIZE, "sort_field": "total", "sort_type": "desc",
            "gtag_ids": "", "senior_search_list": "[]", "permission_uid_list": "",
            "country_code_list": "", "has_statistic": False,
            "req_time_sequence": "/api/storage/lists$$3",
        }
        records, _ = fetch_paged(
            url, body, self._headers(), ("data", "list"), ("data", "total"),
            "仓库库存", page_size=PAGE_SIZE,
        )
        return records

    # -- 发货 --------------------------------------------------------------
    def fetch_shipments(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """发货单列表 POST {erp}/api/shipment/showShipmentListV2。

        list/total 位于 data 内：list_path=("data","list"), total_path=("data","total")。
        返回摘要记录含 shipment_sn / shipment_time 等。
        """
        url = f"{self.erp_host}/api/shipment/showShipmentListV2"
        body = {
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
            "offset": 0, "length": PAGE_SIZE,
            "req_time_sequence": "/api/shipment/showShipmentListV2$$10",
        }
        records, _ = fetch_paged(
            url, body, self._headers(), ("data", "list"), ("data", "total"),
            "发货单列表", page_size=PAGE_SIZE,
        )
        return records

    # -- 采购 --------------------------------------------------------------
    def fetch_purchase_orders(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """采购单列表 POST {erp}/api/purchase/orderListsV2。

        list/total 位于 data 内：list_path=("data","list"), total_path=("data","total")。
        订单级字段：order_sn / status_text / order_time / item_list[].sku 等。
        """
        url = f"{self.erp_host}/api/purchase/orderListsV2"
        body = {
            "offset": 0, "length": PAGE_SIZE,
            "expect_arrive_time_status": "", "sort_field": "create_time", "sort_type": "desc",
            "status_shipped": [], "status": "", "pay_status": [],
            "search_field_time": "create_time", "start_date": start_date, "end_date": end_date,
            "search_field": "order_sn", "search_value": "", "wid": [], "sid": [],
            "gtag_ids": "", "permission_uid_list": [], "senior_search_list": [],
            "is_urgent": "", "change_order_status": "", "is_bad": "",
            "is_tax": "", "is_logistics": "", "is_associate_return": 0,
            "is_associate_exchange": 0, "supplier_ids": [], "bids": [], "cids": [],
            "is_transparency": "", "record_tag_ids": [], "search_options": [],
            "max_item_number": 5,
            "req_time_sequence": "/api/purchase/orderListsV2",
        }
        records, _ = fetch_paged(
            url, body, self._headers(), ("data", "list"), ("data", "total"),
            "采购单列表", page_size=PAGE_SIZE,
        )
        return records

    # -- 财务 --------------------------------------------------------------
    def fetch_receivable(self, settle_month: str, sids: list[Any] | None = None) -> list[dict[str, Any]]:
        """月度应收报告 POST {gw}/bd/sp/api/monthly/receivable/report/list。

        list/total 位于 data 内：list_path=("data","records"), total_path=("data","total")。
        字段（核对自 lingxing_sync.py RECEIVABLE_COLUMNS）：
            storeName / country / settlementDate / incomeAmount / refundAmount /
            spendAmount / endingBalance / receivedAmount 等。
        """
        url = f"{self.gw_host}/bd/sp/api/monthly/receivable/report/list"
        body = {
            "settleMonth": settle_month, "archiveStatus": "", "currencyCode": "CNY",
            "sortField": "endingBalance", "sortType": "desc", "receivedState": "",
            "offset": 0, "length": 100, "sids": sids or [], "mids": [],
            "req_time_sequence": "/bd/sp/api/monthly/receivable/report/list$$3",
        }
        records, _ = fetch_paged(
            url, body, self._headers(), ("data", "records"), ("data", "total"),
            "应收报告", page_size=100,
        )
        return records

    # -- 广告（可选，最小可用，CSRF 限制见 docstring）---------------------
    def fetch_ads_report(self, profile_id: str | None = None) -> list[dict[str, Any]] | None:
        """广告报表（ads 域名 + CSRF）。

        限制（最小可用版本）：
          - 领星广告后台需要额外的 CSRF 令牌与会话，完整逆向流程较复杂；
          - 本方法在缺少 CSRF 上下文时直接返回 None，**不会让主流程失败**；
          - 若后续补齐 CSRF 流程（从 ads 首页 HTML 抽取 token），可在此实现真实拉取，
            返回字段需映射到 impressions/clicks/ad_cost。
        因此 build_canonical_dataframe 的广告字段默认回退到产品表现自带字段。
        """
        if not profile_id:
            logger.info("fetch_ads_report 未提供 profile_id，跳过广告拉取（返回 None）")
            return None
        logger.warning("fetch_ads_report 为最小可用实现：缺少 CSRF 流程，暂返回 None")
        return None


# =============================================================================
# 规范化为 ecompilot 标准 DataFrame
# =============================================================================
def _item_product_name(item: dict[str, Any]) -> str:
    """从一条产品表现数据中提取可用作「商品名」的标识。

    优先级：item_name > asin > sku > price_list[0].seller_sku。
    """
    for key in ("item_name", "asin", "sku"):
        value = item.get(key)
        if value:
            return str(value).strip()
    price_list = item.get("price_list") or []
    if price_list and price_list[0].get("seller_sku"):
        return str(price_list[0]["seller_sku"]).strip()
    return ""


def _build_stock_map(records: list[dict[str, Any]]) -> dict[str, Any]:
    """把库存记录按 sku / asin 建立 标识 -> 库存量 映射。

    取值优先级（依据原脚本 lingxing_sync.py 的字段定义，勿臆造）：
      1. afn_fulfillable_quantity —— FBA 可售量
      2. good_num                 —— 本地仓「可用量」（STOCK_COLUMNS 明确 good_num=可用量）
      3. total                    —— 「实际总量」（含次品/占用，仅在前两者缺失时兜底）
    """
    stock_map: dict[str, Any] = {}
    for rec in records:
        stock = to_number(rec.get("afn_fulfillable_quantity"))
        if stock is None or stock == "":
            stock = to_number(rec.get("good_num"))
        if stock is None or stock == "":
            stock = to_number(rec.get("total"))
        if stock is None:
            stock = 0
        for key in ("sku", "asin"):
            ident = rec.get(key)
            if ident and ident not in stock_map:
                stock_map[str(ident)] = stock
    return stock_map


def build_canonical_dataframe(
    client: LingxingClient,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """把领星多域数据拉取并规范化为 ecompilot 标准 DataFrame。

    主表：产品表现（每个 SKU/ASIN 一行）。
    左连接 FBA / 广州仓库存补 stock；广告字段回退到产品表现自带字段。

    输出列严格对齐 CANONICAL_COLUMNS（来自 backend.services.data_cleaner），
    可直接喂给 analyze_dataframe。
    """
    # 1) 主表：产品表现
    items = client.fetch_product_performance(start_date, end_date) or []

    # 2) 库存补 stock
    fba = client.fetch_fba_inventory() or []
    stock = client.fetch_stock() or []
    stock_map = _build_stock_map(fba)
    stock_map.update(_build_stock_map(stock))  # 广州仓兜底覆盖

    # 3) 广告（可选；失败返回 None 不影响主流程）
    ads = client.fetch_ads_report() or []

    rows: list[dict[str, Any]] = []
    for item in items:
        name = _item_product_name(item)
        if not name:
            continue  # 无商品名无法分析，跳过

        ident = item.get("sku") or item.get("asin") or name
        row_stock = stock_map.get(str(ident), 0)

        # 广告字段：优先广告报表，否则回退产品表现自带字段
        ad_impressions = 0
        ad_clicks = to_number(item.get("clicks")) or 0
        ad_cost = to_number(item.get("spend")) or 0
        if ads:
            # 若未来补齐广告拉取，可在此按 ident 映射覆盖
            pass

        rows.append(
            {
                "product_name": name,
                "impressions": ad_impressions,
                "clicks": ad_clicks,
                "visitors": 0,
                "orders": to_number(item.get("volume")) or 0,
                "payment_amount": to_number(item.get("amount")) or 0,
                "ad_cost": ad_cost,
                "cost": 0,
                "refund_amount": 0,
                "stock": row_stock,
            }
        )

    df = pd.DataFrame(rows, columns=list(CANONICAL_COLUMNS.keys()))
    return df


"""
端点与字段核对来源（避免臆造）：
  - 产品表现  : main.py L79 (LX_API_URL) / L247 build_payload / L418 clean_row 字段
  - FBA 库存  : lingxing_sync.py L807 / L779 fba_body / L752 FBA_COLUMNS / L809 list 在顶层
  - 广州仓库存: lingxing_sync.py L880 / L850 stock_body / L832 STOCK_COLUMNS / L882 data 内
  - 发货单列表: lingxing_sync.py L535 / L510 shipment_list_body / L545 data.list
  - 采购单列表: purchase_tracking.py L277 / L241 post_dict / L299 data.list
  - 应收报告  : lingxing_sync.py L721 / L682 receivable_body / L723 data.records
  - 店铺列表  : lingxing_sync.py L573 / L579 resp.list（顶层）
  - 请求头    : lingxing_sync.py L91-116（版本号沿用 DEFAULT_VERSION）
"""
