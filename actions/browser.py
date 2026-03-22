# actions/browser.py
# JARVIS — Browser Primitive
#
# APPROACH: connect_over_cdp — attaches to the user's REAL browser via the
# Chrome DevTools Protocol. No profile lock conflicts. Real cookies, sessions,
# and logins because it's literally the user's actual browser instance.
#
# Flow:
#   1. Try connecting to an already-running browser on the debug port
#   2. If not running: launch the browser with --remote-debugging-port
#   3. Wait for CDP to be ready, then connect
#   4. All future calls reuse this connection
#
# ─────────────────────────────────────────────────────────────
# DELAY TUNING — change these if pages are loading too slowly/fast
# ─────────────────────────────────────────────────────────────
# DELAY_AFTER_NAVIGATE  : seconds to wait after page load before reading
#                         Increase if JS-heavy pages haven't rendered yet
# DELAY_CDP_READY       : seconds between port-check retries when launching
# DELAY_CDP_MAX_WAIT    : total seconds to wait for browser to open debug port
# DELAY_CLICK           : seconds to pause after a click (lets page react)
# ─────────────────────────────────────────────────────────────
DELAY_AFTER_NAVIGATE = 2.0    # increase to 3-4 for slow/JS-heavy sites
DELAY_CDP_READY      = 0.4    # polling interval while waiting for browser
DELAY_CDP_MAX_WAIT   = 15     # max seconds to wait for debug port
DELAY_CLICK          = 0.5    # pause after click

CDP_PORT = 9222  # remote debugging port — must be free when JARVIS launches

import asyncio
import concurrent.futures
import io
import json
import platform
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote_plus, urljoin

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    _PLAYWRIGHT = True
except ImportError:
    _PLAYWRIGHT = False

try:
    from bs4 import BeautifulSoup
    _BS4 = True
except ImportError:
    _BS4 = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False

JPEG_Q    = 60
IMG_MAX_W = 1280
IMG_MAX_H = 720
_OS       = platform.system()


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


# ─────────────────────────────────────────────────────────────
# BROWSER INSTALL LOCATIONS
# ─────────────────────────────────────────────────────────────

def _h(rel: str) -> Path:
    return Path.home() / rel


