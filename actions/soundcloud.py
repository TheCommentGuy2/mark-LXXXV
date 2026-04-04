# actions/soundcloud.py
# JARVIS — SoundCloud service module
#
# Handles: search and play music on SoundCloud
# All site-specific logic is self-contained here.

import json
from urllib.parse import quote_plus


def soundcloud(parameters: dict, response=None, player=None, session_memory=None) -> str:
    from actions.browser import go_to, wait_for_content, parse_html, get_url

    query = (parameters.get("query") or "").strip()
    if not query:
        return "Please tell me what to play on SoundCloud."

    # 1. Navigate to SoundCloud search
    search_url = f"https://soundcloud.com/search?q={quote_plus(query)}"
    nav = go_to(search_url)
    if "error" in nav.lower() or "timeout" in nav.lower():
        return f"Could not open SoundCloud: {nav}"

    # 2. Sign-in detection
    current = get_url()
    if "signin" in current.lower() or "sign_in" in current.lower():
        return "SoundCloud requires sign-in. Please log in to SoundCloud in your browser first."

    # 3. Wait for SPA to render results
    wait_for_content(timeout_ms=6000)

    # 4. Parse first track link
    raw = parse_html(known_key="soundcloud_track", attribute="href", limit=1)
    try:
        data = json.loads(raw)
        results = data.get("found", [])
    except (json.JSONDecodeError, TypeError):
        results = []

    if not results:
        return f"No tracks found on SoundCloud for '{query}'."

    track_url = results[0].get("value", "")
    track_name = results[0].get("text", query)

    if not track_url:
        return f"Found a track but could not get its URL."

    # 5. Navigate to the track (starts playing automatically on SoundCloud)
    if not track_url.startswith("http"):
        track_url = f"https://soundcloud.com{track_url}"

    go_to(track_url)

    if player:
        player.write_log(f"[soundcloud] Playing: {track_name[:60]}")

    return f"Now playing on SoundCloud: {track_name}"
