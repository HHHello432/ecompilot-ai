# -*- coding: utf-8 -*-
"""
领星ERP → 飞书在线表格 每日数据自动同步脚本

整体流程：
  [0] 直接用配置区传入的浏览器 Cookie 构造领星接口请求头
      （不再使用 Selenium 登录；Cookie 过期后需手动更新）；
  [1] 调用「产品表现-ASIN列表」接口，分页拉取目标日期全部数据；
      随后调用「全局标签-关联分析」接口，按 seller_sku 批量查询 listing 标签；
  [2] 按负责人清洗拆分，写入飞书表格【每日数据导入（X）】页
      （该页 1500 行以下有其他内容，操作严格限制在 1500 行以内）；
  [3] 读取【产品每日情况（X）】页顶部固定区块 A1:R7，
      追加写入该页最后一行往下 2 行的位置（相当于空一行）；
  [4] 取区块第 7 行的 C7:R7，写入【汇总总和（X）】页对应日期行
      （第 1 列为日期，从第 2 列开始写）。

依赖安装：
  pip install requests
  （已移除 Selenium 依赖，无需安装 selenium / webdriver）
运行方式：
  填好下方配置区后，直接执行 python main.py
"""

import json
import os
import re
import time
import uuid
import datetime
import urllib.parse

import requests

# ============================================================
# 配置区（★ 运行前按需填写）
# ============================================================

# 敏感配置统一放在脚本同目录的 config.json（已被 .gitignore 排除，不入库）：
#   领星 Cookie / User-Agent、飞书应用凭证、表格 token、人员与 UID 清单。
# 首次使用：复制 config.example.json 为 config.json 并填写真实值。

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config(path=CONFIG_PATH):
    """读取 config.json；文件缺失时给出可操作的提示"""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"缺少配置文件：{path}\n"
            "请复制 config.example.json 为 config.json，并填入真实的领星 Cookie 与飞书凭证。")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


_CONFIG = load_config()

# ---------- 领星 Cookie / User-Agent ----------
# Cookie 从 config.json 的 lingxing.cookie 读取，支持两种形式：
#   - 浏览器扩展（EditThisCookie）导出的数组：[{"Name": "...", "Value": "..."}, ...]
#   - 浏览器 Network 面板复制的 Cookie 字符串："auth-token=xxxx; company_id=xxxx; ..."
# Cookie 会过期，失效时更新 config.json 即可。
USER_AGENT = _CONFIG["lingxing"]["user_agent"]

# ---------- 人员配置 ----------
# key   = 姓氏（用于拼接飞书页签名，如 每日数据导入（洪））
# value = 负责人全名（用于和接口返回的 principal_names 精确匹配）
#
# ★ 以后加人只需两步：
#   1. 在 config.json 的 persons 中加一行 "姓": "全名"
#   2. 把该负责人的 principal_uid 加入 principal_uids
#   （前提：飞书里已存在对应命名的三个页签）
PERSONS = _CONFIG["persons"]

# 领星接口查询用的负责人 UID 列表（对应 PERSONS 中的人员）
PRINCIPAL_UIDS = _CONFIG["principal_uids"]

# ---------- 领星接口 ----------
# 产品表现-ASIN列表 接口地址（一般不用改）
LX_API_URL = "https://gw.lingxingerp.com/bd/productPerformance/asinLists"

# listing 标签查询接口（全局标签-关联分析，一般不用改）
TAG_API_URL = ("https://gw.lingxingerp.com/global-tag/global/tag/"
               "relation/analysis?id=productExpressionNew")

# 标签接口每批查询的产品数（领星网页端每批 20 条，这里放宽到 100；
# 若报请求过大相关错误，改回 20）
TAG_BATCH_SIZE = 100

# ---------- 飞书开放平台 ----------
# 企业自建应用凭证（https://open.feishu.cn 创建），配置在 config.json 的 feishu 段。
# 要求：应用已开通电子表格读写权限，且已添加为目标表格的「可编辑」协作者
FEISHU_APP_ID = _CONFIG["feishu"]["app_id"]
FEISHU_APP_SECRET = _CONFIG["feishu"]["app_secret"]

# 表格 token：表格链接 https://xxx.feishu.cn/sheets/<这一段>
SPREADSHEET_TOKEN = _CONFIG["feishu"]["spreadsheet_token"]

