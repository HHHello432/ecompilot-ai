import os
import json
import time
import urllib.request

APP_ID = os.environ.get("FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN")

API_BASE = "https://open.feishu.cn/open-apis"

# 已知表1(采购单)的 table_id，表2 将关联回它
TABLE1_ID = "tblU0rUgG5MkU94G"
OLD_TABLE2_ID = "tbl1l7FFkBMniWYf"


def _req(method, path, token, payload=None):
    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_token():
    r = _req("POST", "/auth/v3/tenant_access_token/internal", None,
             {"app_id": APP_ID, "app_secret": APP_SECRET})
    if r.get("code") != 0:
        raise RuntimeError(f"获取 token 失败: {r}")
    return r["tenant_access_token"]


def delete_table(token, table_id):
    r = _req("DELETE", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}", token)
    if r.get("code") != 0:
        raise RuntimeError(f"删除旧表2 [{table_id}] 失败: {r}")
    print(f"  [OK] 已删除旧表2 table_id={table_id}")


def create_table(token, name, fields):
    r = _req("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables", token,
             {"table": {"name": name, "fields": fields}})
    if r.get("code") != 0:
        raise RuntimeError(f"创建表2 失败: {r}")
    data = r.get("data", {})
    tid = data.get("table_id") or data.get("table", {}).get("table_id")
    print(f"  [OK] 创建表2 [{name}] -> table_id={tid}")
    return tid


def add_field(token, table_id, field_name, ftype, property=None):
    payload = {"field_name": field_name, "type": ftype}
    if property is not None:
        payload["property"] = property
    r = _req("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields", token, payload)
    if r.get("code") != 0:
        raise RuntimeError(f"新增字段 [{field_name}] 失败: {r}")
    fid = r.get("data", {}).get("field", {}).get("field_id")
    print(f"  [OK] 字段 [{field_name}] (type={ftype}) -> field_id={fid}")


def show_table(token, table_id):
    r = _req("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}", token)
    if r.get("code") != 0:
        print("  [!] 校验获取表信息失败:", r)
        return
    t = r.get("data", {})
    print(f"\n=== 表2 校验: {t.get('name')} ({table_id}) ===")
    for f in t.get("fields", []):
        primary = " [索引/首列]" if f.get("is_primary") else ""
        prop = f.get("property") or {}
        link = f" -> 关联表 {prop.get('table_id')}" if f.get("type") in (18, 21) else ""
        print(f"   - {f['field_name']} (type={f['type']}){primary}{link}")


def main():
    for k, v in (("FEISHU_APP_ID", APP_ID), ("FEISHU_APP_SECRET", APP_SECRET),
                 ("FEISHU_APP_TOKEN", APP_TOKEN)):
        if not v:
            raise RuntimeError(f"缺少环境变量 {k}")

    token = get_token()
    print("[OK] 获取 tenant_access_token 成功")

    print("\n>> 删除旧表2")
    delete_table(token, OLD_TABLE2_ID)
    time.sleep(2)  # 等底层就绪，避免写冲突

    print("\n>> 重建表2：采购单SKU明细（组合键索引）")
    # 首列必须是文本(1)，作为「采购单号#SKU」组合键，上传时填充
    t2 = create_table(token, "采购单SKU明细",
                      [{"field_name": "采购单号_SKU", "type": 1, "property": {}}])
    time.sleep(1)
    add_field(token, t2, "SKU", 1, {})
    time.sleep(0.5)
    add_field(token, t2, "采购单号", 18, {"multiple": False, "table_id": TABLE1_ID})

    show_table(token, t2)
    print(f"\n新表2 table_id = {t2}")


if __name__ == "__main__":
    main()
