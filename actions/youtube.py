# actions/youtube.py
# JARVIS — YouTube service module
#
# Handles: search, play, and search-by-views on YouTube
# All site-specific logic is self-contained here.

import json
from urllib.parse import quote_plus


def youtube(parameters: dict, response=None, player=None, session_memory=None) -> str:
    from actions.browser import go_to, wait_for_content, parse_html, get_url

    query = (parameters.get("query") or "").strip()
    if not query:
        return "Please tell me what to search for on YouTube."

    sort_by_views = parameters.get("sort_by_views", False)
    if isinstance(sort_by_views, str):
        sort_by_views = sort_by_views.lower() in ("true", "1", "yes")

    # 1. Build search URL
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    if sort_by_views:
        search_url += "&sp=CAM%3D"

    nav = go_to(search_url)
    if "error" in nav.lower() or "timeout" in nav.lower():
        return f"Could not open YouTube: {nav}"

    # 2. Sign-in detection
    current = get_url()
    if "accounts.google.com" in current.lower():
        return "YouTube redirected to Google sign-in. Please log in to Google in your browser first."

    # 3. Wait for React SPA to render results
    wait_for_content(timeout_ms=5000)

    # 4. Parse first video link
    raw = parse_html(known_key="youtube_video_link", attribute="href", limit=1)
    try:
        data = json.loads(raw)
        results = data.get("found", [])
    except (json.JSONDecodeError, TypeError):
        results = []

    if not results:
        return f"No videos found on YouTube for '{query}'."

    video_url = results[0].get("value", "")
    video_title = results[0].get("text", query)

    if not video_url:
        return f"Found a video but could not get its URL."

    # 5. Navigate to the video (YouTube autoplays)
    go_to(video_url)

    if player:
        player.write_log(f"[youtube] Playing: {video_title[:60]}")

    return f"Now playing on YouTube: {video_title}"
