# -*- coding: utf-8 -*-
"""甲组1_广告_手动版 — 领星广告后台（ads.lingxing.com）纯 API 客户端。

替代 RPA 网页自动化的 A1_创建广告结构 / A2_广告优化 两个环节。
状态语义与原流程对齐（A1: 已创建/店铺名错误/没有MSKU/没有匹配模板/广告创建错误；
A2: 已完成/部分完成/无优化方案/广告优化错误），状态写回 deps 项「广告状态」。
A1 跳过已创建/已完成项，A2 仅处理「已创建」项（与原流程过滤逻辑一致）。

接口依据（多次运行抓包逆向所得，本地抓包目录已略）：

  A0/A1 公共
    GET  /common/common_list/common_list/get_profile_list        店铺列表（alias -> profile_id）
  A1 创建链路（build/super 超级创建页）
    POST /ad_report/core/store/products?variants                 店铺产品列表（ASIN -> SKU）
    POST /ad_report/suggestion/power_ad_group/portfolios         广告组合列表
    POST /build/super/index/product_eligibility_list             产品资格检查（JSON）
    POST /build/super/template_name/index                        广告组合模板列表
    POST /build/super/create/create                              创建提交（异步，wait_minutes≈2）
  A2 优化链路（rule_pro 页，x-www-form-urlencoded PHP 风格数组）
    POST /ad_report/rule_pro/index/campaign_ad_groups            活动+广告组+规则组（DataTables 参数）
    POST /ad_report/rule_pro/index/campaigns                     活动列表
    POST /ad_report/rule_pro/index/get_default_target            默认目标
    POST /ad_report/profile/index/ads_objects                    优化对象明细
    POST /ad_report/rule_pro/index/pre_batch_set_multi_rule_to_multi_object   绑规则预检
    POST /ad_report/rule_pro/index/set_moving_relation           设置移词映射（预检 1001 后的前置）
    POST /ad_report/rule_pro/index/batch_set_multi_rule_to_multi_object       批量绑定规则

用法（两文件架构）：
  sx_ads_lark_link.py  飞书长连接收文件 -> waiting/（[{open_id}]YYYYMMDDHHMMSS-原名.xlsx）-> webhook 触发 RPA
  本脚本（贴入 RPA Python 节点）main(task="all", config)：
    waiting 取文件名时间戳最早的 xlsx -> finished/ 归档（取到即归档）
    -> 读依赖表 -> A1 创建 -> A2 优化 -> 广告状态写回 xlsx -> 飞书反馈
    （成功：文本「全部成功执行！」；有错：改名[错误] + 上传文件 + 文本 + 文件发给 open_id）
  __main__ 整段注释（RPA 内不执行）
"""

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

# =============================================================================
# 全局常量（配置区）
# =============================================================================
CONFIG_FILE = "config.json"

# RPA 执行机上的项目目录（waiting/finished 不存在时自动创建）
BASE_DIR = "./"
WAITING_DIR = os.path.join(BASE_DIR, "waiting")
FINISHED_DIR = os.path.join(BASE_DIR, "finished")

ADS_HOST = "https://ads.lingxing.com"
FEISHU_HOST = "https://open.feishu.cn"

REQUEST_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0

# x-AK-Company-Id（部门 ID，固定）
DEFAULT_COMPANY_ID = "YOUR_COMPANY_ID"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

# 各页面 referer / x-AK-Ad-Page-Name（接口校验用，照浏览器原样）
PAGE_BUILD_SUPER = "/build/super/create/index"
PAGE_RULE_PRO = "/ad_report/rule_pro/index/objects"
PAGE_HOME = "/home"

# 取 csrf-token 的候选页（依次尝试，任一成功即可）
CSRF_PAGE_PATHS = [PAGE_HOME, PAGE_BUILD_SUPER]


def log(level, message):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {level} - {message}")


# =============================================================================
# 配置
# =============================================================================
def default_config(cookie_list, csrf_token=None, company_id=DEFAULT_COMPANY_ID, app_name=None,
                   feishu_app_id=None, feishu_app_secret=None):
    """生成配置字典（与 config.json 同构，RPA 流程内构建后直接传入 main）。

    cookie_list: 领星 cookies（Name/Value 列表，浏览器存储获取，含 ads 后台登录态）。
    csrf_token:  x-CSRF-TOKEN；为 None 时自动 GET ads 首页解析 <meta name="csrf-token">。
    app_name:    流程应用名（飞书反馈消息前缀，RPA 内传 SystemVariable.AppName）。
    feishu_*:    飞书反馈应用凭据（须与 sx_ads_lark_link 同一应用，open_id 才能匹配；
                 在 RPA config 节点内明文传入，不落脚本与仓库）。
    """
    return {
        "COOKIES": cookie_list,
        "CSRF_TOKEN": csrf_token,
        "COMPANY_ID": company_id,
        "APP_NAME": app_name,
        "FEISHU_APP_ID": feishu_app_id,
        "FEISHU_APP_SECRET": feishu_app_secret,
    }


def _cookie_header(cookie_list):
    pairs = []
    for c in cookie_list:
        name = c.get("Name", "")
        value = c.get("Value", "")
        if name:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _resolve_path(path):
    if not path or os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), path)


def load_config(config_path=CONFIG_FILE):
    """本地调试用：读 config.json（含 COOKIES）。字段说明见 README。"""
    path = _resolve_path(config_path)
    if not os.path.exists(path):
        raise SystemExit(f"缺少配置文件 {path} —— 字段结构见 README「配置项说明」")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("COOKIES"):
        raise SystemExit("config.json 缺少 COOKIES（领星 ads 后台登录态）")
    return cfg


# =============================================================================
# 公共：ads.lingxing.com 请求
# =============================================================================
def build_headers(cookies, csrf_token, page_name, referer, company_id=DEFAULT_COMPANY_ID,
                  content_type="application/json;charset=UTF-8"):
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": content_type,
        "Origin": ADS_HOST,
        "Referer": referer,
        "User-Agent": USER_AGENT,
        "x-AK-Ad-Page-Name": page_name,
        "x-AK-Company-Id": company_id,
        "x-AK-Cooperative-Key": "0",
        "x-CSRF-TOKEN": csrf_token,
        "x-Requested-With": "XMLHttpRequest",
        "Cookie": _cookie_header(cookies),
    }


