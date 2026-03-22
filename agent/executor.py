# agent/executor.py
# JARVIS — Execution Module
#
# Runs a plan step by step with:
#   - Context enrichment: prior results automatically injected into upcoming steps
#   - Condition evaluation: conditional steps skipped with natural spoken explanation
#   - Step verification: lightweight string-based, ZERO API calls burned
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
        desc = parameters.get("description", parameters.get("task", str(parameters)))
        return terminal(parameters={"task": f"Accomplish: {desc}"}, player=None) or "Done."


# ─────────────────────────────────────────────────────────────
# CONTEXT ENRICHMENT
# ─────────────────────────────────────────────────────────────

_PLACEHOLDER_SIGNALS = [
    "[username]", "[track-slug]", "[slug]", "[id]", "[user]",
    "[artist]", "[song]", "[video-id]", "[example]", "[name]",
    "example.com", "your-", "placeholder", "INSERT_", "<username>",
    "<track>", "<id>", "{username}", "{track}", "{id}",
]

_BORING_RESULTS = {
    "Done.", "Completed.", "Task cancelled.",
    "Page fully loaded and network idle.",
    "Wait complete (network did not fully idle — page may be partially loaded).",
    "Scrolled down.", "Scrolled up.",
}


def _extract_url_from_result(result: str) -> str | None:
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
            if "[" in url or "{" in url:
                continue
            return url
    except Exception:
        pass

    for match in re.finditer(r"https?://[^\s\"'<>\)\]]+", result):
        url = match.group(0).rstrip(".,;)`")
        if any(sig in url for sig in _PLACEHOLDER_SIGNALS):
            continue
        if "[" in url or "{" in url or "`" in url:
            continue
        return url

    return None


def _inject_context(params: dict, tool: str, step_results: dict, goal: str = "") -> dict:
    if not step_results:
        return params

    params = dict(params)

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
                   if v and len(v) > 20 and v not in _BORING_RESULTS]

    if not all_results:
        return params

    latest = all_results[-1]

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

    if tool == "terminal":
        # [CONTENT] injection — must happen before early return
        cmd = params.get("command", "")
        if "[CONTENT]" in cmd:
            parts = [
                v for v in (step_results.get(k) for k in sorted(step_results))
                if v and v not in _BORING_RESULTS and len(v) > 30
            ]
            combined = "\n\n---\n\n".join(parts[:4])
            if combined:
                translated = _translate_to_goal_language(combined, goal)
                params["command"] = cmd.replace(
                    "[CONTENT]", translated[:2000].replace('"', "'")
                )
                print(f"[Executor] 💉 Injected file content ({len(combined)} chars)")

        if not params.get("url") and not params.get("command"):
            extracted = _extract_url_from_result(latest)
            if extracted and any(x in extracted for x in ["youtube", "soundcloud",
                                                            "youtu.be", "vimeo"]):
                params["url"] = extracted
                print(f"[Executor] 💉 Injected download URL: {extracted[:80]}")
        return params

    if tool == "file_controller":
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
    if not goal or len(content) < 50:
        return content
    try:
        from google import genai
        client = genai.Client(api_key=_get_api_key())
        lang_response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=f"What language is this text? Reply with ONLY the language name.\n\nText: {goal[:200]}"
        )
        lang = lang_response.text.strip()
        if lang.lower() == "english":
            return content
        trans_response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=f"Translate to {lang}. Keep all facts intact. Output ONLY the translation.\n\n{content[:3000]}"
        )
        return trans_response.text.strip()
    except Exception as e:
        print(f"[Executor] ⚠️ Translation skipped: {e}")
        return content


# ─────────────────────────────────────────────────────────────
# STEP VERIFICATION — string-based, ZERO API calls
# ─────────────────────────────────────────────────────────────
#
# The previous version called Gemini with a screenshot after every browser
# action. That burned 1–3 API calls per step and hit rate limits within
# a single task. This version checks the return string only — catches
# every real failure without touching the API at all.

