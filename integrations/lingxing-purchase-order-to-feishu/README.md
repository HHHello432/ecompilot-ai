# 采购时效跟踪（lingxing-purchase-order-to-feishu）

从**领星 ERP** 抓取采购单，解析出采购 → 发货 → 签收 → 入库 → 加工的全链路时间节点，
自动写入**飞书多维表格**，并对「未入库单」做增量回跑刷新。

- 纯 Python 标准库实现，**零第三方依赖**，无需 `pip install`
- 全流程一键执行，配置集中在 `config.json`，直接 `python purchase_tracking.py` 即可，无需命令行参数
- 写入采用幂等 upsert（按业务键去重），可反复运行
- 隐私凭据（飞书密钥、领星 cookies）与代码分离，不入库

---

## 一、流程概览

```
┌─ Phase 1：全量 ────────────────────────────┐
│  A2 按日期范围拉取采购单  (领星 orderListsV2) │
│           ↓                                 │
│  A3 并发解析：加工/物流/入库/采购 四个接口    │
│           ↓                                 │
│  A4 upsert 写入飞书表1 + 表2                │
└─────────────────────────────────────────────┘
                   ↓
┌─ Phase 2：增量回跑 ────────────────────────┐
│  B1 读取「未入库单」视图，还原订单结构       │
│           ↓                                 │
│  A3 再次并发解析                             │
│           ↓                                 │
│  A4 再次 upsert（只更新有值的字段）          │
└─────────────────────────────────────────────┘
                   ↓
┌─ Phase 3：清理（可关闭）───────────────────┐
│  C5 表1/表2 任一超 15000 条时，删除：       │
│  表1「加工日期」非空或「备注」非空           │
│  表2 对应引用的记录                          │
└─────────────────────────────────────────────┘
```

| 阶段 | 说明 |
|------|------|
| **A2** | 分页拉取指定日期范围内的采购单，`offset` 每次 +200，直到跑完 `total` |
| **A3** | 每个订单并发请求「加工单 / 物流 / 采购单详情」三个接口，产出订单级与 SKU 级两套记录 |
| **A4** | 先全量读取现有记录建索引，再按业务键分流为「新增」与「更新」，批量提交 |
| **B1** | 从飞书「未入库单」视图 + SKU 明细表，反推出与 A3 输入结构一致的数据，用于回跑 |
| **C5** | 表1 或表2 任一记录数超过 `CLEANUP_THRESHOLD`（默认 15000）才触发：删除表1「加工日期」非空或「备注」非空的记录，并删除表2 引用这些被删订单的行；`CLEANUP_FINISHED=false` 可整体关闭 |

---

## 二、快速开始

### 1. 环境要求

Python 3.8+（仅用标准库，无需安装依赖）。

### 2. 生成配置文件

```bash
cp config.example.json config.json
```

编辑 `config.json`，填写真实值：

```json
{
  "APP_ID": "cli_xxxxxxxxxxxxxxxx",
  "APP_SECRET": "xxxxxxxxxxxxxxxxxxxx",
  "FEISHU_URL": "https://xxx.feishu.cn/base/xxxxxxxxxxxxxxxxxxxxxx",

  "TABLE1_ID": "tblU0rUgG5MkU94G",
  "TABLE2_ID": "tbl4QqMWyPMbMPGb",

  "VIEW_NAME": "未入库单",
  "START_DATE": "2026-08-01",
  "END_DATE": "2026-08-28",
  "A2_TIME_FIELD": "create_time",
  "CLEANUP_FINISHED": true,
  "CLEANUP_THRESHOLD": 15000,

  "COOKIES_PATH": "cookies.json",
  "OUTPUT_FILE": "purchase_tracking_result.json"
}
```

