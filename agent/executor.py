# agent/executor.py
# JARVIS — Execution Module
#
# Runs a plan step by step with:
#   - Context enrichment: prior results automatically injected into upcoming steps
#   - Condition evaluation: conditional steps skipped with natural spoken explanation
#   - Step verification: after key browser steps, screenshots to confirm success
#   - Error recovery: retry, skip, replan via error_handler
#   - Natural summary: synthesizes real data found, not just "done N steps"

import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from agent.planner       import create_plan, replan
from agent.error_handler import analyze_error, generate_fix, ErrorDecision


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _extract_retry_delay(error_str: str) -> int:
    match = re.search(r"retry.*?(\d+)\s*second", str(error_str), re.IGNORECASE)
    if match:
        return min(int(match.group(1)), 60)
    return 8


# ─────────────────────────────────────────────────────────────
# TOOL DISPATCH
# ─────────────────────────────────────────────────────────────

def _call_tool(tool: str, parameters: dict, speak: Callable | None) -> str:
    """Routes a step to the appropriate action function."""

    if tool == "browser":
        from actions.browser import browser
        return browser(parameters=parameters, player=None) or "Done."

    elif tool == "vision":
        from actions.vision import vision
        return vision(parameters=parameters, player=None) or "Done."

    elif tool == "computer":
        from actions.computer import computer
        return computer(parameters=parameters, player=None) or "Done."

    elif tool == "terminal":
        from actions.terminal import terminal
        return terminal(parameters=parameters, player=None) or "Done."

    elif tool == "os_control":
        from actions.os_control import os_control
        return os_control(parameters=parameters, player=None) or "Done."

    elif tool == "screen_process":
        from actions.screen_processor import screen_process
        screen_process(parameters=parameters, player=None)
        return "Screen captured and analyzed."

    elif tool == "file_controller":
        try:
            from actions.file_controller import file_controller
            return file_controller(parameters=parameters, player=None) or "Done."
        except ImportError:
            from actions.terminal import terminal
            return terminal(parameters={"task": json.dumps(parameters)}, player=None)

    elif tool == "reminder":
        try:
            from actions.reminder import reminder
            return reminder(parameters=parameters, player=None) or "Done."
        except ImportError:
            return "Reminder module not available."

    elif tool == "open_app":
        try:
            from actions.open_app import open_app
            return open_app(parameters=parameters, player=None) or "Done."
        except ImportError:
            from actions.terminal import terminal
            app = parameters.get("app_name", "")
            return terminal(parameters={"task": f"open {app}", "visible": False}) or "Done."

    else:
        print(f"[Executor] ⚠️ Unknown tool '{tool}' — falling back to terminal")
        from actions.terminal import terminal
        return terminal(parameters={"task": f"Accomplish: {parameters}"}, player=None) or "Done."


# ─────────────────────────────────────────────────────────────
# CONTEXT ENRICHMENT
# ─────────────────────────────────────────────────────────────

# Strings that indicate Gemini produced a placeholder URL rather than a real one.
# These are rejected during URL extraction so they never get navigated to.
_PLACEHOLDER_SIGNALS = [
    "[username]", "[track-slug]", "[slug]", "[id]", "[user]",
    "[artist]", "[song]", "[video-id]", "[example]", "[name]",
    "example.com", "your-", "placeholder", "INSERT_", "<username>",
    "<track>", "<id>", "{username}", "{track}", "{id}",
]


def _extract_url_from_result(result: str) -> str | None:
    """
    Extract first real URL from a result string.
    Rejects placeholder/template URLs that Gemini sometimes produces
    when vision_read describes a URL structure rather than reading
    an actual href value.
    """
    # parse_html / JS eval returns JSON: {"found": [{"value": "https://..."}]}
    try:
        data  = json.loads(result)
        found = data.get("found", [])
        for item in found:
            url = item.get("value", "")
            if not url.startswith("http"):
                continue
            if any(sig in url for sig in _PLACEHOLDER_SIGNALS):
                print(f"[Executor] ⚠️ Rejected placeholder URL: {url[:80]}")
                continue
            # Reject URLs with literal brackets (template syntax)
            if "[" in url or "{" in url:
                continue
            return url
    except Exception:
        pass

    # Plain URL in text
    for match in re.finditer(r"https?://[^\s\"'<>\)\]]+", result):
        url = match.group(0).rstrip(".,;)`")
        if any(sig in url for sig in _PLACEHOLDER_SIGNALS):
            print(f"[Executor] ⚠️ Rejected placeholder URL: {url[:80]}")
            continue
        if "[" in url or "{" in url or "`" in url:
            continue
        return url

    return None


