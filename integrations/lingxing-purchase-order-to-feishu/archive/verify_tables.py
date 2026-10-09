import os, json, urllib.request

APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]
APP_TOKEN = os.environ["FEISHU_APP_TOKEN"]


def get_token():
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["tenant_access_token"]


def main():
    tok = get_token()
    req = urllib.request.Request(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables",
        headers={"Authorization": f"Bearer {tok}"}, method="GET")
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    for t in resp["data"]["items"]:
        print(f"\n表名: {t['name']}  table_id={t['table_id']}")
        for f in t.get("fields", []):
            prop = f.get("property") or {}
            extra = f" -> 关联表 {prop.get('table_id')}" if f.get("type") in (18, 21) else ""
            print(f"   - {f['field_name']} (type={f['type']}){extra}")


if __name__ == "__main__":
    main()
