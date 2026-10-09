import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Alert,
  Button,
  ConfigProvider,
  Input,
  Layout,
  Segmented,
  Space,
  Table,
  Tag,
  Upload,
  message,
  theme,
} from "antd";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  CheckCircle2,
  Clipboard,
  Download,
  FileText,
  Gauge,
  MessageSquareText,
  PackageCheck,
  RefreshCw,
  RotateCcw,
  Sparkles,
  TrendingUp,
  UploadCloud,
  WalletCards,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import brandIcon from "./assets/brand-icon.png";
import "./styles.css";

const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";

const segmentColors = {
  爆款商品: "#1677ff",
  潜力商品: "#13c2c2",
  引流商品: "#722ed1",
  利润商品: "#52c41a",
  滞销商品: "#8c8c8c",
  高退货风险商品: "#fa541c",
  亏损商品: "#cf1322",
  观察商品: "#d48806",
};

const navItems = [
  { key: "overview", label: "经营看板", icon: BarChart3 },
  { key: "products", label: "商品分层", icon: PackageCheck },
  { key: "diagnosis", label: "运营诊断", icon: Gauge },
  { key: "chat", label: "AI 问答", icon: MessageSquareText },
  { key: "report", label: "日报", icon: FileText },
];

const quickQuestions = [
  "明天应该主推哪些商品？",
  "哪些商品应该立刻降预算或停投？",
  "哪些商品 GMV 高但实际亏钱？",
  "为什么 GMV 高但利润低？",
  "给我一份老板能看懂的日报。",
  "本周最该优化什么？",
];

