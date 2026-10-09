/**
 * 看门狗：每 60s 检查 sink 是否在监听，挂了就拉起。
 * 对话流量经过 sink 时，保证 sink 崩溃也不会断功能。
 *
 * 用法：node watchdog.js [port]
 */
"use strict";

process.title = "TeleAgent Review Sink Watchdog";

const { spawn } = require("child_process");
const net = require("net");
const path = require("path");

const SINK = path.join(__dirname, "review-sink.js");
const PORT = Number(process.argv[2] || 9923);

// 优先用 TeleAgent 自带的 node，其次用系统 node
const TA_NODE = path.join(
	process.env.USERPROFILE || process.env.HOME || "",
	".local", "share", "TeleAgent", "runtimes", "node", "node.exe",
);
const NODE = fsExistsSync(TA_NODE) ? TA_NODE : process.execPath;

function fsExistsSync(p) {
	try { return require("fs").existsSync(p); } catch { return false; }
}

function isUp() {
	return new Promise((resolve) => {
		const s = net.connect(PORT, "127.0.0.1");
		s.setTimeout(1500);
		s.on("connect", () => { s.destroy(); resolve(true); });
		s.on("timeout", () => { s.destroy(); resolve(false); });
		s.on("error", () => resolve(false));
	});
}

(async () => {
	for (;;) {
		if (!(await isUp())) {
			try {
				const p = spawn(NODE, [SINK, String(PORT)], {
					detached: true,
					windowsHide: true,
					stdio: "ignore",
				});
				p.unref();
				console.log(`[${new Date().toISOString()}] sink down, restarted pid=${p.pid}`);
			} catch (e) {
				console.error(`restart failed: ${e.message}`);
			}
		}
		await new Promise((r) => setTimeout(r, 60000));
	}
})();
