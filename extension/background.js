// tabscry bridge: drives Google AI Mode invisibly on behalf of the terminal client.
//
// The AI Mode page runs inside an offscreen document (a hidden extension page: no tab, no window,
// never steals focus). page.js, a content script inside that frame, does the scraping and typing
// and talks to us over a runtime port. Browsers without chrome.offscreen fall back to a background
// tab that is never activated.
const WS_URL = "ws://127.0.0.1:8765";
const POLL_MS = 350;
const STABLE_POLLS = 9; // ~3s without change => answer finished
const TIMEOUT_MS = 120_000;
const AI_MODE = "https://www.google.com/search?udm=50";
const HIDDEN_PAGE_GRACE_MS = 15_000; // no AI Mode content in the hidden frame by then => use a tab instead

let ws = null;
let busy = false;
let retryMs = 1000;

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function connect() {
  if (ws && ws.readyState <= WebSocket.OPEN) return;
  // Chrome logs ERR_CONNECTION_REFUSED for every attempt while the TUI isn't running (harmless);
  // back off to 30s so the errors page doesn't fill up.
  ws = new WebSocket(WS_URL);
  ws.onopen = () => { retryMs = 1000; send({ type: "hello", agent: navigator.userAgent }); };
  ws.onclose = () => {
    ws = null;
    closeWorker(); // TUI went away: tear down the hidden page / tab
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 30_000);
  };
  ws.onerror = () => {};
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handle(msg).catch((e) => send({ type: "error", id: msg.id, message: String(e?.message || e) }));
  };
}

// Service workers get suspended; the alarm wakes us up to reconnect.
chrome.alarms.create("keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => {
  if (!ws || ws.readyState > WebSocket.OPEN) connect();
  else send({ type: "ping" });
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();

// Google refuses to be framed (X-Frame-Options / CSP). Strip those headers, but only for frames
// that don't belong to any tab — i.e. our offscreen document — so normal browsing is untouched.
chrome.declarativeNetRequest.updateSessionRules({
  removeRuleIds: [1],
  addRules: [{
    id: 1,
    priority: 1,
    action: {
      type: "modifyHeaders",
      responseHeaders: [
        { header: "x-frame-options", operation: "remove" },
        { header: "content-security-policy", operation: "remove" },
      ],
    },
    condition: { requestDomains: ["google.com"], resourceTypes: ["sub_frame"], tabIds: [chrome.tabs.TAB_ID_NONE] },
  }],
}).catch(() => {});

// ---------------------------------------------------------------- offscreen worker

let pagePort = null;
let pageUrl = null;
let pageGen = 0; // bumps every time a (new) page in the hidden frame says hello
let rid = 0;
const pending = new Map();
const helloWaiters = new Set();

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "tabscry-page") return;
  port.onMessage.addListener((m) => {
    if (m.hello) {
      pagePort = port;
      pageUrl = m.hello;
      pageGen++;
      for (const w of helloWaiters) w();
      return;
    }
    const p = pending.get(m.rid);
    if (!p) return;
    pending.delete(m.rid);
    m.error ? p.reject(new Error(m.error)) : p.resolve(m.result);
  });
  port.onDisconnect.addListener(() => {
    if (pagePort !== port) return;
    pagePort = null;
    for (const p of pending.values()) p.reject(new Error("page navigated"));
    pending.clear();
  });
});

function waitForPage(afterGen, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (pagePort && pageGen > afterGen) return resolve();
    const done = () => {
      if (!(pagePort && pageGen > afterGen)) return;
      clearTimeout(timer);
      helloWaiters.delete(done);
      resolve();
    };
    const timer = setTimeout(() => { helloWaiters.delete(done); reject(new Error("AI Mode page didn't load")); }, timeoutMs);
    helloWaiters.add(done);
  });
}

async function hasOffscreen() {
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  return contexts.length > 0;
}