function App() {
  const [messageApi, contextHolder] = message.useMessage();
  const [fileId, setFileId] = useState("");
  const [analysis, setAnalysis] = useState(null);
  const [diagnosis, setDiagnosis] = useState(null);
  const [report, setReport] = useState("");
  const [active, setActive] = useState("overview");
  const [loading, setLoading] = useState(false);
  const [question, setQuestion] = useState(quickQuestions[0]);
  const [answer, setAnswer] = useState("");
  const [displayedAnswer, setDisplayedAnswer] = useState("");
  const [chatHistory, setChatHistory] = useState([]);
  const [history, setHistory] = useState([]);
  const [completedTasks, setCompletedTasks] = useState({});
  const [platforms, setPlatforms] = useState([]);

  useEffect(() => {
    if (!answer) {
      setDisplayedAnswer("");
      return undefined;
    }

    setDisplayedAnswer("");
    let index = 0;
    const timer = window.setInterval(() => {
      index = Math.min(answer.length, index + 5);
      setDisplayedAnswer(answer.slice(0, index));
      if (index >= answer.length) {
        window.clearInterval(timer);
      }
    }, 12);

    return () => window.clearInterval(timer);
  }, [answer]);

  useEffect(() => {
    loadDemo();
    refreshPlatforms();
  }, []);

  useEffect(() => {
    if (!fileId) {
      setCompletedTasks({});
      return;
    }
    const stored = window.localStorage.getItem(`ecompilot_tasks_${fileId}`);
    setCompletedTasks(stored ? JSON.parse(stored) : {});
    request(`/api/tasks/${fileId}`)
      .then((result) => {
        setCompletedTasks(result.completed || {});
        window.localStorage.setItem(`ecompilot_tasks_${fileId}`, JSON.stringify(result.completed || {}));
      })
      .catch(() => {
        // Keep the local fallback when the server has no task state yet.
      });
  }, [fileId]);

  async function loadDemo() {
    setLoading(true);
    try {
      const upload = await request("/api/demo", { method: "POST" });
      await loadBundle(upload.file_id);
      messageApi.success("演示数据已载入");
    } catch (error) {
      messageApi.error(error.message);
    } finally {
      setLoading(false);
    }
  }

  async function loadBundle(nextFileId) {
    const [nextAnalysis, nextDiagnosis, nextReport] = await Promise.all([
      request(`/api/analysis/${nextFileId}`),
      request(`/api/diagnosis/${nextFileId}`),
      request(`/api/report/${nextFileId}`),
    ]);
    setFileId(nextFileId);
    setAnalysis(nextAnalysis);
    setDiagnosis(nextDiagnosis);
    setReport(nextReport.report);
    setAnswer("");
    setChatHistory([]);
    await refreshHistory();
  }

  async function uploadFile(file) {
    setLoading(true);
    const formData = new FormData();
    formData.append("file", file);
    try {
      const result = await request("/api/upload", { method: "POST", body: formData });
      await loadBundle(result.file_id);
      setActive("overview");
      messageApi.success(result.message);
    } catch (error) {
      messageApi.error(error.message);
    } finally {
      setLoading(false);
    }
    return false;
  }

  async function askQuestion(nextQuestion) {
    const asked = typeof nextQuestion === "string" ? nextQuestion : question.trim();
    if (!asked || !fileId) return;
    setQuestion(asked);
    setLoading(true);
    try {
      const result = await request(`/api/question/${fileId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: asked }),
      });
      setAnswer(result.answer);
      setChatHistory((history) => [{ question: asked, answer: result.answer, id: Date.now() }, ...history].slice(0, 6));
    } catch (error) {
      messageApi.error(error.message);
    } finally {
      setLoading(false);
    }
  }

  async function copyText(text, label = "内容") {
    if (!text) return;
    await navigator.clipboard.writeText(text);
    messageApi.success(`${label}已复制`);
  }

  async function refreshHistory() {
    try {
      const result = await request("/api/history?limit=8");
      setHistory(result.items || []);
    } catch {
      setHistory([]);
    }
  }

  async function refreshPlatforms() {
    try {
      const result = await request("/api/platforms");
      setPlatforms(result.platforms || []);
    } catch {
      setPlatforms([]);
    }
  }

  async function startPlatformAuth(platformId) {
    try {
      const result = await request(`/api/platforms/${platformId}/auth/start`, { method: "POST" });
      if (result.auth_url) {
        window.open(result.auth_url, "_blank", "noopener,noreferrer");
        messageApi.success("已打开平台授权页，授权完成后回到工作台刷新状态。");
      } else {
        messageApi.info(result.message);
      }
    } catch (error) {
      messageApi.error(error.message);
    }
  }

  async function syncPlatform(platformId) {
    try {
      const result = await request(`/api/platforms/${platformId}/sync`, { method: "POST" });
      messageApi.info(result.message);
      await refreshPlatforms();
    } catch (error) {
      messageApi.error(error.message);
    }
  }

  function toggleTask(taskKey) {
    if (!taskKey) return;
    const next = { ...completedTasks, [taskKey]: !completedTasks[taskKey] };
    setCompletedTasks(next);
    if (fileId) {
      window.localStorage.setItem(`ecompilot_tasks_${fileId}`, JSON.stringify(next));
      request(`/api/tasks/${fileId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ completed: next }),
      }).catch(() => {
        messageApi.warning("任务状态已先保存在本机，后端稍后可继续同步。");
      });
    }
  }

  const products = analysis?.products || [];
  const summary = analysis?.summary || {};
  const topProducts = useMemo(() => [...products].sort((a, b) => b.gmv - a.gmv).slice(0, 10), [products]);
  const segmentData = useMemo(
    () =>
      Object.entries(summary.segment_counts || {})
        .filter(([, count]) => count > 0)
        .map(([name, value]) => ({ name, value })),
    [summary],
  );
  const riskProducts = useMemo(
    () =>
      products
        .filter((product) => product.net_profit < 0 || product.refund_rate >= 0.18 || product.stock_turnover_risk === "高")
        .sort((a, b) => b.gmv - a.gmv),
    [products],
  );
  const actionCards = useMemo(() => buildActionCards(summary, products, diagnosis), [summary, products, diagnosis]);
  const dailyTasks = useMemo(() => buildDailyTasks(summary, products, diagnosis), [summary, products, diagnosis]);
  const riskPlaybook = useMemo(() => buildRiskPlaybook(riskProducts), [riskProducts]);
  const dataQuality = useMemo(() => buildDataQuality(summary, products, analysis?.warnings || []), [summary, products, analysis?.warnings]);

  return (
    <ConfigProvider
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          borderRadius: 6,
          colorPrimary: "#1677ff",
          fontFamily:
            "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', sans-serif",
        },
      }}
    >
      {contextHolder}
      <Layout className="app-shell">
        <aside className="sidebar">
          <div className="brand">
            <div className="brand-mark">
              <img src={brandIcon} alt="" />
            </div>
            <div>
              <h1>EcomPilot AI</h1>
              <span>运营诊断工作台</span>
            </div>
          </div>

          <div className="upload-panel">
            <Upload.Dragger accept=".csv,.xlsx,.xls" beforeUpload={uploadFile} maxCount={1} showUploadList={false}>
              <UploadCloud size={22} />
              <strong>上传经营数据</strong>
              <span>CSV / Excel</span>
            </Upload.Dragger>
            <div className="upload-actions">
              <Button icon={<Download size={16} />} onClick={() => downloadFromUrl(`${API_BASE}/api/template/download`)}>
                下载数据模板
              </Button>
              <Button icon={<RefreshCw size={16} />} onClick={loadDemo} loading={loading}>
                载入演示数据
              </Button>
            </div>
            <FieldGuide />
            <PlatformConnections platforms={platforms} onAuthorize={startPlatformAuth} onSync={syncPlatform} />
          </div>

          <nav className="side-nav">
            {navItems.map((item) => {
              const Icon = item.icon;
              return (
                <button
                  className={active === item.key ? "active" : ""}
                  key={item.key}
                  onClick={() => setActive(item.key)}
                  type="button"
                >
                  <Icon size={17} />
                  <span>{item.label}</span>
                </button>
              );
            })}
          </nav>
        </aside>

        <main className="workspace">
          <header className="topbar">
            <div>
              <p>店铺经营概览</p>
              <h2>{activeTitle(active)}</h2>
            </div>
            <Space>
              <Tag color={diagnosis?.source === "llm" ? "blue" : "default"}>
                {diagnosis?.source === "llm" ? "大模型增强" : "规则诊断"}
              </Tag>
              <Button href={`${API_BASE}/docs`} target="_blank">
                API 文档
              </Button>
            </Space>
          </header>

          {analysis?.warnings?.length ? (
            <Alert className="notice" type="warning" message={analysis.warnings.join("；")} showIcon />
          ) : null}

          <KpiStrip summary={summary} />

          {active === "overview" && (
            <Overview
              topProducts={topProducts}
              segmentData={segmentData}
              products={products}
              riskProducts={riskProducts}
              actionCards={actionCards}
              dailyTasks={dailyTasks}
              dataQuality={dataQuality}
              completedTasks={completedTasks}
              history={history}
              summary={summary}
              toggleTask={toggleTask}
              exportTasks={() => downloadActionTasks(dailyTasks, completedTasks)}
            />
          )}
          {active === "products" && <ProductsTable products={products} />}
          {active === "diagnosis" && <DiagnosisView diagnosis={diagnosis} riskPlaybook={riskPlaybook} />}
          {active === "chat" && (
            <ChatView
              question={question}
              setQuestion={setQuestion}
              answer={displayedAnswer}
              isTyping={Boolean(answer) && displayedAnswer.length < answer.length}
              askQuestion={askQuestion}
              loading={loading}
              chatHistory={chatHistory}
              copyText={copyText}
            />
          )}
          {active === "report" && <ReportView report={report} fileId={fileId} copyText={copyText} />}
        </main>
      </Layout>
    </ConfigProvider>
  );
}

