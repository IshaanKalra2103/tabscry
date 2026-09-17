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
  | `ctrl+o` / `/open` | switch to the AI Mode tab in your browser |
  | `ctrl+t` / `/theme [dusk\|saffron\|ember\|mono]` | switch colour theme (remembered) |
  | `/icons` | toggle emoji ↔ Nerd Font icons (display only; needs a Nerd Font — Ghostty has one built in) |
  | `ctrl+q` / `/quit` | quit |

- `tabscry "question"` — one-shot, prints markdown (images as URLs). `--continue` to follow up in the same Google page.

Chats are saved as small JSON files in `~/.local/share/tabscry/chats/` (override with `TABSCRY_HOME`) — text,
sources and image *URLs* only; images are re-fetched when shown. When you ask something in a reopened chat,
Google gets a fresh thread, so tabscry replays the recent Q&A (up to ~7.5k chars, newest first) as context
with your question; the answer label shows `↺ N earlier turns as context`.

## How it works / caveats
- Google runs in a background tab that's created inactive, so it never takes focus. Every tab tabscry opens is closed as soon as the TUI session ends; one-shot runs keep the tab until the next session so `--continue` works. The scraper (`extension/page.js`) is injected only into that tab.
- The extension scrapes `[data-container-id="main-col"]` (answer) and `rhs-col` (sources) and converts to markdown; an answer is "done" once it stops changing for ~3s.
- Follow-ups are typed into the "Ask anything" box via synthetic input + Enter.
- It's scraping Google's DOM — when Google ships a redesign, fix `scrape()` / `submitFollowUp()` in `extension/page.js`.
- If Google shows a captcha/consent page, `/open` the tab and clear it.
- `ERR_CONNECTION_REFUSED` in the extension's error log just means the TUI isn't running; it retries with backoff (up to 30s).
- After editing `extension/`, hit reload on the extension card.
- `TABSCRY_PORT=8799` runs the TUI on another port (for testing against a modified extension copy).
