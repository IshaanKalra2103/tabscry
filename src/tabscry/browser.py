"""tabscry's own Chromium: a separate browser instance that runs the extension for us.

Why: driving AI Mode in *your* browser means the page gets throttled whenever the browser decides
you can't see it (background tab, minimized/occluded window), so answers stall. Our own instance is
launched with throttling disabled, uses its own profile, and never shows up among your windows.
"""

import contextlib
import json
import os
import signal
import platform
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile
import zlib
from pathlib import Path

from .chat import CHATS_DIR

HOME = CHATS_DIR.parent
PROFILES = HOME / "browser-profiles"  # one throwaway profile per run
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
    """Copy of the extension wired to `port`, so it can't clash with one loaded in your own browser.

    The version encodes a hash of the copied files: Chrome keeps the installed copy of an unpacked
    extension's service worker and won't pick up edited files on its own, so without a version change
    the browser keeps running whatever code it installed first.
    """
    EXTENSION_COPY.mkdir(parents=True, exist_ok=True)
    manifest, files = None, {}
    for file in sorted(source.iterdir()):
        if not file.is_file():
            continue
        if file.name == "manifest.json":
            manifest = json.loads(file.read_text())
            continue
        text = file.read_text()
        if file.name == "background.js":
            text = text.replace("ws://127.0.0.1:8765", f"ws://127.0.0.1:{port}")
        files[file.name] = text
    for name, text in files.items():
        (EXTENSION_COPY / name).write_text(text)
    fingerprint = zlib.crc32("".join(files.values()).encode()) % 65535
    base = manifest["version"].rsplit(".", 1)[0]
    manifest["version"] = f"{base}.{fingerprint}"  # changes whenever the code does => Chrome reinstalls
    (EXTENSION_COPY / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return EXTENSION_COPY


class Engine:
    """Runs tabscry's own Chromium for as long as the TUI is up."""

    def __init__(self, port: int):
        self.port = port
        self.process: subprocess.Popen | None = None
        self.extension = EXTENSION_COPY
        self.binary = find_chromium()
        self.profile: Path | None = None

    @property
    def available(self) -> bool:
        return self.binary is not None

    def kill_stale(self):
        """Kill browsers left over from earlier runs (they hold the port we want) and drop their
        profiles. Every run gets a fresh profile: a reused one keeps Chrome's installed copy of the
        extension, so edits to it — including the port — silently never take effect.
        """
        for pid in self.running_pids():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
        if not self.wait_gone(5):
            for pid in self.running_pids():
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
            # must be really gone before relaunching: the profile lock outlives a dying process,
            # and a launch that hits it exits silently
            self.wait_gone(5)
        shutil.rmtree(PROFILES, ignore_errors=True)

    def running_pids(self) -> list[int]:
        found = subprocess.run(["pgrep", "-f", f"--user-data-dir={PROFILES}"], capture_output=True, text=True)
        mine = self.process.pid if self.process else None
        return [int(pid) for pid in found.stdout.split() if pid.isdigit() and int(pid) != mine]

    def wait_gone(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running_pids():
                return True
            time.sleep(0.2)
        return not self.running_pids()

    def start(self):
        if self.process and self.process.poll() is None:
            return
        self.kill_stale()
        if not self.binary:
            raise RuntimeError("no Chromium found — run `tabscry --install-browser`")
        self.extension = extension_for(self.port)
        PROFILES.mkdir(parents=True, exist_ok=True)
        self.profile = Path(tempfile.mkdtemp(prefix="run-", dir=PROFILES))
        self.spawn()

    def spawn(self):
        self.process = subprocess.Popen(
            [
                str(self.binary),
                f"--user-data-dir={self.profile}",
                f"--load-extension={self.extension}",
                f"--disable-extensions-except={self.extension}",
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
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self.kill_stale()  # also deletes the throwaway profiles
