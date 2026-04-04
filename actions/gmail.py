# actions/gmail.py
# JARVIS — Gmail service module
#
# Handles: check inbox, read unread emails
# All site-specific logic is self-contained here.


def gmail(parameters: dict, response=None, player=None, session_memory=None) -> str:
    from actions.browser import go_to, wait_for_content, vision_read, get_url

    action = (parameters.get("action") or "check_inbox").strip().lower()

    # 1. Navigate to Gmail
    nav = go_to("https://mail.google.com/")
    if "error" in nav.lower() or "timeout" in nav.lower():
        return f"Could not open Gmail: {nav}"

    # 2. Sign-in detection
    current = get_url()
    if "accounts.google.com" in current.lower():
        return "Gmail requires sign-in. Please log in to Google in your browser first."

    # 3. Wait for Gmail React app to load
    wait_for_content(timeout_ms=6000)

    # 4. Use vision_read — Gmail unread state is visual (bold text, styling)
    if action == "check_inbox":
        result = vision_read(
            "List all visible emails in the inbox. For each email show: "
            "sender name, subject line, and whether it is UNREAD (bold) or READ. "
            "List up to 10 emails."
        )
    else:
        result = vision_read(
            "Describe what is currently visible in Gmail."
        )

    if player:
        player.write_log(f"[gmail] {result[:60]}")

    return result
