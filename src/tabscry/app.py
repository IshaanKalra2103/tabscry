"""Chat TUI: typewriter-streamed markdown with inline images, saved history."""

import asyncio
import io
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import PIL.Image
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option
from textual_image.widget import Image as ImageWidget

from .bridge import ENGINE_PORT, PORT, Bridge, free_port
from .browser import Engine
from .chat import Chat, Turn, build_prompt, export, list_chats
from .orb import Orb
from .icons import iconize
from .themes import BY_NAME, PALETTES, load_settings, load_theme_name, save_setting, save_theme_name

IMAGE_LINE = re.compile(r"^!\[([^\]]*)\]\((https?://[^)\s]+)\)\s*$")
FPS = 40
DRAWER_WIDTH = 46
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

def split_segments(md: str) -> list[tuple[str, object]]:
    """Split markdown into ("md", text) and ("images", [(key, alt), ...]) runs.

    Consecutive image lines (blank lines allowed between) become one horizontal row.
    """
    segments: list[tuple[str, object]] = []
    text: list[str] = []
    row: list[tuple[str, str]] = []

    def flush_text():
        if "".join(text).strip():
            segments.append(("md", "\n".join(text)))
        text.clear()

    for line in md.split("\n"):
        if m := IMAGE_LINE.match(line):
            flush_text()
            row.append((m[2], m[1]))
        elif row and not line.strip():
            continue
        else:
            if row:
                segments.append(("images", row))
                row = []
            text.append(line)
    if row:
        segments.append(("images", row))
    flush_text()
    return segments


def fetch_image(url: str) -> PIL.Image.Image | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            pil = PIL.Image.open(io.BytesIO(r.read()))
            pil.load()
        pil.thumbnail((640, 640))
        return pil
    except Exception:
        return None


class ImageRow(Horizontal):
    """A row of images, fetched from their URLs on demand (never stored on disk)."""

    def __init__(self, items: list[tuple[str, str]]):
        super().__init__(classes="images")
        self.items = items  # (url, alt)
        self.signature = None

    def current_signature(self):
        return tuple((url, url in self.app.images) for url, _ in self.items)

    def compose(self) -> ComposeResult:
        self.signature = self.current_signature()
        for url, _ in self.items:
            if url not in self.app.images:
                self.app.request_image(url)
                yield Static("⋯", classes="img-pending")
            elif (pil := self.app.images[url]) is None:
                yield Static("✕", classes="img-pending")
            else:
                yield ImageWidget(pil, classes="img")


class SourcesChip(Static):
    """Collapsed "▸ 8 sources" toggle under an answer; opens the sources drawer."""

    def __init__(self, turn: Turn):
        super().__init__(classes="sources-chip")
        self.turn = turn

    def on_mount(self):
        self.refresh_label()

    def refresh_label(self):
        n = len(self.turn.sources)
        is_open = self.app.sources_turn is self.turn
        arrow = "▾" if is_open else "▸"
        self.set_class(is_open, "-open")
        self.update(Content.from_markup(f"[$accent]{arrow}[/] {n} source{'s' if n != 1 else ''}"))

    def on_click(self):
        self.app.toggle_sources(self.turn)


class SourceLink(Static):
    """One clickable source row: hover underlines the title, click opens it in the browser.

    A single widget (a two-column grid for a hanging indent) so hover isn't split across children.
    """

    def __init__(self, index: int, title: str, url: str):
        super().__init__(classes="drawer-item")
        self.index, self.title, self.url = index, title, url
        self.hovered = False

    def on_mount(self):
        self.draw()

    def draw(self):
        theme = self.app.current_theme
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(ratio=1)
        title_style = Style(color=theme.secondary, underline=True) if self.hovered else Style(color=theme.foreground)
        grid.add_row(Text(str(self.index), style=Style(color=theme.accent, bold=self.hovered)), Text(self.title, style=title_style))
        self.update(grid)

    def on_enter(self):
        self.hovered = True
        self.draw()

    def on_leave(self):
        self.hovered = False
        self.draw()

    def on_click(self):
        self.app.open_url(self.url)
        self.app.notify(f"opened {self.title[:50]}", timeout=2)


