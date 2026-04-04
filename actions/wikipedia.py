# actions/wikipedia.py
# JARVIS — Wikipedia service module
#
# Handles: look up articles on Wikipedia
# All site-specific logic is self-contained here.

from urllib.parse import quote_plus


def wikipedia(parameters: dict, response=None, player=None, session_memory=None) -> str:
    from actions.browser import go_to, get_text

    topic = (parameters.get("topic") or "").strip()
    if not topic:
        return "Please specify a topic to look up on Wikipedia."

    # 1. Navigate to Wikipedia article
    url = f"https://en.wikipedia.org/wiki/{quote_plus(topic)}"
    nav = go_to(url)
    if "error" in nav.lower() or "timeout" in nav.lower():
        return f"Could not open Wikipedia: {nav}"

    # 2. Read article content
    content = get_text(max_chars=8000)

    if not content or len(content) < 50:
        return f"Could not find a Wikipedia article for '{topic}'."

    if player:
        player.write_log(f"[wikipedia] Read: {topic}")

    return content
