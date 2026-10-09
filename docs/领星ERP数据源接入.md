# 领星 ERP 数据源接入说明

## 1. 为什么是「Cookie 逆向接入」而非官方 OpenAPI

领星 ERP 官方并未向普通用户开放一套标准的、带 `app_key/app_secret` 与 MD5 签名的
OpenAPI（类似抖店/拼多多/淘宝/京东的 OAuth 开放平台）。本项目参考的多个开源脚本
（`lingxing-net-input-stats-to-feishu`、`lingxing-product-performance-to-feishu`、
`lingxing-purchase-order-to-feishu`）均采用**逆向其 Web 后台接口**的方式：

- 用户在浏览器登录领星 ERP 后，复制请求里携带的 Cookie（含 `auth-token` /
  `company_id` / `env_key` / `uid` / `zid`）；
- 脚本用这些 Cookie 拼出一组 `X-AK-*` 请求头，直接调用 Web 后台的 JSON 接口；
- **没有** OAuth、没有签名、没有 `client_id/secret`，也不存在 `access_token` 概念。

因此本集成在 `backend/platforms/registry.py` 中把领星标记为 `auth_mode="cookie"`，
不走 OAuth 流程，仅凭 `.env` 的 `LINGXING_COOKIE` 即视为「已配置/已授权」。

### 三个域名

| 域名变量 | 默认值 | 用途 |
| --- | --- | --- |
| `LINGXING_ERP_HOST` | `https://maique.lingxing.com` | ERP 主后台：库存、发货、采购、店铺 |
| `LINGXING_GW_HOST` | `https://gw.lingxingerp.com` | 数据网关：产品表现、应收报告 |
| `LINGXING_ADS_HOST` | `https://ads.lingxing.com` | 广告后台（需 CSRF，当前最小可用） |

### 其他环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LINGXING_COOKIE` | 空 | 必填。浏览器登录态 Cookie（含 `auth-token` 等） |
| `LINGXING_VERSION` | `3.9.0.3.0.037` | 请求头 `X-AK-Version`，随领星前端升级需更新 |
| `LINGXING_WAREHOUSE_IDS` | 空 | 本地仓 `wid_list`，逗号分隔；**留空 = 账号下全部本地仓**。原脚本写死为原作者账号的仓库 id `3275`，已改为可配置 |
| `LINGXING_MIDS` | `1` | marketplace 过滤，沿用原脚本默认值 |

## 2. 如何从浏览器复制 Cookie

