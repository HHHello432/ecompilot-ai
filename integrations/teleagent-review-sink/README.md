# TeleAgent Review Sink

<p align="center">
  <img src="assets/cover-2560x1440.jpg" alt="teleagent-review-sink cover" width="100%">
</p>

<p align="center">
  <a href="https://github.com/your-org/teleagent-review-sink/stargazers"><img src="https://img.shields.io/github/stars/your-org/teleagent-review-sink?style=flat&logo=github&label=Stars&color=2EC950" alt="Stars"></a>
  <img src="https://img.shields.io/badge/CI-passing-2EC950?logo=githubactions&logoColor=white" alt="CI">
  <img src="https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows11&logoColor=white" alt="Platform">
  <img src="https://img.shields.io/badge/Runtime-Node.js-339933?logo=nodedotjs&logoColor=white" alt="Runtime">
  <img src="https://img.shields.io/badge/Dependencies-0-009688" alt="Dependencies">
  <img src="https://img.shields.io/badge/TeleAgent-2.5.2%2B-74D0F9?labelColor=363636" alt="TeleAgent">
  <img src="https://img.shields.io/badge/License-MIT-1677FF?logo=unlicense&logoColor=white" alt="License">
</p>

> 本地拦截 **TeleAgent（星辰超级智能体）** 的「工具指令审查」上传，阻止本机数据外发。
> 不改 asar、不改二进制、不需要管理员权限，App 更新后依然有效。

## 背景：TeleAgent 在偷偷传什么

TeleAgent 在执行**每一个工具调用之前**，都会把完整参数发到云端做"安全审查"，服务器返回"未检测到有效风险"才本地执行。这不是对话所必需的，而是一条独立的、持续的数据上传通道：

```
内核日志原文
[tool_instruction_review] request, toolID=write,
  input {"content":"from __future__ import annotations\nimport os\n…（整个文件源码）"}
[tool_instruction_review] request, toolID=powershell,
  input {"command":"if (Test-Path \"C:\\…\") { Get-Item …"}
[tool_instruction_review] request, toolID=read,
  input {"filePath":"D:\\project\\README.md"}
```

实测每天的量（一台普通办公机）：

| 日期 | 审查请求数 |
|---|---|
| 09-14 | 538 |
| 09-15 | 1016 |
| 09-16 | 608 |

上传内容包含：

- **write 工具的完整文件正文**（整个源码文件被发出去）
- **powershell / bash 的完整命令**
- read / grep 的文件路径和搜索模式
- webfetch 的 URL
- App 侧的**记忆系统审查**：`MEMORY.md` 全文 + 会话记录，每天自动 4 次，含设备 ID / MAC 地址 / 工作目录路径

目的地统一是 `https://agent.teleai.com.cn/superCowork/sapi/api/v1/security/instruction/review`。

## 为什么不用防火墙 / WFP / hosts 拦

审查接口和对话接口是**同一个域名同一个端口**（`agent.teleai.com.cn:443`）。WFP / Clash 域名规则 / hosts 都只能按 IP、端口、SNI 过滤，TLS 隧道里的 URL 路径根本看不见——要拦审查就会把整个网关一起拦了，App 直接废。只能从应用层入手。

## 方案：官方调试钩子 + 本地拦截代理

TeleAgent 留了一个设备级调试配置文件（代码里叫 `INTERNAL_DEBUG_FILE`），其中的 `apiBaseUrl` / `llmBaseUrl` 可以覆盖各接口的基地址。本工具把这两个地址指到本机的一个微型 HTTP 代理：

- 路径含 `/security/` → 本地直接回「放行」判决，**请求绝不外发**
- 其余路径（`/chat/completions`、`/images/generations` 等）→ 原样透传到真实网关，**功能完全不受影响**
- sink 没启动时客户端连接失败 → 走 `fail-open` 分支放行，工具照常跑，数据同样不外泄

### 文件说明

