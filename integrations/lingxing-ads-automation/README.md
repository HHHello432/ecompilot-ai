# 领星广告 API 自动化（lingxing-ads-automation）

领星广告后台 RPA 的配套飞书机器人服务。监听飞书消息事件，自动接收用户发来的广告数据表格，
落盘排队后触发八爪鱼 RPA webhook 进行处理，形成「发文件 → 自动排队 → RPA 处理」的链路。

## 功能

- **文件接收**：监听飞书 `im.message.receive_v1` 事件，仅处理文件名含「广告」的 `.xls/.xlsx` 文件消息
- **自动下载**：通过飞书开放接口下载文件，按 `[发送者open_id]时间戳-原文件名` 命名存入 `waiting/`
- **触发 RPA**：下载成功后调用八爪鱼 RPA webhook（带 HMAC-SHA256 签名，失败自动重试）
- **通知反馈**：文件进入队列后回复发送人确认消息
- **排队进度查询**：机器人菜单点击 `ads_progress` 时，回复当前待处理文件列表与今日完成数
- **幂等去重**：已处理消息 ID 持久化到 `processed_ids.txt`，防止飞书重推导致重复处理

## 处理流程

```
飞书文件消息 → 过滤（文件名含"广告" 且为 .xls/.xlsx）
  → 下载到 waiting/
  → 回复发送人「进入排队序列」
  → 触发八爪鱼 RPA webhook（失败重试 3 次）
  → 全部成功后标记消息已处理

RPA（甲组1_广告_接口版260907）取 waiting/ 中文件名时间戳最早的文件
  → 移入 finished/ 归档（取到即归档，防止重复处理）
  → 读依赖表 → 领星广告后台 API 创建/优化广告（纯 API，不开网页）
  → 广告状态写回 xlsx → 飞书反馈（成功发文本；有错改名[错误]+上传文件+发送）

飞书反馈目标（与老流程一致，发给发件人本人）：
  「开始处理。」（取件后）→「全部成功执行！」/「部分错误…」+ 结果文件（完成后）
  发件人 open_id 从文件名 `[{open_id}]时间戳-原名.xlsx` 解析；
  解析不到（手动丢进 waiting/、重跑归档件）时改发到 config 的 FEISHU_CHAT_ID 群兜底，
  保证任何来源的文件都有反馈，不静默。

A2 无优化方案处理：首轮成功绑定行数为 0（无优化方案）时，等待 60 秒
  （A2_RETRY_WAIT，A1 刚建的活动偶发未就绪）重试一次；第二次仍为 0 才判
  「无优化方案」继续处理下一行。
```

### 幂等与重试策略

- 只有完整走完「下载 → 通知 → 触发 RPA」才标记已处理；中途失败不标记，等飞书重推时自动重试
- 消息解析失败、文件名不含「广告」、非表格文件：无副作用，直接标记已处理，避免重推时重复打日志
- 通知发送人失败不阻断流程（文件已落盘，不应因此丢触发）
- 下载产生的 0 字节残文件会被自动清理
- `processed_ids.txt` 超过 `processed_ids_max`（默认 10000）条时，裁剪保留最新的一半

## 使用

### 1. 安装依赖

```bash
pip install lark-oapi requests
```

### 2. 配置

运行配置只有一份 `config.json`（含凭据，**不入库**），字段结构如下；
完整字段说明见下文「配置项说明」表：

```json
{
  "app_id": "cli_xxxxxxxxxxxxxxxx",
  "app_secret": "your_app_secret",
  "download_dir": ".\\waiting",
  "finished_dir": ".\\finished",
  "rpa_webhook_url": "https://api-rpa.bazhuayu.com/api/v1/bots/webhooks/<webhook_id>/invoke",
  "rpa_webhook_secret": "your_webhook_secret",
  "COOKIES": [{ "Name": "领星后台 cookie 名", "Value": "对应 cookie 值" }],
  "CSRF_TOKEN": null,
  "COMPANY_ID": "your_company_id"
}
```

其余配置项（重试次数、超时、去重上限等）均有内置默认值，按需覆盖。

### 3. 飞书应用侧准备

- 开启**机器人**能力，订阅事件 `im.message.receive_v1`（接收消息）与机器人菜单事件
- 配置机器人菜单，`event_key` 设为 `ads_progress`（对应配置项 `menu_event_key_ads_progress`），供查询排队进度
- 事件通过 **WebSocket 长连接** 接收（`lark.ws.Client`），无需公网回调地址
- 给机器人发文件消息即可触发；文件名需含「广告」且为 `.xls/.xlsx`

### 4. 启动

```bash
python sx_ads_lark_link.py
```