const offscreenDriver = {
  supported: () => !!chrome.offscreen?.createDocument,
  async open(url) {
    if (!(await hasOffscreen())) {
      await chrome.offscreen.createDocument({
        url: "offscreen.html",
        reasons: ["DOM_SCRAPING"],
        justification: "Load Google AI Mode invisibly for the tabscry terminal client",
      });
    }
    const gen = pageGen;
    await chrome.runtime.sendMessage({ target: "offscreen", cmd: "navigate", url });
    await waitForPage(gen, 20_000);
  },
  async has() {
    return !!pagePort && (await hasOffscreen());
  },
  async call(cmd, ...args) {
    if (!pagePort) await waitForPage(pageGen - 1, 10_000); // mid-navigation: wait for the next hello
    const id = ++rid;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      pagePort.postMessage({ rid: id, cmd, args });
      setTimeout(() => pending.delete(id) && reject(new Error("page didn't respond")), 8_000);
    });
  },
  url: () => pageUrl,
  async close() {
    pagePort = null;
    pageUrl = null;
    if (await hasOffscreen()) await chrome.offscreen.closeDocument();
  },
};

// ---------------------------------------------------------------- fallback: background tab

const tabDriver = {
  async tabId() {
    const { tabId } = await chrome.storage.session.get("tabId");
    if (tabId == null) return null;
    try {
      const tab = await chrome.tabs.get(tabId);
      return tab.url?.startsWith("https://www.google.com/") ? tabId : null;
    } catch {
      return null;
    }
  },
  async open(url) {
    // poll instead of listening for onUpdated: a fast load can finish before a listener is attached
    const loaded = async (id) => {
      const deadline = Date.now() + 20_000;
      await sleep(300); // let the navigation start so we don't see the previous page's "complete"
      while (Date.now() < deadline) {
        const tab = await chrome.tabs.get(id).catch(() => null);
        if (!tab) throw new Error("AI Mode tab was closed");
        if (tab.status === "complete") return;
        await sleep(200);
      }
      throw new Error("AI Mode page didn't load");
    };
    const tabId = await this.tabId();
    if (tabId != null) {
      await chrome.tabs.update(tabId, { url });
      return loaded(tabId);
    }
    const tab = await chrome.tabs.create({ url, active: false }); // never activated: no focus change
    await chrome.storage.session.set({ tabId: tab.id });
    return loaded(tab.id);
  },
  async has() {
    return (await this.tabId()) != null;
  },
  async call(cmd, ...args) {
    const tabId = await this.tabId();
    const run = () => chrome.scripting.executeScript({
      target: { tabId },
      func: (c, a) => (window.__tabscry ? { ok: true, value: window.__tabscry[c](...a) } : { missing: true }),
      args: [cmd, args],
    });
    let [{ result }] = await run();
    if (result.missing) {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["page.js"] });
      [{ result }] = await run();
    }
    return result.value;
  },
  url: () => null,
  async close() {
    const tabId = await this.tabId();
    await chrome.storage.session.remove("tabId");
    if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  },
};

let driver = offscreenDriver.supported() ? offscreenDriver : tabDriver;
// remember a fallback for this browser session (survives service-worker restarts)
chrome.storage.session.get("driver").then(({ driver: saved }) => { if (saved === "tab") driver = tabDriver; });

async function useTabDriver(reason) {
  driver = tabDriver;
  await chrome.storage.session.set({ driver: "tab" });
  await offscreenDriver.close().catch(() => {});
  console.warn("tabscry: hidden page unusable, falling back to a background tab:", reason);
}

function describe(diag) {
  if (!diag) return "page didn't respond";
  return `page was "${diag.title}" (${diag.url}) showing: ${diag.text.slice(0, 160) || "nothing"}`;
}

async function openPage(url) {
  try {
    await driver.open(url);
  } catch (e) {
    if (driver !== offscreenDriver) throw e;
    // hidden frame didn't work in this browser (e.g. no offscreen support): use a background tab
    await offscreenDriver.close().catch(() => {});
    driver = tabDriver;
    await driver.open(url);
  }
}

