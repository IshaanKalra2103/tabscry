"""Colour themes for the UI and the orb, plus the saved theme choice."""

import json
from dataclasses import dataclass

from textual.theme import Theme

from .chat import CHATS_DIR

SETTINGS = CHATS_DIR.parent / "settings.json"


@dataclass
class Palette:
    name: str
    label: str
    background: str
    surface: str
    panel: str  # borders, dividers
    foreground: str
    muted: str
    primary: str  # input focus, question bar
    secondary: str  # brand, headings, links
    accent: str  # ◆, key hints, connected dot
    error: str
    scrollbar: str
    orb: dict[int, tuple[str, ...]]  # material -> gradient stops (dark..bright)

    def theme(self) -> Theme:
        bg = self.background
        return Theme(
            name=self.name,
            primary=self.primary,
            secondary=self.secondary,
            accent=self.accent,
            success=self.accent,
            warning=self.secondary,
            error=self.error,
            foreground=self.foreground,
            background=bg,
            surface=self.surface,
            panel=self.panel,
            dark=True,
            variables={
                "text-muted": self.muted,
                **{f"markdown-h{i}-color": self.secondary for i in range(1, 7)},
                "markdown-h1-background": bg,
                "link-color": self.secondary,
                "link-style": "none",
                "link-color-hover": self.accent,
                "link-background-hover": bg,
                "link-style-hover": "underline",
                "scrollbar": self.scrollbar,
                "scrollbar-hover": self.panel,
                "scrollbar-active": self.primary,
                "scrollbar-background": bg,
                "scrollbar-background-hover": bg,
                "scrollbar-background-active": bg,
                "scrollbar-corner-color": bg,
                "footer-background": bg,
                "footer-item-background": bg,
                "footer-key-background": bg,
                "footer-key-foreground": self.accent,
                "footer-description-background": bg,
                "footer-description-foreground": self.muted,
                "input-cursor-background": self.secondary,
                "input-cursor-foreground": bg,
                "input-selection-background": self.panel,
                "block-cursor-background": self.scrollbar,
                "block-cursor-foreground": self.foreground,
                "block-cursor-text-style": "none",
                "block-cursor-blurred-background": self.surface,
            },
        )


PALETTES = [
    # Wisteria Blue 8EA4D2 · Glaucous 6279B8 · Dusk Blue 49516F · Deep Teal 496F5D · Shamrock 4C9F70
    Palette(
        name="tabscry", label="dusk",
        background="#141722", surface="#1A1E2B", panel="#49516F", foreground="#DDE3F0", muted="#7F89A8",
        primary="#6279B8", secondary="#8EA4D2", accent="#4C9F70", error="#D2807E", scrollbar="#2A3042",
        orb={
            1: ("#232838", "#49516F", "#6279B8", "#8EA4D2", "#E6ECF8"),
            2: ("#1C2A26", "#496F5D", "#4C9F70", "#9ED4B2", "#EAF7EF"),
            3: ("#49516F", "#8EA4D2", "#DDE3F0", "#FFFFFF"),
            4: ("#1B2030", "#2F3650", "#49516F", "#6279B8"),
            5: ("#1A1E2B", "#2A3042", "#49516F"),
        },
    ),
    # Charcoal Brown 353531 · Spicy Paprika EC4E20 · Deep Saffron FF9505 · Bright Marine 016FB9 · Black 000000
    Palette(
        name="tabscry-saffron", label="saffron",
        background="#000000", surface="#161614", panel="#353531", foreground="#F1ECE4", muted="#8F8A80",
        primary="#EC4E20", secondary="#FF9505", accent="#2F8FD6", error="#EC4E20", scrollbar="#262623",
        orb={
            1: ("#2A0F06", "#7A2810", "#EC4E20", "#FF9E7A", "#FFE6DA"),
            2: ("#01192B", "#014A7C", "#016FB9", "#62B4EC", "#DDF0FC"),
            3: ("#7A4600", "#FF9505", "#FFD08A", "#FFFFFF"),
            4: ("#121210", "#262623", "#353531", "#6B675E"),
            5: ("#121210", "#262623", "#353531"),
        },
    ),
    # Black 090809 · Pure Red F40000 · Cinnabar F44E3F · Salmon F4796B · Sweet Salmon F4998D
    Palette(
        name="tabscry-ember", label="ember",
        background="#090809", surface="#151112", panel="#4A2320", foreground="#F6E9E6", muted="#9A7F7B",
        primary="#F40000", secondary="#F4998D", accent="#F44E3F", error="#F4796B", scrollbar="#2A1817",
        orb={
            1: ("#2B0000", "#7A0000", "#F40000", "#F4796B", "#FFE3DE"),
            2: ("#2A1311", "#8A3A31", "#F4796B", "#F4998D", "#FFF1EE"),
            3: ("#7A1F17", "#F44E3F", "#F4998D", "#FFFFFF"),
            4: ("#130D0D", "#2A1817", "#4A2320", "#8A3A31"),
            5: ("#130D0D", "#2A1817", "#4A2320"),
        },
    ),
    Palette(
        name="tabscry-mono", label="mono",
        background="#0C0C0C", surface="#151515", panel="#3A3A3A", foreground="#E4E4E4", muted="#7A7A7A",
        primary="#A8A8A8", secondary="#F0F0F0", accent="#C8C8C8", error="#FFFFFF", scrollbar="#242424",
        orb={
            1: ("#1C1C1C", "#4A4A4A", "#8C8C8C", "#CFCFCF", "#FFFFFF"),
            2: ("#141414", "#333333", "#6A6A6A", "#A6A6A6", "#E0E0E0"),
            3: ("#707070", "#C0C0C0", "#EEEEEE", "#FFFFFF"),
            4: ("#121212", "#1E1E1E", "#303030", "#5A5A5A"),
            5: ("#161616", "#242424", "#3A3A3A"),
        },
    ),
]
BY_NAME = {p.name: p for p in PALETTES}


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_setting(key: str, value):
    data = load_settings()
    data[key] = value
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, indent=1))


def load_theme_name() -> str:
    name = load_settings().get("theme")
    return name if name in BY_NAME else PALETTES[0].name


def save_theme_name(name: str):
    save_setting("theme", name)
