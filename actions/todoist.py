# actions/todoist.py
# JARVIS — Todoist service module
#
# Handles: check today/upcoming/inbox tasks
# All site-specific logic is self-contained here.


def todoist(parameters: dict, response=None, player=None, session_memory=None) -> str:
    from actions.browser import go_to, wait_for_content, get_text, get_url

    view = (parameters.get("view") or "today").strip().lower()
    if view not in ("today", "upcoming", "inbox"):
        view = "today"

    # 1. Navigate to the specific Todoist view
    url = f"https://app.todoist.com/app/{view}"
    nav = go_to(url)
    if "error" in nav.lower() or "timeout" in nav.lower():
        return f"Could not open Todoist: {nav}"

    # 2. Sign-in detection
    current = get_url()
    if "todoist.com/auth" in current.lower() or "todoist.com/users" in current.lower():
        return "Todoist requires sign-in. Please log in to Todoist in your browser first."

    # 3. Wait for React SPA to load tasks
    wait_for_content(timeout_ms=7000)

    # 4. Read tasks via get_text (reads entire DOM including off-screen tasks)
    content = get_text(max_chars=15000)

    if not content or len(content) < 30:
        return f"Could not read tasks from Todoist {view} view. The page may still be loading."

    if player:
        player.write_log(f"[todoist] Read {view} tasks")

    return content
