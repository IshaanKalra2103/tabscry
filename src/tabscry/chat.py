"""Conversation model, history storage, context prompts and markdown export."""

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CHATS_DIR = Path(os.environ.get("TABSCRY_HOME", "~/.local/share/tabscry")).expanduser() / "chats"

IMAGE_MD = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
QUIZ_MARKER = re.compile(r"^@@quiz:(\d+)@@\s*$", re.M)  # where an interactive quiz sits in the answer
LEGACY_IMAGE_REF = re.compile(r"!\[([^\]]*)\]\(img:\w+\)")  # pre-URL chats stored base64 files by key

MAX_PROMPT = 7500  # AI Mode's input box caps at 8192 chars
MAX_ANSWER_IN_CONTEXT = 1200


@dataclass
class Turn:
    question: str
    markdown: str = ""
    sources: list[dict] = field(default_factory=list)
    note: str | None = None
    error: str | None = None
    done: bool = False
    context_turns: int = 0  # earlier turns fed to Google as context for this one
    quizzes: list[dict] = field(default_factory=list)  # [{title, questions:[{text, options, hint}]}]

    def expand_quizzes(self, md: str, *, answers: bool = True) -> str:
        """Swap quiz markers for plain markdown (for export, context and one-shot output)."""
        def sub(m: re.Match) -> str:
            i = int(m[1])
            return quiz_markdown(self.quizzes[i], answers=answers) if i < len(self.quizzes) else ""
        return QUIZ_MARKER.sub(sub, md)

    def answer_markdown(self) -> str:
        md = self.expand_quizzes(self.markdown)
        if self.sources:
            md += "\n\n**Sources**\n\n" + "\n".join(
                f"{i}. [{s['title']}]({s['url']})" for i, s in enumerate(self.sources, 1)
            )
        if self.note:
            md += f"\n\n*({self.note})*"
        return md


def quiz_markdown(quiz: dict, *, answers: bool = True) -> str:
    lines = [f"**Quiz: {quiz.get('title') or 'untitled'}**", ""]
    for n, q in enumerate(quiz.get("questions", []), 1):
        lines.append(f"{n}. {q['text']}")
        for o in q["options"]:
            mark = " ✓" if answers and o.get("correct") else ""
            lines.append(f"   - {o['label']}. {o['text']}{mark}")
            if answers and o.get("correct") and o.get("feedback"):
                lines.append(f"     *{o['feedback']}*")
        lines.append("")
    return "\n".join(lines).rstrip()


def plain_answer(md: str) -> str:
    """Answer text for use as context: no images, no markdown noise, collapsed whitespace."""
    md = IMAGE_MD.sub("", md)
    md = re.sub(r"\*\*|__|`|^#+\s*", "", md, flags=re.M)
    md = re.sub(r"\n\s*\n+", "\n", md)
    return re.sub(r"[ \t]+", " ", md).strip()


def build_prompt(history: list[Turn], question: str) -> tuple[str, int]:
    """Prefix a question with as much recent history as fits. Returns (prompt, turns used)."""
    usable = [t for t in history if not t.error and t.markdown]
    if not usable:
        return question, 0
    head = "Here is our earlier conversation, for context (answers may be abridged):\n\n"
    tail = f"\n\nContinue that conversation. My next question: {question}"
    budget = MAX_PROMPT - len(head) - len(tail)
    blocks: list[str] = []
    for t in reversed(usable):  # newest first, so the most recent context survives the budget
        answer = plain_answer(t.expand_quizzes(t.markdown, answers=False))
        if len(answer) > MAX_ANSWER_IN_CONTEXT:
            answer = answer[:MAX_ANSWER_IN_CONTEXT].rsplit(" ", 1)[0] + " …"
        block = f"Q: {t.question}\nA: {answer}"
        if len(block) + 2 > budget:
            break
        blocks.insert(0, block)
        budget -= len(block) + 2
    if not blocks:
        return question, 0
    return head + "\n\n".join(blocks) + tail, len(blocks)


def export(turns: list[Turn], path: Path) -> Path:
    """Write the chat as markdown; images stay as remote URLs."""
    path = path.expanduser().resolve()
    out = [f"# tabscry chat\n\n*Exported {datetime.now():%Y-%m-%d %H:%M} · Google AI Mode*"]
    for turn in turns:
        body = f"> error: {turn.error}" if turn.error else turn.answer_markdown()
        out.append(f"## {turn.question}\n\n{body}")
    path.write_text("\n\n---\n\n".join(out) + "\n")
    return path


@dataclass
class Chat:
    """A saved conversation: <CHATS_DIR>/<id>.json (text + image URLs only)."""

    id: str
    created: float
    turns: list[Turn] = field(default_factory=list)
    updated: float = 0.0

    @classmethod
    def new(cls) -> "Chat":
        now = time.time()
        return cls(id=datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S-%f"), created=now, updated=now)

    @property
    def title(self) -> str:
        return self.turns[0].question if self.turns else "untitled"

    @property
    def path(self) -> Path:
        return CHATS_DIR / f"{self.id}.json"

    def save(self):
        if not self.turns:
            return
        self.updated = time.time()
        CHATS_DIR.mkdir(parents=True, exist_ok=True)
        turns = [
            {"question": t.question, "markdown": t.markdown, "sources": t.sources, "note": t.note,
             "error": t.error, "context_turns": t.context_turns, "quizzes": t.quizzes}
            for t in self.turns
        ]
        data = {"id": self.id, "created": self.created, "updated": self.updated, "turns": turns}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.replace(self.path)
        shutil.rmtree(CHATS_DIR / self.id, ignore_errors=True)  # drop old-format folder if this chat had one

    @classmethod
    def load(cls, chat_id: str) -> "Chat":
        path = CHATS_DIR / f"{chat_id}.json"
        if not path.exists():
            path = CHATS_DIR / chat_id / "chat.json"  # old format
        data = json.loads(path.read_text())
        turns = [
            Turn(t["question"], LEGACY_IMAGE_REF.sub("", t["markdown"]), t.get("sources", []), t.get("note"),
                 t.get("error"), done=True, context_turns=t.get("context_turns", 0), quizzes=t.get("quizzes", []))
            for t in data["turns"]
        ]
        return cls(data["id"], data["created"], turns, data.get("updated", data["created"]))

    def delete(self):
        self.path.unlink(missing_ok=True)
        shutil.rmtree(CHATS_DIR / self.id, ignore_errors=True)


def list_chats() -> list[Chat]:
    """Most recently updated first."""
    chats = []
    for f in [*CHATS_DIR.glob("*.json"), *CHATS_DIR.glob("*/chat.json")]:
        try:
            chats.append(Chat.load(f.stem if f.name != "chat.json" else f.parent.name))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(chats, key=lambda c: c.updated, reverse=True)
