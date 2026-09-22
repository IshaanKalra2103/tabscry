// tabscry page script: scrapes Google AI Mode and types questions into it.
// Injected on demand into tabscry's own background tab only.
(() => {
if (window.__tabscry) return;
// The page has more than one "Ask anything" box (one is a hidden duplicate); use the visible,
// bottom-most one. Falling back to a hidden one types into nothing and the follow-up silently vanishes.
function followUpBox() {
  const boxes = [...document.querySelectorAll("textarea")].filter((t) =>
    /ask anything/i.test(t.placeholder || t.getAttribute("aria-label") || ""));
  const visible = boxes.filter((t) => { const r = t.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  return (visible.length ? visible : boxes).at(-1) || null;
}

function answerCount() {
  return document.querySelectorAll('[data-container-id="main-col"]').length;
}

function submitFollowUp(text, pressSend = false) {
  const ta = followUpBox();
  if (!ta) return { ok: false, error: "couldn't find the AI Mode follow-up box" };
  const count = answerCount();
  ta.focus();
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(ta, text);
  ta.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
  ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
  if (pressSend) {
    // second attempt: also click the composer's send button, if Google's handler ignored Enter
    const composer = ta.closest("form") || ta.parentElement?.parentElement?.parentElement || document;
    const send = [...composer.querySelectorAll("button, [role=button]")].find((b) =>
      /send|submit|ask/i.test(b.getAttribute("aria-label") || ""));
    send?.click();
  }
  return { ok: true, count };
}

// Did Google take the follow-up? A new answer block appears, or the box empties / the text is gone.
function followUpAccepted(count, text) {
  if (answerCount() > count) return true;
  const ta = followUpBox();
  return !!ta && ta.value.trim() !== text.trim();
}

// AI Mode quizzes are an interactive widget, not prose: answer options and their explanations are
// all aria-hidden, so the markdown pass would drop them. Pull them out as data instead.
function extractQuiz(root) {
  const text = (el) => (el?.textContent || "").replace(/\s+/g, " ").trim();
  const title = text(root.querySelector("[role=heading]"));
  const questions = [...root.querySelectorAll("[id^='qtext-']")].map((q) => {
    const index = q.id.slice("qtext-".length);
    const block = q.closest("[data-index]") || root;
    const options = [...root.querySelectorAll(`button[data-mcq-index][data-question-index="${index}"]`)].map((button) => {
      const content = document.getElementById(button.getAttribute("aria-labelledby") || "");
      const raw = text(content);
      const [, label = "", body = raw] = raw.match(/^([A-Z])\.\s*(.*)$/) || [];
      // explanation = everything in the option's wrapper that isn't the option itself
      const wrapper = button.closest("[jscontroller]") || button.parentElement;
      let feedback = text(wrapper).replace(raw, "").trim();
      // Google also marks the right answer in a trailing comment: <!--…[[1,"",2]]--> (1 = correct)
      let flagged = false;
      const comments = document.createTreeWalker(wrapper, NodeFilter.SHOW_COMMENT);
      while (comments.nextNode()) if (/\[\[1,"",\d+\]\]/.test(comments.currentNode.textContent)) flagged = true;
      const correct = flagged || /^correct\b/i.test(feedback);
      return { label, text: body, correct, feedback };
    });
    return {
      text: text(q).replace(/^\d+\.\s*/, ""),
      options,
      hint: text(block.querySelector("[id='quiz-hint-content']")),
    };
  }).filter((q) => q.text && q.options.length);
  return questions.length ? { title, questions } : null;
}

function scrape(baseline) {
  if (location.pathname.startsWith("/sorry") || document.querySelector("form#captcha-form")) return { blocked: true };
  const cols = document.querySelectorAll('[data-container-id="main-col"]');
  if (cols.length <= baseline) return { markdown: "" };
  const col = cols[cols.length - 1];

  const clone = col.cloneNode(true);
  // record each element's real CSS display on the clone: Google wraps inline words in <div>s styled
  // inline (and hides things with classes), so tag names alone mislabel blocks
  const liveAll = col.querySelectorAll("*");
  clone.querySelectorAll("*").forEach((el, i) => {
    const live = liveAll[i];
    if (live) el.dataset.tsd = getComputedStyle(live).display;
  });
  // quizzes: extract from the live DOM, leave a marker line where they sat
  const quizzes = [];
  const cloneAll = clone.querySelectorAll("*");
  const liveList = [...liveAll];
  const seenRoots = new Set();
  for (const first of col.querySelectorAll("[id='qtext-0']")) {
    let root = first;
    while (root.parentElement && root !== col && !root.querySelector("[role=heading]")) root = root.parentElement;
    if (seenRoots.has(root) || root === col) continue;
    seenRoots.add(root);
    const quiz = extractQuiz(root);
    const twin = cloneAll[liveList.indexOf(root)];
    if (!quiz || !twin) continue;
    twin.replaceWith(document.createTextNode(`\n\n@@quiz:${quizzes.length}@@\n\n`));
    quizzes.push(quiz);
  }
  // Google renders the intro text first and the quiz a few seconds later behind an aria-busy loader;
  // report "pending" so the answer isn't declared finished before the quiz exists
  const pending = !quizzes.length && !!col.querySelector("[aria-busy=true]");
  clone.querySelectorAll("[aria-busy=true]").forEach((e) => e.remove()); // "Loading AI-generated quiz…" placeholders
  // images become plain markdown with Google's thumbnail URL (from the carousel's data-im JSON);
  // inline base64 previews without a real URL are dropped
  const liveImgs = col.querySelectorAll("img");
  clone.querySelectorAll("img").forEach((img, i) => {
    const live = liveImgs[i];
    if (!live || live.naturalWidth < 80 || live.closest("button, [aria-hidden=true], [role=dialog]")) return img.remove();
    let url = /^https?:/.test(live.currentSrc || live.src) ? live.currentSrc || live.src : "";
    try {
      const im = JSON.parse(live.closest("[data-im]")?.getAttribute("data-im") || "null");
      url = im?.[2]?.[0] || im?.[3]?.[0] || url;
    } catch {}
    if (!url) return img.remove();
    const alt = new DOMParser().parseFromString(img.alt || "", "text/html").body.textContent.replace(/[\[\]\n]/g, " ").trim();
    img.replaceWith(document.createTextNode(`\n\n![${alt}](${url.replace(/[()\s]/g, encodeURIComponent)})\n\n`));
  });
  clone.querySelectorAll("button, svg, style, script, h1, [role=dialog], [role=status], [role=alert], [aria-hidden=true], [style*='display: none'], [style*='display:none'], [data-tsd=none]").forEach((e) => e.remove());

  const BLOCK = /^(DIV|P|SECTION|UL|OL|LI|H[1-6]|TABLE|TR|PRE|BLOCKQUOTE|BR)$/;
  function md(node) {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const inner = () => [...node.childNodes].map(md).join("");
    const t = node.tagName;
    if (t === "BR") return "\n";
    if (t === "STRONG" || t === "B") { const s = inner().trim(); return s ? `**${s}** ` : ""; }
    if (t === "EM" || t === "I") { const s = inner().trim(); return s ? `*${s}* ` : ""; }
    if (t === "CODE" && node.parentElement?.tagName !== "PRE") return "`" + inner() + "`";
    if (t === "PRE") return "\n```\n" + node.innerText + "\n```\n";
    if (/^H[1-6]$/.test(t) || node.getAttribute("role") === "heading") {
      // Google nests role=heading inside role=heading; use plain text so we never emit "### ### Title"
      const level = Math.min(4, Math.max(3, Number(node.getAttribute("aria-level")) || Number(t[1]) || 3));
      const title = node.textContent.replace(/\s+/g, " ").trim();
      return title ? `\n\n${"#".repeat(level)} ${title}\n\n` : "";
    }
    if (t === "LI") {
      const depth = [...ancestors(node)].filter((a) => a.tagName === "UL" || a.tagName === "OL").length - 1;
      return `\n${"  ".repeat(Math.max(0, depth))}- ${inner().trim()}`;
    }
    if (t === "TR") return "\n| " + [...node.children].map((c) => c.innerText.trim().replace(/\|/g, "\\|")).join(" | ") + " |" +
      (node.closest("table")?.querySelector("tr") === node ? "\n|" + [...node.children].map(() => " --- |").join("") : "");
    if (t === "TABLE") return "\n" + inner() + "\n\n";
    const s = inner();
    const display = node.dataset?.tsd;
    const block = display ? !/^(inline|contents)/.test(display) : BLOCK.test(t);
    return block ? `\n${s}\n` : s;
  }
  function* ancestors(n) { while ((n = n.parentElement)) yield n; }

  const markdown = md(clone)
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/\*\* ([.,;:!?)])/g, "**$1")
    .replace(/\*\* {2,}/g, "** ")
    .replace(/^\s*-\s*$/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/(AI can make mistakes|Copied to clipboard|A copy of this chat|Thanks for letting us know).*$/s, "")
    .trim();

  const rhs = document.querySelectorAll('[data-container-id="rhs-col"]');
  const sources = [];
  const seen = new Set();
  const panel = rhs[rhs.length - 1];
  // only trust the sources panel if it belongs to this turn (comes after the answer column)
  if (panel && col.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING) {
    for (const a of panel.querySelectorAll("a[href]")) {
      const url = a.href; // Google wraps sources in /goto?url=… redirects; those still open the page
      const internal = /^https:\/\/(www\.)?google\.[^/]+\/(?!goto)/.test(url);
      if (!url.startsWith("http") || internal || seen.has(url)) continue;
      seen.add(url);
      const title = (a.getAttribute("aria-label") || a.innerText || url).split("\n").map((s) => s.trim()).filter(Boolean)[0]?.replace(/\.? Opens in new tab\.?$/, "") || url;
      sources.push({ title, url });
    }
  }
  if (!sources.length) {
    // no sources column (narrow layout): fall back to the inline citation links in the answer
    for (const a of col.querySelectorAll("a[href*='/goto?url=']")) {
      if (seen.has(a.href)) continue;
      seen.add(a.href);
      const label = (a.getAttribute("aria-label") || a.innerText || "").split("\n")[0].trim();
      if (label) sources.push({ title: label.replace(/\.? Opens in new tab\.?$/, ""), url: a.href });
    }
  }
  return { markdown, sources: sources.slice(0, 8), quizzes, pending };
}

// what the page actually is, for error messages when no answer shows up
function diagnose() {
  return {
    url: location.href.slice(0, 120),
    title: document.title,
    text: (document.body?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 240),
    answerBlocks: document.querySelectorAll('[data-container-id="main-col"]').length,
  };
}

window.__tabscry = { scrape, submitFollowUp, followUpAccepted, diagnose, url: () => location.href };

})();