def _inject_context(params: dict, tool: str, step_results: dict, goal: str = "") -> dict:
    """
    Enriches step parameters with results from prior steps.
    """
    if not step_results:
        return params

    params = dict(params)  # don't mutate original

    no_enrich    = {"os_control", "screen_process"}
    skip_actions = {"wait", "press", "hotkey", "scroll", "screenshot",
                    "clear_field", "wait_for_content"}
    if tool in no_enrich:
        return params
    if tool == "computer" and params.get("action") in skip_actions:
        return params
    if tool == "browser" and params.get("action") in skip_actions:
        return params

    all_results = [v for v in step_results.values()
                   if v and len(v) > 20 and v not in (
                       "Done.", "Completed.", "Task cancelled.",
                       "Page fully loaded and network idle.",
                       "Wait complete (network did not fully idle — page may be partially loaded)."
                   )]

    if not all_results:
        return params

    latest = all_results[-1]

    # ── Browser go_to: inject URL from prior parse_html result ──
    if tool == "browser" and params.get("action") == "go_to":
        url = params.get("url", "")
        if not url or url == "":
            extracted = _extract_url_from_result(latest)
            if extracted:
                params["url"] = extracted
                print(f"[Executor] 💉 Injected URL: {extracted[:80]}")
            else:
                print(f"[Executor] ⚠️ No valid URL found in prior result — skipping injection")
        return params

    # ── Terminal download: inject URL from prior result ──
    if tool == "terminal":
        if not params.get("url") and not params.get("command"):
            extracted = _extract_url_from_result(latest)
            if extracted and any(x in extracted for x in ["youtube", "soundcloud",
                                                            "youtu.be", "vimeo"]):
                params["url"] = extracted
                print(f"[Executor] 💉 Injected download URL: {extracted[:80]}")
        return params

    # ── File write: inject content from prior search/reading results ──
    if tool in ("file_controller", "terminal"):
        cmd     = params.get("command", "")
        content = params.get("content", "")
        if (not content or len(content) < 30) and "[CONTENT]" in cmd:
            combined   = "\n\n---\n\n".join(all_results[:4])
            translated = _translate_to_goal_language(combined, goal)
            params["command"] = cmd.replace(
                "[CONTENT]", translated[:2000].replace('"', "'")
            )
            print(f"[Executor] 💉 Injected file content ({len(combined)} chars)")

    return params


def _translate_to_goal_language(content: str, goal: str) -> str:
    """Translates content to match the language of the goal."""
    if not goal or len(content) < 50:
        return content
    try:
        from google import genai
        client = genai.Client(api_key=_get_api_key())

        lang_response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(f"What language is this text? Reply with ONLY the language name.\n\n"
                      f"Text: {goal[:200]}")
        )
        lang = lang_response.text.strip()

        if lang.lower() == "english":
            return content

        trans_response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(f"Translate to {lang}. Keep all facts and data intact. "
                      f"Output ONLY the translated text.\n\n{content[:3000]}")
        )
        return trans_response.text.strip()
    except Exception as e:
        print(f"[Executor] ⚠️ Translation skipped: {e}")
        return content


# ─────────────────────────────────────────────────────────────
# STEP VERIFICATION
# ─────────────────────────────────────────────────────────────

# Actions worth verifying — only ones where "it worked" is non-obvious
_VERIFY_ACTIONS = {
    "go_to", "click", "type", "press",
}

# Tools that need no verification (they either report success correctly or
# are fire-and-forget with no meaningful visual state to check)
_SKIP_VERIFY_TOOLS = {
    "terminal", "os_control", "vision", "screen_process",
    "file_controller", "reminder", "open_app",
}


def _should_verify(tool: str, params: dict) -> bool:
    """Returns True if this step is worth taking a screenshot to verify."""
    if tool in _SKIP_VERIFY_TOOLS:
        return False
    if tool == "browser":
        action = params.get("action", "")
        return action in _VERIFY_ACTIONS
    if tool == "computer":
        action = params.get("action", "")
        return action in {"click", "type", "press"}
    return False


