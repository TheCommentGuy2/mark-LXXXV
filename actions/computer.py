# actions/computer.py
# JARVIS — Computer Control Primitive
#
# Raw input control: type, click, hotkeys, scroll, drag, wait.
# Also includes an AI-powered screen click that uses Gemini Vision to find
# an element by description and click it. This is a FALLBACK — use HTML
# parsing via browser whenever possible as it is exact and free.
#
# All vision calls use the new google.genai SDK. Screenshots sent as JPEG.

import io
import json
import re
import sys
import time
from pathlib import Path

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE    = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

JPEG_Q = 65


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _ensure_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")


def _screenshot_jpeg() -> bytes:
    """Takes a screenshot and returns JPEG bytes at reduced quality."""
    _ensure_pyautogui()
    img = pyautogui.screenshot()
    if _PIL:
        w, h = img.size
        # Scale down if very large to reduce API payload
        if w > 1280:
            scale  = 1280 / w
            img    = img.resize((1280, int(h * scale)), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_Q)
        return buf.getvalue()
    # Fallback: save as PNG if PIL unavailable
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _find_element_on_screen(description: str) -> tuple[int, int] | None:
    """
    Takes a screenshot and asks Gemini Vision to find the coordinates of
    a described element. Returns (x, y) or None.

    This is a FALLBACK. Prefer HTML parsing (browser fetch_html + parse_html)
    when looking for things to click — it is exact, free, and never misses.
    Use this only when HTML parsing cannot find the element.
    """
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=_get_api_key())

        _ensure_pyautogui()
        screen_w, screen_h = pyautogui.size()
        image_bytes = _screenshot_jpeg()

        # Calculate the actual image dimensions after resize in _screenshot_jpeg
        if _PIL and screen_w > 1280:
            scale_factor = 1280 / screen_w
            img_w = 1280
            img_h = int(screen_h * scale_factor)
        else:
            scale_factor = 1.0
            img_w = screen_w
            img_h = screen_h

        image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        text_part  = types.Part.from_text(
            text=(
                f"This is a screenshot of size {img_w}x{img_h} pixels. "
                f"Find the element: '{description}'. "
                f"Return ONLY: x,y — the pixel coordinates of the element's center "
                f"relative to the image dimensions ({img_w}x{img_h}). "
                f"If not found, return exactly: NOT_FOUND"
            )
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=types.Content(role="user", parts=[image_part, text_part])
        )

        text = response.text.strip()
        if "NOT_FOUND" in text.upper():
            return None

        match = re.search(r"(\d+)\s*,\s*(\d+)", text)
        if match:
            ix, iy = int(match.group(1)), int(match.group(2))
            # Scale coordinates back to actual screen resolution
            if scale_factor < 1.0:
                ix = int(ix / scale_factor)
                iy = int(iy / scale_factor)
            return ix, iy

    except Exception as e:
        print(f"[Computer] ⚠️ Screen analysis failed: {e}")

    return None


# ─────────────────────────────────────────────────────────────
# CORE ACTIONS
# ─────────────────────────────────────────────────────────────

def _type_text(text: str, interval: float = 0.03) -> str:
    _ensure_pyautogui()
    time.sleep(0.2)
    # Always prefer clipboard paste — handles Unicode, special chars, any length
    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
    else:
        # Last resort: ASCII-only typewrite (no Unicode support)
        pyautogui.typewrite(text, interval=interval)
    return f"Typed: {text[:60]}{'...' if len(text) > 60 else ''}"


def _click(x: int = None, y: int = None, button: str = "left",
           clicks: int = 1) -> str:
    _ensure_pyautogui()
    if x is not None and y is not None:
        pyautogui.click(x, y, button=button, clicks=clicks)
        return f"Clicked ({x}, {y})."
    pyautogui.click(button=button, clicks=clicks)
    return "Clicked at current position."


def _hotkey(*keys) -> str:
    _ensure_pyautogui()
    pyautogui.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def _press(key: str) -> str:
    _ensure_pyautogui()
    pyautogui.press(key)
    return f"Pressed: {key}"


def _scroll(direction: str = "down", amount: int = 3) -> str:
    _ensure_pyautogui()
    clicks = amount if direction in ("up", "right") else -amount
    if direction in ("up", "down"):
        pyautogui.scroll(clicks)
    else:
        pyautogui.hscroll(clicks)
    return f"Scrolled {direction}."


def _move_mouse(x: int, y: int, duration: float = 0.3) -> str:
    _ensure_pyautogui()
    pyautogui.moveTo(x, y, duration=duration)
    return f"Mouse moved to ({x}, {y})."


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    _ensure_pyautogui()
    pyautogui.moveTo(x1, y1)
    pyautogui.dragTo(x2, y2, duration=duration, button="left")
    return f"Dragged from ({x1},{y1}) to ({x2},{y2})."


def _get_clipboard() -> str:
    if _PYPERCLIP:
        return pyperclip.paste()
    _ensure_pyautogui()
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.2)
    return "Copied to clipboard (content unavailable)."


def _set_clipboard(text: str) -> str:
    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        return f"Pasted from clipboard."
    return "pyperclip not available."


