# main.py
# JARVIS — Gemini Live API Voice Session
#
# Five universal primitives: browser, vision, computer, terminal, os_control
# + agent_task (multi-step planner) + screen_process (voice-activated screen analysis)
#
# Text input bar: attached to existing Tkinter window without modifying ui.py.
# Thread-safe queue bridges Tkinter main thread ↔ asyncio session thread.

import asyncio
import json
import queue
import re
import sys
import threading
import traceback

from pathlib import Path

import pyaudio
from google import genai
from google.genai import types

from ui import JarvisUI
from memory.memory_manager import load_memory, update_memory, format_memory_for_prompt
from agent.task_queue import get_queue

# ─────────────────────────────────────────────────────────────
# PATH / CONFIG
# ─────────────────────────────────────────────────────────────

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"

# ─────────────────────────────────────────────────────────────
# AUDIO CONFIG
# ─────────────────────────────────────────────────────────────

LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-preview-12-2025"
FORMAT              = pyaudio.paInt16
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

pya = pyaudio.PyAudio()

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, an autonomous AI assistant. "
            "Act using the five available tools. Never simulate results — always call the tool."
        )


# ─────────────────────────────────────────────────────────────
# MEMORY — async update (every N turns, gated by content check)
# ─────────────────────────────────────────────────────────────

_memory_turn_counter  = 0
_memory_turn_lock     = threading.Lock()
_MEMORY_EVERY_N_TURNS = 5
_last_memory_input    = ""


def _update_memory_async(user_text: str, jarvis_text: str) -> None:
    global _memory_turn_counter, _last_memory_input

    with _memory_turn_lock:
        _memory_turn_counter += 1
        current_count = _memory_turn_counter

        if current_count % _MEMORY_EVERY_N_TURNS != 0:
            return

        text = user_text.strip()
        if len(text) < 10 or text == _last_memory_input:
            return
        _last_memory_input = text

    try:
        from google import genai as _genai
        client = _genai.Client(api_key=_get_api_key())

        check = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                f"Does this message contain personal facts about the user "
                f"(name, age, city, job, hobby, relationship, birthday, preference)? "
                f"Reply only YES or NO.\n\nMessage: {text[:300]}"
            )
        )
        if "YES" not in check.text.upper():
            return

        raw = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                f"Extract personal facts from this message. Any language.\n"
                f"Return ONLY valid JSON or {{}} if nothing found.\n"
                f"Extract: name, age, birthday, city, job, hobbies, preferences, relationships.\n"
                f"Skip: weather, reminders, search results, commands.\n\n"
                f"Format: {{\"identity\":{{\"name\":{{\"value\":\"...\"}}}}}}\n\n"
                f"Message: {text[:500]}\n\nJSON:"
            )
        ).text.strip()

        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        if not raw or raw == "{}":
            return

        data = json.loads(raw)
        if data:
            update_memory(data)
            print(f"[Memory] ✅ Updated: {list(data.keys())}")

    except json.JSONDecodeError:
        pass
    except Exception as e:
        if "429" not in str(e):
            print(f"[Memory] ⚠️ {e}")


# ─────────────────────────────────────────────────────────────
# TOOL DECLARATIONS — 10 tools: 5 primitives + agent_task + screen_process + open_app + reminder + file_controller
# ─────────────────────────────────────────────────────────────