# ---------- 其他 ----------
# 同步日期：
#   当前时间在 15:00 之前 → 抓取昨天的数据
#   当前时间在 15:00 及之后 → 抓取今天的数据
# _SWITCH_HOUR = 15  # 切换时点（24小时制）
# if datetime.datetime.now().hour < _SWITCH_HOUR:
#     TARGET_DATE = datetime.date.today() - datetime.timedelta(days=1)
# else:
#     TARGET_DATE = datetime.date.today()

# 固定前一天
TARGET_DATE = datetime.date.today() - datetime.timedelta(days=1)

# 领星接口单页拉取条数
PAGE_SIZE = 200

# 【每日数据导入】的数据行数硬上限
# 注意：该页第 1500 行以下存在其他内容，清空和写入都绝不能超出第 1500 行
IMPORT_MAX_ROWS = 1500


def sheet_names(surname):
    """由姓氏生成该人员的三个飞书页签名"""
    return {
        "import": f"每日数据导入（{surname}）",
        "daily": f"产品每日情况（{surname}）",
        "sum": f"汇总总和（{surname}）",
    }

# ============================================================
# 第 0 步：解析 Cookie 字符串 → 构造请求头
# ============================================================

def parse_cookie_string(raw):
    """把浏览器复制来的 Cookie 请求头（name1=val1; name2=val2; ...）
    解析成 [{name, value}, ...] 结构，供 build_headers_from_cookies 使用。

    规则：按分号切分，每条按第一个 '=' 拆成 name/value；value 原样保留
    （auth-token 的 URL 编码形态不要提前解码，解码放在构造请求头时统一处理）。"""
    out = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, value = part.split("=", 1)
            out.append({"name": name.strip(), "value": value.strip()})
        else:
            out.append({"name": part, "value": ""})
    return out


def normalize_cookie_list(raw):
    """把浏览器导出的 Cookie 统一成 build_headers_from_cookies 需要的 [{name, value}, ...]。

    支持两种输入：
      - 字符串形式："name=val; name2=val2; ..."   → 按分号解析（parse_cookie_string）
      - 数组形式（EditThisCookie 等扩展导出）：
            [{"Name": "...", "Value": "..."}, ...]  → 取 Name/Value（兼容小写 name/value）
    其余字段（Domain / Path / HttpOnly 等）一律忽略，只关心键值。"""
    # 字符串形式
    if isinstance(raw, str):
        return parse_cookie_string(raw)
    # 数组形式
    out = []
    for c in (raw or []):
        if not isinstance(c, dict):
            continue
        name = c.get("name") if "name" in c else c.get("Name")
        value = c.get("value") if "value" in c else c.get("Value")
        if not name:                      # 跳过没有名字的脏项
            continue
        out.append({"name": str(name),
                    "value": "" if value is None else str(value)})
    return out


def build_headers_from_cookies(cookie_list, user_agent):
    """从 Cookie 动态构造领星接口请求头。

    入参 cookie_list 为 parse_cookie_string 解析出的 [{name, value}, ...]。
    除 Cookie 串外，还要从中解析出 auth-token / company_id /
    env_key / uid / zid 五个字段，填到对应的 X-AK 请求头里。"""
    cookie_pairs = []
    auth_token_encoded = company_id = env_key = uid = zid = None

    for c in cookie_list:
        name = c.get("name", "")
        value = c.get("value", "")
        if not name:
            continue
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

    # 必要字段校验，缺了就直接抛错，方便定位问题
    if not auth_token_encoded:
        raise ValueError("Cookie 中未找到 auth-token，请确认登录态是否有效，"
                         "或检查 token 是否存放在 localStorage")
    missing = [k for k, v in {"company_id": company_id, "env_key": env_key,
                              "uid": uid, "zid": zid}.items() if not v]
    if missing:
        raise ValueError(f"Cookie 中缺少必要字段: {missing}")

    # Cookie 中的 auth-token 经过 URL 编码，需解码后使用
    auth_token = urllib.parse.unquote(auth_token_encoded)

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
        "User-Agent": user_agent,
        "X-AK-Company-Id": company_id,
        "X-AK-ENV-KEY": env_key,
        "X-AK-Language": "zh",
        "X-AK-PLATFORM": "1",
        "X-AK-Request-Id": str(uuid.uuid4()),   # 每次请求随机生成
        "X-AK-Request-Source": "erp",
        "X-AK-Uid": uid,
        "X-AK-Version": "3.8.4.3.0.009",
        "X-AK-Zid": zid,
        "auth-token": auth_token,
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Cookie": "; ".join(cookie_pairs),
    }

