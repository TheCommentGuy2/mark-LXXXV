# actions/terminal.py
# JARVIS — Terminal Primitive
#
# Runs shell commands — silently or in a visible terminal window.
# For media tasks (yt-dlp, ffmpeg), always uses purpose-built CLI tools.
# For unknown tasks, asks Gemini to generate the command, then runs it after safety check.
# Handles rate limiting (429) with retry.

import os
import re
import subprocess
import sys
import time
from pathlib import Path
import json
import platform

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
# PATH HELPERS
# ─────────────────────────────────────────────────────────────

def _get_real_desktop() -> Path:
    """
    Returns the actual Desktop path, correctly handling OneDrive folder
    redirection on Windows (where %USERPROFILE%\\Desktop may not be the
    real desktop if OneDrive has moved it to OneDrive\\Desktop).
    Falls back to the standard path if the registry read fails.
    """
    if _OS == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            )
            desktop, _ = winreg.QueryValueEx(key, "Desktop")
            winreg.CloseKey(key)
            p = Path(desktop)
            if p.exists():
                return p
        except Exception:
            pass
    return Path.home() / "Desktop"


def _expand_path(path: str) -> str:
    """
    Fully expands a path string:
      - %USERPROFILE%, %APPDATA%, %TEMP%, etc. (Windows env vars)
      - ~ (home dir)
      - Normalises backslashes
    Returns the expanded absolute path as a string.
    """
    # Expand Windows environment variables first
    expanded = os.path.expandvars(path)
    # Then expand ~ / ~user
    expanded = str(Path(expanded).expanduser())
    return expanded


def _make_dest(destination: str, task: str) -> str:
    """
    Converts any destination the user or planner provides into a valid
    yt-dlp -o output template.

    Cases handled:
      1. Empty / not provided          → real Desktop / %(title)s.%(ext)s
      2. Folder path (no template)     → folder / %(title)s.%(ext)s
      3. Already has %(title)s         → use as-is (just expand vars)
      4. Ends in a filename with ext   → use as-is (explicit output name)
    """
    if not destination or not destination.strip():
        return str(_get_real_desktop() / "%(title)s.%(ext)s")

    dest = _expand_path(destination.strip())

    # Already a full yt-dlp template
    if "%(title)s" in dest or "%(ext)s" in dest:
        return dest

    # Ends with a known media extension → treat as explicit output filename
    known_exts = {".mp3", ".mp4", ".m4a", ".flac", ".wav", ".ogg",
                  ".mkv", ".avi", ".mov", ".webm"}
    if Path(dest).suffix.lower() in known_exts:
        return dest

    # Otherwise it's a folder — append yt-dlp output template
    return str(Path(dest) / "%(title)s.%(ext)s")


# ─────────────────────────────────────────────────────────────
# HARDCODED COMMAND PATTERNS (fastest path — no API call)
# ─────────────────────────────────────────────────────────────

_MEDIA_PATTERNS = [
    (r"download.*youtube|youtube.*download|yt.?dlp|ytdlp", "yt-dlp"),
    (r"download.*mp3|mp3.*download|audio.*youtube|youtube.*audio", "yt-dlp-audio"),
    (r"download.*video|video.*download", "yt-dlp-video"),
    (r"convert.*\.(mp4|mkv|avi|mov|flv|webm|m4v)", "ffmpeg"),
    (r"convert.*\.(mp3|wav|flac|aac|ogg|m4a|wma)", "ffmpeg"),
    (r"ffmpeg|transcode|re.?encode", "ffmpeg"),
]