def _extract_meta_token(html):
    """从 HTML 的 <meta> 标签解析 token。

    ads 页面模板里是 <meta id='token' name='token' content='....'/>（单引号），
    与常见 name="csrf-token" 写法不同，故按属性解析而非字符串匹配；
    兼容 name=token / csrf-token、单双引号、属性顺序颠倒。
    """
    for tag in re.findall(r"<meta\s[^>]*?>", html, re.I):
        m_name = re.search(r"name\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        m_content = re.search(r"content\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
        if not m_name or not m_content:
            continue
        if m_name.group(1).strip().lower() in ("token", "csrf-token") and m_content.group(1):
            return m_content.group(1)
    return None


def get_csrf_token(cookies, company_id=DEFAULT_COMPANY_ID):
    """GET ads 页面取 x-CSRF-TOKEN（页面 <meta name='token'> 里的会话 token）。

    登录态失效时服务端会 302 到 /restartLogin（返回「您尚未登录」页），
    此时直接抛错而不是拿登录页的 token 继续跑（否则后续接口全部 401）。
    """
    last_err = None
    for path in CSRF_PAGE_PATHS:
        url = f"{ADS_HOST}{path}"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Cookie": _cookie_header(cookies),
                         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                final_url = resp.geturl()
                html = resp.read().decode("utf-8", "replace")
        except Exception as e:
            last_err = f"{path} 请求失败: {e}"
            log("WARNING", last_err)
            continue

        if "restartLogin" in final_url or "您尚未登录" in html:
            last_err = f"ads 登录态失效：{url} 被重定向到 {final_url}"
            log("WARNING", last_err)
            continue

        token = _extract_meta_token(html)
        if token:
            log("INFO", f"获取 csrf-token 成功（{path}）: {token[:8]}...")
            return token
        last_err = f"{path} 未解析到 token（final_url={final_url}, HTML {len(html)} 字节）"
        log("WARNING", last_err)

    names = ",".join(sorted({str(c.get("Name")) for c in (cookies or [])
                             if str(c.get("Name")) in ("token", "authToken", "auth-token",
                                                       "uid", "zid", "isLogin", "is_sellerAuth")}))
    raise ValueError(f"获取 csrf-token 失败 —— {last_err}；"
                     f"携带 cookie {len(cookies or [])} 个"
                     f"{'，关键登录 cookie: ' + names if names else '（缺 token/authToken 等登录 cookie）'}；"
                     "请确认流程已登录 ads.lingxing.com 后再取 Cookie")


def send_request(url, headers, body=None, request_desc="request", method="POST"):
    """发送请求并解析 JSON 响应，指数退避重试，失败返回 None。

    body: bytes（已编码）或 None（GET）。
    """
    result, error = None, None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read().decode("utf-8")
            result = json.loads(raw) if raw.strip() else {}
            break
        except Exception as e:
            error = str(e)
            log("WARNING", f"{request_desc} 第{attempt + 1}次请求失败: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    if result is None:
        log("ERROR", f"{request_desc} 重试{MAX_RETRIES}次仍失败: {error}")
    return result


def send_json(url, payload, headers, request_desc="request", method="POST"):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    return send_request(url, headers, body, request_desc, method)


def php_query(prefix, value):
    """把嵌套 dict/list 编码为 PHP 风格查询串（objects[0][campaign_id]=x&...）。"""
    from urllib.parse import quote
    parts = []

    def walk(key, val):
        if isinstance(val, dict):
            for k, v in val.items():
                walk(f"{key}[{k}]", v)
        elif isinstance(val, list):
            for i, v in enumerate(val):
                walk(f"{key}[{i}]", v)
        else:
            parts.append(f"{key}={quote(str(val), safe='')}")

    walk(prefix, value)
    return "&".join(parts)


def send_form(url, form_dict, headers, request_desc="request"):
    """发送 PHP 风格表单（rule_pro 系列接口）。form_dict 顶层键值，值可嵌套。"""
    encoded = "&".join(php_query(k, v) for k, v in form_dict.items())
    body = encoded.encode("utf-8")
    return send_request(url, headers, body, request_desc)


# =============================================================================
# 公共接口
# =============================================================================
def get_profile_list(cookies, csrf_token, company_id=DEFAULT_COMPANY_ID):
    """店铺列表：返回 {alias: profile_id}（与页面直开接口一致，GET）。"""
    url = f"{ADS_HOST}/common/common_list/common_list/get_profile_list"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": USER_AGENT,
        "Cookie": _cookie_header(cookies),
    }
    resp = send_request(url, headers, None, "店铺列表", method="GET")
    if not resp or not resp.get("successful"):
        raise RuntimeError(f"get_profile_list 失败: {resp}")
    return {item["alias"]: item["profile_id"] for item in resp.get("data", [])}


def resolve_profile_id(store_name, profile_map):
    """按店铺名（中文括号全名）精确匹配 profile_id。"""
    pid = profile_map.get(store_name)
    if not pid:
        raise RuntimeError(f"店铺名未匹配到 profile_id: {store_name}")
    return pid


def _ads_headers(cookies, csrf_token, company_id, page_name, profile_id):
    referer = f"{ADS_HOST}{page_name}?profile_id={profile_id}" if profile_id else f"{ADS_HOST}{page_name}"
    return build_headers(cookies, csrf_token, page_name, referer, company_id)


# =============================================================================
# A1：创建广告结构
# =============================================================================
# 同一店铺（profile）的产品全量缓存：店铺常有几千个 SKU，逐 ASIN 翻页太慢
_PRODUCT_CACHE = {}
PRODUCT_PAGE_SIZE = 100
PRODUCT_MAX_PAGES = 100


def _fetch_all_store_products(config, profile_id):
    """翻页拉全店铺产品。

    接口一页只给 20 条（recordsFiltered 常为几百到几千），只取第 1 页会漏掉
    绝大多数 ASIN，表现成「没有MSKU」。这里翻到 recordsFiltered 耗尽或空页为止。
    返回 (items, ok)；ok=False 表示中途请求失败、列表可能不完整（不缓存）。
    """
    headers = _ads_headers(config["COOKIES"], config["CSRF_TOKEN"], config["COMPANY_ID"],
                           PAGE_BUILD_SUPER, profile_id)
    items, start, page = [], 0, 1
    while page <= PRODUCT_MAX_PAGES:
        # start 按实际拿到的条数递增：服务端可能把 length 压到 20，按请求量跳会漏数据
        payload = {"start": start, "length": PRODUCT_PAGE_SIZE,
                   "page": page, "profile_id": profile_id,
                   "sku_status": ["1"], "is_all_store": 0}
        resp = send_json(f"{ADS_HOST}/ad_report/core/store/products?variants", payload,
                         headers, f"店铺产品列表 p{page}")
        if resp is None:
            log("ERROR", f"店铺产品列表 p{page} 请求失败，产品列表不完整")
            return items, False
        batch = (resp or {}).get("data", []) or []
        items.extend(batch)
        total = (resp or {}).get("recordsFiltered")
        start += len(batch)
        if not batch:
            break
        if isinstance(total, int) and total > 0 and len(items) >= total:
            break
        page += 1
    return items, True


def list_store_products(config, profile_id, asin=None):
    """店铺产品列表（ASIN -> SKU 候选）：按店铺缓存全量，再本地匹配 ASIN。"""
    key = str(profile_id)
    if key not in _PRODUCT_CACHE:
        items, ok = _fetch_all_store_products(config, profile_id)
        if ok:
            _PRODUCT_CACHE[key] = items
            log("INFO", f"店铺产品缓存: profile={profile_id} 共 {len(items)} 条")
        else:
            log("WARNING", f"店铺产品列表不完整（{len(items)} 条），本次不缓存")
            if asin:
                return [it for it in items if str(it.get("asin")) == str(asin)]
            return items
    items = _PRODUCT_CACHE[key]
    if asin:
        items = [it for it in items if str(it.get("asin")) == str(asin)]
    return items


def check_eligibility(config, profile_id, sku, asin, sponsored_type="sp"):
    """产品资格检查，返回 overallStatus（ELIGIBLE 才可创建）。"""
    headers = _ads_headers(config["COOKIES"], config["CSRF_TOKEN"], config["COMPANY_ID"],
                           PAGE_BUILD_SUPER, profile_id)
    payload = {"skus": [{"sku": sku, "asin": asin}],
               "sponsored_type": sponsored_type, "profile_id": profile_id}
    resp = send_json(f"{ADS_HOST}/build/super/index/product_eligibility_list", payload, headers,
                     "资格检查")
    result = ((resp or {}).get("data") or {}).get("result") or {}
    lst = result.get("productResponseList") or []
    return lst[0].get("overallStatus") if lst else None


def get_portfolios(config, profile_id):
    """广告组合列表：返回 [{portfolio_id, name, ...}]。"""
    headers = _ads_headers(config["COOKIES"], config["CSRF_TOKEN"], config["COMPANY_ID"],
                           PAGE_BUILD_SUPER, profile_id)
    resp = send_json(f"{ADS_HOST}/ad_report/suggestion/power_ad_group/portfolios",
                     {"profile_id": profile_id}, headers, "广告组合列表")
    return (resp or {}).get("data", []) or []


def get_templates(config, profile_id):
    """广告组合模板列表：返回 [{id, uuid, name, ...}]。"""
    headers = _ads_headers(config["COOKIES"], config["CSRF_TOKEN"], config["COMPANY_ID"],
                           PAGE_BUILD_SUPER, profile_id)
    resp = send_json(f"{ADS_HOST}/build/super/template_name/index",
                     {"profile_id": profile_id}, headers, "模板列表")
    return (resp or {}).get("data", []) or []


def _adjustments():
    """投放位加价百分比（UI 全部为 0，照样例）。"""
    return [{"predicate": "placementTop", "percentage": 0},
            {"predicate": "placementRestOfSearch", "percentage": 0},
            {"predicate": "placementProductPage", "percentage": 0}]


def _name_template(custom_value):
    """活动/组名 TAG 模板（照样例结构；tag value null 由后端按模板填充）。"""
    tags = [("TAG_CUSTOM", custom_value), ("TAG_MATCH_TYPE", None),
            ("TAG_TARGETING_TYPE", None), ("TAG_TARGET_TYPE", None),
            ("TAG_ASIN", None), ("TAG_CREATED_AT", None)]
    return [{"index": i, "tag": tag, "value": v} for i, (tag, v) in enumerate(tags)]


def build_create_payload(dep, sku, profile_id, portfolio_id, rules, now_str=None):
    """组装 /build/super/create/create 的 payload。

    dep:          依赖表字典（ASIN/品牌名/店铺.../关键词1-50）
    sku:          MSKU（从店铺产品列表按 ASIN 匹配）
    portfolio_id: 广告组合 ID（get_portfolios 按名称匹配）
    rules:        预算/竞价规则 dict（见 DEFAULT_CREATE_RULES）
    now_str:      命名用时间串（缺省取当前）

    手动基础盘关键词 = 依赖表「关键词1-50」非空值：
    - 跳过 "NaN"（空单元格约定值）
    - 跳过与列名相同的占位值（原 A0 去重把重复值改成各自键名）
    """
    asin = dep["ASIN"]
    brand = dep.get("品牌名") or ""
    if not now_str:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    keywords = []
    for i in range(1, 51):
        k = dep.get(f"关键词{i}")
        if not k or k == "NaN" or k == f"关键词{i}":
            continue
        keywords.append(k)

    def keyword_item(text, match_type):
        return {"state": "enabled", "sponsored_type": "sp", "keyword_text": text,
                "match_type": match_type, "bid": "0"}

    def ad_group(cg, match_type, keywords_, targets_, targeting_type, keyword_name):
        return {"negative_keywords": [], "keywords": keywords_, "negative_targets": [],
                "targets": targets_, "state": "enabled", "sponsored_type": "sp",
                "targeting_type": targeting_type, "keyword_name": keyword_name,
                "target_type": "KEYWORD" if keywords_ else "",
                "match_type": match_type if keywords_ else "",
                "default_bid": str(rules["default_bid"]),
                "custom_name": f"{cg}-1"}

    campaigns = []
    # 放大盘：BROAD，关键词=[品牌名]，预算独立
    campaigns.append({
        "state": "enabled", "sponsored_type": "sp", "campaign_negative_keywords": [],
        "campaign_negative_targets": [],
        "custom_name": f"2BROAD-{asin}-放大盘", "keyword_name": "KEYWORD",
        "target_type": "KEYWORD", "match_type": "BROAD", "targeting_type": 2,
        "strategy": "manual", "budget": str(rules["scale_budget"]),
        "ad_groups": [ad_group(f"2BROAD-{asin}-放大盘", "BROAD",
                               [keyword_item(brand, "BROAD")], [], 2, "KEYWORD")],
        "adjustments": _adjustments(),
    })
    # 精准盘：EXACT，关键词=[品牌名]
    campaigns.append({
        "state": "enabled", "sponsored_type": "sp", "campaign_negative_keywords": [],
        "campaign_negative_targets": [],
        "custom_name": f"{brand}-EXACT-手动-关键词-{asin}-{now_str}-精准盘",
        "keyword_name": "关键词", "target_type": "KEYWORD", "match_type": "EXACT",
        "targeting_type": "2", "strategy": "manual", "budget": str(rules["exact_budget"]),
        "ad_groups": [ad_group(f"{brand}-EXACT-手动-关键词-{asin}-{now_str}-精准盘", "EXACT",
                               [keyword_item(brand, "EXACT")], [], "2", "关键词")],
        "adjustments": _adjustments(),
    })
    # 自动基础盘：4 种自动投放表达式
    auto_targets = [{"expression_type": "auto", "state": "enabled", "sponsored_type": "sp",
                     "bid": "0", "expression": [{"type": t}]}
                    for t in ("queryHighRelMatches", "queryBroadRelMatches",
                              "asinSubstituteRelated", "asinAccessoryRelated")]
    campaigns.append({
        "state": "enabled", "sponsored_type": "sp", "campaign_negative_keywords": [],
        "campaign_negative_targets": [],
        "custom_name": f"{brand}-自动-{asin}-{now_str}-基础盘", "keyword_name": "",
        "target_type": "", "match_type": "", "targeting_type": "1",
        "strategy": "manual", "budget": str(rules["auto_budget"]),
        "ad_groups": [ad_group(f"{brand}-自动-{asin}-{now_str}-基础盘", "", [], auto_targets,
                               "1", "")],
        "adjustments": _adjustments(),
    })
    # 手动基础盘：BROAD，关键词=依赖表关键词 + 补充词
    campaigns.append({
        "state": "enabled", "sponsored_type": "sp", "campaign_negative_keywords": [],
        "campaign_negative_targets": [],
        "custom_name": f"{brand}-BROAD-手动-关键词-{asin}-{now_str}-基础盘",
        "keyword_name": "关键词", "target_type": "KEYWORD", "match_type": "BROAD",
        "targeting_type": "2", "strategy": "manual", "budget": str(rules["broad_budget"]),
        "ad_groups": [ad_group(f"{brand}-BROAD-手动-关键词-{asin}-{now_str}-基础盘", "BROAD",
                               [keyword_item(k, "BROAD") for k in keywords], [], "2", "关键词")],
        "adjustments": _adjustments(),
    })

    return {
        "skus": [{"state": "enabled", "sku": sku, "asin": asin, "sponsored_types": ["sp"]}],
        "portfolio_id": portfolio_id,
        "budget": str(rules["portfolio_budget"]),
        "start_date": datetime.now().strftime("%Y-%m-%d"),
        "strategy": "manual",
        "default_bid": str(rules["default_bid"]),
        "build_type": 1,
        "ex_option": {"type": 0, "option": []},
        "adjustments": _adjustments(),
        "keyword_match_type_list": ["EXACT"],
        "keyword_text_list": {"不分类": [""]},
        "campaign_name_template": _name_template(brand),
        "ad_group_name_template": _name_template(brand) + [{"index": 6, "tag": "TAG_CUSTOM",
                                                            "value": "1"}],
        "profile_id": profile_id,
        "campaigns": campaigns,
    }


def create_ads(config, profile_id, payload):
    """提交创建（异步，响应 wait_minutes≈2）。返回响应 dict。"""
    headers = _ads_headers(config["COOKIES"], config["CSRF_TOKEN"], config["COMPANY_ID"],
                           PAGE_BUILD_SUPER, profile_id)
    return send_json(f"{ADS_HOST}/build/super/create/create", payload, headers, "创建广告")


# 活动/组预算与竞价（与 2026-09-07 run152641 抓包一致：组合2/自动2/手动BROAD2/放大4/精准6、默认竞价0.2）
DEFAULT_CREATE_RULES = {
    "portfolio_budget": 2,   # 广告组合总预算
    "auto_budget": 2,        # 自动基础盘
    "broad_budget": 2,       # 手动基础盘
    "scale_budget": 4,       # 放大盘
    "exact_budget": 6,       # 精准盘
    "default_bid": 0.2,      # 组默认竞价
}

# A2 首轮「无优化方案」时的重试等待秒数（A1 刚建的活动偶发查不到/未就绪，
# 等 60s 后重跑一次；第二次仍无优化方案才判「无优化方案」继续）
A2_RETRY_WAIT = 60


class _A1Skip(Exception):
    """A1 专用状态分支（原流程 SetDictionaryValue+Continue，不走 Catch）。

    status: 写回依赖字典的「广告状态」；detail: 进日志的详情。
    """
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _create_resp_looks_duplicate(resp):
    """创建响应疑似「重复规则」警告（原流程弹窗检测的 API 等价；响应结构待实测校准）。"""
    try:
        text = json.dumps(resp, ensure_ascii=False)
    except Exception:
        text = str(resp)
    return "重复" in text


def run_a1(config, deps):
    """A1 创建广告结构：循环依赖表逐个 ASIN 走 API 链路，状态语义与原流程一致。

    deps: 依赖表 dict 列表（字段同 Excel：ASIN/负责人/广告组合模板名称/店铺.../关键词1-50）
    状态写回 deps 项的「广告状态」（原流程 SetDictionaryKeyValue 等价），供 A2 过滤与 xlsx 写回：
      跳过(已创建/已完成) / 店铺名错误 / 没有MSKU / 没有匹配模板 / 已创建 / 广告创建错误
    匹配规则：广告组合按「模板名 in 组合名」模糊匹配取第一个命中
    （原流程 UI 下拉为模糊匹配，取首个候选）。
    """
    rules = DEFAULT_CREATE_RULES
    profile_map = get_profile_list(config["COOKIES"], config["CSRF_TOKEN"],
                                   config["COMPANY_ID"])
    results = []
    for idx, dep in enumerate(deps):
        asin = dep.get("ASIN", "")
        store = dep.get("店铺（带站点、与广告页面填写保持一致）注意括号全部为中文括号", "")
        log("INFO", f"A1[{idx + 1}/{len(deps)}] ASIN={asin} 店铺={store}")
        # 依赖字典为空 -> 跳过（原流程 A1 #6-8 Continue）
        if not dep:
            results.append({"ASIN": asin, "店铺": store, "状态": "跳过(依赖为空)"})
            continue
        # 广告状态预检（原流程 A1 #9-15：键缺失 ThrowError；已创建/已完成跳过；
        # 其余非空值 Raise->Catch=广告创建错误；仅空值(NaN)才进入创建）
        if "广告状态" not in dep:
            dep["广告状态"] = "广告创建错误"
            log("ERROR", f"ASIN={asin} 广告状态字段缺失")
            results.append({"ASIN": asin, "店铺": store, "状态": "广告创建错误"})
            continue
        status = dep["广告状态"]
        if status in ("已创建", "已完成"):
            log("INFO", f"ASIN={asin} 广告状态={status}，跳过 A1")
            results.append({"ASIN": asin, "店铺": store, "状态": f"跳过({status})"})
            continue
        if status != "NaN":
            dep["广告状态"] = "广告创建错误"
            log("ERROR", f"ASIN={asin} 【A1】广告状态异常，值={status}")
            results.append({"ASIN": asin, "店铺": store, "状态": "广告创建错误"})
            continue
        status = "已创建"
        try:
            profile_id = resolve_profile_id(store, profile_map)
        except Exception as e:
            # 原流程 A1 #20-23：shop_id 为空 -> 店铺名错误
            dep["广告状态"] = "店铺名错误"
            log("ERROR", f"ASIN={asin} {e}")
            results.append({"ASIN": asin, "店铺": store, "状态": "店铺名错误"})
            continue
        try:
            # ASIN -> SKU
            products = list_store_products(config, profile_id, asin)
            if not products:
                # 原流程 A1 #48-51：无 MSKU 提示可见 -> 没有MSKU
                raise _A1Skip("没有MSKU", "店铺产品中未找到 ASIN")
            sku = products[0]["sku"]
            # 资格检查（原流程无显式分支，页面无资格时提交会失败->Catch=广告创建错误；
            # 这里提前检查，最终状态语义一致）
            overall = check_eligibility(config, profile_id, sku, asin)
            if overall != "ELIGIBLE":
                raise RuntimeError(f"资格检查未通过: {overall}")
            # 广告组合：模板名模糊匹配组合名，取第一个命中
            template_name = dep.get("广告组合模板名称", "")
            portfolios = get_portfolios(config, profile_id)
            hit = next((p for p in portfolios
                        if template_name and template_name in str(p.get("name", ""))), None)
            if not hit:
                # 原流程 A1 #53-58：下拉选择 Catch -> 没有匹配模板
                raise _A1Skip("没有匹配模板", "广告组合未匹配")
            # 手动基础盘关键词取依赖表「关键词1-50」（清洗去重后的真实词，
            # 占位键名/NaN 已在 build_create_payload 内过滤）
            payload = build_create_payload(dep, sku, profile_id, hit["portfolio_id"],
                                           rules=rules)
            resp = create_ads(config, profile_id, payload)
            if (resp or {}).get("successful"):
                log("INFO", f"创建已受理（wait_minutes={(resp or {}).get('data', {}).get('wait_minutes')}）")
            elif _create_resp_looks_duplicate(resp):
                # 原流程 A1 #171-174：报错弹窗(重复规则) -> 仅警告，状态仍=已创建
                log("WARNING", f"A1重复规则警告：ASIN={asin}，店铺={store}，resp={resp}")
            else:
                raise RuntimeError(f"创建提交失败: {resp}")
        except _A1Skip as e:
            # 专用状态分支：写状态+Continue（原流程不走 Catch）
            dep["广告状态"] = e.status
            log("ERROR", f"A1 ASIN={asin} {e.detail}")
            results.append({"ASIN": asin, "店铺": store, "状态": e.status})
            continue
        except Exception as e:
            # 原流程 A1 #178-180：Catch -> 状态纯文案「广告创建错误」，详情仅进日志
            status = "广告创建错误"
            log("ERROR", f"A1 ASIN={asin} {e}")
        dep["广告状态"] = status
        results.append({"ASIN": asin, "店铺": store, "状态": status})
    return results


# =============================================================================
# A2：广告优化（绑规则）
# =============================================================================
def _rule_pro_headers(config, profile_id):
    """rule_pro 接口用 form 表单提交，其余与 _ads_headers 一致（走它避免参数顺序踩坑）。"""
    headers = _ads_headers(config["COOKIES"], config["CSRF_TOKEN"], config["COMPANY_ID"],
                           PAGE_RULE_PRO, profile_id)
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    return headers


# 优化方案（规则组）缓存：列表内嵌在规则页 HTML 的 `var ruleGroupList = [...]` 里，
# 没有独立接口（各种 rule_group_list 猜测路径都返回统一的 403）。按 profile 缓存，避免每行重下 360KB 页面。
_RULE_GROUP_CACHE = {}
RULE_PAGE_PATH = "/ad_report/rule_pro/index/objects"
# 按活动名匹配优化方案（老流程 A2 的 If/ElseIf：广告名称文本 Contains 基础盘/放大盘/精准盘）
PLAN_KEYWORDS = ("精准盘", "放大盘", "基础盘")


def _extract_rule_group_list(html):
    """从规则页 HTML 解析 ruleGroupList（JS 数组字面量）。"""
    idx = html.find("ruleGroupList")
    if idx == -1:
        return []
    start = html.find("[", idx)
    if start == -1:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(html[start:])
    except Exception:
        log("WARNING", "规则页 ruleGroupList 解析失败")
        return []
    return [g for g in data if isinstance(g, dict) and g.get("uuid")]


def get_rule_groups(config, profile_id):
    """取该店铺可选的优化方案（规则组）列表。"""
    key = str(profile_id)
    if key in _RULE_GROUP_CACHE:
        return _RULE_GROUP_CACHE[key]
    url = f"{ADS_HOST}{RULE_PAGE_PATH}?profile_id={profile_id}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Cookie": _cookie_header(config["COOKIES"]),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        html = resp.read().decode("utf-8", "replace")
    groups = _extract_rule_group_list(html)
    _RULE_GROUP_CACHE[key] = groups
    log("INFO", f"优化方案缓存: profile={profile_id} 共 {len(groups)} 个"
        + ("" if not groups else " -> " + "、".join(str(g.get("name")) for g in groups)))
    return groups


# 移词映射目标盘：老流程 A2 在「去设置」弹窗里按当前活动的盘选目标活动
MOVE_TARGET_PLAN = {"基础盘": "放大盘", "放大盘": "精准盘"}


def build_move_targets(campaign_name, rows, blocked_items):
    """按当前活动的盘选移词目标活动，返回 (targets, 目标活动行)。

    targets 每项 {campaign_id, ad_group_id, is_asin, match_type}；
    match_type 取预检返回的 data（如 ["broad"] / ["exact"]），不再写死。
    """
    name = str(campaign_name or "")
    cur_kw = next((k for k in PLAN_KEYWORDS if k in name), None)
    target_kw = MOVE_TARGET_PLAN.get(cur_kw)
    if not target_kw:
        raise RuntimeError(f"缺少移词映射，但活动「{name}」无法推导目标盘"
                           f"（仅支持 基础盘->放大盘->精准盘）")
    trow = next((r for r in rows if target_kw in str(r.get("name") or "")), None)
    if not trow:
        raise RuntimeError(f"缺少移词映射，且未找到同 ASIN 的「{target_kw}」活动作为目标")
    t_ags = trow.get("ad_groups") or []
    if not t_ags:
        raise RuntimeError(f"移词目标活动「{trow.get('name')}」无广告组")
    targets = []
    for it in blocked_items:
        for mt in (it.get("data") or ["exact"]):
            targets.append({"campaign_id": trow["campaign_id"],
                            "ad_group_id": t_ags[0]["ad_group_id"],
                            "is_asin": 1 if str(mt).lower().startswith("asin") else 0,
                            "match_type": mt})
    return targets, trow


def pick_rule_group(groups, campaign_name, targeting_type):
    """按活动名里的「X盘」+ 投放类型（auto/manual）选优化方案。

    老流程等价：If 广告名称文本 Contains 基础盘 -> 输入「基础盘」到优化方案框，
    ElseIf 放大盘 / 精准盘。同一个盘可能有自动/手动两个方案，按活动的 targeting_type 区分。
    """
    name = str(campaign_name or "")
    kw = next((k for k in PLAN_KEYWORDS if k in name), None)
    if not kw:
        return None
    cands = [g for g in groups if kw in str(g.get("name"))]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    is_auto = str(targeting_type or "").lower() == "auto"
    for g in cands:
        if (str(g.get("targeting_type") or "").lower() == "auto") == is_auto:
            return g
    return cands[0]


def get_campaign_ad_groups(config, profile_id, campaign_name):
    """按活动名（ASIN）查活动+广告组+规则组。"""
    form = {"draw": 2,
            "columns": [{"data": "name", "name": "", "searchable": "true",
                         "orderable": "false", "search": {"value": "", "regex": "false"}}],
            "start": 0, "length": 25, "search": {"value": "", "regex": "false"},
            "profile_id": profile_id, "campaign_name": campaign_name,
            "state": 1, "page": 1}
    resp = send_form(f"{ADS_HOST}/ad_report/rule_pro/index/campaign_ad_groups", form,
                     _rule_pro_headers(config, profile_id), "活动广告组查询")
    return (resp or {}).get("data", []) or []


def pre_batch_set_rules(config, profile_id, objects):
    """绑规则预检。objects: [{sponsored_type, campaign_id, ad_group_id, rule_group_uuid}]。"""
    form = {"profile_id": profile_id, "objects": objects}
    return send_form(f"{ADS_HOST}/ad_report/rule_pro/index/pre_batch_set_multi_rule_to_multi_object",
                     form, _rule_pro_headers(config, profile_id), "绑规则预检")


def set_moving_relation(config, profile_id, targets, sponsored_type, campaign_id, ad_group_id):
    """设置移词映射关系（预检 1001「请先设置移词映射关系」后的前置）。

    targets: [{campaign_id, ad_group_id, is_asin, match_type}]（词来源对象）
    """
    form = {"profile_id": profile_id, "targets": targets,
            "sponsored_type": sponsored_type, "campaign_id": campaign_id,
            "ad_group_id": ad_group_id}
    return send_form(f"{ADS_HOST}/ad_report/rule_pro/index/set_moving_relation", form,
                     _rule_pro_headers(config, profile_id), "设置移词映射")


def batch_set_rules(config, profile_id, objects):
    """批量绑定规则。objects 结构同 pre_batch（不含 ad_group_id 时按 campaign 维度）。"""
    form = {"profile_id": profile_id, "objects": objects}
    return send_form(f"{ADS_HOST}/ad_report/rule_pro/index/batch_set_multi_rule_to_multi_object",
                     form, _rule_pro_headers(config, profile_id), "批量绑定规则")


def _a2_optimize_dep(config, dep, profile_map):
    """单个依赖项的 A2 优化（run_a2 调用；无优化方案时由 run_a2 睡 60s 重试一次）。

    返回成功绑定的活动行数 ok_count；行级异常上抛，由 run_a2 统一转「广告优化错误」。
    """
    asin = dep.get("ASIN", "")
    store = dep.get("店铺（带站点、与广告页面填写保持一致）注意括号全部为中文括号", "")
    profile_id = resolve_profile_id(store, profile_map)
    rows = get_campaign_ad_groups(config, profile_id, asin)
    groups = get_rule_groups(config, profile_id)
    ok_count = 0
    for row in rows:
        # 一行（活动）一个处理单元；单行异常中断整项（原流程 Try 包住整个循环）
        cname = row.get("name") or ""
        if row.get("optimization_rule_id"):
            # 页面该行的「应用」按钮是 disabled（已启用成本控制），照 UI 跳过
            log("INFO", f"ASIN={asin} 活动「{cname}」已启用成本控制，跳过")
            continue
        rg = pick_rule_group(groups, cname, row.get("targeting_type"))
        if not rg:
            log("WARNING", f"ASIN={asin} 活动「{cname}」未匹配到优化方案，跳过")
            continue
        uuid = str(rg.get("uuid"))
        bound = {str((b or {}).get("uuid") or (b or {}).get("rule_group_uuid"))
                 for b in (row.get("rule_groups") or [])}
        if uuid in bound:
            log("INFO", f"ASIN={asin} 活动「{cname}」已绑定「{rg.get('name')}」，跳过")
            continue
        objects = [{"sponsored_type": row.get("sponsored_type", "sp"),
                    "campaign_id": row["campaign_id"],
                    "ad_group_id": ag["ad_group_id"],
                    "rule_group_uuid": uuid}
                   for ag in (row.get("ad_groups") or [])]
        if not objects:
            log("WARNING", f"ASIN={asin} 活动「{cname}」无广告组，跳过")
            continue
        log("INFO", f"ASIN={asin} 活动「{cname}」-> 优化方案「{rg.get('name')}」")
        pre = pre_batch_set_rules(config, profile_id, objects)
        items = (pre or {}).get("data") or []
        blocked = [it for it in items
                   if isinstance(it, dict) and (it.get("errors") or {}).get("code") == 1001]
        if blocked:
            # 1001：缺移词映射。老流程点「去设置」后，按当前活动的盘选目标活动：
            #   基础盘 -> 放大盘活动；放大盘 -> 精准盘活动（同一 ASIN），广告组取目标活动第一个
            targets, trow = build_move_targets(cname, rows, blocked)
            log("INFO", f"ASIN={asin} 设置移词映射: 「{cname}」-> "
                        f"「{trow.get('name')}」({[t['match_type'] for t in targets]})")
            done = set()
            for it in blocked:
                ck = (it.get("campaign_id"), it.get("ad_group_id"))
                if ck in done:
                    continue
                done.add(ck)
                set_moving_relation(config, profile_id, targets,
                                    it.get("sponsored_type") or "sp",
                                    it.get("campaign_id"), it.get("ad_group_id"))
            pre = pre_batch_set_rules(config, profile_id, objects)
            items = (pre or {}).get("data") or []
        still_err = [it for it in items
                     if isinstance(it, dict) and it.get("errors")]
        if still_err:
            raise RuntimeError(f"预检仍有错误: {still_err[:2]}")
        batch = batch_set_rules(config, profile_id, objects)
        if not (batch or {}).get("successful"):
            raise RuntimeError(f"批量绑定失败: {batch}")
        ok_count += 1
    return ok_count


def run_a2(config, deps):
    """A2 广告优化：对「广告状态=已创建」的依赖项绑定优化规则，语义与原流程一致。

    对每个依赖项（原流程 A2 #5-14 过滤：空/无状态键/状态!=已创建 一律跳过，
    已完成跳过；仅「已创建」进入处理）：
      1. _a2_optimize_dep：campaign_ad_groups(ASIN) 拿活动行 -> 逐行 pre_batch 预检
         （1001 缺移词映射时 set_moving_relation 后重试）-> batch_set 绑定 -> 行计数 +1
      2. 首轮成功行数<=0（无优化方案）时：等待 A2_RETRY_WAIT 秒重试一次，
         第二次仍<=0 才判「无优化方案」继续（A1 刚建的活动偶发查不到/未就绪）
    收尾计数判定（原流程 #87-93）：成功行数<=0 无优化方案 / <4 部分完成 / >=4 已完成。
    状态写回 deps 项的「广告状态」，供 xlsx 写回与成功判定。
    """
    profile_map = get_profile_list(config["COOKIES"], config["CSRF_TOKEN"],
                                   config["COMPANY_ID"])
    results = []
    for idx, dep in enumerate(deps):
        asin = dep.get("ASIN", "")
        store = dep.get("店铺（带站点、与广告页面填写保持一致）注意括号全部为中文括号", "")
        log("INFO", f"A2[{idx + 1}/{len(deps)}] ASIN={asin}")
        if not dep:
            results.append({"ASIN": asin, "店铺": store, "状态": "跳过(依赖为空)"})
            continue
        dep_status = dep.get("广告状态")
        if dep_status == "已完成":
            log("INFO", f"ASIN={asin} 广告状态=已完成，跳过 A2")
            results.append({"ASIN": asin, "店铺": store, "状态": "跳过(已完成)"})
            continue
        if dep_status != "已创建":
            # 原流程 #12-14：非「已创建」一律 Continue（键缺失时状态=None 同样跳过）
            log("INFO", f"ASIN={asin} 广告状态={dep_status!r}，跳过 A2")
            results.append({"ASIN": asin, "店铺": store,
                            "状态": f"跳过({dep_status or '无状态'})"})
            continue
        status = "已完成"
        try:
            ok_count = _a2_optimize_dep(config, dep, profile_map)
            if ok_count <= 0:
                log("WARNING", f"ASIN={asin} 无优化方案，等待 {A2_RETRY_WAIT}s 后重试")
                time.sleep(A2_RETRY_WAIT)
                ok_count = _a2_optimize_dep(config, dep, profile_map)
            # 收尾计数判定（原流程 #87-93）
            if ok_count <= 0:
                status = "无优化方案"
            elif ok_count < 4:
                status = "部分完成"
            else:
                status = "已完成"
        except Exception as e:
            # 原流程 #94-96：Catch -> 状态纯文案「广告优化错误」，详情仅进日志
            status = "广告优化错误"
            log("ERROR", f"A2 ASIN={asin} {e}")
        dep["广告状态"] = status
        results.append({"ASIN": asin, "店铺": store, "状态": status})
    return results


# =============================================================================
# 文件层：waiting/ 取件 -> finished/ 归档 -> 读依赖表 -> 写回状态
# =============================================================================
XLSX_NAME_RE = re.compile(r"^\[([^\]]+)\](\d{14})-(.+)$")


def pick_oldest_xlsx():
    """waiting/ 取文件名时间戳最早的 xlsx（lark_link 下载命名 [{open_id}]YYYYMMDDHHMMSS-原名.xlsx）。

    waiting/ 不存在时自动创建；无待处理文件返回 None。
    不符合命名约定的 xlsx 排在符合约定的文件之后（按名称排序，仍会被处理）。
    """
    os.makedirs(WAITING_DIR, exist_ok=True)
    candidates = []
    for name in os.listdir(WAITING_DIR):
        path = os.path.join(WAITING_DIR, name)
        if not name.lower().endswith(".xlsx") or not os.path.isfile(path):
            continue
        m = XLSX_NAME_RE.match(name)
        candidates.append((0, m.group(2), name) if m else (1, "", name))
    if not candidates:
        log("INFO", f"waiting/ 无待处理文件: {WAITING_DIR}")
        return None
    candidates.sort()
    return os.path.join(WAITING_DIR, candidates[0][2])


def parse_xlsx_name(name):
    """解析 lark_link 下载命名，返回 (open_id, 原file名)。

    与原流程 get_open_id / extract_between 语义一致：
    open_id 取第一个 [] 内内容；原file名取第一个 '-' 后的全部内容（含扩展名）。

    重跑失败文件时文件名可能带 [错误] 前缀（mark_error_xlsx 加的），先剥掉，
    否则第一个 [] 会解析成 "错误" 导致通知发不出去。
    """
    if name.startswith("[错误]"):
        name = name[len("[错误]"):]
    m = XLSX_NAME_RE.match(name)
    if m:
        return m.group(1), m.group(3)
    open_id = None
    if "[" in name and "]" in name:
        start, end = name.find("["), name.find("]")
        if start != -1 and end > start:
            open_id = name[start + 1:end]
    first_dash = name.find("-")
    return open_id, (name[first_dash + 1:] if first_dash != -1 else name)


def archive_xlsx(path):
    """文件移动到 finished/（原文件预处理 MoveFile：取到即归档，读取/写回均在归档目录进行）。

    同名冲突覆盖（原流程 FileDuplicatedStrategy=Overwrite）。返回新路径。
    """
    os.makedirs(FINISHED_DIR, exist_ok=True)
    target = os.path.join(FINISHED_DIR, os.path.basename(path))
    if os.path.exists(target):
        os.replace(path, target)
    else:
        shutil.move(path, target)
    return target


def mark_error_xlsx(path):
    """改名 [错误]<原文件名>（原主流程 Catch / A4 错误分支等价）。

    已带 [错误] 前缀时不重复改；同名冲突覆盖（原流程为 Terminate，这里改覆盖更稳）。
    """
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    if name.startswith("[错误]"):
        return path
    target = os.path.join(parent, f"[错误]{name}")
    if os.path.exists(target):
        os.replace(path, target)
    else:
        os.rename(path, target)
    return target


def read_deps_xlsx(path):
    """读依赖表 xlsx -> dict 列表（语义对齐原流程 read_excel_to_dict + A0 关键词清洗）。

    1. pandas 读第一个 sheet -> records
    2. 所有 NaN/None -> "NaN" 字符串（保证「广告状态」等键可直接比较）
    3. 关键词值清洗（原 A0[17]）：re.sub(r'[^\\w\\s.]|_', '', v)，仅处理 str
    4. 关键词去重（原 A0[17]）：重复值改为各自键名（占位值由创建 payload 过滤）
    """
    import pandas as pd
    deps = pd.read_excel(path, sheet_name=0).to_dict("records")
    cleaned = []
    for item in deps:
        if not isinstance(item, dict):
            continue
        fixed = {}
        for k, v in item.items():
            if v is None or (isinstance(v, float) and v != v):
                v = "NaN"
            fixed[k] = v
        key_list = [k for k in fixed if "关键词" in str(k)]
        for k in key_list:
            if isinstance(fixed[k], str):
                fixed[k] = re.sub(r"[^\w\s.]|_", "", fixed[k])
        seen = set()
        for k in key_list:
            val = fixed[k]
            if val in seen:
                fixed[k] = k
            else:
                seen.add(val)
        cleaned.append(fixed)
    return cleaned


def write_status_xlsx(path, deps):
    """把 deps 各项「广告状态」写回 xlsx 并保存（原 A4：openpyxl 表头定位，行=i+2）。"""
    from openpyxl import load_workbook
    wb = load_workbook(path)
    ws = wb.active
    status_col = None
    for cell in ws[1]:
        if cell.value is not None and str(cell.value).strip() == "广告状态":
            status_col = cell.column
            break
    if not status_col:
        wb.close()
        raise RuntimeError(f"依赖表未找到[广告状态]表头列: {path}")
    for i, dep in enumerate(deps):
        ws.cell(row=i + 2, column=status_col, value=dep.get("广告状态"))
    wb.save(path)
    wb.close()
    log("INFO", f"广告状态已写回: {path}")


def _all_done(deps):
    """原 A4 判定：所有 dep 广告状态==已完成（空列表视为成功）。"""
    for dep in deps:
        if dep.get("广告状态") != "已完成":
            return False
    return True


# =============================================================================
# 飞书层：反馈（凭据与 sx_ads_lark_link 同应用，open_id 才能匹配）
# =============================================================================
def _feishu_token(app_id, app_secret):
    """获取 tenant_access_token（原 GetLarkAccessToken 等价）。"""
    payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(
        f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal",
        data=payload, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
    return data["tenant_access_token"]


def send_text_msg(token, open_id, text):
    """发文本消息给 open_id（原 发送消息[文本] 等价）。"""
    payload = {"receive_id": open_id, "msg_type": "text",
               "content": json.dumps({"text": text}, ensure_ascii=False)}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"{FEISHU_HOST}/open-apis/im/v1/messages?receive_id_type=open_id"
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"发送文本消息失败: {data}")
    return data


def upload_xlsx_msg(token, file_path):
    """上传 xlsx 到飞书消息文件接口，返回 file_key（原 上传文件 等价，file_type=xls）。

    坑：file_type / file_name 必须放在 multipart 表单字段里，放 URL query 会 400；
    file 字段名固定为 file。失败时把飞书返回体带进异常，便于定位（code 234001 等）。
    """
    boundary = uuid.uuid4().hex
    file_name = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    def field(name, value):
        return (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n').encode("utf-8") \
            + value.encode("utf-8") + b"\r\n"

    file_part = (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8") \
        + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = field("file_type", "xls") + field("file_name", file_name) + file_part

    req = urllib.request.Request(f"{FEISHU_HOST}/open-apis/im/v1/files", data=body,
                                 headers={
                                     "Content-Type": f"multipart/form-data; boundary={boundary}",
                                     "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"上传文件失败 HTTP {e.code}: {detail}")
    if data.get("code") != 0:
        raise RuntimeError(f"上传文件失败: {data}")
    return data["data"]["file_key"]


def send_file_msg(token, open_id, file_key):
    """发文件消息给 open_id（原 发送消息[文件] 等价）。"""
    payload = {"receive_id": open_id, "msg_type": "file",
               "content": json.dumps({"file_key": file_key})}
    body = json.dumps(payload).encode("utf-8")
    url = f"{FEISHU_HOST}/open-apis/im/v1/messages?receive_id_type=open_id"
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"发送文件消息失败: {data}")
    return data


# =============================================================================
# 文件流编排：等价老主流程 Try-Catch + 文件预处理 + A0[17] + A1 + A2 + A4
# （A3 多维表格填结果已淘汰，不实现）
# =============================================================================
def _feishu_recipients(config, open_id):
    """反馈接收者列表：发件人 open_id 优先；解析不到（手动放文件、重跑归档件）时落飞书群兜底。

    返回 [(id_type, receive_id), ...]；两者皆无返回空列表（调用方记日志后跳过通知）。
    与老流程一致：消息发给发件人本人；飞书群是兜底，避免任何来源的文件都静默无反馈。
    """
    recv = []
    if open_id:
        recv.append(("open_id", open_id))
    else:
        chat_id = config.get("FEISHU_CHAT_ID")
        if chat_id:
            recv.append(("chat_id", chat_id))
    return recv


def _notify_text(token, recipients, text):
    """向全部接收者发文本通知；单个失败只记日志，绝不中断主流程。"""
    if not recipients:
        log("WARNING", f"无反馈接收者，跳过文本通知: {text[:60]}...")
        return False
    sent = False
    for id_type, recv_id in recipients:
        try:
            send_text_msg(token, recv_id, text)
            sent = True
        except Exception as e:
            log("ERROR", f"飞书文本通知失败（{id_type}={recv_id}）: {e}")
    return sent


def run_file_flow(config):
    """waiting/ 取件 -> finished/ 归档 -> 读依赖表 -> A1/A2 -> 写回 -> 飞书反馈。"""
    app_id = config.get("FEISHU_APP_ID")
    app_secret = config.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("config 缺少 FEISHU_APP_ID/FEISHU_APP_SECRET（反馈凭据，"
                           "须与 sx_ads_lark_link 同一飞书应用）")
    app_name = config.get("APP_NAME") or "甲组1_广告_接口版"

    src = pick_oldest_xlsx()
    if not src:
        return
    file_name = os.path.basename(src)
    open_id, orig_name = parse_xlsx_name(file_name)
    log("INFO", f"取件: {file_name} (open_id={open_id}, 原file名={orig_name})")

    # 反馈接收者：文件发件人 open_id 优先，解析不到（手动放文件等）落群兜底
    recipients = _feishu_recipients(config, open_id)
    for id_type, recv_id in recipients:
        log("INFO", f"飞书反馈接收者: {id_type}={recv_id}")
    if not recipients:
        log("WARNING", "文件无 open_id 前缀且 config 未配 FEISHU_CHAT_ID，本次无飞书反馈")

    file_path = archive_xlsx(src)
    deps = read_deps_xlsx(file_path)
    log("INFO", f"依赖表 {len(deps)} 行")

    token = _feishu_token(app_id, app_secret)
    _notify_text(token, recipients, f"【{app_name}】【{orig_name}】开始处理。")

    try:
        res = run_a1(config, deps)
        log("INFO", f"A1 结果: {json.dumps(res, ensure_ascii=False)}")
        res = run_a2(config, deps)
        log("INFO", f"A2 结果: {json.dumps(res, ensure_ascii=False)}")

        write_status_xlsx(file_path, deps)
        if _all_done(deps):
            log("INFO", f"【{orig_name}】全部成功执行！")
            _notify_text(token, recipients, f"【{app_name}】【{orig_name}】全部成功执行！")
        else:
            log("ERROR", f"【{orig_name}】部分错误，详情查看文件[广告状态]列！")
            file_path = mark_error_xlsx(file_path)
            # 到这里广告操作与状态写回都已完成，反馈失败只记日志，不回滚、不中断
            file_key = None
            try:
                file_key = upload_xlsx_msg(token, file_path)
            except Exception as up_err:
                log("ERROR", f"上传结果文件失败（反馈降级为纯文本）: {up_err}")
            _notify_text(token, recipients,
                         f"【{app_name}】【{orig_name}】部分错误，详情查看文件[广告状态]列！")
            if file_key:
                for id_type, recv_id in recipients:
                    try:
                        send_file_msg(token, recv_id, file_key)
                    except Exception as send_err:
                        log("ERROR", f"发送结果文件失败（{id_type}={recv_id}）: {send_err}")
    except Exception as e:
        # 等价主流程 Catch：尽力改名 [错误] + 异常中断通知（通知失败仅记日志）后原样上抛
        log("ERROR", f"文件流异常: {e}")
        try:
            if os.path.exists(file_path):
                file_path = mark_error_xlsx(file_path)
        except Exception as rename_err:
            log("ERROR", f"[错误]改名失败: {rename_err}")
        _notify_text(token, recipients,
                     f"【{app_name}】【{orig_name}】异常中断，建议重新发送文件！"
                     f"错误信息：{e}")
        raise


# =============================================================================
# 入口
# =============================================================================
def main(task, config):
    """RPA 流程调用入口。

    task:   固定 "all"（文件流：waiting 取件 -> A1/A2 -> 写回 -> 飞书反馈）
    config: default_config(...) 产物（COOKIES/CSRF_TOKEN/COMPANY_ID/APP_NAME/FEISHU_APP_*）
    """
    if config.get("CSRF_TOKEN") is None:
        config["CSRF_TOKEN"] = get_csrf_token(config["COOKIES"], config["COMPANY_ID"])
    if task != "all":
        log("WARNING", f"task={task} 非 all，按 all 处理")
    run_file_flow(config)


# if __name__ == "__main__":
#     # 本地调试入口（RPA 流程内保持整段注释，不执行）：
#     # 取消注释后运行 python sx_ads_api.py，走真实提交链路（谨慎）
#     run_file_flow(load_config(CONFIG_FILE))