TOOL_DECLARATIONS = [

    {
        "name": "browser",
        "description": (
            "Universal browser primitive — all web interaction. "
            "Use for: navigating to URLs (go_to), fetching and parsing HTML to find links "
            "(parse_html — PREFERRED over visual clicking), reading page text (get_text), "
            "taking a page screenshot and asking Gemini a specific question (vision_read), "
            "clicking elements, typing in fields, scrolling, and tab management. "
            "Also use to construct service URLs without navigating (construct_url). "
            "For reading content: get_text (Tier 3). "
            "For finding links to navigate to: parse_html (Tier 2) — exact and free. "
            "For checking visual state (unread, colors, badges): vision_read (Tier 4). "
            "The browser uses the user's real default browser with their existing sessions."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "go_to | construct_url | fetch_html | parse_html | "
                        "wait_for_content | get_text | "
                        "vision_read | click | type | scroll | press | get_url | "
                        "back | reload | new_tab | close_tab | close"
                    )
                },
                "url": {"type": "STRING", "description": "URL for go_to action"},
                "service": {
                    "type": "STRING",
                    "description": (
                        "For construct_url: google | youtube | youtube_by_views | "
                        "soundcloud | spotify | "
                        "google_flights | google_maps | google_hotels | google_calendar | "
                        "gmail | google_classroom | classroom_todo | "
                        "whatsapp | wikipedia | amazon | weather | "
                        "booking | airbnb | tripadvisor | "
                        "reddit | github | twitter"
                    )
                },
                "query": {"type": "STRING", "description": "Search query (for construct_url, search)"},
                "origin": {"type": "STRING", "description": "Origin location (for flights/maps)"},
                "destination": {"type": "STRING", "description": "Destination location"},
                "date": {"type": "STRING", "description": "Date for flights"},
                "selector": {"type": "STRING", "description": "CSS selector for parse_html/click"},
                "known_key": {
                    "type": "STRING",
                    "description": (
                        "Known selector key for parse_html: youtube_video_link | "
                        "google_first_result | google_weather_temp | wikipedia_content"
                    )
                },
                "attribute": {
                    "type": "STRING",
                    "description": "Attribute to extract: href (default) | text | src"
                },
                "question": {
                    "type": "STRING",
                    "description": (
                        "Specific answerable question for vision_read. "
                        "Ask specific questions, not 'describe the page'."
                    )
                },
                "text": {"type": "STRING", "description": "Text for click or type actions"},
                "description": {"type": "STRING", "description": "Element description for click"},
                "direction": {"type": "STRING", "description": "up | down for scroll"},
                "amount": {"type": "INTEGER", "description": "Scroll amount in pixels"},
                "key": {"type": "STRING", "description": "Key for press action (e.g. Enter, Tab)"},
                "limit": {"type": "INTEGER", "description": "Max results for parse_html (default 5)"},
                "max_chars": {"type": "INTEGER", "description": "Max characters for get_text (default 6000)"},
                "timeout_ms": {"type": "INTEGER", "description": "Timeout in ms for wait_for_content (default 5000)"},
                "checkin": {"type": "STRING", "description": "Check-in date for hotel search (YYYY-MM-DD)"},
                "checkout": {"type": "STRING", "description": "Check-out date for hotel search (YYYY-MM-DD)"}
            },
            "required": ["action"]
        }
    },

    {
        "name": "vision",
        "description": (
            "Captures the screen or webcam and asks Gemini Vision a specific question. "
            "Use when: user asks what is on screen, asks you to analyze the screen, "
            "asks to look at the camera, or any request requiring visual understanding "
            "of the current screen state or physical environment. "
            "For page-specific vision inside the browser, use browser's vision_read instead. "
            "After calling this tool for screen analysis, the result is returned as text."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {
                    "type": "STRING",
                    "description": (
                        "Specific question to answer from the image. "
                        "Example: 'what application is currently open?' or "
                        "'what is the person doing in front of the camera?'"
                    )
                },
                "angle": {
                    "type": "STRING",
                    "description": "'screen' (default) — captures monitor | 'camera' — uses webcam"
                }
            },
            "required": ["text"]
        }
    },

    {
        "name": "computer",
        "description": (
            "Direct computer input control: type text, click at coordinates, use keyboard "
            "shortcuts, scroll, move mouse, drag, take screenshots, wait. "
            "Use for: typing into any focused field, clicking at known x,y coordinates, "
            "pressing keys, keyboard shortcuts. "
            "screen_click and screen_find are available but cost a Gemini API call — "
            "prefer browser parse_html for finding links/elements in web pages. "
            "Use screen_click only when HTML parsing cannot work (native apps, non-web UI)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "type | click | double_click | right_click | hotkey | press | "
                        "scroll | move | drag | copy | paste | screenshot | wait | "
                        "clear_field | focus_window | screen_find | screen_click"
                    )
                },
                "text": {"type": "STRING", "description": "Text for type/paste"},
                "x": {"type": "INTEGER", "description": "X coordinate for click/move"},
                "y": {"type": "INTEGER", "description": "Y coordinate for click/move"},
                "keys": {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key": {"type": "STRING", "description": "Single key e.g. 'enter', 'escape'"},
                "direction": {"type": "STRING", "description": "up | down | left | right"},
                "amount": {"type": "INTEGER", "description": "Scroll amount (default 3)"},
                "seconds": {"type": "NUMBER", "description": "Seconds to wait"},
                "title": {"type": "STRING", "description": "Window title for focus_window"},
                "description": {
                    "type": "STRING",
                    "description": "Element description for screen_find/screen_click (AI fallback)"
                },
                "path": {"type": "STRING", "description": "Save path for screenshot"},
                "interval": {"type": "NUMBER", "description": "Typing interval in seconds"}
            },
            "required": ["action"]
        }
    },

    {
        "name": "terminal",
        "description": (
            "Runs shell commands — silently or in a visible terminal window. "
            "Use for: downloading with yt-dlp, converting files with ffmpeg, "
            "installing packages, checking system info (disk, RAM, processes, IP), "
            "writing files, running scripts, any shell task. "
            "For media tasks (YouTube download, format conversion): always uses "
            "purpose-built CLI tools (yt-dlp, ffmpeg). "
            "Long-running tasks (downloads, installs) automatically open a visible terminal."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {
                    "type": "STRING",
                    "description": "Natural language description of what to do in the terminal"
                },
                "command": {
                    "type": "STRING",
                    "description": "Exact command to run (skips AI generation)"
                },
                "visible": {
                    "type": "BOOLEAN",
                    "description": "Open visible terminal window (default: auto for long tasks)"
                },
                "timeout": {"type": "INTEGER", "description": "Timeout in seconds (default 30)"},
                "url": {"type": "STRING", "description": "URL for download tasks"},
                "destination": {"type": "STRING", "description": "Output path for downloads"},
                "input_file": {"type": "STRING", "description": "Input file for conversion"},
                "output_file": {"type": "STRING", "description": "Output file for conversion"}
            },
            "required": ["task"]
        }
    },

    {
        "name": "os_control",
        "description": (
            "System-level OS controls. Use for: volume (set/up/down/mute), "
            "brightness (set/up/down), dark mode, Wi-Fi (toggle/on/off), "
            "display sleep, lock screen, restart, shutdown, "
            "window management (minimize, maximize, fullscreen, snap, alt-tab, show desktop), "
            "screenshots, task manager, file explorer. "
            "This is the FASTEST path for any OS-level control — no browser needed. "
            "NEVER use agent_task or browser for these. Call this directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "volume_set | volume_up | volume_down | mute | unmute | "
                        "brightness_set | brightness_up | brightness_down | "
                        "dark_mode | toggle_dark_mode | light_mode | "
                        "toggle_wifi | wifi_on | wifi_off | "
                        "sleep_display | turn_off_screen | "
                        "lock_screen | lock | "
                        "restart | restart_computer | "
                        "shutdown | shut_down | power_off | "
                        "minimize | maximize | full_screen | fullscreen | "
                        "snap_left | snap_right | switch_window | show_desktop | "
                        "screenshot | take_screenshot | "
                        "task_manager | file_explorer"
                    )
                },
                "description": {
                    "type": "STRING",
                    "description": "Natural language description if action not specified (any language)"
                },
                "value": {
                    "type": "INTEGER",
                    "description": "Numeric value: 0-100 for volume_set or brightness_set"
                }
            },
            "required": []
        }
    },

    {
        "name": "agent_task",
        "description": (
            "Executes complex multi-step goals that require multiple different tools. "
            "The planner breaks the goal into steps using available tools: "
            "browser, vision, computer, terminal, os_control, open_app, file_controller, reminder. "
            "Use ONLY when the task genuinely requires 2+ sequential steps where "
            "one result feeds into the next. "
            "Examples: 'research X and save to a file', 'find the most viewed YouTube video "
            "and download it', 'check Gmail for unread emails and reply', "
            "'convert all .mp4 files in Downloads to MP3', "
            "'check Google Classroom for tomorrow's assignments'. "
            "DO NOT use for: single OS controls, single terminal commands, "
            "single browser navigations. Call those tools directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {
                    "type": "STRING",
                    "description": "Complete description of what needs to be accomplished"
                },
                "priority": {
                    "type": "STRING",
                    "description": "low | normal | high (default: normal)"
                }
            },
            "required": ["goal"]
        }
    },

    {
        "name": "screen_process",
        "description": (
            "Captures and analyzes the screen or webcam in a dedicated Gemini Live "
            "audio session — the response is spoken as voice directly. "
            "MUST be called when user asks: what is on screen, what do you see, "
            "analyze my screen, look at the camera, describe what's on screen. "
            "You have NO visual ability without calling this tool first. "
            "After calling this tool, stay COMPLETELY SILENT — the vision module speaks directly. "
            "Do NOT summarize, repeat, or add any spoken output after this call."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {
                    "type": "STRING",
                    "description": "'screen' to capture display (default) | 'camera' for webcam"
                },
                "text": {
                    "type": "STRING",
                    "description": "The question or instruction about the captured image"
                }
            },
            "required": ["text"]
        }
    },

    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer by name. "
            "Use when user asks to open, launch, or start any app, "
            "desktop software, or program that is not a website. "
            "For websites, use browser go_to instead. "
            "Examples: 'open Spotify', 'launch Discord', 'open VS Code', 'start Notepad'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Name of the application (e.g. 'Spotify', 'Discord', 'VSCode')"
                }
            },
            "required": ["app_name"]
        }
    },

    {
        "name": "reminder",
        "description": (
            "Set a timed reminder that triggers a toast notification and sound at the specified date/time. "
            "Use for: 'remind me to...', 'set a reminder for...', 'alert me at...'. "
            "Handles scheduling automatically via Windows Task Scheduler. "
            "ALWAYS call this directly — NEVER use agent_task or terminal for reminders."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date": {"type": "STRING", "description": "Target date in YYYY-MM-DD format"},
                "time": {"type": "STRING", "description": "Target time in HH:MM (24-hour) format"},
                "message": {"type": "STRING", "description": "Reminder text shown in the notification"}
            },
            "required": ["date", "time", "message"]
        }
    },

    {
        "name": "file_controller",
        "description": (
            "File management operations — list, create, delete, move, copy, rename, "
            "read, write, find, organize. Handles desktop organization, file search, "
            "disk usage, and detailed file info. "
            "Supports path shortcuts: 'desktop', 'downloads', 'documents', 'home'. "
            "Use for: 'list files on my desktop', 'organize my desktop', "
            "'find all PDFs in Documents', 'move this file to Downloads', "
            "'what's taking up space?'. "
            "ALWAYS call this directly — NEVER use terminal for file operations."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "list | create_file | create_folder | delete | move | copy | "
                        "rename | read | write | find | largest | disk_usage | "
                        "organize_desktop | info"
                    )
                },
                "path": {
                    "type": "STRING",
                    "description": "Target path or shortcut (desktop, downloads, documents, pictures, music, videos, home)"
                },
                "name": {"type": "STRING", "description": "File or folder name"},
                "content": {"type": "STRING", "description": "Content for create_file or write"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name": {"type": "STRING", "description": "New name for rename action"},
                "extension": {"type": "STRING", "description": "File extension filter for find (.pdf, .docx)"},
                "append": {"type": "BOOLEAN", "description": "Append to file instead of overwrite (for write)"}
            },
            "required": ["action"]
        }
    },

]

