# agent/planner.py
# JARVIS — Planning Module

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


PLANNER_PROMPT = """You are the planning module of JARVIS, a personal AI assistant.
Your job: break any user goal into a sequence of steps using ONLY the tools listed below.

═══════════════════════════════════════════════════════
HOW TO THINK
═══════════════════════════════════════════════════════

STEP A — Understand the actual intent.
Strip the phrasing. Respond to the real need, not the surface words.
"check YouTube for something relaxing" → play music on YouTube
"what do I have tomorrow?" → check Google Classroom To-do for tomorrow specifically
"anything coming up?" asked about Todoist → check Todoist upcoming view

STEP B — Classify.
Can this be done in ONE tool call? → call that tool directly.
Does it need multiple steps where one result feeds the next? → create a plan.

STEP C — Choose the right tier for each step.
Tier 0: No reading. OS controls, terminal tasks, launching apps → instant, no browser.
Tier 1: URL construction. Build the URL and navigate directly. Zero extra calls.
Tier 2: DOM parsing via parse_html. Reads the LIVE rendered DOM using JavaScript —
        works on React/SPA sites. PREFERRED for finding clickable links.
Tier 3: get_text. Reads ALL text in the page body via inner_text() — includes
        content that is off-screen or requires scrolling. Use for task lists,
        assignment lists, emails, any content you need to read and speak back.
        get_text is BETTER than vision_read for task managers because vision_read
        only captures the visible viewport — tasks below the fold are invisible to it.
Tier 4: vision_read. Screenshot + Gemini question. ONLY when content is truly visual
        (colors, icons, unread badges, images). NOT for reading lists of tasks.
Tier 5: Vision loop. Multiple vision+computer iterations. Only for complex UI.

ROUTING RULES:
- Single tool = call directly. NEVER route these to agent_task:
  any OS setting, any single terminal command, launching an app
- Multiple steps or conditional logic = agent_task with a plan
- Conditional steps: add "condition" field.

JS-HEAVY SITES RULE — CRITICAL:
Always insert a wait_for_content step between go_to and parse_html/get_text when
navigating to any of these sites:
  classroom.google.com, soundcloud.com, youtube.com, mail.google.com,
  app.todoist.com, todoist.com, notion.so, figma.com, trello.com,
  asana.com, linear.app, monday.com, airtable.com, app.slack.com,
  drive.google.com, docs.google.com, or ANY React/SPA application.
These sites load content via JavaScript AFTER the page loads. Without
wait_for_content, parse_html and get_text will see an empty skeleton.
Pattern is always: go_to → wait_for_content → parse_html/get_text.

TASK MANAGER RULE:
For any task manager (Todoist, Notion, Trello, Asana, ClickUp, Linear, Monday):
  - ALWAYS use get_text, not vision_read, to read task lists
  - vision_read only captures the visible viewport — tasks below the fold are missed
  - get_text reads the ENTIRE DOM regardless of scroll position — all tasks included
  - Navigate to the specific view URL (upcoming, today, inbox) not just the homepage

═══════════════════════════════════════════════════════
AVAILABLE TOOLS AND THEIR PARAMETERS
═══════════════════════════════════════════════════════

browser
  action: go_to | construct_url | fetch_html | parse_html | wait_for_content |
          get_text | vision_read | click | type | scroll | press | get_url |
          back | reload | new_tab | close_tab | close
  url: string (for go_to)
  service: string (for construct_url: google, youtube, youtube_by_views,
           soundcloud, spotify, google_flights, google_maps, google_hotels,
           gmail, google_classroom, classroom_todo, google_calendar,
           whatsapp, wikipedia, amazon, booking, airbnb, tripadvisor,
           reddit, github, twitter, weather)
  query, origin, destination, date, checkin, checkout: string (for construct_url)
  selector: CSS selector (for parse_html)
  known_key: string (for parse_html: youtube_video_link, google_first_result,
             google_weather_temp, wikipedia_content, soundcloud_track,
             classroom_assignments)
  attribute: "href" (default) | "text" | "src" (for parse_html)
  question: string (for vision_read — specific, answerable question)
  text: string (for click/type)
  description: string (for click by description)
  direction: "up" | "down" (for scroll)
  key: string (for press: Enter, Escape, Tab)
  limit: int (for parse_html, default 5)
  timeout_ms: int (for wait_for_content, default 5000)

vision
  text: string (required — specific question about what to look for)
  angle: "screen" (default) | "camera"

computer
  action: type | click | double_click | right_click | hotkey | press |
          scroll | move | copy | paste | screenshot | wait | clear_field |
          focus_window | screen_find | screen_click
  text: string (for type/paste)
  x, y: int (for click/move)
  keys: string (for hotkey, e.g. "ctrl+c")
  key: string (for press)
  direction: "up" | "down" (for scroll)
  amount: int (for scroll)
  seconds: float (for wait)
  description: string (for screen_find/screen_click)

terminal
  task: string (natural language description of what to do)
  command: string (exact command — skips AI generation)
  visible: bool (open visible terminal, default auto)
  timeout: int (seconds)
  url: string (for download tasks)
  destination: string (output folder or full path)
  input_file: string (for conversion tasks)
  output_file: string (for conversion tasks)

os_control
  action: volume_set | volume_up | volume_down | mute | unmute |
          brightness_set | brightness_up | brightness_down |
          dark_mode | toggle_dark_mode | light_mode |
          toggle_wifi | wifi_on | wifi_off |
          sleep_display | turn_off_screen |
          lock_screen | lock |
          restart | shutdown | shut_down |
          minimize | maximize | full_screen | snap_left | snap_right |
          switch_window | show_desktop |
          screenshot | task_manager | file_explorer
  description: string (natural language if action not specified — any language)
  value: int (for volume_set and brightness_set: 0-100)

open_app
  app_name: string (name of application to launch, e.g. "Spotify", "Discord", "Word")

file_controller
  action: list | create_file | create_folder | delete | move | copy | rename |
          read | write | find | largest | disk_usage | organize_desktop | info
  path: string (directory or shortcut: desktop, downloads, documents, home)
  name: string (file/folder name)
  content: string (for create_file/write)
  destination: string (for move/copy)
  new_name: string (for rename)
  extension: string (for find, e.g. ".pdf")

reminder
  date: string (YYYY-MM-DD)
  time: string (HH:MM 24-hour)
  message: string (reminder text)

═══════════════════════════════════════════════════════
WORKED EXAMPLES — FULL REASONING CHAINS
═══════════════════════════════════════════════════════

Goal: "Set volume to 40"
Reasoning: OS control. Tier 0. One call.
{
  "goal": "Set volume to 40",
  "steps": [
    {"step": 1, "tool": "os_control", "description": "Set volume to 40%",
     "parameters": {"action": "volume_set", "value": 40}, "critical": true}
  ]
}

---

Goal: "What's the weather in Istanbul?"
Reasoning: Tier 1 construct weather URL. parse_html works here (Google is server-rendered).
Tier 4 fallback only if parse returns nothing.
{
  "goal": "Weather in Istanbul",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Navigate to Google weather for Istanbul",
     "parameters": {"action": "go_to", "url": "https://www.google.com/search?q=weather+Istanbul"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Parse temperature from HTML",
     "parameters": {"action": "parse_html", "known_key": "google_weather_temp", "attribute": "text"},
     "critical": false},
    {"step": 3, "tool": "browser", "description": "Vision read if HTML parse returned nothing",
     "parameters": {"action": "vision_read", "question": "What is the current temperature and weather conditions shown?"},
     "condition": "only if step 2 found nothing or returned empty",
     "critical": false}
  ]
}

---

Goal: "Check my Todoist for anything due in the next 7 days"
Reasoning: Navigate to Todoist upcoming view (specific URL, not homepage).
Todoist is a React SPA — wait_for_content is mandatory.
CRITICAL: Use get_text not vision_read. get_text reads the entire DOM including
off-screen tasks. vision_read only captures the visible viewport — tasks below the
fold are completely missed. After get_text, filter and speak tasks due in next 7 days.
{
  "goal": "Check Todoist for tasks due in the next 7 days",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Navigate to Todoist upcoming view",
     "parameters": {"action": "go_to", "url": "https://app.todoist.com/app/upcoming"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Wait for Todoist React app to load all tasks",
     "parameters": {"action": "wait_for_content", "timeout_ms": 7000},
     "critical": false},
    {"step": 3, "tool": "browser", "description": "Read all upcoming task text from the full DOM",
     "parameters": {"action": "get_text", "max_chars": 8000}, "critical": true}
  ]
}

---

Goal: "Check my Todoist today view"
Reasoning: Same as above but use the today-specific URL.
{
  "goal": "Check Todoist today tasks",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Navigate to Todoist today view",
     "parameters": {"action": "go_to", "url": "https://app.todoist.com/app/today"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Wait for Todoist to load",
     "parameters": {"action": "wait_for_content", "timeout_ms": 7000},
     "critical": false},
    {"step": 3, "tool": "browser", "description": "Read all today's tasks from full DOM",
     "parameters": {"action": "get_text", "max_chars": 8000}, "critical": true}
  ]
}

---

Goal: "Play lo-fi music on YouTube"
Reasoning: YouTube is React — wait_for_content before parse_html.
{
  "goal": "Play lo-fi music on YouTube",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Search YouTube for lo-fi music",
     "parameters": {"action": "go_to", "url": "https://www.youtube.com/results?search_query=lo-fi+music"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Wait for YouTube React app to render results",
     "parameters": {"action": "wait_for_content", "timeout_ms": 5000},
     "critical": false},
    {"step": 3, "tool": "browser", "description": "Parse first video link from live DOM",
     "parameters": {"action": "parse_html", "known_key": "youtube_video_link",
                    "attribute": "href", "limit": 1},
     "critical": true},
    {"step": 4, "tool": "browser", "description": "Navigate to the video to play it",
     "parameters": {"action": "go_to", "url": ""},
     "critical": true}
  ]
}

---

Goal: "Play the flash theme on SoundCloud"
Reasoning: SoundCloud is React — wait_for_content before parse_html.
{
  "goal": "Play the flash theme on SoundCloud",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Search SoundCloud for the flash theme",
     "parameters": {"action": "go_to", "url": "https://soundcloud.com/search?q=the+flash+theme"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Wait for SoundCloud React app to load tracks",
     "parameters": {"action": "wait_for_content", "timeout_ms": 6000},
     "critical": false},
    {"step": 3, "tool": "browser", "description": "Parse first track link from live DOM",
     "parameters": {"action": "parse_html", "known_key": "soundcloud_track",
                    "attribute": "href", "limit": 1},
     "critical": true},
    {"step": 4, "tool": "browser", "description": "Navigate to the track to play it",
     "parameters": {"action": "go_to", "url": ""},
     "critical": true}
  ]
}

---

Goal: "Download this YouTube video as MP3: https://youtube.com/watch?v=abc"
Reasoning: Tier 0. yt-dlp. Single terminal call. No browser needed.
{
  "goal": "Download YouTube video as MP3",
  "steps": [
    {"step": 1, "tool": "terminal", "description": "Download as MP3 with yt-dlp",
     "parameters": {"task": "download youtube video as mp3",
                    "url": "https://youtube.com/watch?v=abc",
                    "destination": "~/Desktop/%(title)s.%(ext)s",
                    "visible": true},
     "critical": true}
  ]
}

---

Goal: "Check Gmail for urgent emails"
Reasoning: Gmail is React — wait_for_content. Use vision_read here because unread
state (bold text, styling) is visual and not reliably in DOM text.
{
  "goal": "Check Gmail for urgent emails",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open Gmail",
     "parameters": {"action": "go_to", "url": "https://mail.google.com/"}, "critical": true},
    {"step": 2, "tool": "browser", "description": "Wait for Gmail to load inbox",
     "parameters": {"action": "wait_for_content", "timeout_ms": 6000}, "critical": false},
    {"step": 3, "tool": "browser", "description": "Read inbox visually for unread emails",
     "parameters": {"action": "vision_read",
                    "question": "List all unread emails showing sender name and subject. Mark each UNREAD or READ."},
     "critical": true}
  ]
}

---

Goal: "What assignments do I have due tomorrow on Google Classroom?"
Reasoning: Classroom is React — wait_for_content. Use get_text (not vision_read)
because all assignment data including off-screen items is in the DOM text.
{
  "goal": "Assignments due tomorrow on Google Classroom",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open Google Classroom To-do page",
     "parameters": {"action": "go_to",
                    "url": "https://classroom.google.com/a/not-turned-in/all"},
     "critical": true},
    {"step": 2, "tool": "browser",
     "description": "Wait for Classroom to finish loading assignments via API",
     "parameters": {"action": "wait_for_content", "timeout_ms": 7000}, "critical": false},
    {"step": 3, "tool": "browser", "description": "Read all assignments and due dates from full DOM",
     "parameters": {"action": "get_text", "max_chars": 8000}, "critical": true}
  ]
}

---

Goal: "Send Ahmed a WhatsApp message: I'll be 10 minutes late"
Reasoning: WhatsApp Web is React — wait_for_content. Use vision_read to find contact
(visual positioning needed), then computer to click and type.
{
  "goal": "Send WhatsApp message to Ahmed",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open WhatsApp Web",
     "parameters": {"action": "go_to", "url": "https://web.whatsapp.com/"}, "critical": true},
    {"step": 2, "tool": "browser", "description": "Wait for WhatsApp to load chats",
     "parameters": {"action": "wait_for_content", "timeout_ms": 8000}, "critical": false},
    {"step": 3, "tool": "browser", "description": "Find Ahmed in contacts",
     "parameters": {"action": "vision_read",
                    "question": "Find Ahmed in the contacts or recent chats list. Describe his location on screen."},
     "critical": true},
    {"step": 4, "tool": "computer", "description": "Click Ahmed's chat",
     "parameters": {"action": "screen_click", "description": "Ahmed chat in WhatsApp"},
     "condition": "only if step 3 found Ahmed", "critical": false},
    {"step": 5, "tool": "computer", "description": "Type the message",
     "parameters": {"action": "type", "text": "I'll be 10 minutes late"},
     "condition": "only if step 3 found Ahmed", "critical": false},
    {"step": 6, "tool": "computer", "description": "Press Enter to send",
     "parameters": {"action": "press", "key": "Return"},
     "condition": "only if step 3 found Ahmed", "critical": false}
  ]
}

---

Goal: "Find cheap flights from Istanbul to London on Friday March 27"
Reasoning: Tier 1 Google Flights URL. JS-rendered — wait_for_content then get_text.
{
  "goal": "Flights Istanbul to London March 27",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open Google Flights for the route",
     "parameters": {"action": "go_to",
                    "url": "https://www.google.com/travel/flights?q=Flights+from+Istanbul+to+London+on+2026-03-27"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Wait for flight results to load",
     "parameters": {"action": "wait_for_content", "timeout_ms": 7000}, "critical": false},
    {"step": 3, "tool": "browser", "description": "Read flight prices and options",
     "parameters": {"action": "get_text"}, "critical": true}
  ]
}

---

Goal: "Research quantum computing and save a summary to my desktop"
Reasoning: Multiple sources via get_text. Terminal writes file.
{
  "goal": "Research quantum computing and save to desktop",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open Google search for quantum computing",
     "parameters": {"action": "go_to", "url": "https://www.google.com/search?q=quantum+computing+overview"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Read search result content",
     "parameters": {"action": "get_text"}, "critical": true},
    {"step": 3, "tool": "browser", "description": "Open Wikipedia for quantum computing",
     "parameters": {"action": "go_to", "url": "https://en.wikipedia.org/wiki/Quantum_computing"},
     "critical": false},
    {"step": 4, "tool": "browser", "description": "Read Wikipedia content",
     "parameters": {"action": "get_text"}, "critical": false},
    {"step": 5, "tool": "terminal", "description": "Save combined research to desktop",
     "parameters": {"task": "write research content to file on desktop",
                    "command": "echo [CONTENT] > \"%USERPROFILE%\\Desktop\\quantum_computing.txt\"",
                    "visible": false},
     "critical": true}
  ]
}

---

Goal: "Find the most viewed Interstellar edit on YouTube and download it"
Reasoning: Sort by views. wait_for_content. Parse HTML for first result. yt-dlp download.
{
  "goal": "Download most viewed Interstellar edit from YouTube",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Search YouTube sorted by view count",
     "parameters": {"action": "go_to",
                    "url": "https://www.youtube.com/results?search_query=Interstellar+edit&sp=CAM%3D"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Wait for YouTube to render sorted results",
     "parameters": {"action": "wait_for_content", "timeout_ms": 5000}, "critical": false},
    {"step": 3, "tool": "browser", "description": "Get first video URL from sorted results",
     "parameters": {"action": "parse_html", "known_key": "youtube_video_link",
                    "attribute": "href", "limit": 1},
     "critical": true},
    {"step": 4, "tool": "terminal", "description": "Download video with yt-dlp to Desktop",
     "parameters": {"task": "download youtube video",
                    "destination": "~/Desktop/%(title)s.%(ext)s",
                    "visible": true},
     "critical": true}
  ]
}

---

Goal: "Get a route from Kadıköy to Beşiktaş"
Reasoning: Tier 1 Google Maps URL. wait_for_content then get_text.
{
  "goal": "Route from Kadıköy to Beşiktaş",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open Google Maps route",
     "parameters": {"action": "go_to",
                    "url": "https://www.google.com/maps/dir/Kad%C4%B1k%C3%B6y/Be%C5%9Fikta%C5%9F"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Wait for Maps to load route data",
     "parameters": {"action": "wait_for_content", "timeout_ms": 5000}, "critical": false},
    {"step": 3, "tool": "browser", "description": "Read travel duration from page",
     "parameters": {"action": "get_text"}, "critical": true}
  ]
}

---

Goal: "Open Spotify"
Reasoning: App launch. Tier 0. Single open_app call.
{
  "goal": "Open Spotify",
  "steps": [
    {"step": 1, "tool": "open_app", "description": "Launch Spotify",
     "parameters": {"app_name": "Spotify"}, "critical": true}
  ]
}

---

Goal: "Remind me to call Ahmed tomorrow at 3 PM"
Reasoning: Reminder. Tier 0. Single reminder call. Compute date from "tomorrow".
{
  "goal": "Remind to call Ahmed tomorrow at 3 PM",
  "steps": [
    {"step": 1, "tool": "reminder", "description": "Set reminder for tomorrow at 15:00",
     "parameters": {"date": "2026-03-23", "time": "15:00",
                    "message": "Call Ahmed"}, "critical": true}
  ]
}

---

Goal: "Organize my desktop"
Reasoning: File management. Tier 0. Single file_controller call.
{
  "goal": "Organize desktop",
  "steps": [
    {"step": 1, "tool": "file_controller", "description": "Organize desktop files into folders",
     "parameters": {"action": "organize_desktop", "path": "desktop"}, "critical": true}
  ]
}

---

Goal: "List all PDF files in my Downloads"
Reasoning: File search. Tier 0. Single file_controller call with find + extension.
{
  "goal": "List PDFs in Downloads",
  "steps": [
    {"step": 1, "tool": "file_controller", "description": "Find all PDF files in Downloads",
     "parameters": {"action": "find", "path": "downloads", "extension": ".pdf"},
     "critical": true}
  ]
}

═══════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════

Return ONLY valid JSON. No markdown, no explanation, no code blocks.

{
  "goal": "...",
  "steps": [
    {
      "step": 1,
      "tool": "browser|vision|computer|terminal|os_control|open_app|file_controller|reminder",
      "description": "what this step does",
      "parameters": {},
      "condition": "optional — only if step N found X",
      "critical": true
    }
  ]
}

RULES:
- Max 8 steps. Minimum steps to accomplish the goal.
- Only use the 8 tools listed above.
- For conditional steps, always add a "condition" field.
- Leave url "" or content "" when the executor will fill from prior step result.
- For ANY JS-heavy/SPA site: always insert wait_for_content between go_to and parse_html/get_text.
- For task managers (Todoist, Notion, Trello, etc.): always use get_text not vision_read.
- vision_read is for VISUAL state (colors, icons, bold/unread, images) not for reading lists.
"""


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _extract_retry_delay(error_str: str) -> int:
    match = re.search(r"retry.*?(\d+)\s*second", str(error_str), re.IGNORECASE)
    if match:
        return min(int(match.group(1)), 60)
    return 8


