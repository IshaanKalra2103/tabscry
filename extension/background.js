// tabscry bridge: drives Google AI Mode on behalf of the terminal client.
//
// Route "window" (default): a separate window created already-minimized, so your own windows and
// tabs are untouched. Route "tab": an inactive tab in your current window. Either way, everything
// tabscry opened is closed as soon as the terminal session ends. page.js (scraper + typing) is
// injected on demand into that page only, so it never runs on your own Google searches.
const WS_URL = "ws://127.0.0.1:8765";
const POLL_MS = 350;
const STABLE_POLLS = 9; // ~3s without change => answer finished
const TIMEOUT_MS = 120_000;
const AI_MODE = "https://www.google.com/search?udm=50";

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
    closeTabs(); // session over: close everything we opened
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

// ---------------------------------------------------------------- the worker page
// storage.session: { tabId, windowId, owned: [tab ids we opened], ownedWindows: [window ids], keep }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function currentTab() {
  const { tabId } = await chrome.storage.session.get("tabId");
  if (tabId == null) return null;
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  return tab?.url?.startsWith("https://www.google.com/") ? tab : null;
}

async function waitForLoad(tabId) {
  // poll instead of listening for onUpdated: a fast load can finish before a listener is attached
  const deadline = Date.now() + 20_000;
  await sleep(300); // let the navigation start so we don't see the previous page's "complete"
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw new Error("the AI Mode tab was closed");
    if (tab.status === "complete") return;
    await sleep(200);
  }
  throw new Error("AI Mode page didn't load");
}

async function openTab(url, route, id) {
  let existing = await currentTab();
  const { windowId } = await chrome.storage.session.get("windowId");
  const inOwnWindow = existing != null && existing.windowId === windowId;
  if (existing && inOwnWindow !== (route === "window")) {
    // route was switched (/route): retire the old page instead of reusing it
    await chrome.tabs.remove(existing.id).catch(() => {});
    await chrome.storage.session.remove(["tabId", "windowId"]);
    existing = null;
  }
  if (existing) {
    await chrome.tabs.update(existing.id, { url });
    return waitForLoad(existing.id);
  }
  const { owned = [], ownedWindows = [] } = await chrome.storage.session.get(["owned", "ownedWindows"]);
  // tabscry's own browser starts with no windows at all, so there's nothing to open a tab in
  const normalWindows = await chrome.windows.getAll({ windowTypes: ["normal"] }).catch(() => []);
  let tab;
  if (route === "window" || !normalWindows.length) {
    const before = await chrome.windows.getLastFocused().catch(() => null);
    // created minimized in one step (creating normal + minimizing is what pulls the browser forward)
    const win = await chrome.windows.create({ url, state: "minimized" });
    tab = win.tabs[0];
    await chrome.storage.session.set({ windowId: win.id, ownedWindows: [...ownedWindows, win.id] });
    const after = await chrome.windows.getLastFocused().catch(() => null);
    if (after?.id === win.id && before?.id !== win.id) {
      send({ type: "notice", id, message: "this browser focused tabscry's window — use /route tab if that keeps happening" });
    }
  } else {
    tab = await chrome.tabs.create({ url, active: false }); // inactive: never steals focus
  }
  await chrome.storage.session.set({ tabId: tab.id, owned: [...owned, tab.id] });
  return waitForLoad(tab.id);
}

async function closeTabs() {
  const { keep, owned = [], ownedWindows = [] } = await chrome.storage.session.get(["keep", "owned", "ownedWindows"]);
  if (keep || busy || !(owned.length || ownedWindows.length)) return;
  await chrome.storage.session.remove(["tabId", "windowId", "owned", "ownedWindows"]);
  for (const winId of ownedWindows) {
    // close the whole window only if it holds nothing but our Google tabs (some browsers merge windows)
    const tabs = await chrome.tabs.query({ windowId: winId }).catch(() => null);
    if (tabs?.length && tabs.every((t) => owned.includes(t.id) && t.url?.startsWith("https://www.google.com/"))) {
      await chrome.windows.remove(winId).catch(() => {});
    }
  }
  for (const id of owned) {
    const tab = await chrome.tabs.get(id).catch(() => null);
    // only close it if it's still a Google page (tab ids can be reused)
    if (tab?.url?.startsWith("https://www.google.com/")) await chrome.tabs.remove(id).catch(() => {});
  }
}

// run a page.js function in the tab, injecting page.js first if this page doesn't have it yet
async function call(cmd, ...args) {
  const tab = await currentTab();
  if (!tab) throw new Error("the AI Mode tab was closed");
  const run = () => chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (c, a) => (window.__tabscry ? { ok: true, value: window.__tabscry[c](...a) } : { missing: true }),
    args: [cmd, args],
  });
  let [{ result }] = await run();
  if (result.missing) {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["page.js"] });
    [{ result }] = await run();
  }
  return result.value;
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
    if (!ws || ws.readyState !== WebSocket.OPEN) closeTabs();
  }
}

// Show the conversation: switch to the tab and bring its window forward.
async function reveal(id) {
  const tab = await currentTab();
  if (!tab) return send({ type: "error", id, message: "nothing to show yet — ask something first" });
  await chrome.tabs.update(tab.id, { active: true });
  await chrome.windows.update(tab.windowId, { state: "normal", focused: true, drawAttention: true }).catch(() => {});
  send({ type: "revealed", id });
}

function describe(diag) {
  if (!diag) return "page didn't respond";
  return `page was "${diag.title}" (${diag.url}) showing: ${diag.text.slice(0, 160) || "nothing"}`;
}

async function ask({ id, text, new: fresh, keep = false, route = "window" }) {
  // keep=true (one-shot CLI) leaves the tab open after disconnect so --continue works
  await chrome.storage.session.set({ keep });
  let baseline = 0;
  const followUp = !fresh && (await currentTab());

  if (!followUp && text.length <= 1500) {
    await openTab(`${AI_MODE}&q=${encodeURIComponent(text)}`, route, id);
  } else {
    if (!followUp) await openTab(`${AI_MODE}&aep=1`, route, id); // long prompts (chat context) go through the input box
    let result;
    for (let i = 0; i < 20; i++) { // the input box can take a moment to hydrate
      result = await call("submitFollowUp", text).catch((e) => ({ ok: false, error: e.message }));
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
      result = await call("scrape", baseline);
    } catch (e) {
      if (/closed/.test(e.message)) throw e;
      continue; // page is mid-navigation (submitting from the landing page reloads it)
    }
    if (!result) continue;
    if (result.blocked) throw new Error("Google is showing a captcha/consent page — press ctrl+o to open it and clear it");
    if (!result.markdown) continue;
    if (result.markdown !== last) {
      last = result.markdown;
      stable = 0;
      send({ type: "chunk", id, markdown: last });
    } else if (++stable >= STABLE_POLLS) {
      return send({ type: "done", id, markdown: last, sources: result.sources });
    }
  }
  if (last) return send({ type: "done", id, markdown: last, sources: [], note: "timed out" });
  const diag = await call("diagnose").catch(() => null);
  throw new Error(`timed out waiting for AI Mode — ${describe(diag)}`);
}
