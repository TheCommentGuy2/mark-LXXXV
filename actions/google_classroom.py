# actions/google_classroom.py
# JARVIS — Google Classroom service module
#
# Handles: check assignments, to-do list
# All site-specific logic is self-contained here.


def google_classroom(parameters: dict, response=None, player=None, session_memory=None) -> str:
    from actions.browser import go_to, wait_for_content, get_text, get_url

    action = (parameters.get("action") or "check_assignments").strip().lower()

    # 1. Navigate to the appropriate Classroom page
    if action in ("check_todo", "todo"):
        url = "https://classroom.google.com/a/not-turned-in/all"
    else:
        url = "https://classroom.google.com/a/not-turned-in/all"

    nav = go_to(url)
    if "error" in nav.lower() or "timeout" in nav.lower():
        return f"Could not open Google Classroom: {nav}"

    # 2. Sign-in detection
    current = get_url()
    if "accounts.google.com" in current.lower():
        return "Google Classroom requires sign-in. Please log in to Google in your browser first."

    # 3. Wait for React SPA to load assignments
    wait_for_content(timeout_ms=7000)

    # 4. Read assignments via get_text (not vision_read — get_text reads ALL
    #    assignments including off-screen, vision_read only sees the viewport)
    content = get_text(max_chars=8000)

    if not content or len(content) < 30:
        return "Could not read assignments from Google Classroom. The page may still be loading."

    if player:
        player.write_log(f"[classroom] Read assignments")

    return content