def _fallback_plan_from_keywords(goal: str) -> dict:
    """Keyword-based fallback plan when Gemini is unavailable.

    Handles compound goals like 'do X and then do Y' by splitting
    on conjunctions, building a plan for each sub-goal, then merging
    all steps into a single sequential plan.
    """
    # Detect compound goals — split on the most specific conjunction first
    _SPLIT_PATTERNS = [
        r"\band\s+then\b", r"\bafter\s+that\b",
        r"\band\s+also\b", r"\bthen\b",
    ]
    sub_goals = [goal]
    for pat in _SPLIT_PATTERNS:
        new_subs = []
        for sg in sub_goals:
            parts = re.split(pat, sg, flags=re.IGNORECASE)
            new_subs.extend(p.strip() for p in parts if p.strip())
        if len(new_subs) > len(sub_goals):
            sub_goals = new_subs
            break  # only split on the most specific pattern

    if len(sub_goals) > 1:
        print(f"[Planner] 🔀 Compound goal split into {len(sub_goals)} parts: "
              + " | ".join(sg[:40] for sg in sub_goals))
        all_steps = []
        step_num = 1
        for sg in sub_goals:
            sub_plan = _fallback_plan_single(sg)
            for s in sub_plan.get("steps", []):
                s["step"] = step_num
                all_steps.append(s)
                step_num += 1
        return {"goal": goal, "steps": all_steps[:8]}  # max 8 steps

    return _fallback_plan_single(goal)


