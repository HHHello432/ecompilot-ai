import os
import json
import time
import urllib.request
import urllib.error

# 凭据通过环境变量传入，避免明文落盘
APP_ID = os.environ.get("FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN")  # 多维表格 app_token

API_BASE = "https://open.feishu.cn/open-apis"


def _http_post(url, token, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_tenant_token():
    url = f"{API_BASE}/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {body}")
    return body["tenant_access_token"]


def create_table(token, name, fields):
    """创建数据表。fields 仅用于指定首列（索引字段），其余字段走 add_field。"""
    path = f"/bitable/v1/apps/{APP_TOKEN}/tables"
    payload = {"table": {"name": name, "fields": fields}}
    r = _http_post(f"{API_BASE}{path}", token, payload)
    if r.get("code") != 0:
        raise RuntimeError(f"创建表 [{name}] 失败: {r}")
    data = r.get("data", {})
    table_id = data.get("table_id") or data.get("table", {}).get("table_id")
    print(f"  [OK] 创建表 [{name}] -> table_id={table_id}")
    return table_id


def add_field(token, table_id, field_name, ftype, property=None):
    path = f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields"
    payload = {"field_name": field_name, "type": ftype}
    if property is not None:
        payload["property"] = property
    r = _http_post(f"{API_BASE}{path}", token, payload)
    if r.get("code") != 0:
        raise RuntimeError(f"新增字段 [{field_name}] 失败: {r}")
    field_id = r.get("data", {}).get("field", {}).get("field_id")
    print(f"  [OK] 字段 [{field_name}] (type={ftype}) -> field_id={field_id}")
    return field_id


def main():
    for k, v in (("FEISHU_APP_ID", APP_ID), ("FEISHU_APP_SECRET", APP_SECRET),
                 ("FEISHU_APP_TOKEN", APP_TOKEN)):
        if not v:
            raise RuntimeError(f"缺少环境变量 {k}")

    token = get_tenant_token()
    print("[OK] 获取 tenant_access_token 成功\n")

    # ---------- 表1：采购单（主表，订单级）----------
    print(">> 创建表1：采购单")
    t1 = create_table(token, "采购单",
                      [{"field_name": "采购单号", "type": 1, "property": {}}])
    time.sleep(1)
    add_field(token, t1, "1688订单号", 1, {})
    time.sleep(0.5)
    add_field(token, t1, "是否完成下单", 7, None)  # 复选框，property 为 null
    time.sleep(0.5)
    date_prop = {"date_formatter": "yyyy/MM/dd HH:mm", "auto_fill": False}
    for fn in ["下单日期", "发货日期", "签收日期", "入库日期", "加工日期"]:
        add_field(token, t1, fn, 5, date_prop)
        time.sleep(0.5)

    # ---------- 表2：采购单SKU明细（子表，SKU 级）----------
    print("\n>> 创建表2：采购单SKU明细")
    t2 = create_table(token, "采购单SKU明细",
                      [{"field_name": "SKU", "type": 1, "property": {}}])
    time.sleep(1)
    # 单向关联(type=18)指向表1的采购单号；multiple=False 表示每行只关联一个采购单
    add_field(token, t2, "采购单号", 18, {"multiple": False, "table_id": t1})
    time.sleep(0.5)

    print("\n=== 建表完成 ===")
    print(f"多维表格地址: https://<你的飞书域名>.feishu.cn/base/{APP_TOKEN}")
    print(f"表1 采购单        table_id = {t1}")
    print(f"表2 采购单SKU明细 table_id = {t2}")


if __name__ == "__main__":
    main()
