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
DELAY_AFTER_NAVIGATE = 3.0    # increased from 2.0 — JS-heavy sites need more time
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


_PROFILE_DIRS = {
    "brave":   "BraveSoftware/Brave-Browser/User Data",
    "chrome":  "Google/Chrome/User Data",
    "edge":    "Microsoft/Edge/User Data",
    "opera":   "Opera Software/Opera Stable",
    "opera_gx":"Opera Software/Opera GX Stable",
    "vivaldi": "Vivaldi/User Data",
}


def _get_profile_dir(browser_name: str) -> Path | None:
    """Returns the user-data-dir for the given Chromium browser, or None."""
    rel = _PROFILE_DIRS.get(browser_name)
    if not rel:
        return None
    if _OS == "Windows":
        import os
        local = os.environ.get("LOCALAPPDATA", "")
        appdata = os.environ.get("APPDATA", "")
        # Opera stores in %APPDATA%, others in %LOCALAPPDATA%
        base = appdata if "Opera" in rel else local
        if base:
            p = Path(base) / rel
            if p.exists():
                return p
    elif _OS == "Darwin":
        p = Path.home() / "Library/Application Support" / rel
        if p.exists():
            return p
    elif _OS == "Linux":
        p = Path.home() / ".config" / rel
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


def _kill_browser_process(exe_name: str) -> bool:
    killed = False
    try:
        if _OS == "Windows":
            # Added /t to force kill all related background child processes
            result = subprocess.run(
                ["taskkill", "/f", "/im", exe_name, "/t"],
                capture_output=True, text=True
            )
            killed = result.returncode == 0
        else:
            # Added -9 for hard kill on Unix/Mac
            result = subprocess.run(
                ["pkill", "-9", "-f", exe_name],
                capture_output=True
            )
            killed = result.returncode == 0
        if killed:
            print(f"[Browser] 🔴 Restarted {exe_name} to enable automation (tabs will be restored)")
            time.sleep(2.0)
    except Exception as e:
        print(f"[Browser] ⚠️ Could not kill {exe_name}: {e}")
    return killed


def _launch_browser_with_cdp(exe: str, port: int, profile_dir: Path | None = None) -> subprocess.Popen:
    is_firefox = "firefox" in exe.lower()
    if is_firefox:
        args = [exe, f"--remote-debugging-port={port}", "--new-instance"]
    else:
        args = [
            exe,
            f"--remote-debugging-port={port}",
            "--restore-last-session",  # Restores all your tabs so you don't lose your place
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
        ]
        if profile_dir:
            args.append(f"--user-data-dir={profile_dir}")

    print(f"[Browser] 🚀 Launching with CDP on port {port}: {Path(exe).name}")
    
    kwargs = {}
    if _OS == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
        
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)

# ─────────────────────────────────────────────────────────────
# KNOWN SELECTORS — Tier 2 (used as hints for JS evaluation)
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
    "soundcloud_track":      ["a.soundTitle__title", ".trackItem__trackTitle",
                              "a[href*='/'][class*='title']"],
}


# ─────────────────────────────────────────────────────────────
# URL CONSTRUCTION — Tier 1
# ─────────────────────────────────────────────────────────────

