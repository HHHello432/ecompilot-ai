# EcomPilot AI 电商运营诊断助手

EcomPilot AI 是一个面向中小电商商家的智能运营诊断系统。它不是简单的数据看板，而是结合电商核心指标、商品分层规则和可选大语言模型能力，帮助商家快速发现 GMV、ROAS、转化率、退货率和库存周转中的关键问题。

## 已完成能力

- 上传 CSV / Excel，自动清洗商品经营数据
- 计算 CTR、CVR、GMV、ROAS、客单价、毛利、净利润、退货率、库存风险
- 自动识别爆款商品、潜力商品、引流商品、利润商品、滞销商品、高退货风险商品、亏损商品
- 生成老板能看懂的运营诊断、风险提醒和优化动作
- 支持 AI 问答：预算加给谁、哪些商品亏钱、店铺最大问题、日报生成
- Streamlit 可视化前端：经营看板、商品分层、诊断、问答、日报导出
- FastAPI 后端接口：上传、分析、诊断、问答、日报下载
- React 工作台：今日必做清单、任务完成状态、近次复盘趋势、预算模拟、模板下载、Markdown / HTML 报告下载
- 风险商品处理台：按商品给出风险原因、处理动作、优先级和预计影响
- 行动清单可导出 CSV，任务状态会按分析记录保存到后端并保留本地兜底
- 预留抖店、拼多多、淘宝 / 天猫、京东连接器状态接口；默认关闭，不影响本地演示和文件上传模式
- **领星 ERP 数据源**：Cookie 会话接入，一键拉取产品表现 / 库存 / 发货 / 采购 / 应收并直接生成诊断（见 `docs/领星ERP数据源接入.md`）
- **RPA 控制台集成**：内置八爪鱼 RPA 控制台，作为托管子服务经 `/console/*` 反向代理提供完整 Web UI
- Docker / Docker Compose / GitHub Actions / pytest 基础测试

## 快速开始

