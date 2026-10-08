// QimeiWeb 离线 uskey 铸造（Node 无浏览器）
// 用法: node mint_uskey.js [h38] [timestamp_ms]
// 输出: JSON {uskey, md5, ts, h38}

const fs = require("fs");
const crypto = require("crypto");

const CHUNK_PATH = process.env.QIMEI_CHUNK || require("path").join(__dirname, "..", "capture", "qimei_sdk.js");
const H38 = process.argv[2] || "e9632faf082420cd40bb971703000001419610";
const TS = process.argv[3] ? String(process.argv[3]) : String(Date.now());
const APP_KEY = "0WEB05U9OEC1ZNRY";
const USKEY_APP = "7800385";

// ---------- 浏览器 shims ----------
const store = {
  "_qimei_h38": H38,
};
global.self = global;
global.window = global;
global.navigator = {
  userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
  platform: "MacIntel",
  language: "zh-CN",
  languages: ["zh-CN"],
  hardwareConcurrency: 8,
  maxTouchPoints: 0,
  vendor: "Google Inc.",
  plugins: {length: 0, item: () => null, namedItem: () => null, refresh: () => {}},
  mimeTypes: {length: 0, item: () => null, namedItem: () => null},
  webdriver: false,
  getBattery: async () => ({}),
  sendBeacon: () => true,
};
global.location = {href: "https://yuanbao.tencent.com/", protocol: "https:", host: "yuanbao.tencent.com", hostname: "yuanbao.tencent.com", pathname: "/", search: "", hash: ""};
global.document = {
  cookie: "",
  referrer: "",
  title: "yuanbao",
  createElement: (tag) => {
    const el = {
      tagName: (tag || "").toUpperCase(),
      style: {},
      children: [],
      attributes: {},
      setAttribute(k, v) { this.attributes[k] = v; if (k === "src") this.src = v; },
      getAttribute(k) { return this.attributes[k] ?? null; },
      appendChild(c) { this.children.push(c); return c; },
      removeChild() {},
      addEventListener() {},
      removeEventListener() {},
      attachEvent() {},
      getContext: () => ({
        fillRect() {}, getImageData: () => ({data: new Uint8Array(4)}),
        arc() {}, beginPath() {}, closePath() {}, fill() {}, stroke() {},
        moveTo() {}, lineTo() {}, font: "", fillText() {},
        measureText: () => ({width: 0}),
      }),
      toDataURL: () => "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
      width: 0, height: 0,
    };
    return el;
  },
  getElementsByTagName: () => [],
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {},
  documentElement: {getAttribute: () => null, setAttribute() {}},
  body: {appendChild() {}, removeChild() {}, append() {}},
  head: {appendChild() {}},
  createEvent: () => ({initEvent() {}}),
  elementFromPoint: () => null,
  hidden: false,
  visibilityState: "visible",
  attachEvent() {},
};
global.localStorage = {
  _d: store,
  getItem(k) { return this._d[k] ?? null; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
  clear() { this._d = {}; },
  key(i) { return Object.keys(this._d)[i] ?? null; },
  get length() { return Object.keys(this._d).length; },
};
global.sessionStorage = {
  _d: {},
  getItem(k) { return this._d[k] ?? null; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
  clear() { this._d = {}; },
};
global.XMLHttpRequest = class {
  constructor() { this.headers = {}; }
  open(m, u) { this.method = m; this.url = u; }
  setRequestHeader(k, v) { this.headers[k] = v; }
  send() { this.status = 0; this.readyState = 4; setTimeout(() => this.onerror && this.onerror(new Error("offline")), 0); }
  abort() {}
  addEventListener() {}
  setRequestHeader2() {}
  getResponseHeader() { return null; }
  getAllResponseHeaders() { return ""; }
  overrideMimeType() {}
};
global.CSS = {supports: () => false, escape: (s) => s};
global.performance = global.performance || {now: () => Date.now(), timeOrigin: Date.now(), getEntriesByType: () => [], mark() {}, measure() {}};
global.screen = {width: 2560, height: 1440, availWidth: 2560, availHeight: 1416, colorDepth: 30, pixelDepth: 30};
global.history = {length: 1, pushState() {}, replaceState() {}, back() {}, forward() {}, go() {}};
global.crypto = global.crypto || require("crypto").webcrypto;
global.setImmediate = global.setImmediate || ((f, ...a) => setTimeout(f, 0, ...a));
global.clearImmediate = global.clearImmediate || clearTimeout;
// clearInterval/setInterval 透传（SDK 会 save/restore）
const _si = global.setInterval, _ci = global.clearInterval, _st = global.setTimeout, _ct = global.clearTimeout;
Object.defineProperty(global, "setInterval", {get: () => _si, set: (v) => {}, configurable: true});
Object.defineProperty(global, "clearInterval", {get: () => _ci, set: (v) => {}, configurable: true});
Object.defineProperty(global, "setTimeout", {get: () => _st, set: (v) => {}, configurable: true});
Object.defineProperty(global, "clearTimeout", {get: () => _ct, set: (v) => {}, configurable: true});

// ---------- 加载 Qimei chunk + 主包（跨 chunk 依赖解析） ----------
const APP_CHUNK = process.env.APP_CHUNK || require("path").join(__dirname, "..", "capture", "_app.js");
const moduleTables = [];
global.self.webpackChunk_N_E = {
  push(args) {
    const [, modules] = args;
    moduleTables.push(modules);
  },
};
// 主包在前（提供跨 chunk 依赖），qimei chunk 在后
try {
  require(APP_CHUNK);
} catch (e) {
  console.error("[harness] APP_CHUNK require 失败（忽略，模块表可能已注册一部分）:", String(e).slice(0, 120));
}
try {
  require(CHUNK_PATH);
} catch (e) {
  console.error("[harness] CHUNK_PATH require 失败:", String(e).slice(0, 120));
}
console.error("[harness] 已注册模块表:", moduleTables.map(m => Object.keys(m).length));

// mini webpack require（跨表解析）
const registry = {};
function __webpack_require__(id) {
  id = String(id);
  if (registry[id]) return registry[id].exports;
  const mod = {exports: {}, loaded: false};
  registry[id] = mod;
  const fn = (() => {
    for (const m of moduleTables) if (m[id] !== undefined) return m[id];
    throw new Error("module " + id + " not found (已注册: " + moduleTables.map(m => Object.keys(m).length).join("+") + ")");
  })();
  if (typeof fn !== "function") {
    console.error("[harness] 模块 " + id + " 的工厂不是函数: " + typeof fn);
  }
  mod.loaded = true;
  fn.call(mod.exports, mod, mod.exports, __webpack_require__);
  return mod.exports;
}

// ---------- 实例化并铸造 ----------
console.error("[harness] 表键:", moduleTables.map(m => Object.keys(m).join(",")));
console.error("[harness] 72101 typeof:", typeof moduleTables[1]?.[72101], typeof moduleTables[0]?.[72101]);
const QimeiWeb = __webpack_require__(72101);
const sdk = new QimeiWeb({
  appKey: APP_KEY,
  disableDebugger: true,
  disableConsoleDetection: false,
});

// 等 SDK 异步初始化（若有）
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  // 尝试多种触发路径
  let h38 = H38;
  try {
    if (typeof sdk.init === "function") {
      const r = sdk.init({appKey: APP_KEY, needInitQimei: false, needQueryConfig: false, needReportRqdEvent: false});
      if (r && typeof r.then === "function") await r;
    }
  } catch (e) {}

  try {
    if (typeof sdk.setUserId === "function") sdk.setUserId({});
  } catch (e) {}

  const signStr = `h38=${h38}&timestamp=${TS}&platform=web`;
  let uskey = null;
  try {
    uskey = sdk.getUSKeySync(USKEY_APP, h38, signStr);
  } catch (e) {
    console.error(JSON.stringify({error: "getUSKeySync failed: " + String(e)}));
    process.exit(1);
  }
  if (!uskey) {
    console.error(JSON.stringify({error: "getUSKeySync returned empty"}));
    process.exit(1);
  }
  const md5 = crypto.createHash("md5").update(signStr).digest("hex");
  console.log(JSON.stringify({
    uskey: encodeURIComponent(uskey),
    md5,
    ts: TS,
    h38,
    sdkH38: (() => { try { const q = sdk.getLocalQimei36(); return q ? q.h38 : null; } catch (e) { return null; } })(),
    status: sdk.status,
  }));
})();
