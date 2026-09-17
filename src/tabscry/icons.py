"""Swap emoji for single-width Nerd Font icons at display time (stored/exported text keeps the emoji).

Emoji are double-width with inconsistent terminal support, so they misalign text and tables;
Nerd Font glyphs are single cells and ship with Ghostty (and any patched Nerd Font).
"""

import re
import unicodedata

from .icon_map import DEFAULT, EMOJI, FLAG, KEYCAPS, KEYWORDS

_MODIFIERS = re.compile("[︎️‍\U0001F3FB-\U0001F3FF]")
_EMOJI = re.compile(
    "(?:[0-9#*]️?⃣)"  # keycaps: 1️⃣
    "|(?:[\U0001F1E6-\U0001F1FF]{2})"  # regional-indicator flags: 🇺🇸
    "|(?:[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿←-⇿ℹⓂ〰〽㊗㊙]"
    "[︎️\U0001F3FB-\U0001F3FF]*"
    "(?:‍[\U0001F000-\U0001FAFF☀-➿][︎️\U0001F3FB-\U0001F3FF]*)*)"
)
# plain text arrows/symbols that aren't emoji unless followed by U+FE0F
_TEXT_SYMBOLS = set("←↑→↓↔↕⌘⌥⏎")


def _icon(match: re.Match) -> str:
    raw = match[0]
    if raw[-1] == "⃣":
        return KEYCAPS.get(raw[0], DEFAULT)
    if "\U0001F1E6" <= raw[0] <= "\U0001F1FF":
        return FLAG
    if raw[0] in _TEXT_SYMBOLS and "️" not in raw:
        return raw
    base = _MODIFIERS.sub("", raw)
    if base in EMOJI:
        return EMOJI[base]
    if base[:1] in EMOJI:  # ZWJ sequence: use its first component
        return EMOJI[base[0]]
    try:
        name = unicodedata.name(base[0])
    except (ValueError, IndexError):
        return DEFAULT
    for keyword, icon in KEYWORDS:
        if keyword in name.split():
            return icon
    return DEFAULT


def iconize(text: str) -> str:
    return _EMOJI.sub(_icon, text)