def _verify_step(tool: str, params: dict, step_result: str,
                 step_description: str) -> tuple[bool, str]:
    """
    Takes a screenshot of the browser and asks Gemini whether the step
    actually succeeded. Returns (success: bool, explanation: str).

    Only called for browser navigation/interaction steps.
    Skips verification if rate-limited — defaults to trusting the result.
    """
    try:
        from actions.browser import _bt, _bt_started
        if not _bt_started:
            return True, "Browser not started — skipping verify."

        import asyncio, io
        from google import genai
        from google.genai import types

        # Capture the current browser screenshot
        try:
            png_bytes = _bt.run(_bt._get_page().__class__  # type check
                                and asyncio.coroutine,     # not used
                                timeout=1)
        except Exception:
            pass  # fall through to the async approach

        # Use the browser thread's loop to take the screenshot
        import concurrent.futures

        async def _grab():
            page = await _bt._get_page()
            return await page.screenshot(full_page=False)

        try:
            future = asyncio.run_coroutine_threadsafe(_grab(), _bt._loop)
            png_bytes = future.result(timeout=8)
        except Exception as e:
            print(f"[Verify] ⚠️ Screenshot failed: {e}")
            return True, "Could not screenshot — assuming success."

        try:
            import PIL.Image
            img = PIL.Image.open(io.BytesIO(png_bytes)).convert("RGB")
            img.thumbnail([1280, 720], PIL.Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=60)
            image_bytes = buf.getvalue()
        except Exception:
            image_bytes = png_bytes

        client = genai.Client(api_key=_get_api_key())

        action  = params.get("action", "")
        url     = params.get("url", "")
        target  = params.get("description", "") or params.get("text", "") or url

        prompt = (
            f"I just attempted this browser action and got this result.\n\n"
            f"Step description: {step_description}\n"
            f"Action: {action}\n"
            f"Target: {target}\n"
            f"Reported result: {step_result[:200]}\n\n"
            f"Look at the screenshot and answer:\n"
            f"1. Did the action succeed? (YES or NO)\n"
            f"2. If NO, what is the actual state of the page? (1 sentence)\n\n"
            f"Reply in this exact format:\n"
            f"RESULT: YES\n"
            f"or\n"
            f"RESULT: NO\n"
            f"ISSUE: <what went wrong>\n"
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=types.Content(role="user", parts=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                types.Part.from_text(text=prompt)
            ])
        )

        text = response.text.strip()
        success = "RESULT: YES" in text.upper()
        issue   = ""

        if not success:
            m = re.search(r"ISSUE:\s*(.+)", text, re.IGNORECASE)
            issue = m.group(1).strip() if m else "Unknown issue."
            print(f"[Verify] ❌ Step failed verification: {issue}")
        else:
            print(f"[Verify] ✅ Step verified OK")

        return success, issue

    except Exception as e:
        # If verification itself errors (e.g. rate limit), trust the step result
        if "429" in str(e):
            print(f"[Verify] ⏭️ Rate limited — skipping verify, trusting result")
        else:
            print(f"[Verify] ⚠️ Verify error: {e}")
        return True, ""


# ─────────────────────────────────────────────────────────────
# CONDITION EVALUATION
# ─────────────────────────────────────────────────────────────

def _evaluate_condition(condition: str, step_results: dict) -> bool:
    """
    Evaluates whether a step's condition is satisfied based on prior results.
    Returns True if the step should run, False if it should be skipped.
    """
    if not condition:
        return True

    all_results_text = " ".join(str(v) for v in step_results.values())
    not_found_signals = ["NOT FOUND", "not found", "no assignment", "nothing found",
                         "couldn't find", "could not find", "doesn't exist",
                         "does not exist", "no emails", "no results", "no tasks",
                         "count\": 0", "\"found\": []"]

    condition_lower = condition.lower()
    if any(sig in all_results_text.lower() for sig in not_found_signals):
        if any(w in condition_lower for w in ["found", "exists", "has", "shows"]):
            return False

    try:
        from google import genai
        client = genai.Client(api_key=_get_api_key())

        results_summary = "\n".join(
            f"Step {k}: {str(v)[:200]}" for k, v in step_results.items()
        )
        prompt = (
            f"Based on these prior step results, is this condition TRUE or FALSE?\n\n"
            f"Condition: {condition}\n\n"
            f"Prior results:\n{results_summary}\n\n"
            f"Reply with ONLY: TRUE or FALSE"
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt
        )
        answer = response.text.strip().upper()
        result = "TRUE" in answer
        print(f"[Executor] 🔀 Condition '{condition[:40]}' → {result}")
        return result

    except Exception as e:
        print(f"[Executor] ⚠️ Condition evaluation failed: {e} — defaulting to True")
        return True


def _generate_condition_false_message(condition: str, goal: str) -> str:
    """Generates a natural spoken message when a condition is false."""
    try:
        from google import genai
        client = genai.Client(api_key=_get_api_key())

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                f"Generate a short natural spoken sentence (1-2 sentences) explaining "
                f"that this condition was not met, so remaining steps were skipped. "
                f"Address the user as 'sir'. Be specific about what was not found.\n\n"
                f"Condition that was false: {condition}\n"
                f"Overall goal: {goal}"
            )
        )
        return response.text.strip()
    except Exception:
        return f"I could not complete the task, sir — the required condition was not met."


