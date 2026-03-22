# J.A.R.V.I.S — MARK XXX (Refactored)

> **Just A Rather Very Intelligent System**  
> A voice-activated AI desktop assistant powered by the Gemini Live API.

---

## Credits

This project is a fork of **MARK XXX** by [FatihMakes](https://github.com/FatihMakes).  
The original project provided the Gemini Live API voice session architecture, the animated JARVIS UI (`ui.py`), the memory system, the task queue, and the error handler.

This fork refactors the action layer from 15+ specialised tools into five universal primitives, enabling truly autonomous browser and system control rather than scripted task execution.

---

## What Changed in This Fork

### Architecture — from scripted tools to universal primitives

**Original:** 15+ dedicated action files, each handling one specific task (`weather_report.py`, `flight_finder.py`, `youtube_video.py`, `send_message.py`, `web_search.py`, `desktop.py`, `code_helper.py`, `dev_agent.py`, etc.). Adding a new capability meant writing a new file.

**This fork:** Five universal primitives that cover everything a human can do at a computer:

| Primitive | What it does |
|-----------|-------------|
| `browser` | Navigate URLs, parse HTML, read page text, visual analysis, click, type, scroll |
| `vision` | Capture screen or webcam → Gemini Vision → text answer |
| `computer` | Type, click coordinates, hotkeys, scroll, drag, screenshots |
| `terminal` | Run shell commands, yt-dlp downloads, ffmpeg conversion, pip installs |
| `os_control` | Volume, brightness, dark mode, Wi-Fi, display sleep, lock, shutdown, window management |

### Tiered reading strategy

Browser tasks now follow a cost hierarchy — always starting at the cheapest approach that works:

- **Tier 0** — No browser. OS controls and terminal tasks are direct calls.
- **Tier 1** — URL construction. Navigate directly to constructed URLs (Google, YouTube, Flights, Maps, Gmail, Classroom, WhatsApp Web, Wikipedia, Amazon, Reddit, GitHub).
- **Tier 2** — HTML/DOM parsing with BeautifulSoup. Finds links, prices, titles from raw HTML. Preferred over visual clicking — exact and free.
- **Tier 3** — Clipboard text extraction (`get_text`). All visible text as plain string.
- **Tier 4** — Vision read. Screenshot → Gemini question. One API call. Used deliberately.
- **Tier 5** — Vision loop with computer control. Multi-step UI interaction. Last resort.

### Planner & executor rewrite

- `agent/planner.py` — completely rewritten with 13 worked examples, tiered strategy instructions, 429 rate-limit retry with delay extraction, and keyword fallback that makes zero API calls.
- `agent/executor.py` — context enrichment (prior step results injected into next step parameters automatically), condition evaluation (conditional steps skip with natural spoken explanation), improved natural summary that includes real data found (prices, times, assignments).

### Browser — real profile via CDP

The browser now connects to your actual browser using Chrome DevTools Protocol (`connect_over_cdp`). Your real cookies, sessions, and logins are used — WhatsApp Web, Gmail, Google Classroom all work because it's literally your running browser. No profile lock conflicts.

### Fully configurable delays

All timing constants live at the top of `actions/browser.py`:

```python
DELAY_AFTER_NAVIGATE = 2.0   # wait after page load before reading
DELAY_CDP_READY      = 0.4   # polling interval while browser starts
DELAY_CDP_MAX_WAIT   = 15    # max seconds to wait for debug port
DELAY_CLICK          = 0.5   # pause after a click
```

### Text input bar

A text input bar is available in the JARVIS window (Ctrl+T or the `⌨` button in the bottom-right). Sends text directly to the Gemini Live session without speaking. Useful for typing commands or addresses precisely.

### System prompt rewrite

`core/prompt.txt` is a complete six-part rewrite covering: what JARVIS is, the full tiered strategy, three-step reasoning process, 13 worked examples with full reasoning chains, communication rules, and a decision checklist.

### Files removed (replaced by primitives)

`browser_control.py`, `computer_control.py`, `computer_settings.py`, `cmd_control.py`, `weather_report.py`, `flight_finder.py`, `youtube_video.py`, `send_message.py`, `web_search.py`, `desktop.py`, `code_helper.py`, `dev_agent.py`

### Files kept unchanged from original

`ui.py`, `agent/error_handler.py`, `agent/task_queue.py`, `actions/screen_processor.py`, `actions/open_app.py`, `actions/file_controller.py`, `actions/reminder.py`, `memory/memory_manager.py`

---

## What JARVIS Can Now Do

Things that were impossible with the original and are in scope now:

- Navigate to any website and read its content
- Check Google Classroom for assignments due tomorrow
- Find the most-viewed YouTube video matching a search and download it as MP3
- Open Gmail and read unread emails
- Get flight options from Google Flights for any route
- Send a WhatsApp message via WhatsApp Web
- Convert any video file to any audio format via ffmpeg
- Check disk usage, RAM, running processes from natural language
- Set volume, brightness, dark mode, Wi-Fi via system APIs (not GUI clicking)
- Research a topic across multiple sources and save to a file
- Get a Google Maps route
- Any task on any website — if you can describe it, JARVIS can attempt it

---

## Requirements

```
pip install google-genai pyaudio pillow mss opencv-python pyautogui pyperclip
pip install playwright beautifulsoup4 psutil wmi pycaw comtypes
playwright install
```

> **Note:** `wmi` and `pycaw` are Windows-only. On macOS/Linux those volume/brightness methods fall back to subprocess commands.

---

## Setup

1. Clone the repo
2. Install requirements (see above)
3. Place your `face.png` in the root directory (or remove the reference in `ui.py`)
4. Run `python main.py`
5. Enter your Gemini API key when prompted
6. Select your browser from the selector dialog that appears on every startup
7. Talk to JARVIS

---

## Configuration

All config is stored in `config/api_keys.json`:

```json
{
    "gemini_api_key": "your-key-here",
    "browser": "brave",
    "camera_index": 0
}
```

`browser` is set via the startup selector dialog. Valid values: `brave`, `chrome`, `edge`, `opera`, `opera_gx`, `vivaldi`, `firefox`.

---

## How to phrase requests

JARVIS works best when requests don't require mid-task decisions. If a task has options you'd normally choose between (ride type, seat, which of 5 results), state your preference upfront:

| Less ideal | Better |
|-----------|--------|
| "Order me an Uber" | "Order me an UberX to work using my saved payment method" |
| "Download that YouTube video" | "Download the most-viewed Interstellar edit as MP3 to my Desktop" |
| "Check my flights" | "Show me the cheapest flights from Istanbul to London on March 27" |

For read-and-report tasks (check schedule, read emails, find prices), no special phrasing is needed — those run fully autonomously.

---

## File Structure

```
├── main.py                      # Gemini Live session, tool declarations, UI setup
├── ui.py                        # JARVIS animated UI (original, unchanged)
├── face.png                     # Avatar image
│
├── actions/
│   ├── browser.py               # Browser primitive (CDP + tiered reading)
│   ├── vision.py                # Vision primitive (screen/camera → Gemini)
│   ├── computer.py              # Computer control primitive
│   ├── terminal.py              # Terminal primitive (yt-dlp, ffmpeg, shell)
│   ├── os_control.py            # OS control (volume, brightness, dark mode, etc.)
│   ├── screen_processor.py      # Live API voice-activated screen analysis (original)
│   ├── open_app.py              # App launcher (original)
│   ├── file_controller.py       # File management (original)
│   └── reminder.py              # Windows Task Scheduler reminders (original)
│
├── agent/
│   ├── planner.py               # Goal → step plan via Gemini
│   ├── executor.py              # Step execution with context injection
│   ├── task_queue.py            # Background task queue (original)
│   └── error_handler.py         # Error classification and recovery (original)
│
├── core/
│   └── prompt.txt               # JARVIS system prompt (full rewrite)
│
├── memory/
│   └── memory_manager.py        # Long-term memory (original)
│
└── config/
    └── api_keys.json            # API key + browser preference (gitignored)
```

---

## License

MIT — same as the original MARK XXX project by FatihMakes.