# ============================================================
# 第 1 步：HTTP 请求领星数据（产品表现 + listing 标签）
# ============================================================

def build_payload(offset, length, date_str):
    """构造「产品表现-ASIN列表」接口的请求体。
    结构与浏览器实际请求保持一致，offset/length 用于分页。"""
    is_today = TARGET_DATE == datetime.date.today()
    return {
        "sort_field": "volume", "sort_type": "desc",   # 按销量降序
        "offset": offset, "length": length,
        "search_field": "asin", "search_value": [],
        "mids": "1", "sids": "",
        "date_type": "purchase",
        "start_date": date_str, "end_date": date_str,
        "principal_uids": PRINCIPAL_UIDS,
        "bids": [], "cids": [], "extend_search": [],
        "summary_field": "asin", "purchase_status": 0,
        "currency_code": "CNY", "product_states": [],
        "is_resale": "", "order_types": [], "promotions": [],
        "developers": [], "delivery_methods": [],
        "is_recently_enum": True, "ad_cost_type": "",
        "attr_value_ids": [], "turn_on_summary": 1,
        "summary_field_level1": "asin", "summary_field_level2": "",
        "owner_uids": [], "gtag_ids": [], "auto_tags": [], "regions": [],
        "date_range_type": 0,
        "only_query_today": is_today,
        "today_hour": datetime.datetime.now().strftime("%H"),
        "query_order_profit": True,
        "req_time_sequence": "/bd/productPerformance/asinLists",
    }