async function closeWorker() {
  const { keep } = await chrome.storage.session.get("keep");
  if (keep || busy) return;
  await offscreenDriver.close().catch(() => {});
  await tabDriver.close().catch(() => {});
}

// ---------------------------------------------------------------- protocol

async function handle(msg) {
  if (msg.type === "ping") return send({ type: "pong" });
  if (msg.type === "focus") return reveal(msg.id);
  if (msg.type !== "ask") return;
  if (busy) return send({ type: "error", id: msg.id, message: "still answering the previous question" });
  busy = true;
  try {
    await ask(msg);
  } finally {
    busy = false;
    if (!ws || ws.readyState !== WebSocket.OPEN) closeWorker();
  }
}

// Show the conversation to the user: open it as a real, focused tab.
async function reveal(id) {
  if (!(await driver.has())) return send({ type: "error", id, message: "nothing to show yet — ask something first" });
  const tabId = driver === tabDriver ? await tabDriver.tabId() : null;
  let tab;
  if (tabId != null) {
    tab = await chrome.tabs.update(tabId, { active: true });
  } else {
    const url = (await driver.call("url").catch(() => null)) || driver.url() || AI_MODE;
    const win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] }).catch(() => null);
    tab = await chrome.tabs.create({ url, active: true, ...(win ? { windowId: win.id } : {}) });
  }
  await chrome.windows.update(tab.windowId, { focused: true, drawAttention: true }).catch(() => {});
  send({ type: "revealed", id });
}

async function ask(msg, retried = false) {
  const { id, text, new: fresh, keep = false } = msg;
  // keep=true (one-shot CLI) leaves the page alive after disconnect so --continue works
  await chrome.storage.session.set({ keep });
  let baseline = 0;
  const followUp = !fresh && (await driver.has());

  if (!followUp && text.length <= 1500) {
    await openPage(`${AI_MODE}&q=${encodeURIComponent(text)}`);
  } else {
    if (!followUp) await openPage(`${AI_MODE}&aep=1`); // long prompts (chat context) go through the input box
    let result;
    for (let i = 0; i < 20; i++) { // the input box can take a moment to hydrate
      result = await driver.call("submitFollowUp", text).catch((e) => ({ ok: false, error: e.message }));
      if (result.ok) break;
      await sleep(250);
    }
    if (!result.ok) throw new Error(result.error);
    baseline = result.count;
  }

  const start = Date.now();
  let last = "";
  let stable = 0;
  while (Date.now() - start < TIMEOUT_MS) {
    await sleep(POLL_MS);
    let result;
    try {
      result = await driver.call("scrape", baseline);
    } catch {
      continue; // page is mid-navigation (submitting from the landing page reloads it)
    }
    if (!result) continue;
    if (result.blocked) throw new Error("Google is showing a captcha/consent page — press ctrl+o to open it and clear it");
    if (!result.markdown) {
      if (!last && !retried && driver === offscreenDriver && Date.now() - start > HIDDEN_PAGE_GRACE_MS) {
        // e.g. the browser blocks Google's cookies inside the hidden frame and Google serves something else
        const diag = await driver.call("diagnose").catch(() => null);
        await useTabDriver(describe(diag));
        send({ type: "notice", id, message: "hidden page didn't work in this browser — retrying in a background tab" });
        return ask({ ...msg, new: true }, true);
      }
      continue;
    }
    if (result.markdown !== last) {
      last = result.markdown;
      stable = 0;
      send({ type: "chunk", id, markdown: last });
    } else if (++stable >= STABLE_POLLS) {
      return send({ type: "done", id, markdown: last, sources: result.sources });
    }
  }
  if (last) return send({ type: "done", id, markdown: last, sources: [], note: "timed out" });
  const diag = await driver.call("diagnose").catch(() => null);
  throw new Error(`timed out waiting for AI Mode — ${describe(diag)}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