_WIN_INFO_MAP = [
    (["disk space", "disk usage", "storage", "free space", "c drive"],
     "wmic logicaldisk get caption,freespace,size /format:list"),
    (["running processes", "list processes", "active processes", "tasklist"],
     "tasklist /fo table"),
    (["ip address", "my ip", "network info", "ipconfig"],
     "ipconfig /all"),
    (["system info", "computer info", "hardware info", "pc info", "specs"],
     "systeminfo"),
    (["cpu usage", "processor usage"],
     "wmic cpu get loadpercentage"),
    (["memory usage", "ram usage"],
     "wmic OS get FreePhysicalMemory,TotalVisibleMemorySize /Value"),
    (["battery", "battery level", "power status"],
     'powershell -Command "(Get-WmiObject -Class Win32_Battery).EstimatedChargeRemaining"'),
    (["wifi networks", "available wifi", "wireless networks"],
     "netsh wlan show networks"),
    (["open ports", "listening ports", "netstat"],
     "netstat -an | findstr LISTENING"),
]

BLOCKED_PATTERNS = [
    r"\brm\s+-rf\b", r"\brmdir\s+/s\b", r"\bdel\s+/[fqs]",
    r"\bformat\b", r"\bdiskpart\b",
    r"\breg\s+(delete|add)\b", r"\bbcdedit\b",
    r"\bnet\s+localgroup\b",
    r"\beval\b", r"\b__import__\b",
]
_BLOCKED_RE = re.compile("|".join(BLOCKED_PATTERNS), re.IGNORECASE)


def _is_safe(command: str) -> tuple[bool, str]:
    match = _BLOCKED_RE.search(command)
    if match:
        return False, f"Blocked pattern: '{match.group()}'"
    return True, "OK"


def _check_tool_installed(tool: str) -> bool:
    import shutil
    return shutil.which(tool) is not None