_BROWSERS = {
    "brave": {
        "display": "Brave",
        "exe": {
            "Windows": [
                _h("AppData/Local/BraveSoftware/Brave-Browser/Application/brave.exe"),
                Path("C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe"),
            ],
            "Darwin": [Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")],
            "Linux":  [Path("/usr/bin/brave-browser"), Path("/usr/bin/brave")],
        },
    },
    "chrome": {
        "display": "Google Chrome",
        "exe": {
            "Windows": [
                _h("AppData/Local/Google/Chrome/Application/chrome.exe"),
                Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
            ],
            "Darwin": [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")],
            "Linux":  [Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium-browser")],
        },
    },
    "edge": {
        "display": "Microsoft Edge",
        "exe": {
            "Windows": [
                Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
                Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
            ],
            "Darwin": [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")],
            "Linux":  [Path("/usr/bin/microsoft-edge")],
        },
    },
    "opera": {
        "display": "Opera",
        "exe": {
            "Windows": [
                _h("AppData/Local/Programs/Opera/opera.exe"),
                Path("C:/Program Files/Opera/opera.exe"),
            ],
            "Darwin": [Path("/Applications/Opera.app/Contents/MacOS/Opera")],
            "Linux":  [Path("/usr/bin/opera")],
        },
    },
    "opera_gx": {
        "display": "Opera GX",
        "exe": {
            "Windows": [
                _h("AppData/Local/Programs/Opera GX/opera.exe"),
                Path("C:/Program Files/Opera GX/opera.exe"),
            ],
        },
    },
    "vivaldi": {
        "display": "Vivaldi",
        "exe": {
            "Windows": [
                _h("AppData/Local/Vivaldi/Application/vivaldi.exe"),
                Path("C:/Program Files/Vivaldi/Application/vivaldi.exe"),
            ],
            "Darwin": [Path("/Applications/Vivaldi.app/Contents/MacOS/Vivaldi")],
            "Linux":  [Path("/usr/bin/vivaldi-stable"), Path("/usr/bin/vivaldi")],
        },
    },
    "firefox": {
        "display": "Firefox",
        "exe": {
            "Windows": [
                Path("C:/Program Files/Mozilla Firefox/firefox.exe"),
                Path("C:/Program Files (x86)/Mozilla Firefox/firefox.exe"),
            ],
            "Darwin": [Path("/Applications/Firefox.app/Contents/MacOS/firefox")],
            "Linux":  [Path("/usr/bin/firefox")],
        },
    },
}


def _find_exe(name: str) -> Path | None:
    for p in _BROWSERS.get(name, {}).get("exe", {}).get(_OS, []):
        if p.exists():
            return p
    return None


def detect_installed_browsers() -> list[dict]:
    """Returns list of browsers found on this machine."""
    results = []
    for name, info in _BROWSERS.items():
        exe = _find_exe(name)
        if exe:
            results.append({
                "name":      name,
                "display":   info["display"],
                "exe":       str(exe),
                "available": True,
            })
    return results


# ─────────────────────────────────────────────────────────────
# PREFERENCE
# ─────────────────────────────────────────────────────────────

def get_browser_preference() -> str | None:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("browser")
    except Exception:
        return None


def set_browser_preference(name: str):
    try:
        cfg = {}
        if API_CONFIG_PATH.exists():
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        cfg["browser"] = name
        with open(API_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
        print(f"[Browser] 💾 Preference saved: {name}")
    except Exception as e:
        print(f"[Browser] ⚠️ Could not save preference: {e}")


def _resolve_browser() -> dict | None:
    """Returns browser info for saved/auto-detected browser, or None."""
    saved    = get_browser_preference()
    browsers = {b["name"]: b for b in detect_installed_browsers()}

    if saved and saved in browsers:
        return browsers[saved]
    if saved:
        print(f"[Browser] ⚠️ Saved browser '{saved}' not found — auto-detecting")

    for name in ["brave", "chrome", "edge", "opera_gx", "opera", "vivaldi", "firefox"]:
        if name in browsers:
            print(f"[Browser] 🔍 Auto-detected: {browsers[name]['display']}")
            return browsers[name]

    return None


# ─────────────────────────────────────────────────────────────
# CDP HELPERS
# ─────────────────────────────────────────────────────────────

def _port_open(port: int) -> bool:
    """True if something is listening on localhost:port."""
    try:
        s = socket.create_connection(("localhost", port), timeout=0.5)
        s.close()
        return True
    except Exception:
        return False


def _launch_browser_with_cdp(exe: str, port: int) -> subprocess.Popen:
    """
    Launches the browser with remote debugging enabled.
    The browser opens using whatever the default profile is — no user-data-dir
    flag means Chromium uses its own default profile directory, same as when
    the user opens it normally from the taskbar.
    """
    args = [
        exe,
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
    ]
    # Firefox uses a different flag
    if "firefox" in exe.lower():
        args = [exe, f"--remote-debugging-port={port}", "--new-instance"]

    print(f"[Browser] 🚀 Launching with CDP on port {port}: {Path(exe).name}")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ─────────────────────────────────────────────────────────────
# KNOWN SELECTORS — Tier 2
# ─────────────────────────────────────────────────────────────

KNOWN_SELECTORS = {
    "youtube_video_link":    ["ytd-video-renderer a#video-title",
                              "a#video-title", "ytd-rich-item-renderer a#video-title"],
    "youtube_video_title":   ["#video-title", "h1.ytd-video-primary-info-renderer"],
    "google_first_result":   [".yuRUbf > a", ".tF2Cxc a", "#search .g a"],
    "google_weather_temp":   ["#wob_tm", ".wob_t"],
    "google_weather_desc":   ["#wob_dc", ".wob_dcp"],
    "gmail_unread_rows":     [".zA"],
    "gmail_subject":         [".y6"],
    "wikipedia_content":     ["#mw-content-text p"],
    "wikipedia_title":       ["#firstHeading"],
    "classroom_assignments": ["[data-assignmentid]", ".k3Jkib", ".UVErfc"],
}


# ─────────────────────────────────────────────────────────────
# URL CONSTRUCTION — Tier 1
# ─────────────────────────────────────────────────────────────

def construct_url(service: str, **kwargs) -> str:
    service = service.lower().strip()
    q       = kwargs.get("query", "")
    q_enc   = quote_plus(q)
    patterns = {
        "google":           f"https://www.google.com/search?q={q_enc}",
        "google_search":    f"https://www.google.com/search?q={q_enc}",
        "youtube":          f"https://www.youtube.com/results?search_query={q_enc}",
        "youtube_search":   f"https://www.youtube.com/results?search_query={q_enc}",
        "youtube_by_views": f"https://www.youtube.com/results?search_query={q_enc}&sp=CAM%3D",
        "youtube_views":    f"https://www.youtube.com/results?search_query={q_enc}&sp=CAM%3D",
        "google_flights":   (
            "https://www.google.com/travel/flights?q=Flights+from+"
            f"{quote_plus(kwargs.get('origin',''))}+to+"
            f"{quote_plus(kwargs.get('destination',''))}+on+"
            f"{quote_plus(kwargs.get('date',''))}"
        ),
        "google_maps": (
            "https://www.google.com/maps/dir/"
            f"{quote_plus(kwargs.get('origin',''))}/"
            f"{quote_plus(kwargs.get('destination',''))}"
        ),
        "gmail":            "https://mail.google.com/",
        "google_drive":     "https://drive.google.com/",
        "google_classroom": "https://classroom.google.com/",
        "classroom_todo":   "https://classroom.google.com/a/not-turned-in/all",
        "whatsapp":         "https://web.whatsapp.com/",
        "wikipedia":        f"https://en.wikipedia.org/wiki/{q_enc}",
        "amazon":           f"https://www.amazon.com/s?k={q_enc}",
        "reddit":           f"https://www.reddit.com/search/?q={q_enc}",
        "github":           f"https://github.com/search?q={q_enc}",
        "weather":          f"https://www.google.com/search?q=weather+{q_enc}",
    }
    return patterns.get(service, f"https://www.google.com/search?q={q_enc}")


# ─────────────────────────────────────────────────────────────
# BROWSER THREAD — CDP connection
# ─────────────────────────────────────────────────────────────

class _BrowserThread:

    def __init__(self):
        self._loop:      asyncio.AbstractEventLoop | None = None
        self._thread:    threading.Thread | None          = None
        self._ready:     threading.Event                  = threading.Event()
        self._playwright = None
        self._browser    = None   # CDP browser object
        self._context    = None
        self._page       = None
        self._proc:      subprocess.Popen | None          = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="JarvisBrowserThread"
        )
        self._thread.start()
        self._ready.wait(timeout=20)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._init())
        self._ready.set()
        self._loop.run_forever()

    async def _init(self):
        self._playwright = await async_playwright().start()

    def run(self, coro, timeout: int = 30):
        if not self._loop:
            raise RuntimeError("BrowserThread not started.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _get_page(self):
        if self._page is None or self._page.is_closed():
            await self._launch()
        return self._page

    async def _launch(self):
        """
        Connect to the user's real browser via CDP.

        Step 1: Try connecting to an already-running browser on CDP_PORT.
                This works if the browser was previously launched by JARVIS.
        Step 2: Launch the browser with --remote-debugging-port and wait for
                it to be ready, then connect.
        Step 3: If no browser found/configured, fall back to Playwright's
                built-in Chromium (no real profile but still functional).
        """
        port = CDP_PORT

        # ── Step 1: already running with debug port? ──
        if _port_open(port):
            try:
                self._browser  = await self._playwright.chromium.connect_over_cdp(
                    f"http://localhost:{port}"
                )
                self._context  = self._browser.contexts[0] if self._browser.contexts \
                                 else await self._browser.new_context(viewport=None)
                pages          = self._context.pages
                self._page     = pages[0] if pages else await self._context.new_page()
                print(f"[Browser] ✅ Connected to existing browser on port {port}")
                return
            except Exception as e:
                print(f"[Browser] ⚠️ Existing CDP connection failed: {e}")

        # ── Step 2: launch with debug port ──
        cfg = _resolve_browser()
        if cfg and cfg.get("exe"):
            exe = cfg["exe"]
            self._proc = _launch_browser_with_cdp(exe, port)

            # Wait for debug port to open
            deadline = time.time() + DELAY_CDP_MAX_WAIT
            while time.time() < deadline:
                await asyncio.sleep(DELAY_CDP_READY)
                if _port_open(port):
                    break
            else:
                print(f"[Browser] ⚠️ Browser did not open debug port in {DELAY_CDP_MAX_WAIT}s")

            if _port_open(port):
                try:
                    self._browser  = await self._playwright.chromium.connect_over_cdp(
                        f"http://localhost:{port}"
                    )
                    self._context  = self._browser.contexts[0] if self._browser.contexts \
                                     else await self._browser.new_context(viewport=None)
                    pages          = self._context.pages
                    self._page     = pages[0] if pages else await self._context.new_page()
                    display        = cfg.get("display", "Browser")
                    print(f"[Browser] ✅ {display} launched with real profile via CDP")
                    return
                except Exception as e:
                    print(f"[Browser] ⚠️ CDP connect after launch failed: {e}")

        # ── Step 3: fallback — Playwright's own Chromium ──
        print("[Browser] ⚠️ Falling back to built-in Chromium (no real profile)")
        b              = await self._playwright.chromium.launch(headless=False,
                                                                args=["--start-maximized"])
        self._context  = await b.new_context(viewport=None)
        self._page     = await self._context.new_page()

    async def _close(self):
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser  = None
            self._context  = None
            self._page     = None
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    # ── Navigation ──────────────────────────────────────────

    async def _go_to(self, url: str) -> str:
        if not url.startswith("http"):
            url = "https://" + url
        page = await self._get_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(DELAY_AFTER_NAVIGATE)
            return f"Navigated to: {page.url}"
        except PlaywrightTimeout:
            return f"Timeout: {url}"
        except Exception as e:
            return f"Navigation error: {e}"

    async def _get_url(self) -> str:
        return (await self._get_page()).url

    async def _back(self) -> str:
        await (await self._get_page()).go_back()
        return "Navigated back."

    async def _reload(self) -> str:
        await (await self._get_page()).reload()
        return "Page reloaded."

    async def _new_tab(self, url: str = "") -> str:
        page       = await self._context.new_page()
        self._page = page
        if url:
            if not url.startswith("http"):
                url = "https://" + url
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(DELAY_AFTER_NAVIGATE)
        return f"New tab{': ' + url if url else ''}."

    async def _close_tab(self) -> str:
        if self._page and not self._page.is_closed():
            await self._page.close()
            pages      = self._context.pages
            self._page = pages[-1] if pages else None
        return "Tab closed."

    # ── Tier 2: HTML parsing ─────────────────────────────────

    async def _fetch_html(self) -> str:
        try:
            return await (await self._get_page()).content()
        except Exception as e:
            return f"Could not fetch HTML: {e}"

    async def _parse_html(self, selector: str = "", known_key: str = "",
                           attribute: str = "href", limit: int = 5) -> str:
        if not _BS4:
            return "BeautifulSoup not installed. Run: pip install beautifulsoup4"
        html = await self._fetch_html()
        if html.startswith("Could not"):
            return html
        soup      = BeautifulSoup(html, "html.parser")
        selectors = []
        if known_key and known_key in KNOWN_SELECTORS:
            selectors.extend(KNOWN_SELECTORS[known_key])
        if selector:
            selectors.insert(0, selector)
        if not selectors:
            return json.dumps({"error": "No selector specified."})
        results = []
        for sel in selectors:
            for el in soup.select(sel, limit=limit * 2)[:limit]:
                if attribute == "text":
                    val = el.get_text(strip=True)
                elif attribute == "href":
                    val = el.get("href", "")
                    if val and not val.startswith("http"):
                        try:
                            val = urljoin((await self._get_page()).url, val)
                        except Exception:
                            pass
                else:
                    val = el.get(attribute, el.get_text(strip=True))
                if val:
                    results.append({"value": val, "text": el.get_text(strip=True)[:100]})
            if results:
                break
        if not results:
            return json.dumps({"found": [], "count": 0,
                               "note": f"No elements matched: {selectors}"})
        return json.dumps({"found": results, "count": len(results)}, ensure_ascii=False)

    # ── Tier 3: Page text ────────────────────────────────────

    async def _get_text(self, max_chars: int = 6000) -> str:
        try:
            text = await (await self._get_page()).inner_text("body")
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = re.sub(r"[ \t]+", " ", text)
            return text[:max_chars]
        except Exception as e:
            return f"Could not get text: {e}"

    # ── Tier 4: Vision read ──────────────────────────────────

    async def _vision_read(self, question: str) -> str:
        from google import genai
        from google.genai import types
        page = await self._get_page()
        try:
            png_bytes = await page.screenshot(full_page=False)
            if _PIL:
                img = PIL.Image.open(io.BytesIO(png_bytes)).convert("RGB")
                img.thumbnail([IMG_MAX_W, IMG_MAX_H], PIL.Image.BILINEAR)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=JPEG_Q)
                image_bytes = buf.getvalue()
            else:
                image_bytes = png_bytes
            client   = genai.Client(api_key=_get_api_key())
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=types.Content(role="user", parts=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    types.Part.from_text(text=question)
                ])
            )
            return response.text.strip() if response.text else "Could not read page visually."
        except Exception as e:
            return f"Vision read failed: {e}"

    # ── Interaction ──────────────────────────────────────────

    async def _click_element(self, selector: str = "", text: str = "",
                              description: str = "") -> str:
        page = await self._get_page()
        try:
            if selector:
                await page.click(selector, timeout=8000)
            elif text:
                await page.get_by_text(text, exact=False).first.click(timeout=8000)
            elif description:
                for role in ["button", "link", "menuitem"]:
                    try:
                        await page.get_by_role(role, name=description,
                                               exact=False).first.click(timeout=4000)
                        await asyncio.sleep(DELAY_CLICK)
                        return f"Clicked ({role}): {description}"
                    except Exception:
                        pass
                await page.get_by_text(description, exact=False).first.click(timeout=5000)
            else:
                return "No click target specified."
            await asyncio.sleep(DELAY_CLICK)
            return f"Clicked."
        except Exception as e:
            return f"Click failed: {e}"

    async def _type_into(self, text: str, selector: str = "",
                          clear_first: bool = True) -> str:
        page = await self._get_page()
        try:
            el = page.locator(selector).first if selector else page.locator(":focus")
            if clear_first:
                await el.clear()
            await el.type(text, delay=40)
            return "Typed into field."
        except Exception:
            try:
                await page.keyboard.type(text)
                return "Typed (keyboard)."
            except Exception as e:
                return f"Type failed: {e}"

    async def _scroll(self, direction: str = "down", amount: int = 500) -> str:
        page = await self._get_page()
        try:
            await page.mouse.wheel(0, amount if direction == "down" else -amount)
            return f"Scrolled {direction}."
        except Exception as e:
            return f"Scroll failed: {e}"

    async def _press(self, key: str) -> str:
        try:
            await (await self._get_page()).keyboard.press(key)
            return f"Pressed: {key}"
        except Exception as e:
            return f"Key press failed: {e}"

    async def _close_browser(self) -> str:
        await self._close()
        return "Browser closed."


# ─────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────

_bt         = _BrowserThread()
_bt_started = False
_bt_lock    = threading.Lock()


def _ensure_started():
    global _bt_started
    if not _PLAYWRIGHT:
        raise RuntimeError(
            "Playwright not installed. Run: pip install playwright && playwright install"
        )
    with _bt_lock:
        if not _bt_started:
            _bt.start()
            _bt_started = True


# ─────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────

def browser(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Browser primitive — universal web control with tiered reading strategy.

    DELAY TUNING (top of this file):
        DELAY_AFTER_NAVIGATE  — increase if pages haven't rendered before reading
        DELAY_CDP_MAX_WAIT    — increase if your browser takes a long time to open
        DELAY_CLICK           — increase if clicks aren't registering

    actions:
        go_to        : Navigate to URL
        construct_url: Build service URL (google, youtube, youtube_by_views,
                       google_flights, google_maps, gmail, google_classroom,
                       classroom_todo, whatsapp, wikipedia, amazon, weather)
        fetch_html   : Get raw HTML source (Tier 2)
        parse_html   : Parse HTML for elements — preferred for finding links
                       selector, known_key, attribute (href/text/src), limit
        get_text     : All visible page text (Tier 3)
        vision_read  : Screenshot + Gemini question (Tier 4) — costs API call
                       question: specific answerable question
        click        : Click by selector, text, or description
        type         : Type into field (selector, text, clear_first)
        scroll       : Scroll direction up|down, amount (pixels)
        press        : Press key (Enter, Escape, Tab, etc.)
        get_url      : Get current page URL
        back         : Navigate back
        reload       : Reload page
        new_tab      : Open new tab (optional url)
        close_tab    : Close current tab
        close        : Disconnect from browser
    """
    _ensure_started()

    action = (parameters or {}).get("action", "").lower().strip()
    result = "Unknown action."

    try:
        if action == "go_to":
            url = parameters.get("url", "")
            if not url:
                return "Please provide a URL."
            result = _bt.run(_bt._go_to(url), timeout=35)

        elif action == "construct_url":
            result = construct_url(
                parameters.get("service", "google"),
                **{k: v for k, v in parameters.items() if k not in ("action", "service")}
            )

        elif action == "fetch_html":
            result = _bt.run(_bt._fetch_html(), timeout=20)

        elif action == "parse_html":
            result = _bt.run(_bt._parse_html(
                selector  = parameters.get("selector", ""),
                known_key = parameters.get("known_key", ""),
                attribute = parameters.get("attribute", "href"),
                limit     = int(parameters.get("limit", 5))
            ), timeout=20)

        elif action == "get_text":
            result = _bt.run(_bt._get_text(
                max_chars=int(parameters.get("max_chars", 6000))
            ), timeout=20)

        elif action == "vision_read":
            q = (parameters.get("question", "") or parameters.get("text", "")).strip()
            if not q:
                return "Please provide a question for vision_read."
            result = _bt.run(_bt._vision_read(q), timeout=30)

        elif action == "click":
            result = _bt.run(_bt._click_element(
                selector    = parameters.get("selector", ""),
                text        = parameters.get("text", ""),
                description = parameters.get("description", "")
            ), timeout=15)

        elif action == "type":
            result = _bt.run(_bt._type_into(
                text        = parameters.get("text", ""),
                selector    = parameters.get("selector", ""),
                clear_first = parameters.get("clear_first", True)
            ), timeout=10)

        elif action == "scroll":
            result = _bt.run(_bt._scroll(
                direction = parameters.get("direction", "down"),
                amount    = int(parameters.get("amount", 500))
            ), timeout=10)

        elif action == "press":
            result = _bt.run(_bt._press(parameters.get("key", "Enter")), timeout=10)

        elif action == "get_url":
            result = _bt.run(_bt._get_url(), timeout=10)

        elif action == "back":
            result = _bt.run(_bt._back(), timeout=15)

        elif action == "reload":
            result = _bt.run(_bt._reload(), timeout=15)

        elif action == "new_tab":
            result = _bt.run(_bt._new_tab(parameters.get("url", "")), timeout=25)

        elif action == "close_tab":
            result = _bt.run(_bt._close_tab(), timeout=10)

        elif action == "close":
            result = _bt.run(_bt._close_browser(), timeout=10)

        else:
            result = f"Unknown browser action: '{action}'"

    except concurrent.futures.TimeoutError:
        result = f"Browser action '{action}' timed out."
    except Exception as e:
        result = f"Browser error: {e}"

    print(f"[Browser] {str(result)[:100]}")
    if player:
        player.write_log(f"[browser] {str(result)[:60]}")
    return result