| 配置项 | 说明 |
|--------|------|
| `APP_ID` / `APP_SECRET` | 飞书机器人凭据 |
| `FEISHU_URL` | 多维表格地址（`/base/` 后一段即 `app_token`） |
| `TABLE1_ID` / `TABLE2_ID` | 采购单表 / SKU 明细表的 `table_id` |
| `TABLE1_NAME` / `TABLE2_NAME` | 表1 / 表2 的名称（仅用于日志展示） |
| `VIEW_NAME` | B1 读取的视图名 |
| `START_DATE` / `END_DATE` | A2 查询日期范围，格式 `YYYY-MM-DD` |
| `A2_TIME_FIELD` | A2 拉单按哪个时间字段过滤：`create_time`（创建时间，默认）或 `order_time`（下单日期） |
| `CLEANUP_FINISHED` | 清理开关（`true` 默认）。跑完后按 C5 规则清理记录；设为 `false` 可跳过删除 |
| `CLEANUP_THRESHOLD` | C5 触发阈值（默认 `15000`）。表1 或表2 任一记录数超过该值才执行清理 |
| `COOKIES` / `COOKIES_PATH` | 领星 cookies，二选一，`COOKIES` 优先 |
| `OUTPUT_FILE` | 运行结果统计的落盘文件名 |
| `DEBUG_CONCURRENT` | 并发调试开关（`false` 默认）。开启后把所有多线程请求（加工/物流/采购）的入参与返回结果写入本地文件，排查多线程串号、空返回、字段异常等问题 |
| `DEBUG_FILE` | 并发调试记录文件路径，默认 `concurrent_debug.jsonl`（JSONL，每行一条请求记录） |

> `config.json` 与 `cookies.json` 已被 `.gitignore` 忽略，**不会进入版本库**。
> 可先用 `git check-ignore -v config.json` 确认忽略生效。

### 3. 准备领星 cookies

