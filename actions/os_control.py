# actions/os_control.py
# JARVIS — OS Control Primitive
#
# Direct OS-level controls using native system APIs.
# Never uses GUI navigation (Action Center clicks) — uses APIs instead.
#
# Volume    : pycaw Core Audio API (with AttributeError fallback for newer pycaw)
# Brightness: WMI hardware interface
# Dark mode : Windows Registry write
# Wi-Fi     : netsh adapter command (direct system API, not GUI)
# Display   : WinAPI SendMessage
# Lock/Shutdown/Restart: subprocess native commands (user's original implementations)
# Window mgmt: pyautogui hotkeys (acceptable — system-wide, focus-independent)
# Intent detection: new google.genai SDK

import json
import math
import platform
import re
import subprocess
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

_OS = platform.system()


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
# VOLUME — pycaw Core Audio API (Windows)
# ─────────────────────────────────────────────────────────────

def _get_pycaw_volume_interface():
    """
    Returns the IAudioEndpointVolume COM interface.
    Handles both old pycaw (.Activate() needed) and new pycaw (direct cast).
    """
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    devices = AudioUtilities.GetSpeakers()
    try:
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(interface, POINTER(IAudioEndpointVolume))
    except AttributeError:
        # Newer pycaw returns the interface directly from GetSpeakers()
        vol = cast(devices, POINTER(IAudioEndpointVolume))
    return vol


def volume_set(value: int) -> str:
    value = max(0, min(100, value))
    if _OS == "Windows":
        try:
            vol    = _get_pycaw_volume_interface()
            vol_db = -65.25 if value == 0 else max(-65.25, 20 * math.log10(value / 100))
            vol.SetMasterVolumeLevel(vol_db, None)
            print(f"[OS] 🔊 Volume → {value}%")
            return f"Volume set to {value}%, sir."
        except Exception as e:
            print(f"[OS] ⚠️ pycaw failed ({e}), falling back to key presses")
            _volume_keypress_set(value)
            return f"Volume set to approximately {value}%, sir."
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e", f"set volume output volume {value}"])
        return f"Volume set to {value}%."
    else:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"])
        return f"Volume set to {value}%."


def _volume_keypress_set(target: int):
    """Fallback: use volume keys to approximate a target level."""
    if not _PYAUTOGUI:
        return
    # Mute then bring up
    for _ in range(50):
        pyautogui.press("volumedown")
    steps = int(target / 2)
    for _ in range(steps):
        pyautogui.press("volumeup")


def volume_up() -> str:
    if _OS == "Windows":
        try:
            vol     = _get_pycaw_volume_interface()
            current = vol.GetMasterVolumeLevelScalar()
            new_val = min(1.0, current + 0.1)
            vol.SetMasterVolumeLevelScalar(new_val, None)
            return f"Volume increased to {int(new_val * 100)}%."
        except Exception:
            if _PYAUTOGUI:
                for _ in range(5):
                    pyautogui.press("volumeup")
            return "Volume increased."
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            "set volume output volume (output volume of (get volume settings) + 10)"])
        return "Volume increased."
    else:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"])
        return "Volume increased."


def volume_down() -> str:
    if _OS == "Windows":
        try:
            vol     = _get_pycaw_volume_interface()
            current = vol.GetMasterVolumeLevelScalar()
            new_val = max(0.0, current - 0.1)
            vol.SetMasterVolumeLevelScalar(new_val, None)
            return f"Volume decreased to {int(new_val * 100)}%."
        except Exception:
            if _PYAUTOGUI:
                for _ in range(5):
                    pyautogui.press("volumedown")
            return "Volume decreased."
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            "set volume output volume (output volume of (get volume settings) - 10)"])
        return "Volume decreased."
    else:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"])
        return "Volume decreased."


def volume_mute() -> str:
    if _OS == "Windows":
        try:
            vol     = _get_pycaw_volume_interface()
            current = vol.GetMute()
            vol.SetMute(not current, None)
            state = "muted" if not current else "unmuted"
            return f"Volume {state}."
        except Exception:
            if _PYAUTOGUI:
                pyautogui.press("volumemute")
            return "Volume toggled."
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e", "set volume with output muted"])
        return "Volume muted."
    else:
        subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
        return "Volume toggled."


# ─────────────────────────────────────────────────────────────
# BRIGHTNESS — WMI hardware interface (Windows)
# ─────────────────────────────────────────────────────────────