def _fallback_plan_single(goal: str) -> dict:
    """Keyword-based plan for a single (non-compound) goal."""
    g = goal.lower()

    # Todoist
    if "todoist" in g:
        view = "upcoming" if any(w in g for w in ["upcoming", "next", "week", "7 days"]) else "today"
        return {
            "goal": goal,
            "steps": [
                {"step": 1, "tool": "browser", "description": f"Open Todoist {view} view",
                 "parameters": {"action": "go_to",
                                "url": f"https://app.todoist.com/app/{view}"},
                 "critical": True},
                {"step": 2, "tool": "browser", "description": "Wait for Todoist to load",
                 "parameters": {"action": "wait_for_content", "timeout_ms": 7000},
                 "critical": False},
                {"step": 3, "tool": "browser", "description": "Read all tasks from full DOM",
                 "parameters": {"action": "get_text", "max_chars": 15000},
                 "critical": True},
            ]
        }

    # File conversion — BEFORE download so "convert mp3 to wav" doesn't match download
    if any(w in g for w in ["convert", "ffmpeg", "flac", "wav"]) and not any(w in g for w in ["download", "yt-dlp"]):
        return {
            "goal": goal,
            "steps": [{"step": 1, "tool": "terminal",
                        "description": "Convert file with ffmpeg",
                        "parameters": {"task": goal, "visible": True},
                        "critical": True}]
        }

    # Media downloads — only intent words, not format words alone
    if any(w in g for w in ["download", "yt-dlp", "save video", "save audio"]):
        url_m = re.search(r"https?://\S+", goal)
        url   = url_m.group(0) if url_m else ""
        return {
            "goal": goal,
            "steps": [{"step": 1, "tool": "terminal",
                        "description": "Download with yt-dlp",
                        "parameters": {"task": goal,
                                       "url": url, "visible": True},
                        "critical": True}]
        }

    # OS controls
    if any(w in g for w in ["volume", "brightness", "dark mode", "wifi",
                              "lock", "shutdown", "restart", "mute"]):
        return {
            "goal": goal,
            "steps": [{"step": 1, "tool": "os_control",
                        "description": "OS control",
                        "parameters": {"description": goal},
                        "critical": True}]
        }

    # Open app
    if any(w in g for w in ["open ", "launch ", "start "]) and not any(w in g for w in ["youtube", "google", "website", "http", "https", "www.", ".com", ".org", "soundcloud"]):
        app = re.sub(r"^(open|launch|start)\s+", "", g, flags=re.IGNORECASE).strip()
        return {
            "goal": goal,
            "steps": [{"step": 1, "tool": "open_app",
                        "description": f"Open {app}",
                        "parameters": {"app_name": app},
                        "critical": True}]
        }

    # ── Media playback (before web services) ──────────────────

    # SoundCloud
    if "soundcloud" in g:
        # Extract just the search term — strip all intent/platform words
        query = g
        for pat in [r"\bon\s+soundcloud\b", r"\bsoundcloud\b", r"\bplay\b",
                    r"\bfind\s+and\b", r"\bfind\b", r"\bsearch\b", r"\bfor\b",
                    r"\bthe\b(?!\s+\w+\s+theme)", r"\.+$"]:
            query = re.sub(pat, " ", query, flags=re.IGNORECASE)
        # Prefer anything in quotes
        quoted = re.search(r"['\"](.+?)['\"]", g)
        if quoted:
            query = quoted.group(1)
        query = re.sub(r"\s+", " ", query).strip(" .'\"")
        return {
            "goal": goal,
            "steps": [
                {"step": 1, "tool": "browser", "description": "Search SoundCloud",
                 "parameters": {"action": "go_to",
                                "url": f"https://soundcloud.com/search?q={quote_plus(query)}"},
                 "critical": True},
                {"step": 2, "tool": "browser", "description": "Wait for SoundCloud to load",
                 "parameters": {"action": "wait_for_content", "timeout_ms": 6000},
                 "critical": False},
                {"step": 3, "tool": "browser", "description": "Parse first track link",
                 "parameters": {"action": "parse_html", "known_key": "soundcloud_track",
                                "attribute": "href", "limit": 1},
                 "critical": True},
                {"step": 4, "tool": "browser", "description": "Navigate to track",
                 "parameters": {"action": "go_to", "url": ""},
                 "critical": True},
            ]
        }

    # YouTube
    if "youtube" in g or (re.search(r"\bplay\b", g) and "soundcloud" not in g):
        query = g
        for pat in [r"\bon\s+youtube\b", r"\byoutube\b", r"\bplay\b",
                    r"\bfind\s+and\b", r"\bfind\b", r"\bsearch\b", r"\.+$"]:
            query = re.sub(pat, " ", query, flags=re.IGNORECASE)
        quoted = re.search(r"['\"](.+?)['\"]", g)
        if quoted:
            query = quoted.group(1)
        query = re.sub(r"\s+", " ", query).strip(" .'\"")
        return {
            "goal": goal,
            "steps": [
                {"step": 1, "tool": "browser", "description": "Search YouTube",
                 "parameters": {"action": "go_to",
                                "url": f"https://www.youtube.com/results?search_query={quote_plus(query)}"},
                 "critical": True},
                {"step": 2, "tool": "browser", "description": "Wait for YouTube to render",
                 "parameters": {"action": "wait_for_content", "timeout_ms": 5000},
                 "critical": False},
                {"step": 3, "tool": "browser", "description": "Parse first video link",
                 "parameters": {"action": "parse_html", "known_key": "youtube_video_link",
                                "attribute": "href", "limit": 1},
                 "critical": True},
                {"step": 4, "tool": "browser", "description": "Navigate to video",
                 "parameters": {"action": "go_to", "url": ""},
                 "critical": True},
            ]
        }

    # ── Web services ──────────────────────────────────────────

    # Google Classroom
    if "classroom" in g or ("assignment" in g and "google" in g):
        return {
            "goal": goal,
            "steps": [
                {"step": 1, "tool": "browser", "description": "Open Google Classroom To-do",
                 "parameters": {"action": "go_to",
                                "url": "https://classroom.google.com/a/not-turned-in/all"},
                 "critical": True},
                {"step": 2, "tool": "browser", "description": "Wait for Classroom to load",
                 "parameters": {"action": "wait_for_content", "timeout_ms": 7000},
                 "critical": False},
                {"step": 3, "tool": "browser", "description": "Read assignments from DOM",
                 "parameters": {"action": "get_text", "max_chars": 8000},
                 "critical": True},
            ]
        }

    # Gmail
    if "gmail" in g or ("email" in g and "check" in g):
        return {
            "goal": goal,
            "steps": [
                {"step": 1, "tool": "browser", "description": "Open Gmail",
                 "parameters": {"action": "go_to", "url": "https://mail.google.com/"},
                 "critical": True},
                {"step": 2, "tool": "browser", "description": "Wait for Gmail to load",
                 "parameters": {"action": "wait_for_content", "timeout_ms": 6000},
                 "critical": False},
                {"step": 3, "tool": "browser", "description": "Read inbox visually",
                 "parameters": {"action": "vision_read",
                                "question": "List all unread emails with sender and subject."},
                 "critical": True},
            ]
        }

    # Wikipedia
    if "wikipedia" in g:
        topic = re.sub(r"(wikipedia|search|look up|find|about|on)\s*", "", g, flags=re.IGNORECASE).strip()
        return {
            "goal": goal,
            "steps": [
                {"step": 1, "tool": "browser", "description": f"Open Wikipedia for {topic}",
                 "parameters": {"action": "go_to",
                                "url": f"https://en.wikipedia.org/wiki/{quote_plus(topic)}"},
                 "critical": True},
                {"step": 2, "tool": "browser", "description": "Read article content",
                 "parameters": {"action": "get_text"},
                 "critical": True},
            ]
        }

    # Google Maps
    if "directions" in g or "route" in g or "google maps" in g:
        return {
            "goal": goal,
            "steps": [
                {"step": 1, "tool": "browser", "description": "Search Google Maps",
                 "parameters": {"action": "go_to",
                                "url": f"https://www.google.com/maps/search/{quote_plus(goal)}"},
                 "critical": True},
                {"step": 2, "tool": "browser", "description": "Wait for Maps to load",
                 "parameters": {"action": "wait_for_content", "timeout_ms": 5000},
                 "critical": False},
                {"step": 3, "tool": "browser", "description": "Read map results",
                 "parameters": {"action": "get_text"},
                 "critical": True},
            ]
        }

    # Weather
    if "weather" in g:
        # Extract city/location from the goal
        city = re.sub(r"(what'?s|what is|the|weather|in|check|how'?s|how is|forecast|for)\s*",
                      "", g, flags=re.IGNORECASE).strip() or "my location"
        return {
            "goal": goal,
            "steps": [
                {"step": 1, "tool": "browser", "description": f"Search Google for weather in {city}",
                 "parameters": {"action": "go_to",
                                "url": f"https://www.google.com/search?q=weather+{quote_plus(city)}"},
                 "critical": True},
                {"step": 2, "tool": "browser", "description": "Parse temperature from HTML",
                 "parameters": {"action": "parse_html", "known_key": "google_weather_temp",
                                "attribute": "text"},
                 "critical": False},
                {"step": 3, "tool": "browser", "description": "Read weather visually if HTML failed",
                 "parameters": {"action": "vision_read",
                                "question": "What is the current temperature and weather conditions?"},
                 "condition": "only if step 2 found nothing",
                 "critical": False},
            ]
        }

    # Reminder / timer / alarm
    if any(w in g for w in ["remind", "reminder", "alarm", "timer"]):
        return {
            "goal": goal,
            "steps": [{"step": 1, "tool": "reminder",
                        "description": "Set a reminder",
                        "parameters": {"message": goal, "date": "", "time": ""},
                        "critical": True}]
        }

    # Screenshot
    if "screenshot" in g:
        return {
            "goal": goal,
            "steps": [{"step": 1, "tool": "os_control",
                        "description": "Take a screenshot",
                        "parameters": {"action": "screenshot"},
                        "critical": True}]
        }

    # Generic browser search
    return {
        "goal": goal,
        "steps": [
            {"step": 1, "tool": "browser",
             "description": f"Search for: {goal}",
             "parameters": {"action": "go_to",
                            "url": f"https://www.google.com/search?q={quote_plus(goal)}"},
             "critical": True},
            {"step": 2, "tool": "browser",
             "description": "Read search results",
             "parameters": {"action": "get_text"},
             "critical": True}
        ]
    }