1. 用 Chrome/Edge 登录 [领星 ERP](https://maique.lingxing.com)；
2. 打开任意页面 → F12 → Network → 选一个对 `maique.lingxing.com` 的 XHR 请求；
3. 在 Request Headers 里找到 `Cookie:` 整行复制，形如：
   ```
   auth-token=xxx; company_id=xxxx; env_key=xxx; uid=xxxx; zid=xxxx; ...
   ```
4. 把整段粘贴到 `.env` 的 `LINGXING_COOKIE=`（或直接以参数 `cookie` 传给接口）；
5. 也可使用浏览器扩展（如 EditThisCookie）导出的 `[{"Name":..,"Value":..}]` 数组形式。

> `auth-token` 在 Cookie 中是 URL 编码的，模块会在 `build_lingxing_headers` 中自动
> `urllib.parse.unquote` 解码；缺 `auth-token` 会抛 `ValueError`。

## 3. 用到的端点表

> 下列「方法 / 路径 / 字段」均从参考脚本逐字段核对，**非臆造**。括号内为核对来源。

| 数据域 | 方法 | 路径 | list 路径 | total 路径 | 来源 |
| --- | --- | --- | --- | --- | --- |
| 店铺列表 | GET | `{ERP}/api/my/sellers` | `list`（顶层） | — | lingxing_sync.py:573 |
| 产品表现 | POST | `{GW}/bd/productPerformance/asinLists` | `data.list` | `data.total` | main.py:79 / L247 |
| FBA 库存 | POST | `{ERP}/api/storage/fbaLists` | `list`（**顶层**） | `total`（**顶层**） | lingxing_sync.py:807 / L809 |
| 本地仓库存 | POST | `{ERP}/api/storage/lists` | `data.list` | `data.total` | lingxing_sync.py:880 / L882 |
| 发货单列表 | POST | `{ERP}/api/shipment/showShipmentListV2` | `data.list` | `data.total` | lingxing_sync.py:535 / L545 |
| 采购单列表 | POST | `{ERP}/api/purchase/orderListsV2` | `data.list` | `data.total` | purchase_tracking.py:277 / L299 |
| 应收报告 | POST | `{GW}/bd/sp/api/monthly/receivable/report/list` | `data.records` | `data.total` | lingxing_sync.py:721 / L723 |
| 广告报表 | POST | `{ADS}/...`（需 CSRF） | — | — | 最小可用，见 §6 |

接口通用约定：`code == 1` 表示成功；分页靠请求体 `offset/length`（领星页大小 200），
响应里的 list/total 位置因接口而异（上表已注明在顶层还是 `data` 内）。

## 4. 字段映射表（领星字段 → CANONICAL_COLUMNS）

标准化列定义在 `backend/services/data_cleaner.py` 的 `CANONICAL_COLUMNS`
（`product_name, impressions, clicks, visitors, orders, payment_amount, ad_cost,
cost, refund_amount, stock`）。

`build_canonical_dataframe` 以**产品表现**为主表（每个 SKU/ASIN 一行），左连接库存补 `stock`：

| CANONICAL 列 | 来源字段 | 说明 |
| --- | --- | --- |
| `product_name` | `item_name` › `asin` › `sku` › `price_list[0].seller_sku` | 取第一个非空 |
| `impressions` | （暂无） | 置 0（产品表现接口未返回曝光量） |
| `clicks` | `clicks` | 点击量 |
| `visitors` | （暂无） | 置 0 |
| `orders` | `volume` | 销量 → 下单数 |
| `payment_amount` | `amount` | 销售额 |
| `ad_cost` | `spend` | 广告花费 |
| `cost` | （暂无） | 置 0（FBA 成本为金额字段，未映射） |
| `refund_amount` | （暂无） | 置 0 |
| `stock` | `afn_fulfillable_quantity`（FBA 可售）› `good_num`（本地仓可用量）› `total`（实际总量） | 按 `sku`/`asin` 左连接库存；优先级依据 `lingxing_sync.py:836-837` 的 `STOCK_COLUMNS`（可用量=good_num，实际总量=total） |

数值列统一经 `to_number` 归一；`stock` 缺失时按 0 处理，保证 DataFrame 可直接
喂给 `analyze_dataframe`（不会出现空列或全 NaN）。

> 广告（`impressions` 等）当前回退到产品表现自带的 `clicks/spend`；若后续补齐 ADS 的
> CSRF 拉取（`fetch_ads_report`），可在此映射覆盖。

## 5. 接口使用

```
POST /api/platforms/lingxing/pull?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD[&cookie=...]
```

返回：`{"file_id": "lingxing_<时间戳>", "rows": <行数>, "source": "lingxing"}`。
拿到 `file_id` 后可直接调用：

- `GET  /api/analysis/{file_id}`
- `GET  /api/diagnosis/{file_id}`
- `GET  /api/report/{file_id}`

错误码：参数错误 → 400；领星拉取/解析失败 → 502；其他异常 → 500（不会裸抛 500）。

## 6. 已知限制

1. **Cookie 会过期、无自动续期**：登录态失效后接口返回非 `code==1`，需人工重新复制
   Cookie。模块只做「缺 `auth-token` 抛错」，不处理续期。
2. **`X-AK-Version` 需随前端升级更新**：硬编码默认值 `3.9.0.3.0.037`，领星前端升级后
   若接口拒绝旧版本，需同步更新 `.env` 的 `LINGXING_VERSION`。
3. **接口非官方、可能变更**：逆向接口没有版本契约，字段/路径可能随领星改版变化。
4. **频率限制**：高频调用可能触发限流，分页已按 200/页，必要时在调用方限速。
5. **广告域 CSRF**：`fetch_ads_report` 为最小可用实现（无 CSRF 流程时返回 `None`，
   不影响主流程）；`impressions/visitors` 因此置 0，待补齐 CSRF 后完善。
6. **数据安全**：Cookie 等同账号密码，请勿写入代码或提交仓库；`.env` 已被忽略。