function KpiStrip({ summary }) {
  const items = [
    { label: "GMV", value: money(summary.total_gmv), icon: WalletCards, tone: "blue" },
    { label: "净利润", value: money(summary.net_profit), icon: Activity, tone: summary.net_profit >= 0 ? "green" : "red" },
    { label: "ROAS", value: number(summary.overall_roas), icon: Gauge, tone: "cyan" },
    { label: "CTR", value: percent(summary.overall_ctr), icon: BarChart3, tone: "purple" },
    { label: "CVR", value: percent(summary.overall_cvr), icon: PackageCheck, tone: "amber" },
    { label: "退货率", value: percent(summary.refund_rate), icon: RefreshCw, tone: "red" },
  ];
  return (
    <section className="kpi-grid">
      {items.map((item) => {
        const Icon = item.icon;
        return (
          <div className={`kpi ${item.tone}`} key={item.label}>
            <Icon size={18} />
            <span>{item.label}</span>
            <strong>{item.value}</strong>
          </div>
        );
      })}
    </section>
  );
}

function FieldGuide() {
  const fields = [
    "支付金额填 GMV",
    "成本填商品总成本",
    "广告花费填投放消耗",
    "退货金额填退款/售后金额",
  ];
  return (
    <div className="field-guide">
      <div>
        <CheckCircle2 size={15} />
        <strong>填写规范</strong>
      </div>
      {fields.map((field) => (
        <span key={field}>{field}</span>
      ))}
    </div>
  );
}

function PlatformConnections({ platforms, onAuthorize, onSync }) {
  if (!platforms.length) return null;
  const readyCount = platforms.filter((platform) => platform.enabled && platform.configured).length;
  const authorizedCount = platforms.filter((platform) => platform.authorized).length;
  const disabledCount = platforms.filter((platform) => !platform.enabled).length;
  const primary = platforms.find((platform) => platform.authorized) || platforms.find((platform) => platform.configured) || platforms[0];
  const summary =
    disabledCount === platforms.length
      ? `抖店、拼多多等 ${platforms.length} 个未启用`
      : `${readyCount} 个已配置，${authorizedCount} 个已授权`;

  return (
    <div className="platform-box">
      <div className="platform-head">
        <span>
          <PackageCheck size={15} />
          <strong>平台连接</strong>
        </span>
        <em>
          {readyCount}/{platforms.length} 已配置
        </em>
      </div>
      <p>{summary}</p>
      <div className="platform-actions">
        <Button
          disabled={!primary.enabled || !primary.configured}
          icon={<PackageCheck size={13} />}
          onClick={() => onAuthorize(primary.id)}
          size="small"
          type="text"
        >
          授权
        </Button>
        <Button
          disabled={!primary.authorized}
          icon={<RefreshCw size={13} />}
          onClick={() => onSync(primary.id)}
          size="small"
          type="text"
        >
          同步
        </Button>
      </div>
    </div>
  );
}

