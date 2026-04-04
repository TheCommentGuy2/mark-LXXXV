# actions/weather.py
# JARVIS — Weather service module
#
# Handles: get current weather for a city via Google
# All site-specific logic is self-contained here.

import json
from urllib.parse import quote_plus


def weather(parameters: dict, response=None, player=None, session_memory=None) -> str:
    from actions.browser import go_to, parse_html, vision_read

    city = (parameters.get("city") or "").strip()
    if not city:
        return "Please specify a city for the weather."

    # 1. Navigate to Google weather
    url = f"https://www.google.com/search?q=weather+{quote_plus(city)}"
    nav = go_to(url)
    if "error" in nav.lower() or "timeout" in nav.lower():
        return f"Could not open Google weather: {nav}"

    # 2. Try parsing temperature from HTML (Google weather is server-rendered)
    raw = parse_html(known_key="google_weather_temp", attribute="text")
    try:
        data = json.loads(raw)
        results = data.get("found", [])
    except (json.JSONDecodeError, TypeError):
        results = []

    if results:
        temp = results[0].get("value", results[0].get("text", ""))
        if temp:
            if player:
                player.write_log(f"[weather] {city}: {temp}")
            return f"Current temperature in {city}: {temp}°"

    # 3. Fallback: vision_read if HTML parsing found nothing
    vision_result = vision_read(
        f"What is the current temperature and weather conditions for {city} shown on screen?"
    )

    if player:
        player.write_log(f"[weather] {city}: {vision_result[:60]}")

    return vision_result
