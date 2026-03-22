import json
import re
import sys
from pathlib import Path
from enum import Enum


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


class ErrorDecision(Enum):
    RETRY       = "retry"
    SKIP        = "skip"
    REPLAN      = "replan"
    ABORT       = "abort"


ERROR_ANALYST_PROMPT = """You are the error recovery module of JARVIS AI assistant.

A task step has failed. Analyze the error and decide what to do.

DECISIONS:
- retry   : Transient error (network timeout, temporary file lock, race condition).
             The same step can succeed if tried again.
- skip    : This step is not critical and the task can succeed without it.
- replan  : The approach was wrong. A different tool or method should be tried.
- abort   : The task is fundamentally impossible or unsafe to continue.

Also provide:
- A brief explanation of WHY it failed (1 sentence)
- A fix suggestion if decision is replan (what to try instead)

Return ONLY valid JSON:
{
  "decision": "retry|skip|replan|abort",
  "reason": "why it failed",
  "fix_suggestion": "what to try instead (for replan)",
  "user_message": "Short message to tell the user (max 15 words)"
}
"""


FIX_GENERATOR_PROMPT = """You are the error recovery module of JARVIS AI assistant.

A task step failed. Generate a SINGLE replacement step using ONLY these tools:
  browser, vision, computer, terminal, os_control

Return ONLY valid JSON for a single step:
{"tool": "browser|vision|computer|terminal|os_control", "description": "what this step does", "parameters": {...}}

RULES:
- Use only the 5 tools above. No other tool names.
- The parameters must match the tool's expected format.
- For browser: parameters must include "action" (go_to, get_text, parse_html, vision_read, click, etc.)
- For terminal: parameters must include "task" (natural language) or "command" (exact command)
- For os_control: parameters must include "action" or "description"
"""


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def analyze_error(
    step: dict,
    error: str,
    attempt: int = 1,
    max_attempts: int = 2
) -> dict:
    """
    Analyzes a failed step and returns a recovery decision.

    Returns:
        {
            "decision": ErrorDecision,
            "reason": str,
            "fix_suggestion": str,
            "user_message": str
        }
    """
    # If we've already retried enough, escalate to replan
    if attempt >= max_attempts:
        print(f"[ErrorHandler] ⚠️ Max attempts reached for step {step.get('step')} — forcing replan")
        return {
            "decision":      ErrorDecision.REPLAN,
            "reason":        f"Failed {attempt} times: {error[:100]}",
            "fix_suggestion": "Try a completely different approach or tool",
            "user_message":  "Trying a different approach, sir."
        }

    try:
        from google import genai

        client = genai.Client(api_key=_get_api_key())

        prompt = f"""Failed step:
Tool: {step.get('tool')}
Description: {step.get('description')}
Parameters: {json.dumps(step.get('parameters', {}), indent=2)}
Critical: {step.get('critical', False)}

Error:
{error[:500]}

Attempt number: {attempt}"""

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config={"system_instruction": ERROR_ANALYST_PROMPT}
        )
        text = response.text.strip()
        text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()

        result = json.loads(text)
        decision_str = result.get("decision", "replan").lower()
        decision_map = {
            "retry":  ErrorDecision.RETRY,
            "skip":   ErrorDecision.SKIP,
            "replan": ErrorDecision.REPLAN,
            "abort":  ErrorDecision.ABORT,
        }
        result["decision"] = decision_map.get(decision_str, ErrorDecision.REPLAN)

        if step.get("critical") and result["decision"] == ErrorDecision.SKIP:
            result["decision"]     = ErrorDecision.REPLAN
            result["user_message"] = "This step is critical — finding alternative approach, sir."

        print(f"[ErrorHandler] Decision: {result['decision'].value} — {result.get('reason', '')}")
        return result

    except Exception as e:
        print(f"[ErrorHandler] ⚠️ Analysis failed: {e} — defaulting to replan")
        return {
            "decision":       ErrorDecision.REPLAN,
            "reason":         str(e),
            "fix_suggestion": "Try alternative approach",
            "user_message":   "Encountered an issue, adjusting approach, sir."
        }


def generate_fix(step: dict, error: str, fix_suggestion: str) -> dict:
    """
    When decision is REPLAN and a fix suggestion exists,
    generates a replacement step using real tools.

    Returns a modified step dict with a valid tool name.
    """
    try:
        from google import genai

        client = genai.Client(api_key=_get_api_key())

        prompt = f"""A task step failed. Generate a replacement step.

Original step:
  Tool: {step.get('tool')}
  Description: {step.get('description')}
  Parameters: {json.dumps(step.get('parameters', {}), indent=2)}

Error: {error[:300]}
Fix suggestion: {fix_suggestion}

Return ONLY valid JSON for a single replacement step."""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"system_instruction": FIX_GENERATOR_PROMPT}
        )

        text = response.text.strip()
        text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
        result = json.loads(text)

        valid_tools = {"browser", "vision", "computer", "terminal", "os_control"}
        tool = result.get("tool", "terminal")
        if tool not in valid_tools:
            tool = "terminal"

        return {
            "step":        step.get("step"),
            "tool":        tool,
            "description": result.get("description", f"Auto-fix for: {step.get('description')}"),
            "parameters":  result.get("parameters", {"task": step.get("description", "")}),
            "critical":    step.get("critical", False)
        }

    except Exception as e:
        print(f"[ErrorHandler] ⚠️ Fix generation failed: {e}")
        return {
            "step":        step.get("step"),
            "tool":        "terminal",
            "description": f"Fallback for: {step.get('description')}",
            "parameters":  {"task": step.get("description", "")},
            "critical":    step.get("critical", False)
        }