_FAILURE_SIGNALS = [
    "navigation error:", "timeout:", "click failed:", "type failed:",
    "could not fetch", "browser error:", "playwright timeout",
    "net::err_", "err_name_not_resolved", "refused to connect",
    "please provide a url", "unknown browser action", "vision read failed",
    "not found", "element not found", "no results",
]

_SUCCESS_SIGNALS = [
    "navigated to: https://", "navigated to: http://",
    "clicked", "typed", "pressed:", "scrolled",
    "page fully loaded", "wait complete", '{"found":',
]


def _verify_step(tool: str, params: dict, step_result: str,
                 step_description: str) -> tuple[bool, str]:
    """String-based verification. No API calls."""
    step_result = step_result or ""
    result_lower = step_result.lower().strip()

    for sig in _FAILURE_SIGNALS:
        if sig in result_lower:
            return False, f"Failure signal '{sig}': {step_result[:120]}"

    for sig in _SUCCESS_SIGNALS:
        if sig in result_lower:
            return True, ""

    if len(step_result.strip()) > 5:
        return True, ""

    action = params.get("action", "")
    if action in ("go_to", "click", "type") and not step_result.strip():
        return False, "Step returned empty result"

    return True, ""


def _should_verify(tool: str, params: dict) -> bool:
    if tool != "browser":
        return False
    return params.get("action") in ("go_to", "click", "type", "press")


# ─────────────────────────────────────────────────────────────
# CONDITION EVALUATION
# ─────────────────────────────────────────────────────────────

def _evaluate_condition(condition: str, step_results: dict) -> bool:
    if not condition:
        return True

    all_results_text = " ".join(str(v) for v in step_results.values())
    not_found_signals = [
        "NOT FOUND", "not found", "no assignment", "nothing found",
        "couldn't find", "could not find", "doesn't exist", "does not exist",
        "no emails", "no results", "no tasks", "count\": 0", "\"found\": []"
    ]

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
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                f"Based on these prior step results, is this condition TRUE or FALSE?\n\n"
                f"Condition: {condition}\n\n"
                f"Prior results:\n{results_summary}\n\n"
                f"Reply with ONLY: TRUE or FALSE"
            )
        )
        result = "TRUE" in response.text.strip().upper()
        print(f"[Executor] 🔀 Condition '{condition[:40]}' → {result}")
        return result
    except Exception as e:
        print(f"[Executor] ⚠️ Condition evaluation failed: {e} — defaulting to True")
        return True


def _generate_condition_false_message(condition: str, goal: str) -> str:
    try:
        from google import genai
        client = genai.Client(api_key=_get_api_key())
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                f"Generate a short natural spoken sentence (1-2 sentences) explaining "
                f"that this condition was not met, so remaining steps were skipped. "
                f"Address the user as 'sir'. Be specific about what was not found.\n\n"
                f"Condition: {condition}\nGoal: {goal}"
            )
        )
        return response.text.strip()
    except Exception:
        return "I could not complete the task, sir — the required condition was not met."


# ─────────────────────────────────────────────────────────────
# SUMMARY GENERATION
# ─────────────────────────────────────────────────────────────

def _build_raw_summary(goal: str, step_results: dict) -> str:
    """
    No-API fallback summary. Just returns the most informative step result
    directly — used when Gemini is rate-limited so the user still gets the data.
    """
    useful = [
        v for v in step_results.values()
        if v and v not in _BORING_RESULTS and len(v) > 30
    ]
    if not useful:
        return f"Task complete, sir: {goal[:60]}."
    # Return the longest result — most likely to be the actual content
    best = max(useful, key=len)
    # Trim to something speakable
    trimmed = best[:600].strip()
    return f"Here's what I found, sir: {trimmed}"