def _screenshot(save_path: str = None) -> str:
    _ensure_pyautogui()
    if not save_path:
        save_path = str(Path.home() / "Desktop" / "jarvis_screenshot.png")
    img = pyautogui.screenshot()
    img.save(save_path)
    return f"Screenshot saved: {save_path}"


def _wait(seconds: float) -> str:
    time.sleep(max(0, seconds))
    return f"Waited {seconds}s."


def _clear_field() -> str:
    _ensure_pyautogui()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Field cleared."


def _focus_window(title: str) -> str:
    import platform, subprocess
    if platform.system() == "Windows":
        try:
            script = f'(New-Object -ComObject WScript.Shell).AppActivate("{title}")'
            subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, timeout=5)
            time.sleep(0.3)
            return f"Focused: {title}"
        except Exception as e:
            return f"Could not focus window: {e}"
    return f"Window focus only supported on Windows."


# ─────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────

def computer(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Computer control primitive — raw input control.

    actions:
        type         : type text at current cursor position
        click        : click at x,y coordinates
        double_click : double click at x,y
        right_click  : right click at x,y
        hotkey       : key combination e.g. keys="ctrl+c"
        press        : single key press e.g. key="enter"
        scroll       : scroll direction up|down|left|right, amount (default 3)
        move         : move mouse to x,y
        drag         : drag from x1,y1 to x2,y2
        copy         : get clipboard content
        paste        : set clipboard and paste (requires text)
        screenshot   : save screenshot to path
        wait         : wait for seconds
        clear_field  : select all + delete current field
        focus_window : bring window to foreground by title
        screen_find  : AI-powered element finder — returns x,y coordinates
                       (FALLBACK: use browser parse_html when possible)
        screen_click : AI-powered element finder + click
                       (FALLBACK: use browser parse_html when possible)

    Note: screen_find and screen_click use Gemini Vision and cost one API call each.
    Prefer browser's parse_html for finding navigatable elements — it is exact and free.
    """
    action = (parameters or {}).get("action", "").lower().strip()

    if not action:
        return "Please specify an action for computer, sir."

    print(f"[Computer] ▶️ {action}  params={parameters}")
    if player:
        player.write_log(f"[computer] {action}")

    try:
        if action == "type":
            return _type_text(
                text=parameters.get("text", ""),
                interval=float(parameters.get("interval", 0.03))
            )

        elif action in ("click", "left_click"):
            x = parameters.get("x")
            y = parameters.get("y")
            return _click(
                x=int(x) if x is not None else None,
                y=int(y) if y is not None else None,
                button="left", clicks=1
            )

        elif action == "double_click":
            x = parameters.get("x")
            y = parameters.get("y")
            return _click(
                x=int(x) if x is not None else None,
                y=int(y) if y is not None else None,
                button="left", clicks=2
            )

        elif action == "right_click":
            x = parameters.get("x")
            y = parameters.get("y")
            return _click(
                x=int(x) if x is not None else None,
                y=int(y) if y is not None else None,
                button="right", clicks=1
            )

        elif action == "hotkey":
            keys = parameters.get("keys", "")
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.split("+")]
            return _hotkey(*keys)

        elif action == "press":
            return _press(parameters.get("key", "enter"))

        elif action == "scroll":
            return _scroll(
                direction=parameters.get("direction", "down"),
                amount=int(parameters.get("amount", 3))
            )

        elif action == "move":
            return _move_mouse(
                x=int(parameters.get("x", 0)),
                y=int(parameters.get("y", 0)),
                duration=float(parameters.get("duration", 0.3))
            )

        elif action == "drag":
            return _drag(
                x1=int(parameters.get("x1", 0)),
                y1=int(parameters.get("y1", 0)),
                x2=int(parameters.get("x2", 0)),
                y2=int(parameters.get("y2", 0))
            )

        elif action == "copy":
            return _get_clipboard()

        elif action == "paste":
            return _set_clipboard(parameters.get("text", ""))

        elif action == "screenshot":
            return _screenshot(parameters.get("path"))

        elif action == "wait":
            return _wait(float(parameters.get("seconds", 1.0)))

        elif action == "clear_field":
            return _clear_field()

        elif action == "focus_window":
            return _focus_window(parameters.get("title", ""))

        elif action == "screen_find":
            description = parameters.get("description", "")
            if not description:
                return "Please provide a description of what to find."
            coords = _find_element_on_screen(description)
            if coords:
                return f"{coords[0]},{coords[1]}"
            return "NOT_FOUND"

        elif action == "screen_click":
            description = parameters.get("description", "")
            if not description:
                return "Please provide a description of what to click."
            print(f"[Computer] 🤖 AI-powered click (fallback): {description!r}")
            coords = _find_element_on_screen(description)
            if coords:
                time.sleep(0.2)
                _click(x=coords[0], y=coords[1])
                return f"Found and clicked: {description} at {coords}."
            return f"Could not find on screen: {description}"

        else:
            return f"Unknown computer action: '{action}'"

    except Exception as e:
        print(f"[Computer] ❌ {e}")
        return f"Computer action failed ({action}): {e}"