def _ensure_yt_dlp() -> str | None:
    if _check_tool_installed("yt-dlp"):
        return "yt-dlp"
    print("[Terminal] 📦 yt-dlp not found, installing...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "yt-dlp"],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode == 0 and _check_tool_installed("yt-dlp"):
        return "yt-dlp"
    return None


def _ensure_ffmpeg() -> str | None:
    if _check_tool_installed("ffmpeg"):
        return "ffmpeg"
    return None


def _build_yt_dlp_command(task: str, url: str = "", params: dict = None) -> str:
    """
    Builds the yt-dlp command.

    BUG FIXES vs old version:
      1. _make_dest() properly expands all path formats and appends the
         %(title)s.%(ext)s template when only a folder is given — so
         custom paths like Filmora's project folder now work correctly.
      2. Uses _get_real_desktop() instead of Path.home()/"Desktop" so
         OneDrive-redirected Desktops are found correctly.
      3. URL is quoted separately so paths with spaces don't break quoting.
    """
    params   = params or {}
    dest     = _make_dest(params.get("destination", ""), task)
    task_low = task.lower()
    is_audio = any(w in task_low for w in
                   ["mp3", "audio", "music", "flac", "m4a", "wav", "sound"])
    url_part = f' "{url}"' if url else ""

    if is_audio:
        fmt = params.get("format", "mp3")
        return f'yt-dlp -x --audio-format {fmt} -o "{dest}"{url_part}'
    else:
        return f'yt-dlp -o "{dest}"{url_part}'


def _build_ffmpeg_command(task: str, params: dict = None) -> str | None:
    params   = params or {}
    input_f  = params.get("input_file", "")
    output_f = params.get("output_file", "")
    task_low = task.lower()

    if not input_f:
        match = re.search(r'[\"\'"]?([\S]+\.[a-zA-Z0-9]+)[\"\'"]?', task)
        if match:
            input_f = match.group(1)

    if not input_f:
        return None

    # Expand ~ and env vars in input path
    input_f = _expand_path(input_f)

    if not output_f:
        out_match = re.search(r'to\s+([a-zA-Z0-9]+)(?:\s|$)', task_low)
        if out_match:
            ext      = out_match.group(1)
            stem     = Path(input_f).stem
            parent   = Path(input_f).parent
            output_f = str(parent / f"{stem}.{ext}")

    if not output_f:
        return None

    output_f = _expand_path(output_f)

    if output_f.endswith(".flac"):
        return f'ffmpeg -i "{input_f}" -vn "{output_f}"'
    if output_f.endswith(".mp3"):
        return f'ffmpeg -i "{input_f}" -q:a 0 -map a "{output_f}"'
    return f'ffmpeg -i "{input_f}" "{output_f}"'


def _find_hardcoded(task: str, params: dict) -> str | None:
    task_lower = task.lower()

    if "notepad" in task_lower or "open" in task_lower:
        file_m = re.search(r'[\"\'"]?([\S]+\.(?:txt|log|md|csv|json|xml|py))[\"\'"]?',
                           task, re.IGNORECASE)
        if file_m:
            f = file_m.group(1)
            p = Path(f) if Path(f).is_absolute() else _get_real_desktop() / f
            return f'notepad "{p}"' if _OS == "Windows" else f'open -t "{p}"'

    pip_m = re.search(r"install\s+([\w\-]+)", task_lower)
    if pip_m:
        pkg = pip_m.group(1)
        return f"{sys.executable} -m pip install {pkg}"

    if _OS == "Windows":
        for keywords, command in _WIN_INFO_MAP:
            if any(kw in task_lower for kw in keywords):
                return command

    return None


def _ask_gemini_command(task: str) -> str:
    try:
        from google import genai
        client = genai.Client(api_key=_get_api_key())

        prompt = (
            f"Convert this request to a single {'Windows CMD' if _OS == 'Windows' else 'shell'} command.\n"
            f"Output ONLY the command. No explanation, no markdown, no backticks.\n"
            f"If unsafe or impossible, output: UNSAFE\n\n"
            f"Request: {task}\n\nCommand:"
        )

        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash-lite",
                    contents=prompt
                )
                cmd = response.text.strip().strip("`").strip()
                if cmd.startswith("```"):
                    lines = cmd.split("\n")
                    # Strip language prefix line (```cmd, ```bash, etc.) and closing ```
                    cmd = "\n".join(lines[1:]).rstrip("`").strip()
                    # Also strip if the first remaining line is just a language name
                    first_line = cmd.split("\n")[0].strip().lower()
                    if first_line in ("cmd", "bash", "sh", "powershell", "ps1", "bat", "shell"):
                        cmd = "\n".join(cmd.split("\n")[1:]).strip()
                return cmd
            except Exception as e:
                if "429" in str(e):
                    wait = _extract_retry_delay(str(e))
                    print(f"[Terminal] ⏳ Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                raise

    except Exception as e:
        return f"ERROR: {e}"
    return "ERROR: all retries failed"


def _extract_retry_delay(error_str: str) -> int:
    match = re.search(r"retry.*?(\d+)\s*second", error_str, re.IGNORECASE)
    if match:
        return min(int(match.group(1)), 60)
    return 5


def _run_silent(command: str, timeout: int = 30, cwd: str = None) -> str:
    """Runs command silently, returns output."""
    try:
        if _OS == "Windows":
            is_ps = command.strip().lower().startswith("powershell")
            if is_ps:
                inner  = re.sub(r'^powershell\s+-Command\s+"?', "", command,
                                flags=re.IGNORECASE).rstrip('"')
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", inner],
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=timeout, cwd=cwd or str(Path.home())
                )
            else:
                result = subprocess.run(
                    ["cmd", "/c", command],
                    capture_output=True, text=True,
                    encoding="cp1252", errors="replace",
                    timeout=timeout, cwd=cwd or str(Path.home())
                )
        else:
            shell  = "/bin/zsh" if _OS == "Darwin" else "/bin/bash"
            result = subprocess.run(
                command, shell=True, executable=shell,
                capture_output=True, text=True, errors="replace",
                timeout=timeout, cwd=cwd or str(Path.home())
            )

        out = result.stdout.strip()
        err = result.stderr.strip()
        if out:  return out[:3000]
        if err:  return f"[stderr]: {err[:800]}"
        return "Command completed with no output."

    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s."
    except Exception as e:
        return f"Execution error: {e}"


