# actions/google_maps.py
# JARVIS — Google Maps service module
#
# Handles: directions, routes, location search
# All site-specific logic is self-contained here.

from urllib.parse import quote_plus


def google_maps(parameters: dict, response=None, player=None, session_memory=None) -> str:
    from actions.browser import go_to, wait_for_content, get_text

    origin = (parameters.get("origin") or "").strip()
    destination = (parameters.get("destination") or "").strip()
    query = (parameters.get("query") or "").strip()

    # 1. Build the appropriate Maps URL
    if origin and destination:
        url = f"https://www.google.com/maps/dir/{quote_plus(origin)}/{quote_plus(destination)}"
    elif query:
        url = f"https://www.google.com/maps/search/{quote_plus(query)}"
    elif destination:
        url = f"https://www.google.com/maps/search/{quote_plus(destination)}"
    else:
        return "Please specify a destination, origin+destination, or search query for Google Maps."

    nav = go_to(url)
    if "error" in nav.lower() or "timeout" in nav.lower():
        return f"Could not open Google Maps: {nav}"

    # 2. Wait for Maps to load route/search data
    wait_for_content(timeout_ms=5000)

    # 3. Read results
    content = get_text(max_chars=6000)

    if not content or len(content) < 30:
        return "Could not read directions from Google Maps. The page may still be loading."

    if player:
        player.write_log(f"[maps] Route loaded")

    return content