def fetch_lingxing_data(headers, date_str):
    """分页拉取当日全部 ASIN 数据，直到拿满 total 条为止"""
    session = requests.Session()
    items, offset = [], 0
    while True:
        resp = session.post(LX_API_URL, headers=headers,
                            json=build_payload(offset, PAGE_SIZE, date_str),
                            timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 1:
            raise RuntimeError(f"领星接口返回异常（登录态可能已失效）: "
                               f"{body.get('msg') or body}")
        data = body["data"]
        total = data.get("total", 0)
        batch = data.get("list") or []
        items.extend(batch)
        print(f"  已拉取 {len(items)}/{total} 条")
        if len(items) >= total or not batch:
            break
        offset += PAGE_SIZE
        time.sleep(0.5)  # 轻微限速，避免触发接口限流
    return items


def item_seller_identity(item):
    """从一条产品表现数据中提取 (seller_sku, sid)。

    优先取 price_list[0]（标签接口的 relation_id 就是 seller_sku）；
    取不到时兜底解析 msku_sid_str（格式：seller_sku$|$sid$|$asin$|$...）。"""
    price_list = item.get("price_list") or []
    if price_list:
        sku = price_list[0].get("seller_sku")
        sid = price_list[0].get("sid")
        if sku:
            return sku, sid
    parts = (item.get("msku_sid_str") or "").split("$|$")
    sku = parts[0] if parts and parts[0] else None
    sid = parts[1] if len(parts) > 1 and parts[1] else None
    return sku, sid


def fetch_listing_tags(headers, items):
    """调用「全局标签-关联分析」接口，批量查询每个产品的 listing 标签。

    入参 items 为产品表现接口返回的列表；
    返回 {seller_sku: "A,UP"} 形式的映射（无标签的产品不会出现在映射中）。

    接口按 index 对齐请求与响应：请求里的每个 detail 带一个批次内序号，
    响应原样带回，据此把标签映射回对应产品。"""
    # 先为每条数据提取 seller_sku / sid，跳过取不到 sku 的条目
    candidates = []   # [(item_index, seller_sku, sid)]
    for idx, it in enumerate(items):
        sku, sid = item_seller_identity(it)
        if sku:
            candidates.append((idx, sku, sid))

    tag_map = {}
    session = requests.Session()
    total = len(candidates)

    for start in range(0, total, TAG_BATCH_SIZE):
        batch = candidates[start:start + TAG_BATCH_SIZE]
        detail_list = [{
            "index": i,                       # 批次内序号，响应按它对齐
            "label": 1,
            "base_label_list": [{"relation_id": sku, "sid": str(sid or "")}],
            "global_tag_id": None,
        } for i, (_, sku, sid) in enumerate(batch)]

        # 每次请求使用新的 Request-Id，避免服务端去重
        h = dict(headers)
        h["X-AK-Request-Id"] = str(uuid.uuid4())

        resp = session.post(TAG_API_URL, headers=h, json={
            "product_expression_detail_list": detail_list,
            "source": "tag",
            "req_time_sequence": "/global-tag/global/tag/relation/analysis?id=productExpressionNew",
        }, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 1:
            raise RuntimeError(f"标签接口返回异常: {body.get('msg') or body}")

        details = (body.get("data") or {}).get("product_expression_detail_list") or []
        for d in details:
            i = d.get("index")
            if i is None or not (0 <= i < len(batch)):
                continue
            sku = batch[i][1]
            # 标签名优先取 tag_set.tag_name，兼容 tags.tagName
            names = [str(t["tag_name"]) for t in (d.get("tag_set") or []) if t.get("tag_name")]
            if not names:
                names = [str(t["tagName"]) for t in (d.get("tags") or []) if t.get("tagName")]
            if names:
                tag_map[sku] = ",".join(names)

        print(f"  标签查询进度 {min(start + TAG_BATCH_SIZE, total)}/{total}")
        if start + TAG_BATCH_SIZE < total:
            time.sleep(0.3)  # 轻微限速

    return tag_map


def split_by_principal(items):
    """按 PERSONS 配置的全名，把数据拆分到各负责人名下。

    接口返回的 principal_names 是负责人名字列表，用全名做精确匹配；
    匹配不到任何负责人的数据会被跳过并提示数量（方便发现漏配的人）。"""
    groups = {surname: [] for surname in PERSONS}
    unmatched = 0
    for it in items:
        names = it.get("principal_names") or []
        for surname, full_name in PERSONS.items():
            if full_name in names:
                groups[surname].append(it)
                break
        else:
            unmatched += 1
    if unmatched:
        print(f"  注意：{unmatched} 条数据的负责人不在 PERSONS 配置中，已跳过")
    return groups

# ============================================================
# 第 2 步：清洗为【每日数据导入】的 15 列
# ============================================================

def smart_num(v, default=0):
    """尽量转成 int/float；空值返回 default，转不了的保留原样。
    不做四舍五入，保留原始精度。"""
    if v is None:
        return default
    s = str(v).strip()
    if s in ("", "null"):
        return default
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return default


def clean_row(item, tag_map):
    """一条接口数据 → 【每日数据导入】的一行（15 列，顺序与表头一致）。

    第一列 listing 标签来自标签接口的查询结果 tag_map（按 seller_sku 匹配），
    查不到时留空。"""
    sku, _ = item_seller_identity(item)
    return [
        tag_map.get(sku, ""),                                    # listing标签
        smart_num(item.get("afn_fulfillable_quantity")),         # FBA-可售
        smart_num(item.get("volume")),                           # 销量
        smart_num(item.get("predict_gross_profit")),             # 订单毛利润
        smart_num(item.get("amount")),                           # 销售额
        smart_num(item.get("ad_sales_amount")),                  # 广告销售额
        smart_num(item.get("predict_gross_margin")),             # 订单毛利率
        smart_num(item.get("spend")),                            # 广告花费
        smart_num(item.get("acoas")),                            # ACoAS
        smart_num(item.get("acos")),                             # ACOS
        smart_num(item.get("avg_custom_price"), default=""),     # 销售均价
        smart_num(item.get("clicks")),                           # 点击
        smart_num(item.get("nature_order_items")),               # 自然订单量
        smart_num(item.get("ad_order_quantity")),                # 广告订单量
        smart_num(item.get("afn_inbound_shipped_quantity")),     # FBA-在途
    ]

# ============================================================
# 飞书 Sheets API 封装
# ============================================================

def col_letter(n):
    """列号转字母：1->A, 2->B, 27->AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters):
    """字母转列号：A->1, B->2, AA->27"""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def parse_date_cell(v):
    """解析单元格中的日期，兼容 2026-08-01 / 2026/8/1 / Excel 日期序列号。
    解析不了返回 None。"""
    if v is None:
        return None
    s = str(v).strip()
    m = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", s)
    if m:
        try:
            return datetime.date(*map(int, m.groups()))
        except ValueError:
            return None
    try:
        f = float(s)
        if 20000 < f < 80000:  # Excel 序列日期范围
            return datetime.date(1899, 12, 30) + datetime.timedelta(days=int(f))
    except (TypeError, ValueError):
        pass
    return None


class FeishuSheet:
    """飞书电子表格 API 封装：读取 / 写入 / 清空 / 自动增行"""

    BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id, app_secret, spreadsheet_token):
        self.spreadsheet_token = spreadsheet_token

        # 换取 tenant_access_token
        r = requests.post(f"{self.BASE}/auth/v3/tenant_access_token/internal",
                          json={"app_id": app_id, "app_secret": app_secret}, timeout=15)
        body = self._json(r)
        if body.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {body}")
        self.token = body["tenant_access_token"]

        # 拉取全部工作表，同时记录每个工作表的 网格行数
        # （后续所有范围操作都按它收窄，避免 90202 RangeVal 校验失败）
        r = requests.get(f"{self.BASE}/sheets/v3/spreadsheets/"
                         f"{spreadsheet_token}/sheets/query",
                         headers=self._headers, timeout=15)
        body = self._json(r)
        if body.get("code") != 0:
            raise RuntimeError(f"获取工作表列表失败（检查应用是否为表格协作者）: {body}")
        self.sheet_ids = {}
        self.sheet_rows = {}
        for s in body["data"]["sheets"]:
            self.sheet_ids[s["title"]] = s["sheet_id"]
            gp = s.get("grid_properties") or {}
            self.sheet_rows[s["title"]] = gp.get("row_count", 2000)

    @staticmethod
    def _json(resp):
        """解析响应 JSON；若服务端返回的不是 JSON（如网关错误页），抛出可读错误"""
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(f"接口未返回 JSON（HTTP {resp.status_code}）: "
                               f"{resp.text[:300]}")

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8"}

    def sheet_id(self, title):
        """按页签名取 sheet_id，不存在时给出明确提示"""
        if title not in self.sheet_ids:
            raise KeyError(f"表格中不存在工作表【{title}】，现有：{list(self.sheet_ids)}")
        return self.sheet_ids[title]

    def _clamp_rows(self, title, cell_range):
        """把范围的结束行收窄到该工作表实际网格行数内"""
        m = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", cell_range)
        if not m:
            return cell_range
        c1, r1, c2, r2 = m.groups()
        end = min(int(r2), self.sheet_rows.get(title, 2000))
        return f"{c1}{r1}:{c2}{end}"

    def ensure_rows(self, title, need_rows):
        """写入前检查：网格行数不足 need_rows 时，调官方接口在表尾追加行"""
        cur = self.sheet_rows.get(title, 2000)
        if cur >= need_rows:
            return
        # add = need_rows - cur + 50   # 多补 50 行余量，减少增行次数
        add = need_rows - cur    # 多补 1 行余量
        url = f"{self.BASE}/sheets/v2/spreadsheets/{self.spreadsheet_token}/dimension_range"
        payload = {"dimension": {"sheetId": self.sheet_id(title),
                                 "majorDimension": "ROWS", "length": add}}
        r = requests.post(url, headers=self._headers, json=payload, timeout=30)
        body = self._json(r)
        if body.get("code") != 0:
            raise RuntimeError(f"为【{title}】增加行失败: {body}")
        self.sheet_rows[title] = cur + add
        print(f"    【{title}】网格行数不足，已追加 {add} 行（现 {cur + add} 行）")

    def read_values(self, title, cell_range):
        """读取区域数据（范围自动收窄到网格内）。
        日期按格式化字符串返回，便于 parse_date_cell 解析"""
        cell_range = self._clamp_rows(title, cell_range)
        url = f"{self.BASE}/sheets/v2/spreadsheets/{self.spreadsheet_token}/values/{self.sheet_id(title)}!{cell_range}"
        r = requests.get(url, headers=self._headers,
                         params={"valueRenderOption": "ToString",
                                 "dateTimeRenderOption": "FormattedString"}, timeout=30)
        body = self._json(r)
        if body.get("code") != 0:
            raise RuntimeError(f"读取【{title}】失败: {body}")
        return body["data"]["valueRange"].get("values") or []

    def write_values(self, title, cell_range, values):
        """向指定区域写入二维数组"""
        url = f"{self.BASE}/sheets/v2/spreadsheets/{self.spreadsheet_token}/values"
        payload = {"valueRange": {"range": f"{self.sheet_id(title)}!{cell_range}", "values": values}}
        r = requests.put(url, headers=self._headers, json=payload, timeout=60)
        body = self._json(r)
        if body.get("code") != 0:
            raise RuntimeError(f"写入【{title}】失败: {body}")

    def clear_values(self, title, cell_range):
        """清空区域内容：先按网格实际行数收窄范围，再分片写入空字符串。
        （飞书单区域清空接口不可用；且单次写入单元格数量有上限，必须分片）"""
        cell_range = self._clamp_rows(title, cell_range)
        m = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", cell_range)
        c1, r1, c2, r2 = m.groups()
        n_cols = col_index(c2) - col_index(c1) + 1
        r1, r2 = int(r1), int(r2)
        if r2 < r1:
            return
        CHUNK = 200  # 每片 200 行，远低于单次写入量上限
        row = r1
        while row <= r2:
            part_end = min(row + CHUNK - 1, r2)
            self.write_values(title, f"{c1}{row}:{c2}{part_end}",
                              [[""] * n_cols for _ in range(part_end - row + 1)])
            row = part_end + 1

    def write_rows(self, title, start_row, n_cols, rows, chunk=200):
        """分片写入多行数据；网格行数不足时先自动增行"""
        if not rows:
            return
        self.ensure_rows(title, start_row + len(rows) - 1)
        last_col = col_letter(n_cols)
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            r1 = start_row + i
            r2 = r1 + len(part) - 1
            self.write_values(title, f"A{r1}:{last_col}{r2}", part)
            print(f"    已写入 {r2 - start_row + 1}/{len(rows)} 行")


# ============================================================
# 第 3 步：复制【产品每日情况】固定区块 A1:R7 到页尾
# ============================================================

def copy_daily_block(fs, sheet_title, retries=3, wait=15):
    """读取【产品每日情况】顶部固定区块 A1:R7，
    追加写入该页 B 列最后一行往下 2 行的位置（中间空一行），
    并返回区块第 7 行 C:R 的数据（供第 4 步写入【汇总总和】）。

    注意：
      - 页尾定位只看 B 列最后一个有数据的行；
      - 区块 A2（日期单元格）强制改写为本次抓取的日期文本，
        避免接口返回值被飞书自动转成日期序列号（如 46237）；
      - 该区块由公式基于【每日数据导入】实时计算，刷新有延迟，
        若第 7 行 C:R 读出来全空，最多自动重试 retries 次。"""
    N_COLS = 18  # A:R 共 18 列

    for attempt in range(1, retries + 1):
        # 读取顶部区块 A1:R7，并补齐为规整的 7 行 × 18 列
        block = fs.read_values(sheet_title, "A1:R7")
        block = [list(r) + [""] * (N_COLS - len(r)) for r in block]
        while len(block) < 7:
            block.append([""] * N_COLS)

        # 校验第 7 行 C:R 是否有数据（公式可能还没算出来）
        summary = block[6][2:]
        if any(str(c).strip() != "" for c in summary):
            break
        if attempt < retries:
            print(f"  区块公式尚未刷新，{wait}s 后重试 ({attempt}/{retries}) ...")
            time.sleep(wait)
    else:
        raise RuntimeError(f"【{sheet_title}】A1:R7 区块第 7 行数据为空，"
                           "请确认该页公式已基于新导入数据正常计算")

    # 区块 A2（第 2 行第 1 列）强制写入抓取日期的文本，如 2026/8/2
    date_text = f"{TARGET_DATE.year}/{TARGET_DATE.month}/{TARGET_DATE.day}"
    block[1][0] = date_text

    # 页尾定位：B 列最后一个有数据的行
    grid_rows = fs.sheet_rows.get(sheet_title, 2000)
    col_b = fs.read_values(sheet_title, f"B1:B{grid_rows}")
    last_row = 0
    for i, row in enumerate(col_b, start=1):
        if row and str(row[0]).strip() != "":
            last_row = i

    # 追加位置：最后一行往下 2 行（即空一行）
    start = last_row + 2
    end = start + 6
    fs.ensure_rows(sheet_title, end)
    fs.write_values(sheet_title, f"A{start}:R{end}", block)
    print(f"  已将 A1:R7 区块追加到第 {start}~{end} 行（第 {last_row + 1} 行留空）")

    # 返回区块第 7 行的 C:R（16 列汇总数据）
    return summary


# ============================================================
# 主流程
# ============================================================

def main(cookie_list):
    date_str = TARGET_DATE.strftime("%Y-%m-%d")
    print(f"目标日期: {date_str}")
    print(f"同步人员: {'、'.join(f'{k}({v})' for k, v in PERSONS.items())}")

    # [0] 用传入的 Cookie（字符串或浏览器导出的数组）直接构造领星请求头
    print("[0/4] 构造领星请求头 ...")
    if not cookie_list:
        raise RuntimeError("请传入 cookie_list（浏览器导出的 Cookie 数组或 Cookie 字符串）")
    cookie_list = normalize_cookie_list(cookie_list)
    if not cookie_list:
        raise RuntimeError("cookie_list 解析后为空，请检查格式是否正确")
    headers = build_headers_from_cookies(cookie_list, USER_AGENT)

    # [1] 拉取产品表现数据 + 查询 listing 标签
    print("[1/4] 请求领星ERP数据 ...")
    items = fetch_lingxing_data(headers, date_str)
    print("  查询 listing 标签 ...")
    tag_map = fetch_listing_tags(headers, items)
    print(f"  共取到 {len(tag_map)} 个产品的标签")

    # 按负责人拆分并打印数量
    groups = split_by_principal(items)
    for surname, full_name in PERSONS.items():
        print(f"  {surname}（{full_name}）: {len(groups[surname])} 条")

    fs = FeishuSheet(FEISHU_APP_ID, FEISHU_APP_SECRET, SPREADSHEET_TOKEN)

    # [2]~[4] 逐人处理：导入数据 → 复制区块 → 写入汇总总和
    for surname, full_name in PERSONS.items():
        names = sheet_names(surname)
        rows = [clean_row(it, tag_map) for it in groups[surname]]

        # 数据量硬上限校验：绝不能超过第 1500 行（下方有其他内容）
        if len(rows) + 1 > IMPORT_MAX_ROWS:
            raise RuntimeError(
                f"【{names['import']}】数据量 {len(rows)} 行超出上限："
                f"第 1 行表头 + 数据后共 {len(rows) + 1} 行 > {IMPORT_MAX_ROWS} 行，"
                "继续执行会覆盖下方其他内容，请先人工处理")

        # [2] 写入【每日数据导入】（先清空旧数据再写，范围严格限制在 1500 行内）
        print(f"[2/4] 写入【{names['import']}】共 {len(rows)} 行 ...")
        fs.clear_values(names["import"], f"A2:O{IMPORT_MAX_ROWS}")
        fs.write_rows(names["import"], start_row=2, n_cols=15, rows=rows)

        # 等待表格内公式基于新数据重算（数据量大时可调长）
        print("  等待公式刷新 ...")
        time.sleep(8)

        # [3] 复制【产品每日情况】顶部区块 A1:R7，追加到该页末尾（空一行）
        print(f"[3/4] 复制【{names['daily']}】A1:R7 区块到页尾 ...")
        summary_values = copy_daily_block(fs, names["daily"])
        print(f"  取到 C7:R7 共 {len(summary_values)} 列汇总数据")

        # [4] 写入【汇总总和】对应日期行（第 1 列为日期，从第 2 列开始写）
        print(f"[4/4] 写入【{names['sum']}】 ...")
        sum_rows = fs.read_values(names["sum"], "A1:A5000")
        target_row = None
        for idx, row in enumerate(sum_rows, start=1):
            if row and parse_date_cell(row[0]) == TARGET_DATE:
                target_row = idx
                break
        if not target_row:
            raise RuntimeError(f"【{names['sum']}】第一列未找到日期 {date_str}，请先补上该日期行")
        end_col = col_letter(2 + len(summary_values) - 1)   # B 起 16 列 → B:Q
        values = [[smart_num(c, default=c) if str(c).strip() != "" else ""
                   for c in summary_values]]
        fs.write_values(names["sum"], f"B{target_row}:{end_col}{target_row}", values)
        print(f"  已写入第 {target_row} 行 B:{end_col}")

    print("全部完成 ✅")


if __name__ == "__main__":
    cookie = (_CONFIG.get("lingxing", {}) or {}).get("cookie") or ""
    main(cookie)
