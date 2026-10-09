# 领星 ERP 数据同步 · 4 任务整合版（lingxing-net-input-stats-to-feishu）

从**领星 ERP** 拉取四类数据，整体覆盖写入**飞书在线表格**（Sheets）。
单文件 `lingxing_sync.py` 统一管理，纯 Python 标准库实现，零第三方依赖。

## 一、四个任务

| 任务 | 命令 | 数据源接口 | 目标 sheet |
|------|------|-----------|-----------|
| 发货单明细（含成本） | `python lingxing_sync.py shipment` | `maique.lingxing.com/api/shipment/*` | 2hshkO「发货单详情」 |
| 月度应收报告 | `python lingxing_sync.py receivable` | `gw.lingxingerp.com/bd/sp/api/monthly/receivable/report/list` | 3WpisA「应收结算报告」 |
| FBA 在售库存（含成本） | `python lingxing_sync.py fba` | `maique.lingxing.com/api/storage/fbaLists` | 4DjziB「FBA在售库存详情」 |
| 广州仓本地库存 | `python lingxing_sync.py stock` | `maique.lingxing.com/api/storage/lists` | 5vpUvJ「广州仓库存」 |

全部任务：`python lingxing_sync.py all`（按上表顺序执行）。

`--dry-run` 只拉取并映射、不写飞书，结果写入 `output/{task}_result.json`。

## 二、快速开始

```bash
cp config.example.json config.json
# 编辑 config.json：填飞书凭据、领星 cookies、各任务筛选参数与表格 URL
python lingxing_sync.py all
```

config.json 分组结构：

```json
{
  "APP_ID": "cli_xxx",
  "APP_SECRET": "xxx",
  "COOKIES": [{"Name": "auth-token", "Value": "..."}, ...],
  "SHIPMENT":   {"START_DATE": "auto", "END_DATE": "auto", "FEISHU_URL": "..."},
  "RECEIVABLE": {"SETTLE_MONTH": "auto", "SIDS": [], "EXCLUDE_SELLERS": [], "FEISHU_URL": "..."},
  "FBA":        {"FEISHU_URL": "..."},
  "STOCK":      {"FEISHU_URL": "..."}
}
```

> **日期自动动态化**：发货单日期与应收结算月设为 `"auto"`（或留空）时，
> 脚本自动取当前自然月（发货单 = 当月 1 号 ~ 月末，结算月 = 当月），每月运行无需改配置；
> 也可显式写死日期（如补跑上月）。若直接以字典方式调用任务函数，需先经
> `load_config()` 填充（`month_excel_serial` 与任务入口会拒绝 `"auto"` 脏值）。

> `config.json`、`cookies.json`、`output/`（运行产物）已在 `.gitignore` 中，不入库。

## 三、流程

所有任务共用同一套模式（cookies 构造请求头 → 分页拉取 → 字段映射 → v2 分批写入飞书）：

```
分页拉取领星接口（通用 fetch_paged，按任务配置 list/total 路径）
        ↓
字段映射英文变量 → 中文表头（各任务独立 HEADERS / COLUMN_MAPPING）
        ↓
整体覆盖写入飞书 + 特殊列处理（公式列 / 日期序列号）
```

任务特有处理：
- **发货单**：按当月发货时间拉取（config `SHIPMENT` 的 START/END_DATE）；列表枚举 SP 单号 →
  并发（10 线程）调详情取成本；发货时间统一格式化；货件单号用 FBA 号（shipment_id）；
  发货仓库店铺空值回退店铺名；T 列「店铺(不含站点)」公式；**U 列「时间」自动填当月
  1 号 Excel 序列号**（用户手动维护的月份列，脚本自动填充避免新数据行缺失）。
- **应收报告**：结算月 = config `SETTLE_MONTH`、币种 CNY；**店铺动态获取全部店铺并排除
  名称含「某店铺」的**（config `RECEIVABLE.EXCLUDE_SELLERS`，SIDS 留空即自动获取，新增店铺自动包含）；
  `data.records` 分页；R 列「店铺」公式；S 列「月份」写结算月 1 号 Excel 序列号。
- **FBA 库存**：`list`/`total` 在响应顶层；负责人/属性列表解析；数据量大（1.5 万+ 行）分批写入。
- **广州仓库存**：`data.list` 分页；店铺/产品负责人列表解析；采购单价 `-` 原样保留；分批写入。

## 四、注意事项

1. **领星 cookies 会过期**：失效特征为接口返回「非正常访问/未登录」，
   重新登录领星后导出最新 cookies 更新 `config.json`。
2. **飞书权限**：应用需开通电子表格读写权限，且各目标表格需分享给该应用（可编辑）。
3. **接口非官方**：领星网页端私有接口，字段变更时需同步调整各任务 `COLUMN_MAPPING`。
4. **全量覆盖**：写入会覆盖目标 sheet 数据区（表头保留）；写入后自动探测并清空旧数据残留行。
5. **耗时**：FBA 库存与广州仓库存全量拉取约 3~8 分钟，长任务建议后台运行。

## 五、文件说明

| 文件 | 作用 |
|------|------|
| `lingxing_sync.py` | **主入口**（4 任务整合，统一 CLI） |
| `config.json` | 隐私配置（分组），已被 .gitignore 忽略 |
| `config.example.json` | 配置模板 |
| `output/` | 运行产物（dry-run 结果 JSON、旧数据备份、探测文件、skill 分发包），已 gitignore |
| `archive/` | 旧版单任务脚本（shipment_sync / receivable_sync / fba_inventory_sync / stock_sync），功能已并入整合版 |