def _run_visible(command: str, cwd: str = None) -> str:
    """
    Opens a visible terminal window and runs the command.

    BUG FIX: Old version used f'cmd /k "{command}"' which broke whenever
    the command itself contained double quotes (e.g. yt-dlp paths with spaces).
    Now passes command as a list argument so CMD receives it correctly regardless
    of internal quoting.
    """
    try:
        if _OS == "Windows":
            # Pass as list — avoids double-quote collision when command
            # already contains quoted paths (yt-dlp, ffmpeg, etc.)
            subprocess.Popen(
                ["cmd", "/k", command],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                cwd=cwd or str(Path.home())
            )
        elif _OS == "Darwin":
            # Escape inner quotes for AppleScript string
            safe_cmd = command.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["osascript", "-e",
                f'tell application "Terminal" to do script "{safe_cmd}"'])
        else:
            for term in ["gnome-terminal", "xterm", "konsole"]:
                try:
                    subprocess.Popen([term, "--", "bash", "-c",
                                      f"{command}; exec bash"], cwd=cwd)
                    break
                except FileNotFoundError:
                    continue
        return f"Terminal opened: {command[:80]}"
    except Exception as e:
        return f"Could not open terminal: {e}"


def _verify_file_exists(path: str) -> bool:
    p = Path(_expand_path(path))
    return p.exists() and p.is_file()


# ─────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────