def brightness_set(value: int) -> str:
    value = max(0, min(100, value))
    if _OS == "Windows":
        try:
            import wmi
            c       = wmi.WMI(namespace="wmi")
            methods = c.WmiMonitorBrightnessMethods()[0]
            methods.WmiSetBrightness(value, 0)
            return f"Brightness set to {value}%."
        except Exception as e:
            print(f"[OS] ⚠️ WMI brightness failed: {e}")
            return f"Could not set brightness via WMI: {e}"
    elif _OS == "Darwin":
        # macOS: use brightness CLI tool if available
        try:
            subprocess.run(["brightness", str(value / 100)], check=True)
        except Exception:
            subprocess.run(["osascript", "-e",
                f'tell application "System Events" to key code {144 if value > 50 else 145}'])
        return f"Brightness adjusted."
    else:
        subprocess.run(["brightnessctl", "set", f"{value}%"])
        return f"Brightness set to {value}%."


def brightness_up() -> str:
    if _OS == "Windows":
        try:
            import wmi
            c       = wmi.WMI(namespace="wmi")
            mon     = c.WmiMonitorBrightness()[0]
            current = mon.CurrentBrightness
            return brightness_set(min(100, current + 10))
        except Exception:
            return brightness_set(70)
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to key code 144'])
        return "Brightness increased."
    else:
        subprocess.run(["brightnessctl", "set", "+10%"])
        return "Brightness increased."


def brightness_down() -> str:
    if _OS == "Windows":
        try:
            import wmi
            c       = wmi.WMI(namespace="wmi")
            mon     = c.WmiMonitorBrightness()[0]
            current = mon.CurrentBrightness
            return brightness_set(max(0, current - 10))
        except Exception:
            return brightness_set(40)
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to key code 145'])
        return "Brightness decreased."
    else:
        subprocess.run(["brightnessctl", "set", "10%-"])
        return "Brightness decreased."


# ─────────────────────────────────────────────────────────────
# DARK MODE — Windows Registry write (not Action Center)
# ─────────────────────────────────────────────────────────────

def toggle_dark_mode() -> str:
    if _OS == "Windows":
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            key      = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path,
                                      0, winreg.KEY_READ | winreg.KEY_WRITE)
            current, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            new_val    = 1 - current  # toggle: 0=dark, 1=light
            winreg.SetValueEx(key, "AppsUseLightTheme",  0, winreg.REG_DWORD, new_val)
            winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, new_val)
            winreg.CloseKey(key)
            mode = "light" if new_val else "dark"
            # Broadcast the settings change
            import ctypes
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "ImmersiveColorSet", 0, 1000, None
            )
            return f"Switched to {mode} mode."
        except Exception as e:
            return f"Could not toggle dark mode: {e}"
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell app "System Events" to tell appearance preferences'
            ' to set dark mode to not dark mode'])
        return "Dark mode toggled."
    else:
        return "Dark mode toggle not implemented for this OS."


def set_dark_mode(enable: bool = True) -> str:
    if _OS == "Windows":
        try:
            import winreg, ctypes
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            key      = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path,
                                      0, winreg.KEY_READ | winreg.KEY_WRITE)
            val      = 0 if enable else 1  # 0=dark, 1=light
            winreg.SetValueEx(key, "AppsUseLightTheme",    0, winreg.REG_DWORD, val)
            winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, val)
            winreg.CloseKey(key)
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "ImmersiveColorSet", 0, 1000, None
            )
            return f"{'Dark' if enable else 'Light'} mode enabled."
        except Exception as e:
            return f"Could not set dark mode: {e}"
    return toggle_dark_mode()


# ─────────────────────────────────────────────────────────────
# WI-FI — netsh adapter command (direct system, not Action Center)
# ─────────────────────────────────────────────────────────────

