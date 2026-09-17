# tabscry

*Scry into a browser tab* — Google **AI Mode** in your terminal, driven through your real logged-in browser (Dia, Chrome, any Chromium).

```
terminal TUI  ⇄  ws://127.0.0.1:8765  ⇄  Dia extension  ⇄  background tab: google.com/search?udm=50
```

## Setup
1. Dia → `dia://extensions` → enable Developer mode → **Load unpacked** → pick `extension/`.
2. `uv tool install -e .`

## Use
- `tabscry` — chat TUI. Answers type out as they stream; images render inline (Kitty graphics in Ghostty/Kitty/WezTerm, sixel or half-blocks elsewhere).

  | key | |
  | --- | --- |
  | `ctrl+n` / `/new` | new chat |
  | `ctrl+r` / `/history` | browse, filter, reopen or delete saved chats |
  | `ctrl+s` / `/export [path]` | save chat as markdown |
  | `ctrl+o` / `/open` | show the AI Mode page in your browser |
  | `ctrl+t` / `/theme [dusk\|saffron\|ember\|mono]` | switch colour theme (remembered) |
  | `ctrl+b` / click `▸ N sources` | open/close the sources drawer on the right (`esc` closes) |
  | `/route` | switch between a minimized window (default) and a background tab (remembered) |
  | `/icons` | toggle emoji ↔ Nerd Font icons (display only; needs a Nerd Font — Ghostty has one built in) |
  | `ctrl+q` / `/quit` | quit |

- `tabscry "question"` — one-shot, prints markdown (images as URLs). `--continue` to follow up in the same Google page.

Chats are saved as small JSON files in `~/.local/share/tabscry/chats/` (override with `TABSCRY_HOME`) — text,
sources and image *URLs* only; images are re-fetched when shown. When you ask something in a reopened chat,
Google gets a fresh thread, so tabscry replays the recent Q&A (up to ~7.5k chars, newest first) as context
with your question; the answer label shows `↺ N earlier turns as context`.

## How it works / caveats
- Google runs in its own window, created already-minimized, so your own windows and tabs are untouched (`/route` switches to an inactive tab in your current window instead, e.g. if a browser brings that window forward). Everything tabscry opened is closed as soon as the TUI session ends; one-shot runs keep it until the next session so `--continue` works. The scraper (`extension/page.js`) is injected only into that page.
- The extension scrapes `[data-container-id="main-col"]` (answer) and `rhs-col` (sources) and converts to markdown; an answer is "done" once it stops changing for ~3s.
- Follow-ups are typed into the "Ask anything" box via synthetic input + Enter.
- It's scraping Google's DOM — when Google ships a redesign, fix `scrape()` / `submitFollowUp()` in `extension/page.js`.
- If Google shows a captcha/consent page, `/open` the tab and clear it.
- `ERR_CONNECTION_REFUSED` in the extension's error log just means the TUI isn't running; it retries with backoff (up to 30s).
- After editing `extension/`, hit reload on the extension card.
- `TABSCRY_PORT=8799` runs the TUI on another port (for testing against a modified extension copy).

## Future work

### Sandboxed browser engine (instead of the extension)
Today tabscry drives Google AI Mode inside *your* browser via the extension. That has a structural
problem: Chromium throttles pages it thinks you can't see (background tabs, minimized/occluded
windows), so an answer can stall until you look at the tab — and an extension can't turn that off.

Idea: let tabscry launch its own Chromium (e.g. via Playwright/CDP) with a dedicated profile under
`~/.local/share/tabscry/browser` and throttling disabled (`--disable-background-timer-throttling`,
`--disable-backgrounding-occluded-windows`, `--disable-renderer-backgrounding`), and inject the
existing `extension/page.js` directly.

- **Pros:** no stalls, zero interference with your browser (no tabs/windows/focus), works regardless
  of which browser you use, no extension or websocket bridge to install and reload.
- **Risks / costs:** headless Chromium is more likely to hit Google's bot detection (fallback: a
  headful window parked off-screen); no signed-in session/personalisation by default; an extra
  ~300–500 MB process and a one-time browser download.
- **Plan:** spike first (does headless get AI Mode across queries + follow-ups without captchas?);
  if yes, make it the default engine and keep the extension as an optional "use my browser" mode.