终端标题会显示版本号（如 `260907.1118`），日志输出处理过程。

## 目录结构

```
sx_ads_lark_link.py    主程序（飞书事件监听 + 下载 + RPA 触发）
sx_ads_api.py          领星广告后台纯 API 客户端（贴入 RPA 流程 Python 节点）
config.json            运行配置（含凭据，不入库）
waiting/               待处理文件队列（RPA 输入）
finished/              RPA 处理完成的文件
processed_ids.txt      已处理消息 ID 持久化（自动维护，超限自动裁剪）
```

## 配置项说明

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `app_id` / `app_secret` | 空 | 飞书应用凭据（必填，缺失时启动报错退出） |
| `chat_id` | 空 | 群聊 ID（保留备用，当前未使用） |
| `download_dir` | `.\waiting` | 待处理文件目录 |
| `finished_dir` | `.\finished` | 处理完成文件目录 |
| `rpa_webhook_url` | 空 | 八爪鱼 RPA webhook 地址 |
| `rpa_webhook_secret` | 空 | webhook 签名密钥（空则不签名） |
| `menu_event_key_ads_progress` | `ads_progress` | 排队进度菜单的 event_key |
| `processed_ids_file` | `processed_ids.txt` | 已处理消息 ID 持久化文件 |
| `processed_ids_max` | `10000` | 去重集合上限，超限裁剪保留一半 |
| `request_timeout` | `[5, 60]` | HTTP 超时（连接秒, 读取秒） |
| `token_refresh_margin` | `300` | token 过期前提前刷新的秒数 |
| `webhook_retry` | `3` | RPA webhook 触发失败重试次数 |
| `webhook_retry_delay` | `2` | 重试间隔秒数 |
| `COOKIES` | 空 | 领星后台 cookie 列表（`Name`/`Value` 结构；RPA 流程内由 GetCookies 指令获取，本地调试在此手工维护） |
| `CSRF_TOKEN` | `null` | 领星 x-CSRF-TOKEN（保持 null，RPA 脚本自动从 ads 首页解析） |
| `COMPANY_ID` | 空 | 领星部门 ID（请求头 x-AK-Company-Id） |
| `app_id_test` / `app_secret_test` / `chat_id_test` | 空 | 测试应用凭据（保留备用，当前未使用） |

## 故障排查

### `获取 csrf-token 失败`

`sx_ads_api.py` 启动时先 GET ads 页面取 `x-CSRF-TOKEN`（页面 `<meta name='token'>` 里的会话 token），
再调业务接口。失败时按日志里的具体原因处理：

| 日志关键字 | 含义 | 处理 |
|---|---|---|
| `登录态失效：... 被重定向到 /restartLogin` | Cookie 没有 ads 登录态 | 确认流程已用「谷歌登录领星」登录成功，且跳转过 `ads.lingxing.com/home` 后再取 Cookie；本地调试需更新 `config.json` 的 `COOKIES` |
| `未解析到 token` | 页面结构变了 | 检查 ads 首页 HTML 里 token 所在 `<meta>` 的 `name` 属性（曾为 `token`，非 `csrf-token`） |
| `请求失败` | 网络/超时 | 检查执行机到 `ads.lingxing.com` 的连通性 |

候选页顺序：`/home` → `/build/super/create/index`（任一成功即可，见 `CSRF_PAGE_PATHS`）。
注意：登录页也会返回 token，脚本会先判断是否被重定向到 `/restartLogin`，避免拿着无效 token 跑完才报 401。

### A2 全是「无优化方案」

A2 要给活动绑优化方案（规则组），但**规则组列表没有独立接口**——它内嵌在规则页
`ad_report/rule_pro/index/objects?profile_id=xxx` 的 HTML 里（`var ruleGroupList = [...]`）。
脚本用 `get_rule_groups()` 抓页面解析，`pick_rule_group()` 按老流程规则选：

| 活动名含 | 优化方案 |
|---|---|
| 基础盘（targeting=auto） | 基础盘：自动广告 |
| 基础盘（targeting=manual） | 基础盘：关键词-广泛 |
| 放大盘 | 放大盘：关键词-广泛 |
| 精准盘 | 精准盘：关键词-精准 |

若日志出现「未匹配到优化方案」，检查该店铺规则页里是否有对应「X盘」的方案。

### 预检报 1001「请先设置移词映射关系」

规则需要移词目标，新活动默认没有。老流程规则：**基础盘 → 放大盘活动，放大盘 → 精准盘活动**
（同 ASIN），广告组取目标活动第一个；`match_type` 用预检返回的 `data`（broad/exact，不再写死）。
找不到目标活动会报错并记为「广告优化错误」。