class SourcesDrawer(Vertical):
    """Right-hand panel listing one answer's sources."""

    def compose(self) -> ComposeResult:
        # fixed-width body: while the drawer's width animates, text is uncovered instead of re-wrapping
        with Vertical(id="drawer-body"):
            yield Static(id="drawer-title")
            yield VerticalScroll(id="drawer-list")
            yield Static(Content.from_markup("[$accent]esc[/] [$text-muted]close   click a title to open it[/]"), id="drawer-hint")

    async def show(self, turn: Turn):
        question = Content(self.app.display(turn.question[:60])).markup
        self.query_one("#drawer-title", Static).update(
            Content.from_markup(f"[b $secondary]sources[/]  [$text-muted]{question}[/]")
        )
        items = self.query_one("#drawer-list", VerticalScroll)
        await items.remove_children()
        for i, src in enumerate(turn.sources, 1):
            await items.mount(SourceLink(i, self.app.display(src["title"]), src["url"]))
        items.scroll_home(animate=False)


class AnswerView(Vertical):
    """Reveals the answer a few characters per frame, like an LLM stream."""

    def __init__(self, turn: Turn, instant: bool = False):
        super().__init__(classes="answer")
        self.turn = turn
        self.instant = instant
        self.shown = 0
        self.rendered = ""
        self.segments: list[tuple[str, object]] = []
        self.started = time.monotonic()
        self.finished_at: float | None = None
        self.frame = 0
        self._ticking = False
        self.images_seen = -1

    def compose(self) -> ComposeResult:
        yield Static(classes="label")
        yield Vertical(classes="body")

    async def on_mount(self):
        if self.instant:
            self.shown = len(self.target)
            await self.render_text(self.target)
            await self.finish()
            self.set_interval(0.25, self.tick)  # only to pick up images as they download
        else:
            self.update_label()
            self.set_interval(1 / FPS, self.tick)

    @property
    def target(self) -> str:
        return self.app.display(self.turn.markdown)

    @property
    def typing(self) -> bool:
        return self.finished_at is None

    def update_label(self):
        label = self.query_one(".label", Static)
        n = self.turn.context_turns
        context = f"  [$text-muted]↺ {n} earlier {'turn' if n == 1 else 'turns'} as context[/]" if n else ""
        if self.finished_at is not None:
            took = "" if self.instant else f"  [$text-muted]{self.finished_at - self.started:.1f}s[/]"
            label.update(Content.from_markup(f"[b $accent]◆[/] [b $secondary]AI Mode[/]{took}{context}"))
            return
        spin = SPINNER[(self.frame // 3) % len(SPINNER)]
        status = "" if self.rendered else " searching"
        label.update(Content.from_markup(
            f"[b $accent]◆[/] [b $secondary]AI Mode[/]  [$accent]{spin}[/][$text-muted]{status}[/]{context}"
        ))

    async def tick(self):
        if self._ticking:
            return
        if self.finished_at is not None:
            # only thing left to do: pick up images that land after the text finished
            if self.images_seen != self.app.image_version:
                self._ticking = True
                try:
                    await self.render_text(self.target)
                finally:
                    self._ticking = False
            return
        self._ticking = True
        try:
            self.frame += 1
            await self._tick()
            if self.finished_at is None:
                self.update_label()
        finally:
            self._ticking = False

    async def _tick(self):
        target = self.target
        if self.rendered and not target.startswith(self.rendered):
            # page re-rendered earlier text: keep the common prefix and keep typing from there
            common = 0
            for a, b in zip(self.rendered, target):
                if a != b:
                    break
                common += 1
            self.shown = common
        if self.shown >= len(target):
            if self.rendered != target or self.images_seen != self.app.image_version:
                await self.render_text(target)
            elif self.turn.done and self.finished_at is None:
                await self.finish()
            return

        backlog = len(target) - self.shown
        step = max(2, backlog // 20)  # speed up when we're falling behind the page
        end = min(len(target), self.shown + step)
        # don't type image refs character by character
        line_start = target.rfind("\n", 0, end) + 1
        if target.startswith("![", line_start):
            line_end = target.find("\n", end)
            end = len(target) if line_end == -1 else line_end
        self.shown = end
        await self.render_text(target[:end])

    async def finish(self):
        self.finished_at = time.monotonic()
        if self.rendered != self.target:
            await self.render_text(self.target)
        self.update_label()
        if self.turn.sources:
            await self.mount(SourcesChip(self.turn))
        self.app.follow_scroll()

    async def render_text(self, text: str):
        self.rendered = text
        self.images_seen = self.app.image_version
        body = self.query_one(".body", Vertical)
        segments = split_segments(text)
        children = list(body.children)

        for i, (kind, value) in enumerate(segments):
            existing = children[i] if i < len(children) else None
            if kind == "md":
                if isinstance(existing, Markdown):
                    old = self.segments[i][1] if i < len(self.segments) and self.segments[i][0] == "md" else None
                    if old is not None and value.startswith(old):
                        if value != old:
                            await existing.append(value[len(old):])
                    else:
                        await existing.update(value)
                    continue
                widget = Markdown(value)
            else:
                if isinstance(existing, ImageRow) and existing.items == value and existing.signature == existing.current_signature():
                    continue
                widget = ImageRow(value)
            if existing is not None:
                await body.mount(widget, before=existing)
                await existing.remove()
            else:
                await body.mount(widget)
            children = list(body.children)

        for extra in children[len(segments):]:
            await extra.remove()
        self.segments = segments
        self.app.follow_scroll()


def question_widget(text: str, icons: bool = True) -> Static:
    shown = iconize(text) if icons else text
    return Static(Content.from_markup(f"[b $secondary]you[/]\n{Content(shown).markup}"), classes="you")


EMPTY = """[b $secondary]t a b s c r y[/]
[$text-muted]Google AI Mode, in your terminal[/]

[$accent]ctrl+n[/] [$text-muted]new chat[/]    [$accent]ctrl+r[/] [$text-muted]history[/]    [$accent]ctrl+s[/] [$text-muted]export[/]    [$accent]ctrl+t[/] [$text-muted]theme[/]"""


def welcome() -> Vertical:
    return Vertical(Orb(), Static(Content.from_markup(EMPTY), classes="tagline"), id="empty")


class HistoryScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("ctrl+d", "delete", "Delete", priority=True),
    ]
    CSS = """
    HistoryScreen { align: center middle; background: $background 70%; }
    #panel { width: 96; max-width: 95%; height: 80%; background: $surface; border: round $primary; padding: 1 2; }
    #panel-title { height: 1; margin-bottom: 1; }
    #filter { height: 3; background: $surface; border: round $panel; padding: 0 1; }
    #filter:focus { border: round $secondary; }
    #chats { height: 1fr; margin-top: 1; background: $surface; border: none; padding: 0; scrollbar-size-vertical: 1; }
    #chats > .option-list--option { padding: 0 1; }
    #chats > .option-list--option-highlighted { background: $panel 45%; }
    #panel-hint { height: 1; margin-top: 1; color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static(Content.from_markup("[b $secondary]history[/]"), id="panel-title")
            yield Input(placeholder="filter…", id="filter")
            yield OptionList(id="chats")
            yield Static(Content.from_markup("[$accent]enter[/] open   [$accent]ctrl+d[/] delete   [$accent]esc[/] close"), id="panel-hint")

    def on_mount(self):
        self.chats = list_chats()
        self.refresh_options("")

    def refresh_options(self, query: str):
        chats = self.query_one("#chats", OptionList)
        chats.clear_options()
        q = query.lower()
        matches = [c for c in self.chats if not q or any(q in t.question.lower() or q in t.markdown.lower() for t in c.turns)]
        if not matches:
            chats.add_option(Option(Content.from_markup("[$text-muted]no saved chats[/]" if not q else "[$text-muted]no matches[/]"), disabled=True))
            return
        for c in matches:
            when = datetime.fromtimestamp(c.updated).strftime("%b %d, %H:%M")
            n = len(c.turns)
            title = Content(c.title[:84]).markup
            chats.add_option(Option(
                Content.from_markup(f"{title}\n[$text-muted]{when} · {n} question{'s' if n != 1 else ''}[/]"), id=c.id
            ))
            chats.add_option(None)  # separator
        chats.highlighted = 0

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed):
        self.refresh_options(event.value)

    def action_move(self, delta: int):
        chats = self.query_one("#chats", OptionList)
        chats.action_cursor_down() if delta > 0 else chats.action_cursor_up()

    @on(Input.Submitted, "#filter")
    def submit_filter(self):
        chats = self.query_one("#chats", OptionList)
        if chats.highlighted is not None:
            option = chats.get_option_at_index(chats.highlighted)
            if option.id:
                self.dismiss(option.id)

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected):
        if event.option.id:
            self.dismiss(event.option.id)

    def action_close(self):
        self.dismiss(None)

    def action_delete(self):
        chats = self.query_one("#chats", OptionList)
        if chats.highlighted is None:
            return
        chat_id = chats.get_option_at_index(chats.highlighted).id
        if not chat_id or chat_id == self.app.chat.id:
            return self.notify("Can't delete the open chat", severity="warning") if chat_id else None
        chat = next(c for c in self.chats if c.id == chat_id)
        chat.delete()
        self.chats.remove(chat)
        self.refresh_options(self.query_one("#filter", Input).value)


class Tabscry(App):
    TITLE = "tabscry"
    CSS = """
    Screen { background: $background; }
    #topbar { height: 1; padding: 0 2; background: $background; }
    #brand { width: auto; }
    #status { width: 1fr; text-align: right; }
    #log { background: $background; align-horizontal: center; padding: 0 2; scrollbar-size-vertical: 1; overflow-x: hidden; }
    #log > * { width: 1fr; max-width: 100; }
    #empty { height: auto; margin-top: 1; align-horizontal: center; }
    #empty .tagline { width: 1fr; height: auto; text-align: center; margin-top: 1; }
    .you { height: auto; margin: 1 0 0 0; padding: 1 2; background: $surface; border-left: thick $primary; }
    .answer { height: auto; margin: 1 0 1 0; }
    .answer .label { height: 1; padding: 0 2; margin-bottom: 1; }
    .answer .body { height: auto; }
    .answer Markdown { margin: 0; padding: 0 2; background: $background; }
    .sources-chip { width: auto; height: 1; margin: 1 2 0 2; padding: 0 1; background: $surface; color: $text-muted; }
    .sources-chip:hover, .sources-chip.-open { background: $panel; color: $foreground; }
    #main { height: 1fr; }
    #log { width: 1fr; }
    #drawer { display: none; width: 0; height: 1fr; background: $surface; border-left: tall $panel; overflow: hidden hidden; }
    #drawer-body { width: 45; height: 1fr; padding: 1 2; }
    #drawer-title { height: auto; margin-bottom: 1; }
    #drawer-list { height: 1fr; scrollbar-size-vertical: 1; background: $surface; }
    .drawer-item { height: auto; margin-bottom: 1; pointer: pointer; }
    #drawer-hint { height: 1; margin-top: 1; }
    .images { height: auto; max-height: 14; overflow-x: auto; padding: 0 2; margin: 0 0 1 0; scrollbar-size-horizontal: 1; }
    .images .img { width: auto; height: 12; margin-right: 1; }
    .img-pending { width: 18; height: 12; content-align: center middle; background: $surface; color: $text-muted; margin-right: 1; }
    .error { height: auto; margin: 1 0; padding: 0 2; color: $error; }
    #bottom { dock: bottom; height: auto; background: $background; padding: 1 2 0 2; }
    #prompt { height: 3; background: $background; border: round $panel; padding: 0 1; }
    #prompt:focus { border: round $primary; background: $background; }
    #bottom Footer { dock: none; height: 1; background: $background; padding: 0 0; margin-top: 0; }
    Toast { background: $surface; border-left: outer $accent; }
    """
    BINDINGS = [
        Binding("ctrl+n", "new_chat", "new"),
        Binding("ctrl+r", "history", "history"),
        Binding("ctrl+s", "export", "export"),
        Binding("ctrl+o", "open_tab", "open tab"),
        Binding("ctrl+t", "cycle_theme", "theme"),
        Binding("ctrl+b", "toggle_latest_sources", "sources"),
        Binding("escape", "close_sources", show=False),
        Binding("ctrl+q", "quit", "quit"),
    ]

    def __init__(self):
        super().__init__()
        settings = load_settings()
        self.engine = Engine(0)  # port is decided below
        # "own": tabscry's own Chromium (never throttled); "browser": the extension in your browser
        self.engine_mode = settings.get("engine") or ("own" if self.engine.available else "browser")
        if self.engine_mode == "own" and not self.engine.available:
            self.engine_mode = "browser"
        port = (ENGINE_PORT or free_port()) if self.engine_mode == "own" else PORT
        self.bridge = Bridge(on_status=lambda c: self.call_later(self._set_status, c), port=port)
        self.chat = Chat.new()
        self.fresh = True
        self.busy = False
        self.sources_turn: Turn | None = None  # whose sources the drawer shows
        self.icons = settings.get("icons", "nerd") != "emoji"
        self.route = "tab" if settings.get("route") == "tab" else "window"
        self.images: dict[str, PIL.Image.Image | None] = {}  # url -> image (None = failed)
        self.image_version = 0
        self.fetching: set[str] = set()

    def request_image(self, url: str):
        if url in self.images or url in self.fetching:
            return
        self.fetching.add(url)

        async def fetch():
            self.images[url] = await asyncio.to_thread(fetch_image, url)
            self.fetching.discard(url)
            self.image_version += 1

        self.run_worker(fetch(), group="images")

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(Content.from_markup("[b $secondary]tabscry[/]"), id="brand")
            yield Static(id="status")
        with Horizontal(id="main"):
            with VerticalScroll(id="log"):
                yield welcome()
            yield SourcesDrawer(id="drawer")
        with Vertical(id="bottom"):
            yield Input(placeholder="Ask anything…", id="prompt")
            yield Footer(compact=True, show_command_palette=False)

    def on_mount(self):
        for palette in PALETTES:
            self.register_theme(palette.theme())
        self.theme = load_theme_name()
        self._set_status(False)
        self.run_worker(self.serve_bridge(), group="bridge")
        if self.engine_mode == "own":
            self.engine.port = self.bridge.port
            try:
                self.engine.start()
            except RuntimeError as e:
                self.notify(str(e), severity="error", timeout=10)
        self.query_one("#prompt", Input).focus()

    async def serve_bridge(self):
        try:
            await self.bridge.serve()
        except OSError as e:  # e.g. another tabscry already listening on this port
            self.notify(f"can't listen on port {self.bridge.port}: {e.strerror}. Another tabscry running?",
                        severity="error", timeout=12)

    def _set_status(self, connected: bool):
        if not self.is_running:
            return
        where = "tabscry's browser" if self.engine_mode == "own" else "your browser"
        self.query_one("#status", Static).update(Content.from_markup(
            f"[$accent]●[/] [$text-muted]{where}[/]" if connected
            else f"[$error]○[/] [$text-muted]starting {where} on :{self.bridge.port}…[/]"
        ))

    def on_unmount(self):
        self.engine.stop()

    @work(group="drawer", exclusive=True)
    async def toggle_sources(self, turn: Turn | None):
        drawer = self.query_one("#drawer", SourcesDrawer)
        if turn is None or turn is self.sources_turn or not turn.sources:
            was_open = self.sources_turn is not None
            self.sources_turn = None
            drawer.remove_class("-open")
            if was_open:
                def hide():
                    if self.sources_turn is None:  # not reopened mid-animation
                        drawer.display = False
                drawer.styles.animate("width", 0, duration=0.16, easing="in_cubic", on_complete=hide)
        else:
            already_open = self.sources_turn is not None
            self.sources_turn = turn
            await drawer.show(turn)
            drawer.add_class("-open")
            if not already_open:  # slide in; switching answers while open just swaps the list
                drawer.display = True
                drawer.styles.animate("width", DRAWER_WIDTH, duration=0.22, easing="out_cubic")
        for chip in self.query(SourcesChip):
            chip.refresh_label()

    def action_toggle_latest_sources(self):
        latest = next((t for t in reversed(self.chat.turns) if t.sources), None)
        if latest is None:
            return self.notify("no sources yet", severity="warning")
        self.toggle_sources(latest)

    def action_close_sources(self):
        if self.sources_turn is not None:
            self.toggle_sources(None)

    def follow_scroll(self):
        log = self.query_one("#log", VerticalScroll)
        if log.max_scroll_y - log.scroll_y <= 4:  # only auto-scroll if the user hasn't scrolled up
            log.scroll_end(animate=False)

    async def reset_log(self, empty: bool):
        if self.sources_turn is not None:
            self.toggle_sources(None)
        log = self.query_one("#log", VerticalScroll)
        await log.remove_children()
        if empty:
            await log.mount(welcome())

    async def action_new_chat(self):
        if self.busy:
            return self.notify("Wait for the current answer to finish", severity="warning")
        self.chat = Chat.new()
        self.fresh = True
        await self.reset_log(empty=True)

    def action_history(self):
        if isinstance(self.screen, HistoryScreen):
            return
        self.push_screen(HistoryScreen(), self.open_chat)

    @work(group="history")
    async def open_chat(self, chat_id: str | None):
        if not chat_id or chat_id == self.chat.id:
            return
        if self.busy:
            return self.notify("Wait for the current answer to finish", severity="warning")
        self.chat = await asyncio.to_thread(Chat.load, chat_id)
        self.fresh = True  # Google's side of the old thread is gone; next question starts a new one
        await self.reset_log(empty=False)
        log = self.query_one("#log", VerticalScroll)
        for turn in self.chat.turns:
            await log.mount(question_widget(turn.question, self.icons))
            if turn.error:
                await log.mount(Static(f"✕ {turn.error}", classes="error"))
            else:
                await log.mount(AnswerView(turn, instant=True))
        log.scroll_end(animate=False)

    def display(self, text: str) -> str:
        return iconize(text) if self.icons else text

    async def toggle_icons(self):
        self.icons = not self.icons
        save_setting("icons", "nerd" if self.icons else "emoji")
        self.notify("icons: nerd font" if self.icons else "icons: emoji")
        if self.chat.turns and not self.busy:
            chat_id, self.chat = self.chat.id, Chat.new()
            self.open_chat(chat_id)  # re-render the conversation with the new glyphs

    def set_theme(self, name: str):
        self.theme = name
        for link in self.query(SourceLink):  # rows paint with theme colours directly
            link.draw()
        save_theme_name(name)
        self.notify(f"theme: {BY_NAME[name].label}")

    def action_cycle_theme(self):
        names = [p.name for p in PALETTES]
        current = names.index(self.theme) if self.theme in names else -1
        self.set_theme(names[(current + 1) % len(names)])

    @work(group="focus", exclusive=True)
    async def action_open_tab(self):
        if error := await self.bridge.focus():
            self.notify(error, severity="warning")
        else:
            self.notify("opened in your browser")

    def action_export(self, arg: str = ""):
        if not self.chat.turns:
            return self.notify("Nothing to export yet", severity="warning")
        stamp = datetime.fromtimestamp(self.chat.created).strftime("%Y%m%d-%H%M%S")
        path = Path(arg) if arg else Path.cwd() / f"tabscry-{stamp}.md"
        if path.is_dir():
            path = path / f"tabscry-{stamp}.md"
        try:
            written = export(self.chat.turns, path)
        except OSError as e:
            return self.notify(f"Export failed: {e}", severity="error")
        self.notify(f"Saved to {written}", title="exported")

    @on(Input.Submitted, "#prompt")
    async def submitted(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.clear()
        if not text:
            return
        command, _, arg = text.partition(" ")
        match command:
            case "/quit" | "/exit":
                return self.exit()
            case "/new" | "/clear":
                return await self.action_new_chat()
            case "/theme":
                by_label = {p.label: p.name for p in PALETTES}
                if not arg.strip():
                    return self.action_cycle_theme()
                if arg.strip() not in by_label:
                    return self.notify(f"themes: {', '.join(by_label)}", severity="warning")
                return self.set_theme(by_label[arg.strip()])
            case "/route":
                self.route = "tab" if self.route == "window" else "window"
                save_setting("route", self.route)
                self.fresh = True  # next question opens Google the new way
                return self.notify(
                    "route: minimized window" if self.route == "window" else "route: background tab in your window"
                )
            case "/font":
                font = arg.strip() or load_settings().get("font") or "ABC Areal Mono"
                save_setting("font", font)
                return self.notify(f"terminals own the font — run: tabscry --font {font!r}", timeout=10)
            case "/engine":
                self.engine_mode = "browser" if self.engine_mode == "own" else "own"
                if self.engine_mode == "own" and not self.engine.available:
                    self.engine_mode = "browser"
                    return self.notify("no Chromium for tabscry's own browser — run `tabscry --install-browser`", severity="warning")
                save_setting("engine", self.engine_mode)
                return self.notify(
                    ("engine: tabscry's own browser" if self.engine_mode == "own" else "engine: the extension in your browser")
                    + " — restart tabscry to switch"
                )
            case "/icons":
                return await self.toggle_icons()
            case "/history":
                return self.action_history()
            case "/open":
                return await self.action_open_tab()
            case "/export":
                return self.action_export(arg.strip())
        if self.busy:
            return self.notify("Still answering…", severity="warning")
        self.ask(text)

    @work(exclusive=True, group="ask")
    async def ask(self, text: str):
        self.busy = True
        log = self.query_one("#log", VerticalScroll)
        for empty in log.query("#empty"):
            await empty.remove()
        chat = self.chat
        turn = Turn(text)
        prompt = text
        if self.fresh and chat.turns:
            # new Google thread inside an existing chat (e.g. reopened from history): replay it as context
            prompt, turn.context_turns = build_prompt(chat.turns, text)
        chat.turns.append(turn)
        await log.mount(question_widget(text, self.icons))
        view = AnswerView(turn)
        await log.mount(view)
        log.scroll_end(animate=False)
        new, self.fresh = self.fresh, False
        try:
            async for msg in self.bridge.ask(prompt, new=new, route=self.route):
                match msg["type"]:
                    case "chunk":
                        turn.markdown = msg["markdown"]
                    case "done":
                        turn.markdown = msg["markdown"]
                        turn.sources = msg.get("sources", [])
                        turn.note = msg.get("note")
                        turn.done = True
                    case "notice":
                        self.notify(msg["message"], severity="warning", timeout=8)
                    case "error":
                        turn.error = msg["message"]
                        turn.done = True
                        await view.remove()
                        await log.mount(Static(f"✕ {msg['message']}", classes="error"))
                        if new:
                            self.fresh = True
        finally:
            self.busy = False
            await asyncio.to_thread(chat.save)