# ─────────────────────────────────────────────────────────────
# SUMMARY GENERATION
# ─────────────────────────────────────────────────────────────

def _generate_summary(goal: str, completed_steps: list, step_results: dict,
                       speak: Callable | None) -> str:
    """
    Generates a natural, informative summary of what was accomplished.
    Includes real data found (prices, times, assignments, etc.).
    """
    fallback = (f"All done, sir. Completed {len(completed_steps)} steps "
                f"for: {goal[:50]}.")

    try:
        from google import genai
        client = genai.Client(api_key=_get_api_key())

        steps_str   = "\n".join(f"- {s.get('description', '')}" for s in completed_steps)
        results_str = "\n".join(
            f"Step {k} result: {str(v)[:300]}"
            for k, v in step_results.items()
            if v and v not in ("Done.", "Completed.", "Screen captured and analyzed.",
                               "Page fully loaded and network idle.")
        )

        prompt = (
            f'User goal: "{goal}"\n\n'
            f"Steps completed:\n{steps_str}\n\n"
            f"Results obtained:\n{results_str}\n\n"
            "Write 1-2 natural sentences summarizing what was accomplished. "
            "If real data was obtained (prices, times, assignments, file paths, "
            "temperatures, track names), include the specific data in the summary. "
            "Do NOT say 'I completed N steps'. Address the user as 'sir'. "
            "Respond in the same language as the goal."
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt
        )
        summary = response.text.strip()
        if speak:
            speak(summary)
        return summary

    except Exception as e:
        print(f"[Executor] ⚠️ Summary failed: {e}")
        if speak:
            speak(fallback)
        return fallback


# ─────────────────────────────────────────────────────────────
# PLAN PREPROCESSING
# ─────────────────────────────────────────────────────────────

def _preprocess_plan(plan: dict) -> dict:
    """
    Rewrites plan steps to insert wait_for_content before parse_html or get_text
    on known JS-heavy sites, so the DOM has time to populate before reading.
    This is what fixes Google Classroom, SoundCloud, YouTube DOM reading.
    """
    JS_HEAVY_DOMAINS = [
        "classroom.google.com",
        "soundcloud.com",
        "youtube.com",
        "youtu.be",
        "mail.google.com",
        "drive.google.com",
        "docs.google.com",
        "notion.so",
        "figma.com",
        "canva.com",
    ]

    steps     = plan.get("steps", [])
    new_steps = []
    inserted  = set()  # track which step numbers already have a wait before them

    for step in steps:
        tool   = step.get("tool", "")
        params = step.get("parameters", {})
        action = params.get("action", "")

        # Check if the previous step navigated to a JS-heavy site
        needs_wait = False
        if tool == "browser" and action in ("parse_html", "get_text"):
            # Look back at previous steps for a go_to to a JS-heavy site
            for prev in new_steps:
                if (prev.get("tool") == "browser" and
                        prev.get("parameters", {}).get("action") == "go_to"):
                    url = prev.get("parameters", {}).get("url", "")
                    if any(domain in url for domain in JS_HEAVY_DOMAINS):
                        needs_wait = True
                        break

        step_num = step.get("step", len(new_steps) + 1)

        if needs_wait and step_num not in inserted:
            # Insert a wait_for_content step before this read step
            wait_step = {
                "step":        f"{step_num}_wait",
                "tool":        "browser",
                "description": "Wait for page JavaScript to finish loading",
                "parameters":  {"action": "wait_for_content", "timeout_ms": 6000},
                "critical":    False,
            }
            if "condition" in step:
                wait_step["condition"] = step["condition"]
            new_steps.append(wait_step)
            inserted.add(step_num)
            print(f"[Executor] 💉 Auto-inserted wait_for_content before step {step_num}")

        new_steps.append(step)

    plan["steps"] = new_steps
    return plan


# ─────────────────────────────────────────────────────────────
# MAIN EXECUTOR
# ─────────────────────────────────────────────────────────────

