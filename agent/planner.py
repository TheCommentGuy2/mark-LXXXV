# agent/planner.py
# JARVIS — Planning Module
#
# Receives a goal and produces a JSON plan of steps using the five primitives.
# Handles rate limiting (429) with retry + fallback keyword detection.
# Supports conditional steps via "condition" field.

import json
import re
import sys
import time
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


PLANNER_PROMPT = """You are the planning module of JARVIS, a personal AI assistant.
Your job: break any user goal into a sequence of steps using ONLY the five tools listed below.

═══════════════════════════════════════════════════════
HOW TO THINK
═══════════════════════════════════════════════════════

STEP A — Understand the actual intent.
Strip the phrasing. Respond to the real need, not the surface words.
"check YouTube for something relaxing" → play music on YouTube
"what do I have tomorrow?" → check Google Classroom To-do for tomorrow specifically

STEP B — Classify.
Can this be done in ONE tool call? → call that tool directly (do NOT create agent_task for it).
Does it need multiple steps where one result feeds the next? → create a plan.

STEP C — Choose the right tier for each step.
Tier 0: No reading. OS controls, terminal tasks, launching apps → instant, no browser.
Tier 1: URL construction. Build the URL and navigate directly. Zero extra calls.
Tier 2: HTML parsing. fetch_html + parse_html. PREFERRED for finding links. Exact and free.
Tier 3: get_text. All visible text. For reading content you will speak back.
Tier 4: vision_read. Screenshot + Gemini question. Costs one API call. Use deliberately.
Tier 5: Vision loop. Multiple vision+computer iterations. Only for complex UI.

ROUTING RULES:
- Single tool = call directly. NEVER route these to agent_task:
  any OS setting, any single terminal command, launching an app
- Multiple steps or conditional logic = agent_task with a plan
- Conditional steps: add "condition" field: "only if step N found X"
  If condition is false, executor skips that step and speaks a natural explanation.

═══════════════════════════════════════════════════════
AVAILABLE TOOLS AND THEIR PARAMETERS
═══════════════════════════════════════════════════════

browser
  action: go_to | construct_url | fetch_html | parse_html | get_text |
          vision_read | click | type | scroll | press | get_url | back |
          reload | new_tab | close_tab | close
  url: string (for go_to)
  service: string (for construct_url: google, youtube, youtube_by_views,
           google_flights, google_maps, gmail, google_classroom, classroom_todo,
           whatsapp, wikipedia, amazon, weather)
  query, origin, destination, date: string (service-specific for construct_url)
  selector: CSS selector (for parse_html)
  known_key: string (for parse_html: youtube_video_link, google_first_result,
             google_weather_temp, wikipedia_content)
  attribute: "href" (default) | "text" | "src" (for parse_html)
  question: string (for vision_read — specific, answerable question)
  text: string (for click/type)
  description: string (for click by description)
  direction: "up" | "down" (for scroll)
  key: string (for press: Enter, Escape, Tab)
  limit: int (for parse_html, default 5)

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
  description: string (for screen_find/screen_click — AI fallback only)
  NOTE: screen_find and screen_click cost a Gemini API call each.
        Use browser parse_html instead whenever you need a URL or link.

terminal
  task: string (natural language description of what to do)
  command: string (exact command — skips AI generation)
  visible: bool (open visible terminal, default auto)
  timeout: int (seconds)
  url: string (for download tasks)
  destination: string (output path)
  input_file: string (for conversion tasks)
  output_file: string (for conversion tasks)

os_control
  action: volume_set | volume_up | volume_down | mute | unmute |
          brightness_set | brightness_up | brightness_down |
          dark_mode | toggle_dark_mode | light_mode |
          toggle_wifi | wifi_on | wifi_off |
          sleep_display | turn_off_screen |
          lock_screen | lock |
          restart | restart_computer |
          shutdown | shut_down |
          minimize | maximize | full_screen | snap_left | snap_right |
          switch_window | show_desktop |
          screenshot | take_screenshot |
          task_manager | file_explorer
  description: string (natural language if action not specified — any language)
  value: int (for volume_set and brightness_set: 0-100)

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
Reasoning: Tier 1 construct weather URL. Tier 2 parse HTML for temperature.
Tier 4 fallback if parse returns nothing (JS-rendered).
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

Goal: "Play lo-fi music on YouTube"
Reasoning: Tier 1 YouTube search URL. Tier 2 parse HTML for first video link.
Tier 1 navigate to that video. No coordinate clicking.
{
  "goal": "Play lo-fi music on YouTube",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Search YouTube for lo-fi music",
     "parameters": {"action": "go_to", "url": "https://www.youtube.com/results?search_query=lo-fi+music"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Parse first video link from results",
     "parameters": {"action": "parse_html", "known_key": "youtube_video_link",
                    "attribute": "href", "limit": 1},
     "critical": true},
    {"step": 3, "tool": "browser", "description": "Navigate to the video",
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
Reasoning: Tier 1 Gmail URL. Unread state is visual (bold), not in raw HTML.
Tier 4 vision_read with specific question.
{
  "goal": "Check Gmail for urgent emails",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open Gmail",
     "parameters": {"action": "go_to", "url": "https://mail.google.com/"}, "critical": true},
    {"step": 2, "tool": "browser", "description": "Read inbox visually for unread emails",
     "parameters": {"action": "vision_read",
                    "question": "List all unread emails showing sender name and subject. Mark each UNREAD or READ."},
     "critical": true}
  ]
}

---

Goal: "What assignments do I have due tomorrow on Google Classroom?"
Reasoning: Go to To-do section specifically (not homepage — incomplete).
Tier 3 get_text to read all assignments. Filter for tomorrow in synthesis.
{
  "goal": "Assignments due tomorrow on Google Classroom",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open Google Classroom To-do page",
     "parameters": {"action": "go_to", "url": "https://classroom.google.com/a/not-turned-in/all"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Read all assignments and due dates",
     "parameters": {"action": "get_text"}, "critical": true}
  ]
}

---

Goal: "Check Classroom and submit Mr Omar's assignment if there is one"
Reasoning: Tier 1 navigate. Tier 4 vision_read to check if assignment exists (conditional).
If found: interact. If not: skip and speak natural message.
{
  "goal": "Submit Mr Omar assignment if it exists",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open Google Classroom",
     "parameters": {"action": "go_to", "url": "https://classroom.google.com/"}, "critical": true},
    {"step": 2, "tool": "browser", "description": "Check if Mr Omar has a pending assignment",
     "parameters": {"action": "vision_read",
                    "question": "Is there a pending assignment posted by Mr Omar or from Mr Omar's class? Say NOT FOUND if none."},
     "critical": true},
    {"step": 3, "tool": "browser", "description": "Click Mr Omar's assignment",
     "parameters": {"action": "click", "description": "Mr Omar assignment"},
     "condition": "only if step 2 found an assignment from Mr Omar",
     "critical": false},
    {"step": 4, "tool": "browser", "description": "Click submit button",
     "parameters": {"action": "click", "description": "Turn in or Submit button"},
     "condition": "only if step 2 found an assignment from Mr Omar",
     "critical": false}
  ]
}

---

Goal: "Send Ahmed a WhatsApp message: I'll be 10 minutes late"
Reasoning: Tier 1 WhatsApp Web (already logged in). Tier 4 vision_read to find contact.
Tier 5 interaction: click chat, type, send.
{
  "goal": "Send WhatsApp message to Ahmed",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open WhatsApp Web",
     "parameters": {"action": "go_to", "url": "https://web.whatsapp.com/"}, "critical": true},
    {"step": 2, "tool": "browser", "description": "Find Ahmed in contacts",
     "parameters": {"action": "vision_read",
                    "question": "Find Ahmed in the contacts or recent chats list. Describe his location on screen."},
     "critical": true},
    {"step": 3, "tool": "computer", "description": "Click Ahmed's chat",
     "parameters": {"action": "screen_click", "description": "Ahmed chat in WhatsApp"},
     "condition": "only if step 2 found Ahmed",
     "critical": false},
    {"step": 4, "tool": "computer", "description": "Type the message",
     "parameters": {"action": "type", "text": "I'll be 10 minutes late"},
     "condition": "only if step 2 found Ahmed",
     "critical": false},
    {"step": 5, "tool": "computer", "description": "Press Enter to send",
     "parameters": {"action": "press", "key": "Return"},
     "condition": "only if step 2 found Ahmed",
     "critical": false}
  ]
}

---

Goal: "Find cheap flights from Istanbul to London on Friday March 27"
Reasoning: Tier 1 Google Flights URL with date. Tier 3 get_text for prices.
{
  "goal": "Flights Istanbul to London March 27",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open Google Flights for the route",
     "parameters": {"action": "go_to",
                    "url": "https://www.google.com/travel/flights?q=Flights+from+Istanbul+to+London+on+2026-03-27"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Read flight prices and options",
     "parameters": {"action": "get_text"}, "critical": true}
  ]
}

---

Goal: "Research quantum computing and save a summary to my desktop"
Reasoning: Tier 1 URLs for multiple sources. Tier 3 get_text each.
Tier 0 terminal to write the file.
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

Goal: "Convert ~/Downloads/hero.mp4 to FLAC"
Reasoning: First verify file exists. Then convert. Verify output. Tier 0 terminal.
{
  "goal": "Convert hero.mp4 to FLAC",
  "steps": [
    {"step": 1, "tool": "terminal", "description": "Check if hero.mp4 exists in Downloads",
     "parameters": {"task": "check if ~/Downloads/hero.mp4 exists",
                    "command": "if exist \"%USERPROFILE%\\Downloads\\hero.mp4\" (echo FOUND) else (echo NOT FOUND)",
                    "visible": false},
     "critical": true},
    {"step": 2, "tool": "terminal", "description": "Convert to FLAC with ffmpeg",
     "parameters": {"task": "convert mp4 to flac",
                    "input_file": "~/Downloads/hero.mp4",
                    "output_file": "~/Downloads/hero.flac",
                    "visible": true},
     "condition": "only if step 1 found the file",
     "critical": true},
    {"step": 3, "tool": "terminal", "description": "Verify output file was created",
     "parameters": {"task": "check if hero.flac exists in Downloads",
                    "command": "if exist \"%USERPROFILE%\\Downloads\\hero.flac\" (echo SUCCESS) else (echo FAILED)",
                    "visible": false},
     "condition": "only if step 1 found the file",
     "critical": false}
  ]
}

---

Goal: "Find the most viewed Interstellar edit on YouTube and download it"
Reasoning: Sort by views (not default relevance). Parse HTML for first result.
Ask user about save location. yt-dlp download. Fallback if it fails.
{
  "goal": "Download most viewed Interstellar edit from YouTube",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Search YouTube sorted by view count",
     "parameters": {"action": "go_to",
                    "url": "https://www.youtube.com/results?search_query=Interstellar+edit&sp=CAM%3D"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Get first video URL and title from sorted results",
     "parameters": {"action": "parse_html", "known_key": "youtube_video_link",
                    "attribute": "href", "limit": 1},
     "critical": true},
    {"step": 3, "tool": "terminal", "description": "Download video with yt-dlp to Desktop",
     "parameters": {"task": "download youtube video",
                    "destination": "~/Desktop/%(title)s.%(ext)s",
                    "visible": true},
     "critical": true}
  ]
}

---

Goal: "Get a route from Kadıköy to Beşiktaş"
Reasoning: Tier 1 Google Maps URL. Tier 3 get_text for duration.
{
  "goal": "Route from Kadıköy to Beşiktaş",
  "steps": [
    {"step": 1, "tool": "browser", "description": "Open Google Maps route",
     "parameters": {"action": "go_to",
                    "url": "https://www.google.com/maps/dir/Kad%C4%B1k%C3%B6y/Be%C5%9Fikta%C5%9F"},
     "critical": true},
    {"step": 2, "tool": "browser", "description": "Read travel duration from page",
     "parameters": {"action": "get_text"}, "critical": true}
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
      "tool": "browser|vision|computer|terminal|os_control",
      "description": "what this step does",
      "parameters": {},
      "condition": "optional — only if step N found X",
      "critical": true
    }
  ]
}

RULES:
- Max 8 steps. Minimum steps to accomplish the goal.
- Only use the 5 tools listed above.
- For conditional steps, always add a "condition" field.
- Steps that depend on another step's result: the executor will inject context automatically.
- Do NOT write the previous step's result into parameters literally — leave url "" or content ""
  and the executor will fill it from the prior result.
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
    """
    Keyword-based fallback plan when Gemini is unavailable.
    Avoids making any API call.
    """
    g = goal.lower()

    # Media downloads
    if any(w in g for w in ["download", "mp3", "youtube", "yt-dlp"]):
        url_m = re.search(r"https?://\S+", goal)
        url   = url_m.group(0) if url_m else ""
        return {
            "goal": goal,
            "steps": [{
                "step": 1, "tool": "terminal",
                "description": "Download with yt-dlp",
                "parameters": {"task": "download youtube video as mp3",
                               "url": url, "visible": True},
                "critical": True
            }]
        }

    # File conversion
    if any(w in g for w in ["convert", "ffmpeg", "mp4", "flac", "mp3", "wav"]):
        return {
            "goal": goal,
            "steps": [{
                "step": 1, "tool": "terminal",
                "description": "Convert file with ffmpeg",
                "parameters": {"task": goal, "visible": True},
                "critical": True
            }]
        }

    # OS controls
    if any(w in g for w in ["volume", "brightness", "dark mode", "wifi", "lock",
                              "shutdown", "restart", "mute"]):
        return {
            "goal": goal,
            "steps": [{
                "step": 1, "tool": "os_control",
                "description": "OS control",
                "parameters": {"description": goal},
                "critical": True
            }]
        }

    # Generic browser search
    return {
        "goal": goal,
        "steps": [{
            "step": 1, "tool": "browser",
            "description": f"Search for: {goal}",
            "parameters": {"action": "go_to",
                           "url": f"https://www.google.com/search?q={quote_plus(goal)}"},
            "critical": True
        }, {
            "step": 2, "tool": "browser",
            "description": "Read search results",
            "parameters": {"action": "get_text"},
            "critical": True
        }]
    }


try:
    from urllib.parse import quote_plus
except ImportError:
    def quote_plus(s): return s.replace(" ", "+")


def create_plan(goal: str, context: str = "") -> dict:
    """
    Creates a plan for the given goal.
    Retries up to 3 times on 429 rate limit errors.
    Falls back to keyword detection if all retries fail.
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

            # Safety: ensure only valid tools are used
            valid_tools = {"browser", "vision", "computer", "terminal", "os_control"}
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
            print(f"[Planner] ⚠️ JSON parse failed: {e}")
            last_error = e
            break  # JSON error won't fix with retry

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


def replan(goal: str, completed_steps: list, failed_step: dict, error: str) -> dict:
    """
    Creates a revised plan after a failure, covering only remaining work.
    Retries up to 3 times on 429.
    """
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
        f"Create a REVISED plan for the REMAINING work only. Do not repeat completed steps. "
        f"Use only browser, vision, computer, terminal, os_control tools."
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

            valid_tools = {"browser", "vision", "computer", "terminal", "os_control"}
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