function Overview({
  topProducts,
  segmentData,
  products,
  riskProducts,
  actionCards,
  dailyTasks,
  dataQuality,
  completedTasks,
  history,
  summary,
  toggleTask,
  exportTasks,
}) {
  return (
    <>
      <TodayTasks completedTasks={completedTasks} exportTasks={exportTasks} tasks={dailyTasks} toggleTask={toggleTask} />
      <section className="action-strip">
        {actionCards.map((card) => {
          const Icon = card.icon;
          return (
            <div className={`action-card ${card.tone}`} key={card.title}>
              <Icon size={18} />
              <span>{card.label}</span>
              <strong>{card.title}</strong>
              <p>{card.detail}</p>
            </div>
          );
        })}
      </section>
      <DataHealth dataQuality={dataQuality} />
      <TrendBrief history={history} />
      <div className="grid overview-grid">
      <section className="panel span-7">
        <PanelHead title="商品 GMV 排名" action="Top 10" />
        <div className="chart-box">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={topProducts} margin={{ top: 10, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="product_name" tick={{ fontSize: 11 }} interval={0} height={64} angle={-35} textAnchor="end" />
              <YAxis tick={{ fontSize: 11 }} tickFormatter={(value) => `${Math.round(value / 10000)}万`} />
              <Tooltip formatter={(value) => money(value)} />
              <Bar dataKey="gmv" radius={[4, 4, 0, 0]}>
                {topProducts.map((entry) => (
                  <Cell key={entry.product_name} fill={segmentColors[entry.segment] || "#1677ff"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      <section className="panel span-5">
        <PanelHead title="商品分层占比" action={`${segmentData.length} 类`} />
        <div className="chart-box">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie data={segmentData} dataKey="value" nameKey="name" innerRadius={62} outerRadius={96} paddingAngle={2}>
                {segmentData.map((entry) => (
                  <Cell key={entry.name} fill={segmentColors[entry.name] || "#8c8c8c"} />
                ))}
              </Pie>
              <Tooltip />
              <Legend />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </section>

      <section className="panel span-8">
        <PanelHead title="ROAS / 净利润矩阵" action="气泡大小代表 GMV" />
        <div className="chart-box short">
          <ResponsiveContainer width="100%" height="100%">
            <ScatterChart margin={{ top: 10, right: 16, bottom: 8, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis type="number" dataKey="roas" name="ROAS" tick={{ fontSize: 11 }} />
              <YAxis type="number" dataKey="net_profit" name="净利润" tick={{ fontSize: 11 }} tickFormatter={(value) => `${Math.round(value / 1000)}k`} />
              <ReferenceLine y={0} stroke="#cf1322" strokeDasharray="4 4" />
              <Tooltip cursor={{ strokeDasharray: "3 3" }} formatter={(value, name) => (name === "净利润" ? money(value) : value)} />
              <Scatter data={products}>
                {products.map((entry) => (
                  <Cell key={entry.product_name} fill={segmentColors[entry.segment] || "#1677ff"} />
                ))}
              </Scatter>
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      </section>

      <section className="panel span-4">
        <PanelHead title="优先处理" action={`${riskProducts.length} 项`} />
        <div className="risk-list">
          {riskProducts.slice(0, 5).map((product) => (
            <div className="risk-row" key={product.product_name}>
              <div>
                <strong>{product.product_name}</strong>
                <span>{product.segment}</span>
              </div>
              <b>{money(product.net_profit)}</b>
            </div>
          ))}
          {!riskProducts.length && <div className="empty">暂无高优先级风险</div>}
        </div>
      </section>
      <section className="panel span-4">
        <BudgetSimulator products={products} />
      </section>
      </div>
    </>
  );
}

function TodayTasks({ completedTasks, exportTasks, tasks, toggleTask }) {
  const doneCount = tasks.filter((task) => completedTasks[task.key]).length;
  return (
    <section className="panel today-board">
      <div className="task-head">
        <PanelHead title="今日必做清单" action={`${doneCount}/${tasks.length} 已完成`} />
        <Button icon={<Download size={15} />} onClick={exportTasks} size="small">
          导出清单
        </Button>
      </div>
      <div className="task-grid">
        {tasks.map((task, index) => (
          <div className={`task-card ${task.tone} ${completedTasks[task.key] ? "done" : ""}`} key={task.key || `${task.title}-${index}`}>
            <button aria-label={`标记${task.title}`} onClick={() => toggleTask(task.key)} type="button">
              {completedTasks[task.key] ? "✓" : task.priority}
            </button>
            <div>
              <strong>{task.title}</strong>
              <p>{task.detail}</p>
              <em>{task.owner}</em>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function TrendBrief({ history }) {
  const items = [...history].reverse();
  const current = history[0]?.summary;
  const previous = history[1]?.summary;
  const metrics = [
    { label: "GMV", key: "total_gmv", render: money },
    { label: "净利润", key: "net_profit", render: money },
    { label: "ROAS", key: "overall_roas", render: number },
  ];

  return (
    <section className="trend-brief">
      <div>
        <strong>近次复盘</strong>
        <span>{history.length > 1 ? "和上一次分析对比" : "上传更多数据后显示趋势"}</span>
      </div>
      <div className="trend-metrics">
        {metrics.map((metric) => {
          const currentValue = Number(current?.[metric.key] || 0);
          const previousValue = Number(previous?.[metric.key] || 0);
          const delta = currentValue - previousValue;
          return (
            <div key={metric.key}>
              <span>{metric.label}</span>
              <strong>{metric.render(currentValue)}</strong>
              {previous ? <em className={delta >= 0 ? "up" : "down"}>{delta >= 0 ? "+" : ""}{metric.render(delta)}</em> : <em>暂无对比</em>}
            </div>
          );
        })}
      </div>
      <div className="sparkline" aria-hidden="true">
        {items.slice(-8).map((item) => {
          const value = Number(item.summary?.net_profit || 0);
          const max = Math.max(...items.map((entry) => Math.abs(Number(entry.summary?.net_profit || 0))), 1);
          const height = Math.max(14, Math.round((Math.abs(value) / max) * 48));
          return <span className={value >= 0 ? "positive" : "negative"} key={item.file_id} style={{ height }} />;
        })}
      </div>
    </section>
  );
}

function DataHealth({ dataQuality }) {
  return (
    <section className="data-health">
      <span>数据质量</span>
      <strong>{dataQuality.summary}</strong>
      <em>{dataQuality.detail}</em>
      <div>
        {dataQuality.items.map((item) => (
          <Tag color={item.tone} key={item.label}>{item.label}</Tag>
        ))}
      </div>
    </section>
  );
}

function BudgetSimulator({ products }) {
  const [lift, setLift] = useState(20);
  const candidates = products
    .filter((product) => product.roas >= 4 && product.net_profit > 0 && product.stock_turnover_risk !== "高")
    .sort((a, b) => b.priority_score - a.priority_score)
    .slice(0, 4);
  const baseAd = candidates.reduce((sum, product) => sum + Number(product.ad_cost || 0), 0);
  const totalGmv = candidates.reduce((sum, product) => sum + Number(product.gmv || 0), 0);
  const totalProfit = candidates.reduce((sum, product) => sum + Number(product.net_profit || 0), 0);
  const avgRoas = baseAd > 0 ? totalGmv / baseAd : 0;
  const profitRate = totalGmv > 0 ? totalProfit / totalGmv : 0;
  const extraAd = baseAd * (lift / 100);
  const expectedGmv = extraAd * avgRoas;
  const expectedProfit = expectedGmv * profitRate - extraAd;

  return (
    <>
      <PanelHead title="预算模拟" action="放量前测算" />
      <div className="budget-sim">
        <Segmented value={lift} options={[10, 20, 30].map((value) => ({ label: `+${value}%`, value }))} onChange={setLift} />
        <div className="budget-result">
          <span>新增预算</span>
          <strong>{money(extraAd)}</strong>
        </div>
        <div className="budget-result">
          <span>预估新增 GMV</span>
          <strong>{money(expectedGmv)}</strong>
        </div>
        <div className="budget-result">
          <span>预估利润变化</span>
          <strong className={expectedProfit >= 0 ? "profit" : "loss"}>{money(expectedProfit)}</strong>
        </div>
        <p>
          {candidates.length
            ? `建议只放量 ${candidates.map((item) => item.product_name).join("、")}，24 小时后按净利润复盘。`
            : "暂时没有适合直接加预算的商品，先处理亏损和转化问题。"}
        </p>
      </div>
    </>
  );
}

function ProductsTable({ products }) {
  const [segment, setSegment] = useState("全部");
  const segments = ["全部", ...Array.from(new Set(products.map((item) => item.segment)))];
  const filtered = segment === "全部" ? products : products.filter((item) => item.segment === segment);

  const columns = [
    { title: "商品", dataIndex: "product_name", fixed: "left", width: 154 },
    {
      title: "运营动作",
      width: 150,
      render: (_, row) => <span className="table-advice">{productAdvice(row)}</span>,
    },
    {
      title: "分层",
      dataIndex: "segment",
      width: 130,
      render: (value) => <Tag color={segmentTagColor(value)}>{value}</Tag>,
    },
    { title: "优先级", dataIndex: "priority_score", sorter: (a, b) => a.priority_score - b.priority_score, width: 92 },
    { title: "GMV", dataIndex: "gmv", render: money, sorter: (a, b) => a.gmv - b.gmv, width: 116 },
    { title: "ROAS", dataIndex: "roas", render: number, sorter: (a, b) => a.roas - b.roas, width: 90 },
    { title: "CTR", dataIndex: "ctr", render: percent, width: 88 },
    { title: "CVR", dataIndex: "cvr", render: percent, width: 88 },
    { title: "退货率", dataIndex: "refund_rate", render: percent, width: 92 },
    { title: "净利润", dataIndex: "net_profit", render: money, sorter: (a, b) => a.net_profit - b.net_profit, width: 116 },
    { title: "库存风险", dataIndex: "stock_turnover_risk", width: 92 },
    {
      title: "风险 / 机会",
      render: (_, row) => (
        <Space size={[4, 4]} wrap>
          {(row.risk_flags || []).map((item) => (
            <Tag color="red" key={item}>{item}</Tag>
          ))}
          {(row.opportunity_flags || []).map((item) => (
            <Tag color="green" key={item}>{item}</Tag>
          ))}
        </Space>
      ),
    },
  ];

  return (
    <section className="panel">
      <div className="table-toolbar">
        <Segmented value={segment} options={segments} onChange={setSegment} />
        <Button icon={<Download size={16} />} onClick={() => downloadCsv(products)}>
          导出商品分析表
        </Button>
      </div>
      <Table
        rowKey="product_name"
        columns={columns}
        dataSource={filtered}
        pagination={{ pageSize: 8, showSizeChanger: false }}
        size="middle"
        scroll={{ x: 1320 }}
      />
    </section>
  );
}

function DiagnosisView({ diagnosis, riskPlaybook }) {
  return (
    <div className="grid diagnosis-grid">
      <section className="panel span-7">
        <PanelHead title="核心判断" action={diagnosis?.source === "llm" ? "LLM" : "规则"} />
        <p className="diagnosis-summary">{diagnosis?.summary}</p>
        <div className="diagnosis-columns">
          <div>
            <h3>风险提醒</h3>
            {(diagnosis?.alerts?.length ? diagnosis.alerts : ["暂未发现高优先级风险。"]).map((item) => (
              <p className="bullet danger" key={item}>{item}</p>
            ))}
          </div>
          <div>
            <h3>优化动作</h3>
            {(diagnosis?.suggestions || []).map((item) => (
              <p className="bullet success" key={item}>{item}</p>
            ))}
          </div>
        </div>
      </section>
      <section className="panel span-5">
        <PanelHead title="风险商品处理台" action="原因 / 动作 / 影响" />
        <div className="queue playbook">
          {riskPlaybook.slice(0, 8).map((item, index) => (
            <div className={`queue-row playbook-row ${item.tone}`} key={item.product.product_name}>
              <span>{index + 1}</span>
              <div className="queue-main">
                <div className="queue-title">
                  <strong>{item.product.product_name}</strong>
                  <Tag color={item.priorityColor}>{item.priority}</Tag>
                </div>
                <div className="queue-meta">
                  <Tag color={segmentTagColor(item.product.segment)}>{item.product.segment}</Tag>
                  <b>{money(item.product.net_profit)}</b>
                </div>
                <div className="playbook-detail">
                  <p><span>原因</span>{item.reason}</p>
                  <p><span>动作</span>{item.action}</p>
                  <p><span>影响</span>{item.impact}</p>
                </div>
              </div>
            </div>
          ))}
          {!riskPlaybook.length && <div className="empty">暂无需要立即处理的风险商品</div>}
        </div>
      </section>
    </div>
  );
}

function ChatView({ question, setQuestion, answer, isTyping, askQuestion, loading, chatHistory, copyText }) {
  return (
    <div className="grid chat-grid">
      <section className="panel chat-panel span-8">
        <PanelHead title="AI 运营问答" action="基于当前数据" />
        <div className="question-row">
          <Input value={question} onChange={(event) => setQuestion(event.target.value)} onPressEnter={askQuestion} />
          <Button type="primary" icon={<Bot size={16} />} loading={loading} onClick={askQuestion}>
            生成回答
          </Button>
        </div>
        <div className="quick-questions">
          {quickQuestions.map((item) => (
            <button key={item} type="button" onClick={() => askQuestion(item)} disabled={loading}>
              {item}
            </button>
          ))}
        </div>
        <div className="answer-actions">
          <Button icon={<RotateCcw size={15} />} onClick={askQuestion} disabled={loading || !question.trim()}>
            重新生成
          </Button>
          <Button icon={<Clipboard size={15} />} onClick={() => copyText(answer, "回答")} disabled={!answer}>
            复制回答
          </Button>
        </div>
        <div className="answer-box">
          {loading && !answer ? "正在分析当前商品、广告、利润和退货数据..." : answer || "输入问题后生成回答。"}
          {isTyping ? <span className="typing-cursor" /> : null}
        </div>
      </section>
      <section className="panel span-4">
        <PanelHead title="问答记录" action={`${chatHistory.length} 条`} />
        <div className="history-list">
          {chatHistory.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => {
                setQuestion(item.question);
              }}
            >
              <strong>{item.question}</strong>
              <span>{item.answer}</span>
            </button>
          ))}
          {!chatHistory.length && <div className="empty">暂无问答记录</div>}
        </div>
      </section>
    </div>
  );
}

function ReportView({ report, fileId, copyText }) {
  return (
    <section className="panel report-panel">
      <div className="table-toolbar">
        <PanelHead title="老板日报" action="Markdown / HTML" />
        <Space wrap>
          <Button icon={<Clipboard size={16} />} onClick={() => copyText(report, "日报")}>
            复制日报
          </Button>
          <Button icon={<Download size={16} />} onClick={() => downloadText(report, "ecompilot_daily_report.md")}>
            下载 Markdown
          </Button>
          <Button icon={<FileText size={16} />} onClick={() => downloadFromUrl(`${API_BASE}/api/report/${fileId}/html`)}>
            下载 HTML 报告
          </Button>
        </Space>
      </div>
      <pre>{report}</pre>
    </section>
  );
}

function PanelHead({ title, action }) {
  return (
    <div className="panel-head">
      <h3>{title}</h3>
      <span>{action}</span>
    </div>
  );
}

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || "请求失败");
  }
  return response.json();
}

function activeTitle(key) {
  return navItems.find((item) => item.key === key)?.label || "经营看板";
}

function money(value = 0) {
  return `¥${Number(value || 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`;
}

function number(value = 0) {
  return Number(value || 0).toFixed(2);
}

function percent(value = 0) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function segmentTagColor(value) {
  const map = {
    爆款商品: "blue",
    潜力商品: "cyan",
    引流商品: "purple",
    利润商品: "green",
    滞销商品: "default",
    高退货风险商品: "orange",
    亏损商品: "red",
    观察商品: "gold",
  };
  return map[value] || "default";
}

function buildActionCards(summary = {}, products = [], diagnosis) {
  const budgetProducts = products
    .filter((product) => product.roas >= 4 && product.net_profit > 0)
    .sort((a, b) => b.priority_score - a.priority_score);
  const lossProducts = products.filter((product) => product.net_profit < 0).sort((a, b) => b.gmv - a.gmv);
  const refundProducts = products.filter((product) => product.refund_rate >= 0.18).sort((a, b) => b.refund_rate - a.refund_rate);
  const stockProducts = products.filter((product) => product.stock_turnover_risk === "高").sort((a, b) => b.stock - a.stock);

  return [
    {
      icon: Sparkles,
      tone: "primary",
      label: "今日最大问题",
      title: summary.net_profit < 0 ? "先止损再拉新" : "利润结构可继续放量",
      detail: diagnosis?.summary || "等待数据分析完成。",
    },
    {
      icon: TrendingUp,
      tone: "success",
      label: "加预算候选",
      title: budgetProducts[0]?.product_name || "暂无明确候选",
      detail: budgetProducts[0]
        ? `ROAS ${number(budgetProducts[0].roas)}，净利润 ${money(budgetProducts[0].net_profit)}，建议小步加 10%-20%。`
        : "先优化转化和利润，再考虑加投。",
    },
    {
      icon: AlertTriangle,
      tone: "danger",
      label: "止损队列",
      title: lossProducts.length ? `${lossProducts.length} 个亏损商品` : "暂无亏损商品",
      detail: lossProducts[0]
        ? `${lossProducts[0].product_name} GMV ${money(lossProducts[0].gmv)}，净利润 ${money(lossProducts[0].net_profit)}。`
        : "继续观察广告消耗和退货波动。",
    },
    {
      icon: PackageCheck,
      tone: "warning",
      label: "售后 / 库存",
      title: refundProducts[0]?.product_name || stockProducts[0]?.product_name || "暂无高风险",
      detail: refundProducts[0]
        ? `退货率 ${percent(refundProducts[0].refund_rate)}，优先排查详情页承诺和差评。`
        : stockProducts[0]
          ? `库存 ${stockProducts[0].stock}，周转风险高，谨慎补货。`
          : "售后和库存暂未触发高风险规则。",
    },
  ];
}

function buildDailyTasks(summary = {}, products = [], diagnosis) {
  const budgetProducts = products
    .filter((product) => product.roas >= 4 && product.net_profit > 0 && product.stock_turnover_risk !== "高")
    .sort((a, b) => b.priority_score - a.priority_score);
  const lossProducts = products.filter((product) => product.net_profit < 0).sort((a, b) => b.gmv - a.gmv);
  const highRefund = products.filter((product) => product.refund_rate >= 0.18).sort((a, b) => b.refund_rate - a.refund_rate);
  const lowCtr = products
    .filter((product) => product.impressions > 3000 && product.ctr < 0.02)
    .sort((a, b) => b.impressions - a.impressions);
  const lowCvr = products
    .filter((product) => product.visitors > 200 && product.cvr < 0.02)
    .sort((a, b) => b.visitors - a.visitors);

  const tasks = [];
  if (lossProducts[0]) {
    tasks.push({
      key: `loss:${lossProducts[0].product_name}`,
      priority: "P0",
      tone: "danger",
      title: `先处理 ${lossProducts[0].product_name}`,
      detail: `净利润 ${money(lossProducts[0].net_profit)}，先降预算或暂停投放，复盘广告、成本和退货。`,
      owner: "投放 / 商品运营",
    });
  }
  if (budgetProducts[0]) {
    tasks.push({
      key: `budget:${budgetProducts[0].product_name}`,
      priority: "P1",
      tone: "success",
      title: `给 ${budgetProducts[0].product_name} 小步放量`,
      detail: `ROAS ${number(budgetProducts[0].roas)}，净利润 ${money(budgetProducts[0].net_profit)}，建议先加 10%-20%。`,
      owner: "投放运营",
    });
  }
  if (highRefund[0]) {
    tasks.push({
      key: `refund:${highRefund[0].product_name}`,
      priority: "P1",
      tone: "warning",
      title: `排查 ${highRefund[0].product_name} 售后`,
      detail: `退货率 ${percent(highRefund[0].refund_rate)}，优先看差评、客服记录、详情页承诺和尺码/质量问题。`,
      owner: "客服 / 商品运营",
    });
  }
  if (lowCtr[0]) {
    tasks.push({
      key: `ctr:${lowCtr[0].product_name}`,
      priority: "P2",
      tone: "info",
      title: `重做 ${lowCtr[0].product_name} 点击入口`,
      detail: `曝光 ${number(lowCtr[0].impressions)}，CTR ${percent(lowCtr[0].ctr)}，先换主图、标题和首屏价格表达。`,
      owner: "内容 / 商品运营",
    });
  }
  if (lowCvr[0]) {
    tasks.push({
      key: `cvr:${lowCvr[0].product_name}`,
      priority: "P2",
      tone: "info",
      title: `优化 ${lowCvr[0].product_name} 转化`,
      detail: `访客 ${number(lowCvr[0].visitors)}，CVR ${percent(lowCvr[0].cvr)}，先补评价、利益点和促销门槛。`,
      owner: "商品 / 活动运营",
    });
  }
  tasks.push({
    key: "daily-review",
    priority: "P3",
    tone: "neutral",
    title: "固定复盘 5 个指标",
    detail: `今天重点看 GMV、ROAS、净利润、退货率和 CVR。${diagnosis?.summary || ""}`,
    owner: "店铺负责人",
  });

  if (summary.net_profit < 0) {
    return tasks.filter((task) => task.priority !== "P3").concat(tasks.filter((task) => task.priority === "P3")).slice(0, 6);
  }
  return tasks.slice(0, 6);
}

function buildRiskPlaybook(riskProducts = []) {
  return riskProducts.map((product) => {
    const severe = product.net_profit <= -5000 || product.refund_rate >= 0.25 || product.stock_turnover_risk === "高";
    const priority = severe ? "高优先级" : product.net_profit < 0 || product.roas < 2.5 ? "中优先级" : "观察";
    return {
      product,
      priority,
      priorityColor: severe ? "red" : priority === "中优先级" ? "orange" : "gold",
      tone: severe ? "danger" : priority === "中优先级" ? "warning" : "info",
      reason: riskReason(product),
      action: riskAction(product),
      impact: riskImpact(product),
    };
  });
}

function buildDataQuality(summary = {}, products = [], warnings = []) {
  const zeroAd = products.filter((product) => Number(product.ad_cost || 0) === 0 && Number(product.gmv || 0) > 0).length;
  const zeroOrders = products.filter((product) => Number(product.visitors || 0) > 0 && Number(product.orders || 0) === 0).length;
  const negativeProfit = products.filter((product) => Number(product.net_profit || 0) < 0).length;
  const items = [
    { label: `${summary.total_products || products.length || 0} 个商品`, tone: "blue" },
    { label: `${summary.profitable_product_count || 0} 个盈利`, tone: "green" },
    { label: `${summary.loss_product_count || negativeProfit} 个亏损`, tone: negativeProfit ? "red" : "default" },
  ];
  if (zeroAd) items.push({ label: `${zeroAd} 个商品无广告花费`, tone: "gold" });
  if (zeroOrders) items.push({ label: `${zeroOrders} 个有访客未成交`, tone: "orange" });
  if (warnings.length) items.push({ label: `${warnings.length} 条清洗提醒`, tone: "volcano" });

  return {
    summary: warnings.length ? "数据已清洗，有字段需要复核" : "字段完整，可以直接诊断",
    detail: warnings.length ? warnings.join("；") : "上传后已完成商品汇总、指标计算和异常检查。",
    items,
  };
}

function riskReason(product) {
  if (product.net_profit < 0) return `净利润为负，GMV ${money(product.gmv)} 但利润 ${money(product.net_profit)}。`;
  if (product.refund_rate >= 0.18) return `退货率 ${percent(product.refund_rate)} 偏高，售后正在吞掉利润。`;
  if (product.stock_turnover_risk === "高") return `库存 ${number(product.stock)}，周转风险高。`;
  if (product.roas < 2.5 && product.ad_cost > 0) return `ROAS ${number(product.roas)} 偏低，投放回收不足。`;
  if (product.cvr < 0.02 && product.visitors > 200) return `访客不少但 CVR 只有 ${percent(product.cvr)}。`;
  return (product.risk_flags || []).join("；") || "指标波动，需要继续观察。";
}

function riskAction(product) {
  if (product.net_profit < 0) return "先降预算 30%-50% 或暂停投放，拆广告花费、成本和退款来源。";
  if (product.refund_rate >= 0.18) return "暂停放量，复盘差评、尺码/质量、客服记录和详情页承诺。";
  if (product.stock_turnover_risk === "高") return "停止补货，配合清仓券或搭配购，先把库存水位压下来。";
  if (product.roas < 2.5 && product.ad_cost > 0) return "收缩低效计划，把预算转向 ROAS 高且净利润为正的商品。";
  if (product.cvr < 0.02 && product.visitors > 200) return "优化详情页前三屏、评价露出、价格锚点和优惠门槛。";
  return "先保持观察，连续 2 天恶化再进入止损队列。";
}

function riskImpact(product) {
  if (product.net_profit < 0) return `优先减少 ${money(Math.abs(product.net_profit) * 0.5)} 左右的亏损暴露。`;
  if (product.refund_rate >= 0.18) return `退货率每降 3 个点，预计可回收约 ${money(product.gmv * 0.03)} GMV 风险。`;
  if (product.stock_turnover_risk === "高") return "能降低库存占用和后续清仓压力。";
  if (product.roas < 2.5) return `先省下低效广告预算 ${money(product.ad_cost * 0.3)} 起。`;
  return "降低后续放量误判概率。";
}

function productAdvice(product) {
  if (product.net_profit < 0) return "降预算 / 止损";
  if (product.refund_rate >= 0.18) return "查售后 / 暂停放量";
  if (product.stock_turnover_risk === "高") return "控补货 / 清库存";
  if (product.roas >= 5 && product.net_profit > 0) return "小步加预算";
  if (product.ctr >= 0.03 && product.cvr < 0.04) return "优化详情页转化";
  if (product.ctr < 0.02 && product.impressions > 3000) return "换主图标题";
  return "观察 24 小时";
}

function downloadCsv(rows) {
  const columns = Object.keys(rows[0] || {});
  const content = [columns.join(","), ...rows.map((row) => columns.map((col) => JSON.stringify(row[col] ?? "")).join(","))].join("\n");
  downloadText(`\ufeff${content}`, "ecompilot_product_analysis.csv", "text/csv;charset=utf-8");
}

function downloadActionTasks(tasks, completedTasks) {
  const rows = [
    ["状态", "优先级", "任务", "执行人", "说明"],
    ...tasks.map((task) => [
      completedTasks[task.key] ? "已完成" : "待处理",
      task.priority,
      task.title,
      task.owner,
      task.detail,
    ]),
  ];
  const content = rows.map((row) => row.map((item) => JSON.stringify(item ?? "")).join(",")).join("\n");
  downloadText(`\ufeff${content}`, "ecompilot_action_tasks.csv", "text/csv;charset=utf-8");
}

function downloadFromUrl(url) {
  const link = document.createElement("a");
  link.href = url;
  link.download = "";
  link.click();
}

function downloadTemplate() {
  const template = [
    ["商品名", "曝光量", "点击量", "访客数", "下单数", "支付金额", "广告花费", "成本", "退货金额", "库存"],
    ["示例商品A", "10000", "500", "450", "35", "6990", "1200", "3200", "200", "120"],
    ["示例商品B", "8000", "260", "240", "18", "2880", "900", "1500", "0", "80"],
  ];
  const content = template.map((row) => row.join(",")).join("\n");
  downloadText(`\ufeff${content}`, "ecompilot_upload_template.csv", "text/csv;charset=utf-8");
}

function downloadText(content, filename, type = "text/markdown;charset=utf-8") {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

createRoot(document.getElementById("root")).render(<App />);