class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui              = ui
        self.session         = None
        self.audio_in_queue  = None
        self.out_queue       = None
        self._loop           = None
        self._text_send_queue: queue.Queue = queue.Queue()  # thread-safe bridge

    def speak(self, text: str):
        """Thread-safe speak — any thread can call this."""
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def send_text(self, text: str):
        """Called from Tkinter thread to send text input to the session."""
        self._text_send_queue.put(text)

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        memory  = load_memory()
        mem_str = format_memory_for_prompt(memory)

        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        if mem_str:
            sys_prompt = time_ctx + mem_str + "\n\n" + sys_prompt
        else:
            sys_prompt = time_ctx + sys_prompt

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=sys_prompt,
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Charon"
                    )
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 TOOL: {name}  ARGS: {args}")

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "browser":
                from actions.browser import browser
                r      = await loop.run_in_executor(
                    None, lambda: browser(parameters=args, player=self.ui)
                )
                result = r or "Browser action completed."

            elif name == "vision":
                from actions.vision import vision
                r      = await loop.run_in_executor(
                    None, lambda: vision(parameters=args, player=self.ui)
                )
                result = r or "Could not capture image."

            elif name == "computer":
                from actions.computer import computer
                r      = await loop.run_in_executor(
                    None, lambda: computer(parameters=args, player=self.ui)
                )
                result = r or "Done."

            elif name == "terminal":
                from actions.terminal import terminal
                r      = await loop.run_in_executor(
                    None, lambda: terminal(parameters=args, player=self.ui)
                )
                result = r or "Command executed."

            elif name == "os_control":
                from actions.os_control import os_control
                r      = await loop.run_in_executor(
                    None, lambda: os_control(parameters=args, player=self.ui)
                )
                result = r or "Done."

            elif name == "agent_task":
                goal         = args.get("goal", "")
                priority_str = args.get("priority", "normal").lower()

                from agent.task_queue import get_queue, TaskPriority
                priority_map = {
                    "low":    TaskPriority.LOW,
                    "normal": TaskPriority.NORMAL,
                    "high":   TaskPriority.HIGH,
                }
                priority = priority_map.get(priority_str, TaskPriority.NORMAL)
                queue_   = get_queue()
                task_id  = queue_.submit(
                    goal=goal,
                    priority=priority,
                    speak=self.speak,
                )
                result = f"Task started (ID: {task_id}). I'll update you as I make progress, sir."

            elif name == "screen_process":
                from actions.screen_processor import screen_process
                threading.Thread(
                    target=screen_process,
                    kwargs={"parameters": args, "response": None,
                            "player": self.ui, "session_memory": None},
                    daemon=True
                ).start()
                result = (
                    "Vision module activated. "
                    "Stay completely silent — vision module will speak directly."
                )

            elif name == "open_app":
                from actions.open_app import open_app
                r      = await loop.run_in_executor(
                    None, lambda: open_app(parameters=args, player=self.ui)
                )
                result = r or f"Opened {args.get('app_name')}."

            elif name == "reminder":
                from actions.reminder import reminder
                r      = await loop.run_in_executor(
                    None, lambda: reminder(parameters=args, player=self.ui)
                )
                result = r or "Reminder set."

            elif name == "file_controller":
                from actions.file_controller import file_controller
                r      = await loop.run_in_executor(
                    None, lambda: file_controller(parameters=args, player=self.ui)
                )
                result = r or "File operation completed."

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")

        return types.FunctionResponse(
            id=fc.id,
            name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        """Sends microphone audio and text input to the session."""
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _poll_text_input(self):
        """
        Polls the thread-safe text queue and sends typed messages to the session.
        Bridges Tkinter main thread → asyncio session thread.
        """
        while True:
            await asyncio.sleep(0.05)
            while not self._text_send_queue.empty():
                try:
                    text = self._text_send_queue.get_nowait()
                    if text and self.session:
                        print(f"[JARVIS] ⌨️ Text input: {text[:60]}")
                        self.ui.write_log(f"You (text): {text}")
                        await self.session.send_client_content(
                            turns={"parts": [{"text": text}]},
                            turn_complete=True
                        )
                except queue.Empty:
                    break
                except Exception as e:
                    print(f"[JARVIS] ⚠️ Text send failed: {e}")

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        stream = await asyncio.to_thread(
            pya.open,
            format=FORMAT,
            channels=CHANNELS,
            rate=SEND_SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
        try:
            while True:
                data = await asyncio.to_thread(
                    stream.read, CHUNK_SIZE, exception_on_overflow=False
                )
                await self.out_queue.put({"data": data, "mime_type": "audio/pcm"})
        except Exception as e:
            print(f"[JARVIS] ❌ Mic error: {e}")
            raise
        finally:
            stream.close()

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf = []
        in_buf  = []

        try:
            while True:
                turn = self.session.receive()
                async for response in turn:

                    if response.data:
                        self.audio_in_queue.put_nowait(response.data)

                    if response.server_content:
                        sc = response.server_content

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = sc.input_transcription.text.strip()
                            if txt:
                                in_buf.append(txt)

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = sc.output_transcription.text.strip()
                            if txt:
                                out_buf.append(txt)

                        if sc.turn_complete:
                            full_in  = ""
                            full_out = ""

                            if in_buf:
                                full_in = " ".join(in_buf).strip()
                                if full_in:
                                    self.ui.write_log(f"You: {full_in}")
                            in_buf = []

                            if out_buf:
                                full_out = " ".join(out_buf).strip()
                                if full_out:
                                    self.ui.write_log(f"Jarvis: {full_out}")
                            out_buf = []

                            if full_in and len(full_in) > 5:
                                threading.Thread(
                                    target=_update_memory_async,
                                    args=(full_in, full_out),
                                    daemon=True
                                ).start()

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 Tool call: {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )

        except Exception as e:
            print(f"[JARVIS] ❌ Recv error: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")
        stream = await asyncio.to_thread(
            pya.open,
            format=FORMAT,
            channels=CHANNELS,
            rate=RECEIVE_SAMPLE_RATE,
            output=True,
        )
        try:
            while True:
                chunk = await self.audio_in_queue.get()
                await asyncio.to_thread(stream.write, chunk)
        except Exception as e:
            print(f"[JARVIS] ❌ Play error: {e}")
            raise
        finally:
            stream.close()

    async def run(self):
        client = genai.Client(
            api_key=_get_api_key(),
            http_options={"api_version": "v1beta"}
        )

        while True:
            try:
                print("[JARVIS] 🔌 Connecting...")
                config = self._build_config()

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session        = session
                    self._loop          = asyncio.get_event_loop()
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue      = asyncio.Queue(maxsize=10)

                    print("[JARVIS] ✅ Connected.")
                    self.ui.write_log("JARVIS online.")

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._poll_text_input())

            except Exception as e:
                print(f"[JARVIS] ⚠️ Error: {e}")
                traceback.print_exc()

            print("[JARVIS] 🔄 Reconnecting in 3s...")
            await asyncio.sleep(3)


# ─────────────────────────────────────────────────────────────
# TEXT INPUT BAR — attached to ui.root without modifying ui.py
# ─────────────────────────────────────────────────────────────



def _show_browser_selector(root) -> None:
    """
    Shows a one-time browser selection dialog styled to match the JARVIS UI.
    Saves the user's choice to config/api_keys.json under "browser".
    Skipped if a browser preference is already saved.
    """
    from actions.browser import detect_installed_browsers, get_browser_preference, set_browser_preference
    import tkinter as tk

    browsers = [b for b in detect_installed_browsers() if b["available"]]
    if not browsers:
        return  # nothing to show; auto-detect will handle it

    C_BG  = "#000000"
    C_PRI = "#00d4ff"
    C_MID = "#007a99"
    C_DIM = "#003344"

    dialog = tk.Toplevel(root)
    dialog.title("Browser Setup")
    dialog.configure(bg=C_BG)
    dialog.resizable(False, False)
    dialog.grab_set()  # modal

    # Centre over main window
    root.update_idletasks()
    dw, dh = 340, 80 + len(browsers) * 38 + 60
    x = root.winfo_x() + (root.winfo_width()  - dw) // 2
    y = root.winfo_y() + (root.winfo_height() - dh) // 2
    dialog.geometry(f"{dw}x{dh}+{x}+{y}")

    tk.Label(dialog, text="◈  SELECT BROWSER",
             fg=C_PRI, bg=C_BG, font=("Courier", 12, "bold")).pack(pady=(18, 4))
    tk.Label(dialog,
             text="JARVIS will use this browser for all web tasks.\nYour real profile, cookies and sessions will be used.",
             fg=C_MID, bg=C_BG, font=("Courier", 8), justify="center").pack(pady=(0, 12))

    # Pre-select saved preference if there is one, otherwise first in list
    saved = get_browser_preference()
    default_sel = saved if saved and any(b["name"] == saved for b in browsers) else browsers[0]["name"]
    selected = tk.StringVar(value=default_sel)

    btn_frame = tk.Frame(dialog, bg=C_BG)
    btn_frame.pack()

    for b in browsers:
        rb = tk.Radiobutton(
            btn_frame,
            text=f"  {b['display']}",
            variable=selected,
            value=b["name"],
            fg=C_PRI, bg=C_BG,
            activeforeground=C_PRI, activebackground=C_BG,
            selectcolor=C_DIM,
            font=("Courier", 10),
            anchor="w",
        )
        rb.pack(fill="x", pady=2, padx=20)

    def _confirm():
        set_browser_preference(selected.get())
        dialog.destroy()

    tk.Button(
        dialog, text="▸  CONFIRM",
        command=_confirm,
        bg=C_BG, fg=C_PRI,
        activebackground=C_DIM,
        font=("Courier", 10),
        borderwidth=0, pady=8,
    ).pack(pady=14)

    dialog.protocol("WM_DELETE_WINDOW", _confirm)  # treat close as confirm
    root.wait_window(dialog)


def _attach_text_input(ui, jarvis) -> None:
    """
    Attaches a text input bar below the JARVIS canvas.
    Strategy: extend the window height by 50px and place the bar in that
    extra space. The canvas (which uses place() and redraws every 16ms)
    is completely unaffected.

    Toggle: Ctrl+T or the small ⌨ icon in the bottom-right.
    """
    import tkinter as tk

    root   = ui.root
    canvas = ui.bg    # the animated canvas that fills W×H
    W      = ui.W
    H      = ui.H

    BAR_H = 48  # height of the text input bar in pixels

    _visible = [False]

    # Pre-build the bar frame (hidden until toggled)
    bar = tk.Frame(root, bg="#0a1015", pady=4)
    bar.place(x=0, y=H, width=W, height=BAR_H)  # starts hidden (below canvas)
    bar.place_forget()

    entry_var = tk.StringVar()
    entry = tk.Entry(
        bar,
        textvariable=entry_var,
        bg="#0d1f2d",
        fg="#e0e0e0",
        insertbackground="#00d4ff",
        relief=tk.FLAT,
        font=("Courier", 11),
        width=46,
    )
    entry.pack(side=tk.LEFT, padx=(10, 6), ipady=6, fill=tk.X, expand=True)

    def _on_send(event=None):
        text = entry_var.get().strip()
        if not text:
            return
        entry_var.set("")
        jarvis.send_text(text)

    send_btn = tk.Button(
        bar, text="SEND",
        command=_on_send,
        bg="#003344", fg="#00d4ff",
        activebackground="#004455",
        activeforeground="#00ffff",
        relief=tk.FLAT,
        font=("Courier", 9, "bold"),
        padx=10, pady=4,
        cursor="hand2",
        bd=0,
    )
    send_btn.pack(side=tk.LEFT, padx=(0, 10))
    entry.bind("<Return>", _on_send)

    def _toggle_bar():
        if _visible[0]:
            bar.place_forget()
            root.geometry(f"{W}x{H}")
            _visible[0] = False
        else:
            root.geometry(f"{W}x{H + BAR_H}")
            bar.place(x=0, y=H, width=W, height=BAR_H)
            _visible[0] = True
            entry.focus_set()

    # Small toggle button drawn directly on the canvas area via place()
    toggle_btn = tk.Button(
        root,
        text="⌨",
        command=_toggle_bar,
        bg="#000000", fg="#334455",
        activebackground="#001520",
        activeforeground="#00d4ff",
        relief=tk.FLAT,
        font=("Courier", 11),
        cursor="hand2",
        bd=0,
    )
    # Place in the footer strip (bottom-right, on top of canvas)
    toggle_btn.place(x=W - 28, y=H - 24, width=24, height=20)

    root.bind("<Control-t>", lambda e: _toggle_bar())
    print("[UI] ⌨ Text input bar ready (Ctrl+T to toggle)")


def main():
    # ── DPI: force Windows to report physical pixels before Tk init ──
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI
    except Exception:
        pass

    ui   = JarvisUI("face.png")
    root = ui.root

    # ── Window fix ───────────────────────────────────────────────────
    # ui.py sets resizable(False, False). Override to allow minimize and
    # ensure the window isn't taller than the usable screen area.
    root.update_idletasks()

    sw       = root.winfo_screenwidth()
    sh       = root.winfo_screenheight()
    taskbar  = 48  # approximate Windows taskbar height
    max_h    = sh - taskbar

    W = ui.W
    H = min(ui.H, max_h)

    # Reposition if needed so title bar is always reachable
    x = max(0, (sw - W) // 2)
    y = max(0, (sh - H) // 2 - taskbar // 2)

    root.resizable(False, False)   # keep fixed (ui.py expectation)
    root.geometry(f"{W}x{H}+{x}+{y}")

    # Ensure standard window decorations (title bar, minimize, close)
    try:
        root.attributes("-toolwindow", False)
    except Exception:
        pass
    # ─────────────────────────────────────────────────────────────────

    jarvis = JarvisLive(ui)

    def runner():
        ui.wait_for_api_key()

        # Browser selector — shown once, skipped if preference already saved
        # Must run on main thread via root.after() since Tkinter isn't thread-safe
        ready = threading.Event()
        root.after(0, lambda: (_show_browser_selector(root), ready.set()))
        ready.wait()

        root.after(0, lambda: _attach_text_input(ui, jarvis))
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    root.mainloop()


if __name__ == "__main__":
    main()