class AgentExecutor:

    MAX_REPLAN_ATTEMPTS = 2

    def execute(
        self,
        goal:        str,
        speak:       Callable | None        = None,
        cancel_flag: threading.Event | None = None,
    ) -> str:

        print(f"\n[Executor] 🎯 Goal: {goal}")

        replan_attempts  = 0
        completed_steps  = []
        step_results:    dict[int, str] = {}
        condition_spoken = False
        plan             = _preprocess_plan(create_plan(goal))

        while True:
            steps = plan.get("steps", [])

            if not steps:
                msg = "I couldn't create a valid plan for this task, sir."
                if speak:
                    speak(msg)
                return msg

            success      = True
            failed_step  = None
            failed_error = ""

            for step in steps:
                if cancel_flag and cancel_flag.is_set():
                    if speak:
                        speak("Task cancelled, sir.")
                    return "Task cancelled."

                step_num  = step.get("step", "?")
                tool      = step.get("tool", "browser")
                desc      = step.get("description", "")
                params    = dict(step.get("parameters", {}))
                condition = step.get("condition", "")

                # ── Condition check ──────────────────────────────
                if condition and not condition_spoken:
                    satisfied = _evaluate_condition(condition, step_results)
                    if not satisfied:
                        msg = _generate_condition_false_message(condition, goal)
                        if speak:
                            speak(msg)
                        condition_spoken = True
                        print(f"[Executor] 🔀 Condition false — skipping remaining conditional steps")
                        continue
                elif condition and condition_spoken:
                    print(f"[Executor] ⏭️ Skipping step {step_num} (prior condition was false)")
                    continue

                # ── Context enrichment ───────────────────────────
                params = _inject_context(params, tool, step_results, goal=goal)

                print(f"\n[Executor] ▶️ Step {step_num}: [{tool}] {desc}")

                attempt = 1
                step_ok = False

                while attempt <= 3:
                    if cancel_flag and cancel_flag.is_set():
                        break

                    try:
                        result                = _call_tool(tool, params, speak)
                        step_results[step_num] = result

                        # ── Verification ─────────────────────────
                        # Only verify steps where failure isn't self-evident
                        # from the return value (browser navigation/interaction).
                        if _should_verify(tool, params):
                            verified, issue = _verify_step(tool, params, result, desc)
                            if not verified:
                                # Treat verification failure as a step failure
                                raise RuntimeError(
                                    f"Step verified as failed: {issue}"
                                )

                        completed_steps.append(step)
                        print(f"[Executor] ✅ Step {step_num}: {str(result)[:100]}")
                        step_ok = True
                        break

                    except Exception as e:
                        error_msg = str(e)
                        print(f"[Executor] ❌ Step {step_num} attempt {attempt} "
                              f"failed: {error_msg[:100]}")

                        recovery = analyze_error(step, error_msg, attempt=attempt)
                        decision = recovery["decision"]
                        user_msg = recovery.get("user_message", "")

                        if speak and user_msg:
                            speak(user_msg)

                        if decision == ErrorDecision.RETRY:
                            attempt += 1
                            time.sleep(2)
                            continue

                        elif decision == ErrorDecision.SKIP:
                            print(f"[Executor] ⏭️ Skipping step {step_num}")
                            step_results[step_num] = f"SKIPPED: {error_msg[:100]}"
                            completed_steps.append(step)
                            step_ok = True
                            break

                        elif decision == ErrorDecision.ABORT:
                            msg = f"Task aborted, sir. {recovery.get('reason', '')}"
                            if speak:
                                speak(msg)
                            return msg

                        else:  # REPLAN
                            fix_suggestion = recovery.get("fix_suggestion", "")
                            if fix_suggestion and tool not in ("os_control", "computer"):
                                try:
                                    fixed_step = generate_fix(step, error_msg, fix_suggestion)
                                    if speak:
                                        speak("Trying an alternative approach, sir.")
                                    res = _call_tool(
                                        fixed_step["tool"],
                                        fixed_step["parameters"],
                                        speak
                                    )
                                    step_results[step_num] = res
                                    completed_steps.append(step)
                                    step_ok = True
                                    break
                                except Exception as fix_err:
                                    print(f"[Executor] ⚠️ Fix failed: {fix_err}")

                            failed_step  = step
                            failed_error = error_msg
                            success      = False
                            break

                if not step_ok and not failed_step:
                    failed_step  = step
                    failed_error = "Max retries exceeded"
                    success      = False

                if not success:
                    break

            if success:
                if condition_spoken:
                    return "Condition not met — task partially completed as explained."
                return _generate_summary(goal, completed_steps, step_results, speak)

            if replan_attempts >= self.MAX_REPLAN_ATTEMPTS:
                msg = (f"Task failed after {replan_attempts + 1} attempts, sir. "
                       f"The step '{failed_step.get('description', '')}' "
                       f"could not be completed.")
                if speak:
                    speak(msg)
                return msg

            if speak:
                speak("Adjusting my approach, sir.")

            replan_attempts += 1
            plan = _preprocess_plan(replan(goal, completed_steps, failed_step, failed_error))