def _generate_summary(goal: str, completed_steps: list, step_results: dict,
                       speak: Callable | None) -> str:
    fallback = _build_raw_summary(goal, step_results)

    try:
        from google import genai
        client = genai.Client(api_key=_get_api_key())

        steps_str   = "\n".join(f"- {s.get('description', '')}" for s in completed_steps)
        results_str = "\n".join(
            f"Step {k} result: {str(v)[:400]}"
            for k, v in step_results.items()
            if v and v not in _BORING_RESULTS
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                f'User goal: "{goal}"\n\n'
                f"Steps completed:\n{steps_str}\n\n"
                f"Results obtained:\n{results_str}\n\n"
                "Write 1-2 natural sentences summarizing what was accomplished. "
                "If real data was obtained (tasks, prices, times, names), include ALL "
                "specific items — do not omit any. "
                "Do NOT say 'I completed N steps'. Address the user as 'sir'. "
                "Respond in the same language as the goal."
            )
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
    """Auto-inserts wait_for_content before parse_html/get_text on JS-heavy sites."""
    JS_HEAVY_DOMAINS = [
        "classroom.google.com", "soundcloud.com", "youtube.com", "youtu.be",
        "mail.google.com", "drive.google.com", "docs.google.com",
        "app.todoist.com", "todoist.com",
        "notion.so", "figma.com", "canva.com",
        "trello.com", "asana.com", "linear.app", "clickup.com",
        "monday.com", "airtable.com", "app.slack.com",
    ]

    steps     = plan.get("steps", [])
    new_steps = []
    inserted  = set()

    for step in steps:
        tool   = step.get("tool", "")
        params = step.get("parameters", {})
        action = params.get("action", "")

        needs_wait = False
        if tool == "browser" and action in ("parse_html", "get_text"):
            for prev in reversed(new_steps):
                if (prev.get("tool") == "browser" and
                        prev.get("parameters", {}).get("action") == "go_to"):
                    url = prev.get("parameters", {}).get("url", "")
                    if any(domain in url for domain in JS_HEAVY_DOMAINS):
                        needs_wait = True
                    break  # always stop at most recent go_to

        step_num = step.get("step", len(new_steps) + 1)

        if needs_wait and step_num not in inserted:
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
        condition_results: dict[str, bool] = {}   # per-condition cache
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
                if condition:
                    # Evaluate each unique condition string only once
                    if condition not in condition_results:
                        satisfied = _evaluate_condition(condition, step_results)
                        condition_results[condition] = satisfied
                        if not satisfied:
                            msg = _generate_condition_false_message(condition, goal)
                            if speak:
                                speak(msg)
                            print(f"[Executor] 🔀 Condition '{condition[:40]}' false — skipping step {step_num}")
                    if not condition_results[condition]:
                        print(f"[Executor] ⏭️ Skipping step {step_num} (condition false: '{condition[:40]}')")
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

                        if _should_verify(tool, params):
                            verified, issue = _verify_step(tool, params, result, desc)
                            if not verified:
                                raise RuntimeError(f"Step verified as failed: {issue}")

                        completed_steps.append(step)
                        print(f"[Executor] ✅ Step {step_num}: {str(result)[:100]}")
                        step_ok = True
                        break

                    except Exception as e:
                        error_msg = str(e)
                        print(f"[Executor] ❌ Step {step_num} attempt {attempt} failed: {error_msg[:100]}")

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
                                    res = _call_tool(fixed_step["tool"],
                                                     fixed_step["parameters"], speak)
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
                if any(not v for v in condition_results.values()):
                    return "Condition not met — task partially completed as explained."
                return _generate_summary(goal, completed_steps, step_results, speak)

            if replan_attempts >= self.MAX_REPLAN_ATTEMPTS:
                msg = (f"Task failed after {replan_attempts + 1} attempts, sir. "
                       f"The step '{failed_step.get('description', '')}' could not be completed.")
                if speak:
                    speak(msg)
                return msg

            if speak:
                speak("Adjusting my approach, sir.")

            replan_attempts += 1
            results_context = "\n".join(
                f"Step {k}: {str(v)[:200]}" for k, v in step_results.items()
                if v and v not in _BORING_RESULTS
            )
            plan = _preprocess_plan(replan(goal, completed_steps, failed_step,
                                           failed_error, results_context=results_context))
            step_results.clear()
            condition_results.clear()