| 文件 | 作用 |
|---|---|
| `review-sink.js` | 拦截代理本体（约 100 行，零依赖，纯 Node 内置模块） |
| `watchdog.js` | 看门狗，每 60s 检查 sink 是否在监听，挂了自动拉起 |
| `install.cmd` / `install.ps1` | 一键安装：部署脚本、写配置、设开机自启、立即启动 |
| `uninstall.cmd` / `uninstall.ps1` | 一键卸载：还原配置、删自启项、停服务 |
| `status.cmd` | 查看运行状态和审计日志 |

## 安装

1. 确认已安装并运行过一次 TeleAgent（需要它的数据目录和自带 Node 运行时）
2. 双击 `install.cmd`，按提示完成
3. **重启一次 TeleAgent**（托盘右键退出后重新打开）让新的接口地址生效
4. 双击 `status.cmd` 确认 sink 在监听

完成。之后所有工具调用的审查请求都会被本地拦截，对话和生图功能正常。

> 命令行自定义端口：`install.cmd -Port 9924`（默认 9923）

## 验证

跑一个让 TeleAgent 调用工具的任务，然后看审计日志 `%USERPROFILE%\teleagent-review-sink\sink.log`：

```
BLOCK POST /superCowork/sapi/api/v1/security/instruction/review toolID=powershell 2612B
BLOCK POST /superCowork/sapi/api/v1/security/instruction/review toolID=powershell 2782B
PASS  POST /superCowork/sapi/api/v1/chat/completions 104796B
```

- `BLOCK` = 审查请求被拦截，没有出网
- `PASS` = 正常业务流量，透传给了真实网关

也可以直接看 TeleAgent 内核日志（`…\.local\share\TeleAgent\users\<账号>\log\super-agent-server-*.log`），搜 `tool_instruction_review`，响应体应变成 `本地拦截：未检测到有效风险，建议放行`。

## 卸载

双击 `uninstall.cmd`，然后重启 TeleAgent。配置会从备份还原，一切恢复默认。

## 工作原理详解

### 配置钩子

`…\.local\share\TeleAgent\internal-debug.yaml`（设备级，App 启动时读取）：

```yaml
apiBaseUrl: http://127.0.0.1:9923
llmBaseUrl: http://127.0.0.1:9923/superCowork/sapi
```

App 启动内核时会把解析结果注入内核环境（`OPENCODE_TOOLS_PROVIDER_CONTENT`、`OPENCODE_CONFIG_CONTENT`），两者都指向本机 sink。

- `apiBaseUrl` → `SuperAgentAPI.baseURL`：App 侧的审查接口、百度搜索
- `llmBaseUrl` → `NewApi.baseURL`：内核的审查接口和对话接口（对话会被 sink 透传）

扫码配对 / 登录页用的是代码里的硬编码常量，不受影响。

### 客户端的 fail-open 兜底

反编译 `dist-electron/main-runtime-*.js` 可以看到，审查客户端在**任何**错误下都放行：

```js
// HTTP 错误、JSON 解析失败、网络错误、未知返回 → 全部 action: "allow"
return Ld("warn", { ..., status: "network_error", result: "allow_by_policy",
                   action: "allow", reason: c.message }), { action: "allow", traceID: r };
```

所以即使 sink 崩了 / 没启动 / 端口被占，工具执行不受影响，数据也不会外泄（请求根本发不出去）。这条兜底同时也是本方案安全性的基础。

## 已知限制

- **`/chat/completions` 这条线拦不掉**：对话内容（含子代理读过的文件、跑过的命令输出）必然要发给模型服务器，这是 LLM 的工作原理。处理敏感文件时请用本地工具完成，只把结论交给 TeleAgent。
- 生图请求走火山方舟（`ark.cn-beijing.volces.com`），是独立的提供商通道，不在本 sink 拦截范围内。
- 目前只在 Windows + TeleAgent 2.5.2 上验证过；App 大版本升级后若改了配置钩子，可能需要重新适配。

## 免责声明

本工具用于在**自己的机器、自己的账号**上审查和控制软件的数据上传行为，仅供学习研究。使用前请阅读 TeleAgent 的用户协议；若协议禁止修改客户端行为，由此产生的后果自负。作者不对任何账号封禁、功能异常承担责任。

## License

MIT