def create_plan(goal: str, context: str = "") -> dict:
    """
    Creates a plan for the given goal.
    Retries up to 3 times on 429. Falls back to keyword detection if all fail.
    """
    from google import genai

    client = genai.Client(api_key=_get_api_key())

    user_input = f"Goal: {goal}"
    if context:
        user_input += f"\n\nAdditional context: {context}"

    last_error = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_input,
                config={"system_instruction": PLANNER_PROMPT}
            )
            text = response.text.strip()
            text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
            plan = json.loads(text)

            if "steps" not in plan or not isinstance(plan["steps"], list):
                raise ValueError("Invalid plan structure — missing steps list.")

            valid_tools = {"browser", "vision", "computer", "terminal", "os_control",
                           "open_app", "file_controller", "reminder"}
            for step in plan["steps"]:
                if step.get("tool") not in valid_tools:
                    print(f"[Planner] ⚠️ Invalid tool '{step.get('tool')}' in step "
                          f"{step.get('step')} — replacing with browser get_text")
                    step["tool"]       = "browser"
                    step["parameters"] = {"action": "get_text"}

            print(f"[Planner] ✅ Plan: {len(plan['steps'])} steps for: {goal[:50]}")
            for s in plan["steps"]:
                cond = f" [if: {s['condition'][:40]}]" if "condition" in s else ""
                print(f"  Step {s['step']}: [{s['tool']}] {s['description'][:60]}{cond}")

            return plan

        except json.JSONDecodeError as e:
            print(f"[Planner] ⚠️ JSON parse failed (attempt {attempt+1}/3): {e}")
            last_error = e
            time.sleep(1)
            continue

        except Exception as e:
            last_error = e
            if "429" in str(e) or "quota" in str(e).lower():
                delay = _extract_retry_delay(str(e))
                print(f"[Planner] ⏳ Rate limit (attempt {attempt+1}/3). Waiting {delay}s...")
                time.sleep(delay)
                continue
            print(f"[Planner] ⚠️ Planning failed: {e}")
            break

    print(f"[Planner] 🔄 Falling back to keyword plan (last error: {last_error})")
    return _fallback_plan_from_keywords(goal)