def _get_wifi_adapter_name() -> str:
    """Returns the name of the first wireless adapter found."""
    if _OS != "Windows":
        return "Wi-Fi"
    try:
        result = subprocess.run(
            ["netsh", "interface", "show", "interface"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            lower = line.lower()
            if "wi-fi" in lower or "wireless" in lower or "wlan" in lower:
                parts = line.split()
                if len(parts) >= 4:
                    return " ".join(parts[3:]).strip()
    except Exception:
        pass
    return "Wi-Fi"


def _get_wifi_enabled() -> bool:
    if _OS != "Windows":
        return True
    try:
        adapter = _get_wifi_adapter_name()
        result  = subprocess.run(
            ["netsh", "interface", "show", "interface", adapter],
            capture_output=True, text=True, timeout=5
        )
        return "enabled" in result.stdout.lower() or "connected" in result.stdout.lower()
    except Exception:
        return True


def toggle_wifi() -> str:
    if _OS == "Windows":
        try:
            adapter = _get_wifi_adapter_name()
            enabled = _get_wifi_enabled()
            action  = "disable" if enabled else "enable"
            result  = subprocess.run(
                ["netsh", "interface", "set", "interface", adapter, action],
                capture_output=True, text=True, timeout=8
            )
            if result.returncode == 0:
                return f"Wi-Fi {'disabled' if enabled else 'enabled'}."
            return f"Could not toggle Wi-Fi: {result.stderr.strip()}"
        except Exception as e:
            return f"Wi-Fi toggle failed: {e}"
    elif _OS == "Darwin":
        subprocess.run(["networksetup", "-setairportpower", "en0", "toggle"])
        return "Wi-Fi toggled."
    else:
        result = subprocess.run(["nmcli", "radio", "wifi"], capture_output=True, text=True)
        state  = "off" if "enabled" in result.stdout.lower() else "on"
        subprocess.run(["nmcli", "radio", "wifi", state])
        return f"Wi-Fi turned {state}."


def enable_wifi() -> str:
    if _OS == "Windows":
        adapter = _get_wifi_adapter_name()
        subprocess.run(["netsh", "interface", "set", "interface", adapter, "enable"])
        return "Wi-Fi enabled."
    return toggle_wifi()


def disable_wifi() -> str:
    if _OS == "Windows":
        adapter = _get_wifi_adapter_name()
        subprocess.run(["netsh", "interface", "set", "interface", adapter, "disable"])
        return "Wi-Fi disabled."
    return toggle_wifi()


# ─────────────────────────────────────────────────────────────
# DISPLAY — WinAPI direct message (not keyboard shortcut)
# ─────────────────────────────────────────────────────────────

def sleep_display() -> str:
    if _OS == "Windows":
        try:
            import ctypes
            ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
            return "Display sleeping."
        except Exception as e:
            return f"Could not sleep display: {e}"
    elif _OS == "Darwin":
        subprocess.run(["pmset", "displaysleepnow"])
        return "Display sleeping."
    else:
        subprocess.run(["xset", "dpms", "force", "off"])
        return "Display sleeping."


# ─────────────────────────────────────────────────────────────
# LOCK / SHUTDOWN / RESTART — User's original implementations (kept exactly)
# ─────────────────────────────────────────────────────────────

def lock_screen() -> str:
    if _OS == "Windows":
        subprocess.run(
            ["rundll32.exe", "user32.dll,LockWorkStation"],
            creationflags=0x08000000
        )
        return "Screen locked."
    elif _OS == "Darwin":
        subprocess.run(["pmset", "displaysleepnow"])
        return "Screen locked."
    else:
        subprocess.run(["gnome-screensaver-command", "-l"])
        return "Screen locked."


def restart_computer() -> str:
    if _OS == "Windows":
        subprocess.run(["shutdown", "/r", "/t", "5"],
                       creationflags=0x08000000)
        return "Restarting in 5 seconds, sir."
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e", 'tell app "System Events" to restart'])
        return "Restarting."
    else:
        subprocess.run(["reboot"])
        return "Restarting."


def shutdown_computer() -> str:
    if _OS == "Windows":
        subprocess.run(["shutdown", "/s", "/t", "5"],
                       creationflags=0x08000000)
        return "Shutting down in 5 seconds, sir."
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e", 'tell app "System Events" to shut down'])
        return "Shutting down."
    else:
        subprocess.run(["poweroff"])
        return "Shutting down."


# ─────────────────────────────────────────────────────────────
# WINDOW MANAGEMENT — pyautogui hotkeys (acceptable: system-wide, focus-independent)
# ─────────────────────────────────────────────────────────────

def _ensure_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")


def minimize_window() -> str:
    _ensure_pyautogui()
    pyautogui.hotkey("win", "down") if _OS == "Windows" else pyautogui.hotkey("command", "m")
    return "Window minimized."


def maximize_window() -> str:
    _ensure_pyautogui()
    if _OS == "Windows":
        pyautogui.hotkey("win", "up")
        return "Window maximized."
    return "Window maximize not supported on this platform."


def snap_left() -> str:
    _ensure_pyautogui()
    if _OS == "Windows":
        pyautogui.hotkey("win", "left")
        return "Window snapped left."
    return "Window snap not supported on this platform."


def snap_right() -> str:
    _ensure_pyautogui()
    if _OS == "Windows":
        pyautogui.hotkey("win", "right")
        return "Window snapped right."
    return "Window snap not supported on this platform."


def switch_window() -> str:
    _ensure_pyautogui()
    pyautogui.hotkey("alt", "tab") if _OS != "Darwin" else pyautogui.hotkey("command", "tab")
    return "Window switched."


def show_desktop() -> str:
    _ensure_pyautogui()
    if _OS == "Windows":
        pyautogui.hotkey("win", "d")
    elif _OS == "Darwin":
        pyautogui.hotkey("fn", "f11")
    else:
        pyautogui.hotkey("super", "d")
    return "Desktop shown."


def full_screen() -> str:
    _ensure_pyautogui()
    if _OS == "Darwin":
        pyautogui.hotkey("ctrl", "command", "f")
    else:
        pyautogui.press("f11")
    return "Toggled fullscreen."


def open_file_explorer() -> str:
    if _OS == "Windows":
        subprocess.Popen(["explorer.exe"])
    elif _OS == "Darwin":
        subprocess.Popen(["open", str(Path.home())])
    else:
        subprocess.Popen(["xdg-open", str(Path.home())])
    return "File explorer opened."


def take_screenshot() -> str:
    _ensure_pyautogui()
    if _OS == "Windows":
        pyautogui.hotkey("win", "shift", "s")
    elif _OS == "Darwin":
        pyautogui.hotkey("command", "shift", "3")
    else:
        pyautogui.hotkey("ctrl", "print_screen")
    return "Screenshot taken."


def open_task_manager() -> str:
    if _OS == "Windows":
        pyautogui.hotkey("ctrl", "shift", "esc")
    elif _OS == "Darwin":
        subprocess.Popen(["open", "-a", "Activity Monitor"])
    else:
        subprocess.Popen(["gnome-system-monitor"])
    return "Task manager opened."


# ─────────────────────────────────────────────────────────────
# ACTION MAP
# ─────────────────────────────────────────────────────────────

ACTION_MAP = {
    "volume_up":            volume_up,
    "volume_down":          volume_down,
    "volume_increase":      volume_up,
    "volume_decrease":      volume_down,
    "increase_volume":      volume_up,
    "decrease_volume":      volume_down,
    "louder":               volume_up,
    "quieter":              volume_down,
    "mute":                 volume_mute,
    "unmute":               volume_mute,
    "toggle_mute":          volume_mute,
    "silence":              volume_mute,
    "brightness_up":        brightness_up,
    "brightness_down":      brightness_down,
    "increase_brightness":  brightness_up,
    "decrease_brightness":  brightness_down,
    "brighter":             brightness_up,
    "dimmer":               brightness_down,
    "dark_mode":            toggle_dark_mode,
    "toggle_dark_mode":     toggle_dark_mode,
    "night_mode":           lambda: set_dark_mode(True),
    "light_mode":           lambda: set_dark_mode(False),
    "toggle_wifi":          toggle_wifi,
    "wifi_on":              enable_wifi,
    "wifi_off":             disable_wifi,
    "wifi":                 toggle_wifi,
    "sleep_display":        sleep_display,
    "turn_off_screen":      sleep_display,
    "screen_off":           sleep_display,
    "display_off":          sleep_display,
    "monitor_off":          sleep_display,
    "lock_screen":          lock_screen,
    "lock":                 lock_screen,
    "restart":              restart_computer,
    "restart_computer":     restart_computer,
    "reboot":               restart_computer,
    "shutdown":             shutdown_computer,
    "shut_down":            shutdown_computer,
    "power_off":            shutdown_computer,
    "turn_off_computer":    shutdown_computer,
    "minimize":             minimize_window,
    "minimize_window":      minimize_window,
    "maximize":             maximize_window,
    "maximize_window":      maximize_window,
    "full_screen":          full_screen,
    "fullscreen":           full_screen,
    "toggle_fullscreen":    full_screen,
    "snap_left":            snap_left,
    "snap_right":           snap_right,
    "switch_window":        switch_window,
    "alt_tab":              switch_window,
    "show_desktop":         show_desktop,
    "desktop":              show_desktop,
    "screenshot":           take_screenshot,
    "take_screenshot":      take_screenshot,
    "task_manager":         open_task_manager,
    "file_explorer":        open_file_explorer,
    "open_explorer":        open_file_explorer,
}


# ─────────────────────────────────────────────────────────────
# INTENT DETECTION — new google.genai SDK
# ─────────────────────────────────────────────────────────────

def _detect_action(description: str) -> dict:
    """
    Detects OS control intent from natural language using new google.genai SDK.
    Returns {"action": str, "value": optional}.
    """
    try:
        from google import genai
        client = genai.Client(api_key=_get_api_key())

        available = ", ".join(sorted(ACTION_MAP.keys()))
        available += ", volume_set, brightness_set"

        prompt = (
            f"Detect the computer control intent from this request (any language):\n"
            f'"{description}"\n\n'
            f"Available actions: {available}\n\n"
            f"Return ONLY valid JSON:\n"
            f'{{\"action\": \"action_name\", \"value\": null_or_number}}\n\n'
            f"Examples:\n"
            f'  "set volume to 60" → {{"action": "volume_set", "value": 60}}\n'
            f'  "sesi aç" → {{"action": "volume_up", "value": null}}\n'
            f'  "parlaklığı 70 yap" → {{"action": "brightness_set", "value": 70}}\n'
            f'  "lock screen" → {{"action": "lock_screen", "value": null}}\n'
            f'  "ekranı kapat" → {{"action": "sleep_display", "value": null}}\n'
            f'  "dark mode" → {{"action": "dark_mode", "value": null}}\n\n'
            f"Return ONLY the JSON:"
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt
        )
        text = response.text.strip()
        text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
        return json.loads(text)

    except Exception as e:
        print(f"[OS] ⚠️ Intent detection failed: {e}")
        return {"action": description.lower().replace(" ", "_"), "value": None}


# ─────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────

def os_control(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    OS Control primitive.

    parameters:
        action      : volume_set | volume_up | volume_down | mute |
                      brightness_set | brightness_up | brightness_down |
                      dark_mode | toggle_wifi | wifi_on | wifi_off |
                      sleep_display | lock_screen | restart | shutdown |
                      minimize | maximize | full_screen | snap_left | snap_right |
                      switch_window | show_desktop | screenshot | take_screenshot |
                      task_manager | file_explorer
        description : natural language if action not specified (any language)
        value       : numeric value for volume_set / brightness_set
    """
    params      = parameters or {}
    raw_action  = params.get("action", "").strip()
    description = params.get("description", "").strip()
    value       = params.get("value", None)

    # If no action but description given, detect intent with Gemini
    if not raw_action and description:
        detected   = _detect_action(description)
        raw_action = detected.get("action", "")
        if value is None:
            value = detected.get("value")

    action = raw_action.lower().strip().replace(" ", "_").replace("-", "_")

    if not action:
        return "No action could be determined, sir."

    print(f"[OS] ⚙️ Action: {action}  Value: {value}")
    if player:
        player.write_log(f"[os_control] {action}")

    try:
        # Parametric actions
        if action == "volume_set":
            return volume_set(int(value or 50))

        if action == "brightness_set":
            return brightness_set(int(value or 50))

        if action in ("type_text", "write", "type"):
            text = str(value or params.get("text", ""))
            if not text:
                return "No text specified, sir."
            if _PYPERCLIP:
                import pyperclip as pc
                pc.copy(text)
                pyautogui.hotkey("ctrl", "v")
            else:
                pyautogui.write(text, interval=0.03)
            return f"Typed: {text[:60]}"

        if action == "press_key":
            key = str(value or params.get("key", ""))
            if key and _PYAUTOGUI:
                pyautogui.press(key)
                return f"Pressed: {key}"
            return "No key specified."

        # Map-based actions
        func = ACTION_MAP.get(action)
        if func:
            return func() or f"Done: {action}."

        return f"Unknown os_control action: '{raw_action}', sir."

    except Exception as e:
        print(f"[OS] ❌ Error: {e}")
        return f"OS control failed ({action}): {e}"