登录 [领星 ERP](https://maique.lingxing.com)，从浏览器开发者工具导出 cookies，
保存为项目根目录下的 `cookies.json`：

```json
[
  {"Name": "auth-token", "Value": "..."},
  {"Name": "company_id", "Value": "..."},
  {"Name": "env_key",    "Value": "..."},
  {"Name": "uid",        "Value": "..."},
  {"Name": "zid",        "Value": "..."}
]
```

也可以直接把列表写进 `config.json` 的 `COOKIES` 字段（二选一，`COOKIES` 优先）。

### 4. 运行

```bash
python purchase_tracking.py
```

入口逻辑（`__main__`）只做两件事：读取一次 `config.json` → 把配置整体作为唯一入参**单次调用**
主逻辑方法 `run_and_save_result(config)`。没有命令行参数，所有配置（日期范围、落盘文件名等）都在 `config.json` 里改。
`run_and_save_result` 只接收 `config` 一个参数，日期范围取 `START_DATE` / `END_DATE`、落盘文件名取 `OUTPUT_FILE`；
结果落盘由 `save_result` 单独封装，主流程本身（`run_from_config`）保持纯粹。

返回结构：

```json
{
  "phase1": {"table1": {"created": 12, "updated": 30}, "table2": {"created": 45, "updated": 120}},
  "phase2": {"table1": {"created": 0,  "updated": 8},  "table2": {"created": 0,  "updated": 25}},
  "cleanup": {"triggered": true, "table1_deleted": 3, "table2_deleted": 10}
}
```

### 5. 作为库调用

```python
from purchase_tracking import run_purchase_tracking

result = run_purchase_tracking(
    cookies=cookies,
    app_id="cli_xxx",
    app_secret="xxx",
    feishu_url="https://xxx.feishu.cn/base/xxxxxxxxxxxxxxxxxxxxxx",
    start_date="2026-08-01",
    end_date="2026-08-28",
)
```

从 `config.json` 驱动：

```python
from purchase_tracking import load_config, run_from_config, run_and_save_result

config = load_config()                                   # 读取 config.json
result = run_from_config(config)                         # 只跑主流程

config["OUTPUT_FILE"] = "out.json"                       # 需要换落盘文件时先改 config
run_and_save_result(config)                              # 主流程 + 结果落盘
```

---

## 三、飞书多维表格结构

### 表1「采购单」— 订单级，主键为 `采购单号`

| 字段 | 类型 | 来源 |
|------|------|------|
| 采购单号 | 文本（索引列） | 领星 `order_sn` |
| 1688订单号 | 文本 | 领星 `alibaba_order_sn` |
| 是否完成下单 | 复选框 | 履约状态为「待到货」或「已完成」 |
| 下单日期 | 日期 | 领星 `order_time` |
| 发货日期 | 日期 | 物流轨迹最早一条 |
| 签收日期 | 日期 | 轨迹状态为「已签收」时取最新一条 |
| 入库日期 | 日期 | 采购单详情 `receive[0]` |
| 备注 | 文本 | 物流接口 `data.list[].logistics_order_no` 为「不需要物流」时写入「不需要物流」；其余情况不写入、不覆盖已有值 |

> 加工日期已移至表2（SKU 级）；表1 如需展示，请从表2 按采购单聚合。
> 存量表1 中残留的「加工日期」字段不再写入，可手动删除。

### 表2「采购单SKU明细」— SKU 级，主键为 `采购单号_SKU`（组合键 `采购单号#SKU`）

| 字段 | 类型 | 说明 |
|------|------|------|
| 采购单号_SKU | 文本（索引列） | `采购单号#SKU`，用于去重 |
| SKU | 文本 | SKU 编码 |
| 采购单号 | 单向关联 → 表1 | 存表1 的 `record_id` 数组 |
| plan_sn | 文本 | 加工单查询钥匙，A4 自动确保该字段存在 |
| 加工日期 | 日期 | 该 SKU 对应加工单的完成时间（`finish_time`），写入毫秒时间戳；空值不覆盖；同批加工的 SKU 行相同属正常 |

> 若是全新环境，可用 `archive/create_feishu_tables.py` 建表；表2 结构变更过，可用 `archive/rebuild_table2.py` 重建。

---

## 四、文件说明

### 根目录（核心 + 配置）

| 文件 | 作用 |
|------|------|
| `purchase_tracking.py` | **主入口**，单文件版全流程（A2/A3/A4/B1 + 配置驱动 + CLI）。不依赖任何其他本地模块 |
| `config.json` | 隐私配置（真实凭据），已被 `.gitignore` 忽略，**不入库** |
| `config.example.json` | 配置模板，需复制为 `config.json` 后填写 |
| `cookies.json` | 领星 cookies（自行创建），已忽略，不入库 |

### `archive/`（归档，非核心）

以下模块的功能均已被 `purchase_tracking.py` 单文件版完整覆盖，仅作留存，日常运行不需要：

| 文件 | 作用 |
|------|------|
| `archive/lingxing_collector.py` | 领星采集模块，只含 A3 并发解析 |
| `archive/feishu_uploader.py` | 飞书双表 upsert 上传模块（依赖同目录的 lingxing_collector） |
| `archive/feishu_reader.py` | 读取「未入库单」视图并还原为 A3 输入结构（B1 独立版） |
| `archive/create_feishu_tables.py` | 初始化创建表1、表2 及全部字段 |
| `archive/rebuild_table2.py` | 删除旧表2 并按「组合键索引」结构重建 |
| `archive/verify_tables.py` | 打印多维表格所有表/字段/类型/关联，用于排查结构 |

> 归档文件仍在版本库内（`git mv` 记录为重命名），随时可取回：
> `git mv archive/verify_tables.py .`

### 运维辅助命令

```bash
# 查看表结构与 table_id（需要飞书凭据环境变量）
export FEISHU_APP_ID=cli_xxx
export FEISHU_APP_SECRET=xxx
export FEISHU_APP_TOKEN=xxxxxxxxxxxxxxxxxxxxxx
python archive/verify_tables.py

# 只导出未入库单，不写回（可用于排查）
python archive/feishu_reader.py --out unstocked_orders.json
```

---

## 五、关键实现说明

**并发模型**：A3 使用两个相互独立的线程池 —— 订单池（`ORDER_WORKERS=10`）驱动订单处理，
HTTP 池（`HTTP_WORKERS=20`）承载子请求。两者分离是为了避免嵌套提交导致的线程池死锁。

**幂等 upsert**：
- 表1 以 `采购单号` 为键，表2 以 `采购单号#SKU` 为键
- 先全量读取建索引，再分流 `batch_create` / `batch_update`，每批 500 条
- 表1 必须先写，表2 的关联字段需要用到表1 的 `record_id`

**部分更新（重要）**：A4 只写入非 `None` 的字段。回跑时某些字段（如加工日期）可能取不到值，
若整行全量覆盖会把首轮存下来的数据抹掉。

**空值不覆盖**：同理，表2 的 `plan_sn` 仅在有值时才写入。

**备注打标**：物流接口 `data.list[].logistics_order_no` 为「不需要物流」时，表1「备注」写入「不需要物流」；
非该值则备注字段不写入（None 不覆盖），避免回跑时抹掉已有的人工备注。

**C5 清理（破坏性）**：`CLEANUP_FINISHED=true`（默认）且表1/表2 任一记录数超过
`CLEANUP_THRESHOLD`（默认 15000）时触发：删除表1「加工日期」非空或「备注」非空的记录，
并删除表2 引用这些被删订单的行（避免孤儿关联）。注意：表1「备注」非空即删，
**人工填写的备注也会被删除**；未超阈值或开关关闭时跳过。

**容错**：
- 所有 HTTP 请求带 30s 超时 + 3 次重试 + 指数退避（1s / 2s / 4s）
- 单个订单解析异常只记录失败单号，不会中断整体流程或丢失已解析数据
- 跳过 `status_text` 为空、`已作废`、`item_list` 为空的订单

**日期处理**：写入时把 `yyyy/MM/dd HH:mm` 等格式统一转成 13 位毫秒时间戳（北京时间）；
读取时反向转回字符串。无法解析的值记 WARNING 并跳过该字段。

---

## 六、已知限制

1. **`plan_sn` 需要表2 支持**：B1 回跑依赖表2 的 `plan_sn` 字段还原加工单查询条件。
   `run_purchase_tracking` 会以幂等方式自动补建该字段（`auto_migrate=True`）。
   若关闭该参数且字段缺失，「加工日期」在回跑时无法刷新。

2. **`status_text` 是兜底值**：飞书只存了「是否完成下单」布尔值，无法还原原始状态文本。
   B1 重建时统一填入默认 `"待到货"`（可用 `default_status_text` 参数覆盖）。

3. **「是否完成下单」语义待确认**：代码里该字段判断的是履约状态（`待到货`/`已完成`），
   而非字面意义的「是否已提交下单」。如与业务口径不符，需修改 `process_order` 中的判断。

4. **领星 cookies 会过期**：属于浏览器会话凭据，失效后需在浏览器中重新登录后重新导出
   `cookies.json`。本项目未做自动续期。

5. **非官方接口**：领星侧接口（`maique.lingxing.com/api/...`）为前端逆向所得，
   无官方稳定性承诺，接口变更时需同步调整请求参数。

---

## 七、安全提醒

- `config.json`、`cookies.json` 已在 `.gitignore` 中，**请勿强制提交**（`git add -f`）
- 提交前自检：`git status --short` 中不应出现上述两个文件
- 曾用 `git check-ignore -v config.json` 校验过忽略规则生效
- 若凭据曾经泄露到版本库，请立即到飞书开放平台重置 App Secret，并在领星重新登录使旧 cookies 失效
- 运行输出（`*_result.json`、`unstocked_orders.json`）同样已忽略
- **不要**把真实的 `app_token`、表格域名、App Secret 前缀写进代码注释、docstring 或文档示例里 ——
  这些同样会进版本库。示例代码一律用 `xxx` / `xxxxxxxx` 占位符

---

## 八、常见问题

| 现象 | 排查方向 |
|------|----------|
| `缺少配置文件 config.json` | 未复制模板，执行 `cp config.example.json config.json` |
| `config.json 不是合法 JSON` | 手工编辑时漏了逗号/引号，用 `python -c "import json;json.load(open('config.json',encoding='utf-8'))"` 定位 |
| `auth-token not found in cookies` | cookies 导出不完整，需包含 `auth-token` |
| `未找到名为「未入库单」的视图` | 表1 中视图名与 `VIEW_NAME` 不一致，用 `archive/verify_tables.py` 核对 |
| 加工日期一直为空 | 检查表2 是否有 `plan_sn` 字段且已写入；检查该订单是否真的有加工单 |
| 大量请求重试失败 | 领星 cookies 大概率已过期，重新导出 |
| 表2 报「找不到对应采购单记录」 | 表1 写入失败被跳过，检查表1 的创建/更新统计 |
