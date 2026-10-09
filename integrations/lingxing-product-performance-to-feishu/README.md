# 领星产品表现每日同步（lingxing-product-performance-to-feishu）

> 领星ERP → 飞书在线表格 每日数据自动同步脚本

## 📋 功能简介

本脚本实现从**领星ERP**自动拉取产品表现数据，并按负责人拆分后写入**飞书在线表格**对应页签，完成每日数据汇总的全流程自动化。

### 核心流程

```
[0] 用浏览器 Cookie 构造领星接口请求头（不再使用 Selenium 登录）
    ↓
[1] 调用「产品表现-ASIN列表」接口，分页拉取目标日期全部数据
    → 调用「全局标签-关联分析」接口，批量查询 listing 标签
    ↓
[2] 按负责人清洗拆分，写入飞书【每日数据导入（X）】页（限 1500 行以内）
    ↓
[3] 追加写入【产品每日情况（X）】页最后一行（空一行追加）
    ↓
[4] 取固定区块 C7:R7 写入【汇总总和（X）】页对应日期行
```

## 📁 项目结构

```
lingxing-product-performance-to-feishu/
├── main.py                  # 主程序脚本（核心逻辑）
├── config.json              # 敏感配置（不入库，见下方说明）
├── config.example.json      # 配置模板（占位符，可入库）
├── 飞书在线表格实例.xlsx     # 飞书表格结构参考样例
├── 网页请求示例.txt          # 领星API请求curl示例及返回数据样例（已脱敏）
├── README.md                # 项目说明文档
└── .gitignore               # Git忽略规则
```

## 🔧 环境要求

- **Python**: 3.8+
- **依赖**: `requests`

### 依赖安装

```bash
pip install requests
```

> 已移除 Selenium 依赖，无需安装 selenium / webdriver

## ⚙️ 配置说明

敏感配置（领星 Cookie、飞书应用凭证、人员清单）统一存放在脚本同目录的 `config.json` 中，该文件已被 `.gitignore` 排除，**不会提交到 git**。

首次使用：

```bash
# 1. 复制模板为真实配置
cp config.example.json config.json
# 2. 编辑 config.json，填入真实值（见下方字段说明）
```

### 配置字段

```json
{
  "lingxing": {
    "user_agent": "浏览器 User-Agent（建议与登录环境一致）",
    "cookie": "领星 Cookie，支持两种形式"
  },
  "feishu": {
    "app_id": "飞书应用 App ID",
    "app_secret": "飞书应用 App Secret",
    "spreadsheet_token": "目标表格 Token"
  },
  "persons": { "洪": "洪键垌", "张": "张坚富" },
  "principal_uids": [10841059, 11086010]
}
```

| 字段 | 说明 |
|------|------|
| `lingxing.cookie` | 浏览器导出的 Cookie 数组（EditThisCookie），或 Network 面板复制的 `auth-token=xxx; company_id=xxx; ...` 字符串 |
| `lingxing.user_agent` | 与 Cookie 同一登录环境的浏览器 User-Agent，避免被风控识别 |
| `feishu.*` | 企业自建应用凭证（https://open.feishu.cn 创建），应用需开通电子表格读写权限，并添加为目标表格的「可编辑」协作者 |
| `persons` | key=姓氏（拼接页签名），value=负责人全名（与接口 `principal_names` 匹配） |
| `principal_uids` | 对应负责人的领星 UID 列表 |

> **新增人员步骤：**
> 1. 在 `config.json` 的 `persons` 中添加 `"姓": "全名"`
> 2. 将该负责人的 `principal_uid` 加入 `principal_uids`
> 3. 确保飞书中已存在对应命名的三个页签（每日数据导入、产品每日情况、汇总总和）

### 其他参数（main.py 内）

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `TARGET_DATE` | 同步日期（默认前一天） | `today - 1 day` |
| `PAGE_SIZE` | 接口单页拉取条数 | `200` |
| `IMPORT_MAX_ROWS` | 每日数据导入页行数上限 | `1500` |
| `TAG_BATCH_SIZE` | 标签接口每批查询数 | `100` |

## 🚀 运行方式

```bash
# 1. 确认 config.json 已配置（尤其 lingxing.cookie 未过期）
# 2. 直接执行
python main.py
```

> Cookie 会过期，运行报「领星接口返回异常（登录态可能已失效）」时，从浏览器重新复制 Cookie 更新到 `config.json` 即可。

### 定时任务（可选）

可配合 crontab 或 Windows 任务计划程序实现定时执行：

```bash
# 每天早上 8:30 自动同步前一日数据
30 8 * * * cd /path/to/project && python main.py >> sync.log 2>&1
```

## ⚠️ 注意事项

1. **飞书表格行数限制**：【每日数据导入】页写入严格限制在 **1500 行以内**，该页下方可能存在其他内容
2. **Cookie 有效期**：领星 Cookie 有时效性，长时间未运行可能失效，重新复制更新即可
3. **数据安全**：`config.json` 含账号凭证，已被 `.gitignore` 排除；请勿手动强推该文件或修改忽略规则后提交
4. **风控识别**：频繁调用领星接口仍可能触发风控，脚本已做轻微限速

## 📌 数据字段说明

脚本从领星拉取的核心指标包括：

- **销售维度**：销量(volume)、销售额(amount)、订单量(order_items)、退货量/率
- **广告维度**：点击(clicks)、曝光(impressions)、花费(spend)、ACOS、ROAS
- **库存维度**：FBA可用量、在途量、不可售量、总库存
- **利润维度**：毛利(gross_profit)、预估毛利、毛利率、ROI
- **评价维度**：评论数(reviews_count)、平均星级(avg_star)

完整字段定义参见 `网页请求示例.txt` 中的 API 返回数据样例（已脱敏，凭证部分为占位符）。

## 📄 License

本项目为内部工具，仅供团队内部使用。