```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

启动新版 React 工作台：

```bash
cd frontend/react
npm install
npm run dev
```

访问地址：

```text
React 工作台：http://127.0.0.1:5173
API 文档：http://127.0.0.1:8000/docs
```

工作台会默认载入演示数据，也可以上传自己的 CSV / Excel。旧版 Streamlit 页面仍保留为轻量备用：`streamlit run frontend/streamlit_app.py`。

上传前可以在工作台左侧点击“下载数据模板”，填好模板后再上传。

模板也可以通过接口下载：

```text
GET http://127.0.0.1:8000/api/template/download
```

常用运营接口：

```text
GET  /api/analysis/{file_id}       获取指标和商品明细
GET  /api/diagnosis/{file_id}      获取运营诊断
POST /api/question/{file_id}       基于当前数据问答
GET  /api/report/{file_id}         获取 Markdown 日报
GET  /api/report/{file_id}/html    下载 HTML 日报
GET  /api/history                  获取最近分析记录趋势
GET  /api/tasks/{file_id}          读取今日清单完成状态
PUT  /api/tasks/{file_id}          保存今日清单完成状态
GET  /api/platforms                查看平台连接器配置状态
POST /api/platforms/{platform}/sync 平台同步占位接口
```

## 可选平台对接

项目默认不连接真实店铺，上传文件和演示数据可以完整运行。后端已经提供轻量连接器层，支持生成平台授权地址、接收 OAuth 回调、保存店铺授权状态，并创建同步任务入口；后续补具体商品、订单、售后 API 时只需要扩展 `backend/platforms/`。

启用前需要在对应开放平台创建应用、申请店铺授权和 API 权限。

```text
ECOMPILOT_ENABLE_PLATFORM_CONNECTORS=false
ECOMPILOT_PUBLIC_BASE_URL=http://127.0.0.1:8000
DOUDIAN_APP_KEY=
DOUDIAN_APP_SECRET=
DOUDIAN_AUTH_URL=https://op.jinritemai.com/oauth/authorize
DOUDIAN_TOKEN_URL=https://openapi-fxg.jinritemai.com/oauth2/access_token
PDD_CLIENT_ID=
PDD_CLIENT_SECRET=
PDD_AUTH_URL=https://oauth.pinduoduo.com/authorize
PDD_TOKEN_URL=https://open-api.pinduoduo.com/oauth/token
TAOBAO_APP_KEY=
TAOBAO_APP_SECRET=
TAOBAO_AUTH_URL=https://oauth.taobao.com/authorize
TAOBAO_TOKEN_URL=https://oauth.taobao.com/token
JD_APP_KEY=
JD_APP_SECRET=
JD_AUTH_URL=https://oauth.jd.com/oauth/authorize
JD_TOKEN_URL=https://oauth.jd.com/oauth/token
```

平台连接流程：

```text
POST /api/platforms/{platform_id}/auth/start  生成授权地址
GET  /api/platforms/{platform_id}/callback    接收平台 code 回调并换取 token
POST /api/platforms/{platform_id}/sync        创建同步任务
```

### 领星 ERP 数据源

领星走的是 **Cookie 会话**（不是 OAuth），在 `registry.py` 中以 `auth_mode="cookie"` 注册。
配置 `LINGXING_COOKIE` 后即可一键拉数并直接出诊断：

```text
POST /api/platforms/lingxing/pull?start_date=2026-09-01&end_date=2026-09-07
```

详见 [`docs/领星ERP数据源接入.md`](docs/领星ERP数据源接入.md)。

## RPA 控制台

仓库内置八爪鱼 RPA 控制台（`integrations/octopus-rpa-console-dashboard`）。它自带纯标准库 HTTP 服务与完整前端，
EcomPilot 启动时会把它作为**托管子服务**拉起，并通过 `/console/*` 反向代理暴露，因此只需启动一个进程：

```text
浏览器打开  http://127.0.0.1:8000/console/      # 完整控制台 UI
工作台侧栏「RPA 控制台」卡片 → 打开控制台
GET  /api/console/status                        # 子服务状态
POST /api/console/start | /api/console/stop     # 手动启停
```

关闭方式：`ECOMPILOT_ENABLE_CONSOLE=0`（关闭后相关接口返回 503）。
端口可配：`ECOMPILOT_CONSOLE_PORT`（默认 8010）。

集成细节见 [`docs/集成模块说明.md`](docs/集成模块说明.md)。

## 使用 Docker

```bash
cp .env.example .env
docker compose up --build
```

- React 工作台：http://127.0.0.1:5173
- 后端：http://127.0.0.1:8000
- Streamlit 备用：http://127.0.0.1:8501

## 数据字段

上传文件需要包含以下字段，支持常见中文或英文别名：

| 字段 | 含义 |
| --- | --- |
| 商品名 | SKU 或商品名称 |
| 曝光量 | 商品被展示次数 |
| 点击量 | 商品被点击次数 |
| 访客数 | 访问商品详情或承接页的人数 |
| 下单数 | 成交订单数量 |
| 支付金额 | GMV |
| 广告花费 | 投放消耗 |
| 成本 | 商品成本 |
| 退货金额 | 退款或退货金额 |
| 库存 | 当前库存 |

## 可选大模型接入

没有 `OPENAI_API_KEY` 时，系统会使用内置运营规则生成诊断和问答，项目仍然完整可用。

如需使用大模型增强诊断：

```bash
cp .env.example .env
```

在 `.env` 中填入：

```text
OPENAI_API_KEY=你的密钥
OPENAI_MODEL=gpt-4.1-mini
```

## 测试

```bash
pytest -q
```

前端构建：

```bash
cd frontend/react
npm run build
```

## 项目结构

```text
backend/
  api/                FastAPI 路由
  platforms/          可选电商平台连接器注册表
  services/           数据清洗、指标、分层、诊断、日报
  models/             接口模型
  database/           本地分析结果和任务状态存储
frontend/
  react/              React + Ant Design 工作台
  streamlit_app.py    可视化前端
sample_data/
  ecommerce_demo.csv  演示数据
  ecommerce_template.csv 数据上传模板
integrations/         集成工具（已脱敏）
  octopus-rpa-console-dashboard/  八爪鱼 RPA 控制台（托管子服务）
  lingxing-*/                     领星 ERP 数据拉取脚本（接口依据留存）
  teleagent-*/                    企业微信会话工具（未接入）
docs/
  技术设计文档.md
  接口文档.md
  项目说明书.md
  领星ERP数据源接入.md
  集成模块说明.md
```