def construct_url(service: str, **kwargs) -> str:
    service = service.lower().strip()
    q       = kwargs.get("query", "")
    q_enc   = quote_plus(q)
    patterns = {
        # Search engines
        "google":           f"https://www.google.com/search?q={q_enc}",
        "google_search":    f"https://www.google.com/search?q={q_enc}",
        "bing":             f"https://www.bing.com/search?q={q_enc}",
        "duckduckgo":       f"https://duckduckgo.com/?q={q_enc}",
        # Video / music
        "youtube":          f"https://www.youtube.com/results?search_query={q_enc}",
        "youtube_search":   f"https://www.youtube.com/results?search_query={q_enc}",
        "youtube_by_views": f"https://www.youtube.com/results?search_query={q_enc}&sp=CAM%3D",
        "youtube_views":    f"https://www.youtube.com/results?search_query={q_enc}&sp=CAM%3D",
        "   oud":       f"https://soundcloud.com/search?q={q_enc}",
        "soundcloud_search":f"https://soundcloud.com/search?q={q_enc}",
        "spotify":          f"https://open.spotify.com/search/{q_enc}",
        # Travel / booking
        "google_flights": (
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
        "google_hotels": (
            "https://www.google.com/travel/hotels/"
            f"?q={q_enc}"
            + (f"&checkin={quote_plus(kwargs.get('checkin',''))}" if kwargs.get('checkin') else "")
            + (f"&checkout={quote_plus(kwargs.get('checkout',''))}" if kwargs.get('checkout') else "")
        ),
        "booking":          f"https://www.booking.com/searchresults.html?ss={q_enc}",
        "airbnb":           f"https://www.airbnb.com/s/{q_enc}/homes",
        "tripadvisor":      f"https://www.tripadvisor.com/Search?q={q_enc}",
        # Google services
        "gmail":            "https://mail.google.com/",
        "google_drive":     "https://drive.google.com/",
        "google_classroom": "https://classroom.google.com/",
        "classroom_todo":   "https://classroom.google.com/a/not-turned-in/all",
        "google_calendar":  "https://calendar.google.com/",
        "google_docs":      "https://docs.google.com/",
        # Social / messaging
        "whatsapp":         "https://web.whatsapp.com/",
        "twitter":          f"https://twitter.com/search?q={q_enc}",
        "x":                f"https://x.com/search?q={q_enc}",
        "instagram":        f"https://www.instagram.com/explore/tags/{q_enc}/",
        "reddit":           f"https://www.reddit.com/search/?q={q_enc}",
        # Shopping
        "amazon":           f"https://www.amazon.com/s?k={q_enc}",
        "ebay":             f"https://www.ebay.com/sch/i.html?_nkw={q_enc}",
        # Reference
        "wikipedia":        f"https://en.wikipedia.org/wiki/{q_enc}",
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
        self._browser    = None
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
        if self._page is not None and not self._page.is_closed():
            # Lightweight ping to verify CDP connection is still alive
            try:
                await self._page.evaluate("1")
                return self._page
            except Exception:
                print("[Browser] ⚠️ CDP connection lost — reconnecting...")
                self._browser = None
                self._context = None
                self._page = None
        await self._launch()
        return self._page

    async def _launch(self):
        port = CDP_PORT

        if _port_open(port):
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    f"http://localhost:{port}"
                )
                self._context = (self._browser.contexts[0] if self._browser.contexts
                                 else await self._browser.new_context(viewport=None))
                
                # ALWAYS open a fresh tab for Jarvis so we don't hijack your active tab
                self._page = await self._context.new_page()
                print(f"[Browser] ✅ Connected to existing browser on port {port}")
                return
            except Exception as e:
                print(f"[Browser] ⚠️ Existing CDP connection failed: {e}")

        cfg = _resolve_browser()
        if cfg and cfg.get("exe"):
            exe      = cfg["exe"]
            exe_name = Path(exe).name

            _kill_browser_process(exe_name)
            profile_dir = _get_profile_dir(cfg.get("name", ""))
            self._proc = _launch_browser_with_cdp(exe, port, profile_dir)

            deadline = time.time() + DELAY_CDP_MAX_WAIT
            while time.time() < deadline:
                await asyncio.sleep(DELAY_CDP_READY)
                if _port_open(port):
                    break
            else:
                print(f"[Browser] ⚠️ Browser did not open debug port in {DELAY_CDP_MAX_WAIT}s")

            if _port_open(port):
                try:
                    self._browser = await self._playwright.chromium.connect_over_cdp(
                        f"http://localhost:{port}"
                    )
                    self._context = (self._browser.contexts[0] if self._browser.contexts
                                     else await self._browser.new_context(viewport=None))
                    
                    # ALWAYS open a fresh tab for Jarvis
                    self._page = await self._context.new_page()
                    display    = cfg.get("display", "Browser")
                    print(f"[Browser] ✅ {display} launched with real profile via CDP")
                    return
                except Exception as e:
                    print(f"[Browser] ⚠️ CDP connect after launch failed: {e}")

        print("\n[Browser] ⚠️ FALLBACK: Could not restart your main browser with automation.")
        print("[Browser] ⚠️ Using built-in Chromium (BLANK PROFILE - NO PASSWORDS).")
        print("[Browser] 💡 Close your browser manually before starting to avoid this.\n")
        
        b             = await self._playwright.chromium.launch(
            headless=False, args=["--start-maximized"]
        )
        self._context = await b.new_context(viewport=None)
        self._page    = await self._context.new_page()

    async def _close(self):
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser  = None
            self._context  = None
            self._page     = None
            
        # DO NOT terminate self._proc here so the user's main browser stays open!
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
        try:
            await (await self._get_page()).go_back()
            return "Navigated back."
        except Exception as e:
            return f"Back navigation failed: {e}"

    async def _reload(self) -> str:
        try:
            await (await self._get_page()).reload()
            return "Page reloaded."
        except Exception as e:
            return f"Reload failed: {e}"

    async def _new_tab(self, url: str = "") -> str:
        try:
            page       = await self._context.new_page()
            self._page = page
            if url:
                if not url.startswith("http"):
                    url = "https://" + url
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(DELAY_AFTER_NAVIGATE)
            return f"New tab{': ' + url if url else ''}."
        except Exception as e:
            return f"New tab failed: {e}"

    async def _close_tab(self) -> str:
        if self._page and not self._page.is_closed():
            await self._page.close()
            pages      = self._context.pages
            self._page = pages[-1] if pages else None
        return "Tab closed."

    # ── Tier 2: JS DOM evaluation (replaces BeautifulSoup) ───
    # Works on React / Vue / SPA sites — reads the LIVE rendered DOM
    # via JavaScript, not the static HTML source. This is why SoundCloud,
    # YouTube search results, and other JS-heavy sites now work correctly.

    async def _fetch_html(self) -> str:
        try:
            return await (await self._get_page()).content()
        except Exception as e:
            return f"Could not fetch HTML: {e}"

    async def _parse_html(self, selector: str = "", known_key: str = "",
                           attribute: str = "href", limit: int = 5) -> str:
        """
        Queries the LIVE rendered DOM via JavaScript first.
        Works on any site — SPA, React, server-rendered, anything.
        Falls back to BeautifulSoup on the raw HTML only if JS eval fails.
        """
        page = await self._get_page()

        # Build list of selectors to try
        selectors = []
        if known_key and known_key in KNOWN_SELECTORS:
            selectors.extend(KNOWN_SELECTORS[known_key])
        if selector:
            selectors.insert(0, selector)
        if not selectors:
            return json.dumps({"error": "No selector specified."})

        base_url = page.url

        # ── Primary path: JS page.evaluate() on the live DOM ──
        for sel in selectors:
            try:
                # Escape selector for JS string — single quotes need escaping
                sel_escaped = sel.replace("\\", "\\\\").replace("'", "\\'")
                attr_escaped = attribute.replace("\\", "\\\\").replace("'", "\\'")

                raw = await page.evaluate(f"""
                    () => {{
                        const els = Array.from(
                            document.querySelectorAll('{sel_escaped}')
                        ).slice(0, {limit * 3});

                        return els.map(el => {{
                            let val = '';
                            if ('{attr_escaped}' === 'href') {{
                                val = el.href || el.getAttribute('href') || '';
                            }} else if ('{attr_escaped}' === 'text') {{
                                val = el.textContent.trim();
                            }} else if ('{attr_escaped}' === 'src') {{
                                val = el.src || el.getAttribute('src') || '';
                            }} else {{
                                val = el.getAttribute('{attr_escaped}') ||
                                      el.textContent.trim();
                            }}
                            return {{
                                value: val,
                                text:  el.textContent.trim()
                                           .replace(/\\s+/g, ' ')
                                           .substring(0, 120)
                            }};
                        }}).filter(r => r.value && r.value.length > 0);
                    }}
                """)

                if raw:
                    # Resolve relative URLs to absolute
                    results = []
                    seen    = set()
                    for r in raw:
                        v = r.get("value", "")
                        if v and not v.startswith("http") and not v.startswith("//"):
                            v = urljoin(base_url, v)
                            r["value"] = v
                        # Skip duplicates and javascript: / data: URIs
                        if v in seen or v.startswith("javascript:") or v.startswith("data:"):
                            continue
                        seen.add(v)
                        results.append(r)
                        if len(results) >= limit:
                            break

                    if results:
                        print(f"[Browser] ✅ JS DOM found {len(results)} elements with '{sel}'")
                        return json.dumps(
                            {"found": results, "count": len(results)},
                            ensure_ascii=False
                        )

            except Exception as e:
                print(f"[Browser] ⚠️ JS eval failed for '{sel}': {e}")
                continue

        # ── Fallback: BeautifulSoup on raw HTML ──────────────
        # Only reached if JS eval found nothing (e.g. selector is wrong).
        # For JS-rendered sites this will also return nothing, which means
        # the selector itself needs updating — not a code bug.
        if _BS4:
            try:
                html = await self._fetch_html()
                if not html.startswith("Could not"):
                    soup = BeautifulSoup(html, "html.parser")
                    results = []
                    for sel in selectors:
                        for el in soup.select(sel, limit=limit * 2)[:limit]:
                            if attribute == "text":
                                val = el.get_text(strip=True)
                            elif attribute == "href":
                                val = el.get("href", "")
                                if val and not val.startswith("http"):
                                    val = urljoin(base_url, val)
                            else:
                                val = el.get(attribute, el.get_text(strip=True))
                            if val:
                                results.append({
                                    "value": val,
                                    "text": el.get_text(strip=True)[:100]
                                })
                        if results:
                            break
                    if results:
                        print(f"[Browser] ✅ BS4 fallback found {len(results)} elements")
                        return json.dumps(
                            {"found": results, "count": len(results)},
                            ensure_ascii=False
                        )
            except Exception as e:
                print(f"[Browser] ⚠️ BS4 fallback failed: {e}")

        return json.dumps({
            "found":  [],
            "count":  0,
            "note":   (
                f"No elements matched: {selectors[:3]}. "
                f"Page may need more load time (try wait_for_content first) "
                f"or the selector needs updating for this site."
            )
        })

    # ── Tier 2.5: Wait for JS to finish rendering ────────────

    async def _wait_for_content(self, timeout_ms: int = 5000) -> str:
        """
        Waits for the page network to go idle — i.e. JS has finished
        making API calls and inserting content into the DOM.
        Call this before parse_html on JS-heavy sites like Google Classroom,
        SoundCloud, YouTube when you need DOM content to be ready.
        """
        page = await self._get_page()
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            await asyncio.sleep(0.5)  # small buffer after idle
            return "Page fully loaded and network idle."
        except Exception:
            # Timeout is fine — partial load is better than nothing
            await asyncio.sleep(1.0)
            return "Wait complete (network did not fully idle — page may be partially loaded)."

    # ── Tier 3: Page text ────────────────────────────────────

    async def _get_text(self, max_chars: int = 6000) -> str:
        """
        Smart content extraction — strips nav/footer/sidebar noise via JS,
        tries semantic main-content selectors first, falls back to cleaned body.
        Works on any site without per-site rules.
        """
        page = await self._get_page()
        try:
            text = await page.evaluate("""
                () => {
                    // 1. Remove noise elements from a cloned DOM so original page is untouched
                    const clone = document.body.cloneNode(true);
                    const noiseSelectors = [
                        'nav', 'header', 'footer', 'aside',
                        '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]',
                        '[role="complementary"]',
                        '.sidebar', '.nav', '.navbar', '.menu', '.footer', '.header',
                        '.cookie-banner', '.cookie-consent', '.ad', '.ads', '.advertisement',
                        '.social-share', '.share-buttons', '.related-posts',
                        '.comments', '#comments', '.popup', '.modal',
                        'script', 'style', 'noscript', 'iframe',
                    ];
                    for (const sel of noiseSelectors) {
                        clone.querySelectorAll(sel).forEach(el => el.remove());
                    }

                    // 2. Try semantic main-content selectors
                    const mainSelectors = [
                        'main', 'article', '[role="main"]',
                        '#mw-content-text',          // Wikipedia
                        '#content', '#main-content',
                        '.post-content', '.article-content', '.article-body',
                        '.entry-content', '.page-content',
                    ];
                    for (const sel of mainSelectors) {
                        const el = clone.querySelector(sel);
                        if (el) {
                            const t = el.innerText.trim();
                            if (t.length > 200) return t;
                        }
                    }

                    // 3. Fallback: cleaned body text
                    return clone.innerText.trim();
                }
            """)
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
                img.thumbnail([IMG_MAX_W, IMG_MAX_H], PIL.Image.Resampling.BILINEAR)
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
# PUBLIC API — used by service modules (actions/soundcloud.py, etc.)
# ─────────────────────────────────────────────────────────────

def go_to(url: str) -> str:
    _ensure_started()
    return _bt.run(_bt._go_to(url), timeout=35)

def wait_for_content(timeout_ms: int = 5000) -> str:
    _ensure_started()
    return _bt.run(_bt._wait_for_content(timeout_ms), timeout=10)

def parse_html(selector: str = "", known_key: str = "",
               attribute: str = "href", limit: int = 5) -> str:
    _ensure_started()
    return _bt.run(_bt._parse_html(selector, known_key, attribute, limit), timeout=20)

def get_text(max_chars: int = 6000) -> str:
    _ensure_started()
    return _bt.run(_bt._get_text(max_chars), timeout=20)

def vision_read(question: str) -> str:
    _ensure_started()
    return _bt.run(_bt._vision_read(question), timeout=30)

def get_url() -> str:
    _ensure_started()
    return _bt.run(_bt._get_url(), timeout=10)

def click_element(selector: str = "", text: str = "",
                  description: str = "") -> str:
    _ensure_started()
    return _bt.run(_bt._click_element(selector, text, description), timeout=15)


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
        go_to            : Navigate to URL
        construct_url    : Build service URL (google, youtube, soundcloud, booking,
                           google_flights, google_maps, gmail, google_classroom,
                           classroom_todo, whatsapp, wikipedia, amazon, weather, ...)
        fetch_html       : Get raw HTML source
        parse_html       : Query the LIVE rendered DOM via JavaScript — works on
                           any site including SPAs. Falls back to BeautifulSoup.
                           selector, known_key, attribute (href/text/src), limit
        wait_for_content : Wait for JS to finish loading (call before parse_html
                           on Google Classroom, SoundCloud, YouTube, etc.)
        get_text         : All visible page text (Tier 3)
        vision_read      : Screenshot + Gemini question (Tier 4) — costs API call
                           question: specific answerable question
        click            : Click by selector, text, or description
        type             : Type into field (selector, text, clear_first)
        scroll           : Scroll direction up|down, amount (pixels)
        press            : Press key (Enter, Escape, Tab, etc.)
        get_url          : Get current page URL
        back             : Navigate back
        reload           : Reload page
        new_tab          : Open new tab (optional url)
        close_tab        : Close current tab
        close            : Disconnect from browser
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

        elif action == "wait_for_content":
            result = _bt.run(_bt._wait_for_content(
                timeout_ms=int(parameters.get("timeout_ms", 5000))
            ), timeout=10)

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