def terminal(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Terminal primitive — runs shell commands.

    parameters:
        task        : natural language description of what to do
        command     : exact command to run (skips AI generation)
        visible     : bool — open visible terminal window (default: True for long tasks)
        timeout     : seconds before giving up (default: 30)
        cwd         : working directory
        input_file  : for media conversion tasks
        output_file : for media conversion tasks
        url         : for download tasks
        destination : output folder or full path for downloads
                      Examples:
                        "C:\\Users\\name\\Desktop"
                        "C:\\Users\\name\\AppData\\Roaming\\Wondershare\\Wondershare Filmora"
                        "~/Downloads/myvideo.mp4"
                      If a folder is given, %(title)s.%(ext)s is appended automatically.
    """
    params  = parameters or {}
    task    = params.get("task", "").strip()
    command = params.get("command", "").strip()
    visible = params.get("visible", None)
    timeout = int(params.get("timeout", 30))
    cwd     = params.get("cwd", None)
    url     = params.get("url", "").strip()
    dest    = params.get("destination", "").strip()

    if not task and not command:
        return "Please describe what to do in the terminal, sir."

    if player:
        player.write_log(f"[terminal] {(task or command)[:60]}")

    # ── If command already provided, skip generation ───────────
    if command:
        # Expand any ~ or %VAR% in the command itself
        command = os.path.expandvars(command)
        safe, reason = _is_safe(command)
        if not safe:
            return f"Blocked for safety: {reason}"
        print(f"[Terminal] ⚡ Direct: {command[:80]}")
        if visible is None:
            visible = len(command) > 60
        if visible:
            return _run_visible(command, cwd=cwd)
        return _run_silent(command, timeout=timeout, cwd=cwd)

    task_lower = task.lower()

    # ── Media: yt-dlp ─────────────────────────────────────────
    # Triggers on: youtube, soundcloud, any URL + download, yt-dlp keyword
    is_yt_task = (
        any(re.search(p, task_lower) for p, _ in _MEDIA_PATTERNS
            if _ in ("yt-dlp", "yt-dlp-audio", "yt-dlp-video"))
        or "youtube" in task_lower
        or "soundcloud" in task_lower
        or "yt-dlp" in task_lower
        or (("download" in task_lower or "save" in task_lower)
            and re.search(r"https?://", task))
    )

    if is_yt_task:
        yt = _ensure_yt_dlp()
        if not yt:
            return "yt-dlp could not be installed. Please install it manually: pip install yt-dlp"

        if not url:
            url_m = re.search(r"https?://\S+", task)
            if url_m:
                url = url_m.group(0)

        cmd = _build_yt_dlp_command(task, url=url, params=params)
        print(f"[Terminal] 🎬 yt-dlp: {cmd}")
        _run_visible(cmd, cwd=cwd)

        # Report where the file will actually go
        resolved_dest = _make_dest(dest, task)
        folder        = str(Path(resolved_dest).parent) if "%(title)s" in resolved_dest \
                        else str(Path(resolved_dest).parent)
        return (
            f"Download started in terminal window.\n"
            f"Saving to: {folder}\n"
            f"Command: {cmd}"
        )

    # ── Media: ffmpeg ──────────────────────────────────────────
    is_ffmpeg = any(re.search(p, task_lower) for p, _ in _MEDIA_PATTERNS
                    if _ == "ffmpeg")

    if is_ffmpeg:
        ff = _ensure_ffmpeg()
        if not ff:
            return (
                "ffmpeg is not installed, sir. "
                "Please install it from https://ffmpeg.org or via: winget install ffmpeg"
            )
        cmd = _build_ffmpeg_command(task, params=params)
        if cmd:
            print(f"[Terminal] 🔧 ffmpeg: {cmd}")
            _run_visible(cmd, cwd=cwd)
            return f"Conversion started in terminal. Command: {cmd}"

    # ── Hardcoded system commands ──────────────────────────────
    hardcoded = _find_hardcoded(task, params)
    if hardcoded:
        print(f"[Terminal] ⚡ Hardcoded: {hardcoded[:80]}")
        safe, reason = _is_safe(hardcoded)
        if not safe:
            return f"Blocked for safety: {reason}"
        if any(x in hardcoded.lower() for x in ["notepad", "explorer", "start "]):
            subprocess.Popen(hardcoded, shell=True)
            return f"Opened: {hardcoded}"
        if visible is None:
            visible = False
        if visible:
            return _run_visible(hardcoded, cwd=cwd)
        return _run_silent(hardcoded, timeout=timeout, cwd=cwd)

    # ── File existence check ───────────────────────────────────
    if "exist" in task_lower or "check" in task_lower or "find" in task_lower:
        file_m = re.search(r'[\"\'"]?([\S]+\.[a-zA-Z0-9]+)[\"\'"]?', task)
        if file_m:
            path = file_m.group(1)
            for base in [Path.home(), Path.home() / "Downloads",
                         _get_real_desktop(), Path.home() / "Documents"]:
                candidate = base / Path(path).name
                if candidate.exists():
                    return f"Found: {candidate}"
            if Path(_expand_path(path)).exists():
                return f"Found: {_expand_path(path)}"
            return f"NOT FOUND: {path}"

    # ── Gemini fallback ────────────────────────────────────────
    print(f"[Terminal] 🤖 Asking Gemini for command: {task[:60]}")
    command = _ask_gemini_command(task)

    if command == "UNSAFE":
        return "I cannot generate a safe command for that request, sir."
    if command.startswith("ERROR:"):
        return f"Could not generate command: {command}"

    safe, reason = _is_safe(command)
    if not safe:
        return f"Safety check blocked the generated command: {reason}"

    # Expand any env vars Gemini may have included
    command = os.path.expandvars(command)
    print(f"[Terminal] ✅ Generated: {command[:80]}")

    if visible is None:
        visible = len(command) > 80 or any(
            x in command.lower() for x in ["install", "download", "convert", "compile"]
        )

    if visible:
        _run_visible(command, cwd=cwd)
        return f"Terminal opened with command: {command[:80]}"
    return _run_silent(command, timeout=timeout, cwd=cwd)
