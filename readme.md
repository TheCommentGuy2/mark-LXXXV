# J.A.R.V.I.S — MARK LXXXV

> **Just A Rather Very Intelligent System**
> A voice-activated AI desktop assistant powered by the Gemini Live API.

---

## Credits

This project is a fork of **MARK XXX** by [FatihMakes](https://github.com/FatihMakes).
The original project provided the Gemini Live API voice session architecture, the animated JARVIS UI (`ui.py`), the memory system, the task queue, and the error handler.

This fork refactors the action layer from 15+ specialised tools into eight universal primitives, adds a planning/execution pipeline with automatic fallback, and enables truly autonomous browser and system control rather than scripted task execution.

---

## Installation

```bash
git clone https://github.com/TheCommentGuy2/mark-LXXXV/
cd mark-LXXXV
python setup.py
python main.py
```

---

## Tools

JARVIS has eight tools that cover everything a human can do at a computer:

| Tool | What it does |
|------|-------------|
| `browser` | Navigate URLs, parse HTML, read page text, visual analysis, click, type, scroll |
| `vision` | Capture screen or webcam → Gemini Vision → text answer |
| `computer` | Type, click coordinates, hotkeys, scroll, drag, screenshots |
| `terminal` | Run shell commands, yt-dlp downloads, ffmpeg conversion, pip installs |
| `os_control` | Volume, brightness, dark mode, Wi-Fi, display sleep, lock, shutdown, window management |
| `open_app` | Launch any desktop application by name |
| `file_controller` | List, create, delete, move, copy, rename, find, organize files and folders |
| `reminder` | Set timed reminders with toast notifications and sound via Windows Task Scheduler |

Simple tasks (single tool) are called directly by Gemini. Complex multi-step tasks are routed through `agent_task`, which invokes the planner/executor pipeline.

---

## Tiered Reading Strategy

Browser tasks follow a cost hierarchy — always starting at the cheapest approach that works:

- **Tier 0** — No browser needed. Reminders, file operations, OS controls, app launches, and terminal tasks are direct calls.
- **Tier 1** — URL construction. Navigate directly to constructed URLs (Google, YouTube, Flights, Maps, Gmail, Classroom, WhatsApp Web, Wikipedia, Amazon, Reddit, GitHub, SoundCloud, Todoist).
- **Tier 2** — HTML/DOM parsing with BeautifulSoup. Finds links, prices, titles from raw HTML. Preferred over visual clicking — exact and free.
- **Tier 3** — Clipboard text extraction (`get_text`). All visible text as plain string. Best for reading content that will be spoken back.
- **Tier 4** — Vision read. Screenshot → Gemini question. One API call. Used for visual state (unread emails, bold text, icons).
- **Tier 5** — Vision loop with computer control. Multi-step UI interaction where each action depends on what appeared. Last resort.

---

## Planner & Executor

### Planning (`agent/planner.py`)

For complex multi-step goals, Gemini 2.5 Flash generates a JSON plan with up to 8 steps. The planner prompt includes all 8 tools with full parameter documentation and 17 worked examples.

**Rate-limit resilience:** If the Gemini API returns 429 (rate limited), the planner retries up to 3 times with extracted retry delays. If all attempts fail, it falls back to a keyword-based planner that generates plans with zero API calls.

**Compound goal splitting:** Goals like *"play music on SoundCloud and then check Classroom for assignments"* are automatically split on conjunctions (`and then`, `after that`, `and also`, `then`), with each sub-goal planned independently and merged into a single sequential plan.

**Keyword fallback handlers:**
- Todoist, Google Classroom, Gmail, Wikipedia, Google Maps
- YouTube, SoundCloud (media playback)
- Downloads (yt-dlp), file conversion (ffmpeg)
- OS controls (volume, brightness, dark mode, Wi-Fi, lock, shutdown)
- App launches, weather, reminders, screenshots
- Generic Google search as final fallback

### Execution (`agent/executor.py`)

The executor runs each step sequentially with:

- **Context enrichment** — prior step results are injected into the next step's parameters automatically (e.g. a parsed URL from step 2 fills the `url` field in step 3).
- **Condition evaluation** — conditional steps (`"only if step 3 found Ahmed"`) are evaluated against actual results. Skipped steps get a natural spoken explanation.
- **Step verification** — each step result is checked against success/failure signals to determine if the plan should continue or abort.
- **Automatic replan** — if a critical step fails, the executor can request a new plan from the planner with context about what went wrong. Stale condition results are cleared on replan.
- **Natural summary** — the final result includes real data found during execution (prices, assignments, flight times), not just "done".

---

## Browser — CDP Connection to Real Browser

The browser connects to your actual running browser via the Chrome DevTools Protocol. Your real cookies, sessions, and logins are used — WhatsApp Web, Gmail, Google Classroom, Todoist all work because it's literally your running browser. No profile lock conflicts.

Supported browsers: Brave, Chrome, Edge, Opera, Opera GX, Vivaldi, Firefox.

---

## Text Input Bar

A text input bar is available in the JARVIS window (Ctrl+T or the keyboard button in the bottom-right). Sends text directly to the Gemini Live session without speaking. Useful for typing commands, URLs, or addresses precisely.

---

## System Prompt

`core/prompt.txt` is a six-part instruction set:

1. **What JARVIS is** — all 8 tools and their capabilities
2. **Tiered reading strategy** — cost hierarchy for browser tasks
3. **How to think** — three-step reasoning (intent → complexity → tier)
4. **Worked examples** — 13 examples with full reasoning chains
5. **Communication rules** — language matching, brevity, specificity
6. **Decision checklist** — routing rules for every tool

---

## What Changed From the Original Fork

### Architecture — from scripted tools to universal primitives

**Original:** 15+ dedicated action files, each handling one specific task (`weather_report.py`, `flight_finder.py`, `youtube_video.py`, `send_message.py`, `web_search.py`, `desktop.py`, `code_helper.py`, `dev_agent.py`, etc.). Adding a new capability meant writing a new file.

**This fork:** Eight universal primitives that combine to handle any task. No new code needed for new capabilities — JARVIS plans and executes using the primitives it already has.

### Files removed (replaced by primitives)

`browser_control.py`, `computer_control.py`, `computer_settings.py`, `cmd_control.py`, `weather_report.py`, `flight_finder.py`, `youtube_video.py`, `send_message.py`, `web_search.py`, `desktop.py`, `code_helper.py`, `dev_agent.py`

### Files kept from original

`ui.py`, `agent/error_handler.py`, `agent/task_queue.py`, `actions/screen_processor.py`, `actions/open_app.py`, `memory/memory_manager.py`

### Files significantly rewritten

`main.py`, `agent/planner.py`, `agent/executor.py`, `actions/browser.py`, `actions/terminal.py`, `actions/computer.py`, `actions/vision.py`, `actions/os_control.py`, `actions/reminder.py`, `actions/file_controller.py`, `core/prompt.txt`

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

The API key is entered via the setup dialog on first run. The browser preference is set via the selector dialog that appears on startup.

---

## License

MIT — same as the original MARK XXX project by FatihMakes.
