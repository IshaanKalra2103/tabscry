"""tabscry's own Chromium: a separate browser instance that runs the extension for us.

Why: driving AI Mode in *your* browser means the page gets throttled whenever the browser decides
you can't see it (background tab, minimized/occluded window), so answers stall. Our own instance is
launched with throttling disabled, uses its own profile, and never shows up among your windows.
"""

import json
import os
import platform
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

from .chat import CHATS_DIR

HOME = CHATS_DIR.parent
PROFILE = HOME / "browser-profile"
EXTENSION_COPY = HOME / "engine-extension"  # extension copy pointed at our own port
CHROMIUM_DIR = HOME / "chromium"
CFT_API = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"

BUNDLED_EXTENSION = Path(__file__).resolve().parent.parent.parent / "extension"

# Branded Google Chrome 137+ ignores --load-extension, so it can't host our extension.
CANDIDATES = [
    CHROMIUM_DIR / "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    CHROMIUM_DIR / "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    Path("/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
]


def find_chromium() -> Path | None:
    """A Chromium that still honours --load-extension, or None."""
    if env := os.environ.get("TABSCRY_CHROME"):
        return Path(env) if Path(env).exists() else None
    for path in CANDIDATES:
        if path.exists():
            return path
    # Playwright's Chrome for Testing, if this machine happens to have one
    cache = Path.home() / "Library/Caches/ms-playwright"
    builds = sorted(cache.glob("chromium-*/chrome-mac*/Google Chrome for Testing.app"), reverse=True)
    for app in builds:
        binary = app / "Contents/MacOS/Google Chrome for Testing"
        if binary.exists():
            return binary
    return shutil.which("chromium") and Path(shutil.which("chromium"))


def install_chromium(log=print) -> Path:
    """Download an official Chrome for Testing build into ~/.local/share/tabscry/chromium."""
    arch = "mac-arm64" if platform.machine() == "arm64" else "mac-x64"
    data = json.loads(urllib.request.urlopen(CFT_API, timeout=30).read())
    build = data["channels"]["Stable"]
    url = next(d["url"] for d in build["downloads"]["chrome"] if d["platform"] == arch)
    log(f"downloading Chrome for Testing {build['version']} ({arch})…")
    CHROMIUM_DIR.mkdir(parents=True, exist_ok=True)
    archive = CHROMIUM_DIR / "chrome.zip"
    urllib.request.urlretrieve(url, archive)
    log("unpacking…")
    with zipfile.ZipFile(archive) as z:
        z.extractall(CHROMIUM_DIR)
    archive.unlink()
    binary = next(CHROMIUM_DIR.glob("chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/*"))
    binary.chmod(0o755)
    for helper in binary.parent.parent.rglob("*.app/Contents/MacOS/*"):
        helper.chmod(0o755)
    log(f"installed: {binary}")
    return binary


def extension_for(port: int, source: Path = BUNDLED_EXTENSION) -> Path:
    """Copy of the extension wired to `port`, so it can't clash with one loaded in your own browser."""
    EXTENSION_COPY.mkdir(parents=True, exist_ok=True)
    for file in source.iterdir():
        if file.is_file():
            text = file.read_text()
            if file.name == "background.js":
                text = text.replace("ws://127.0.0.1:8765", f"ws://127.0.0.1:{port}")
            (EXTENSION_COPY / file.name).write_text(text)
    return EXTENSION_COPY


class Engine:
    """Runs tabscry's own Chromium for as long as the TUI is up."""

    def __init__(self, port: int):
        self.port = port
        self.process: subprocess.Popen | None = None
        self.binary = find_chromium()

    @property
    def available(self) -> bool:
        return self.binary is not None

    def start(self):
        if self.process and self.process.poll() is None:
            return
        if not self.binary:
            raise RuntimeError("no Chromium found — run `tabscry --install-browser`")
        extension = extension_for(self.port)
        PROFILE.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [
                str(self.binary),
                f"--user-data-dir={PROFILE}",
                f"--load-extension={extension}",
                f"--disable-extensions-except={extension}",
                # the whole point: pages keep running at full speed even when nothing is visible
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--no-startup-window",  # no window until the extension opens one (minimized)
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=Translate,MediaRouter",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self):
        if not self.process or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.process = None