def replan(goal: str, completed_steps: list, failed_step: dict, error: str,
           results_context: str = "") -> dict:
    """Creates a revised plan after a failure, covering only remaining work."""
    from google import genai

    client = genai.Client(api_key=_get_api_key())

    completed_summary = "\n".join(
        f"  Step {s.get('step')} ({s.get('tool')}): DONE"
        for s in completed_steps
    )

    prompt = (
        f"Goal: {goal}\n\n"
        f"Already completed:\n{completed_summary or '  (none)'}\n\n"
        f"Failed step: [{failed_step.get('tool')}] {failed_step.get('description')}\n"
        f"Error: {error[:300]}\n\n"
    )
    if results_context:
        prompt += f"Data gathered so far:\n{results_context[:1000]}\n\n"
    prompt += (
        f"Create a REVISED plan for the REMAINING work only. Do not repeat completed steps. "
        f"Use only browser, vision, computer, terminal, os_control tools. "
        f"Remember: always insert wait_for_content before parse_html/get_text on JS-heavy sites. "
        f"For task managers (Todoist, Notion, Trello etc): use get_text not vision_read."
    )

    last_error = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={"system_instruction": PLANNER_PROMPT}
            )
            text = response.text.strip()
            text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
            plan = json.loads(text)

            # Structure validation
            if "steps" not in plan or not isinstance(plan.get("steps"), list):
                raise ValueError("Invalid replan structure — missing steps list.")

            valid_tools = {"browser", "vision", "computer", "terminal", "os_control",
                           "open_app", "file_controller", "reminder"}
            for step in plan.get("steps", []):
                if step.get("tool") not in valid_tools:
                    step["tool"]       = "browser"
                    step["parameters"] = {"action": "get_text"}

            print(f"[Planner] 🔄 Revised plan: {len(plan.get('steps', []))} steps")
            return plan

        except Exception as e:
            last_error = e
            if "429" in str(e) or "quota" in str(e).lower():
                delay = _extract_retry_delay(str(e))
                print(f"[Planner] ⏳ Rate limit (replan attempt {attempt+1}/3). Waiting {delay}s...")
                time.sleep(delay)
                continue
            break

    print(f"[Planner] ⚠️ Replan failed: {last_error} — keyword fallback")
    return _fallback_plan_from_keywords(goal)
