# server_fun.py — MCP Tools Server for Weekend Wizard
from mcp.server.fastmcp import FastMCP
from typing import Dict, Any, List
import requests, html, time

mcp = FastMCP("FunTools")

# ─── Retry helper ─────────────────────────────────────────────────────────────

def _get_with_retry(url: str, params=None, timeout: int = 20, max_retries: int = 3) -> requests.Response:
    """HTTP GET with exponential backoff for rate-limit / transient errors."""
    last_error: Exception = RuntimeError("Max retries exceeded")
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 429 and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                last_error = e
                continue
            raise
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                last_error = e
                continue
            raise
    raise last_error

# ─── WMO weather code descriptions ───────────────────────────────────────────

_WMO = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Icy fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Light snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Heavy showers", 95: "Thunderstorm",
}

def _weather_desc(code: int) -> str:
    return _WMO.get(code, f"Weather code {code}")


# ─── Tool: Weather (Open-Meteo) ───────────────────────────────────────────────

@mcp.tool()
def get_weather(latitude: float, longitude: float) -> Dict[str, Any]:
    """Current weather at coordinates via Open-Meteo (no API key needed)."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "timezone": "auto",
    }
    r = _get_with_retry("https://api.open-meteo.com/v1/forecast", params=params)
    data = r.json().get("current", {})
    data["description"] = _weather_desc(data.get("weather_code", 0))
    return data


# ─── Tool: City → Coordinates (Stretch Goal — Open-Meteo Geocoding) ──────────

@mcp.tool()
def city_to_coords(city: str) -> Dict[str, Any]:
    """Convert a city name to latitude/longitude using Open-Meteo geocoding API."""
    r = _get_with_retry(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "en", "format": "json"},
    )
    results = r.json().get("results", [])
    if not results:
        return {"error": f"City '{city}' not found. Try a more specific name."}
    loc = results[0]
    return {
        "city": loc.get("name"),
        "country": loc.get("country"),
        "latitude": loc.get("latitude"),
        "longitude": loc.get("longitude"),
    }


# ─── Tool: Book Recommendations (Open Library) ───────────────────────────────

@mcp.tool()
def book_recs(topic: str, limit: int = 5) -> Dict[str, Any]:
    """Simple book suggestions for a topic via Open Library search (no key needed)."""
    r = _get_with_retry(
        "https://openlibrary.org/search.json",
        params={"q": topic, "limit": limit},
    )
    docs = r.json().get("docs", [])
    picks: List[Dict[str, Any]] = []
    for d in docs:
        picks.append({
            "title": d.get("title"),
            "author": (d.get("author_name") or ["Unknown"])[0],
            "year": d.get("first_publish_year"),
            "link": "https://openlibrary.org" + d.get("key", ""),
        })
    return {"topic": topic, "results": picks}


# ─── Tool: Joke (JokeAPI) ─────────────────────────────────────────────────────

@mcp.tool()
def random_joke() -> Dict[str, Any]:
    """Return a safe, family-friendly single-line joke."""
    r = _get_with_retry("https://v2.jokeapi.dev/joke/Any?type=single&safe-mode")
    data = r.json()
    return {"joke": data.get("joke", "Why do programmers prefer dark mode? Because light attracts bugs!")}


# ─── Tool: Dog Photo (Dog CEO) ────────────────────────────────────────────────

@mcp.tool()
def random_dog() -> Dict[str, Any]:
    """Return a random dog image URL for good weekend vibes."""
    r = _get_with_retry("https://dog.ceo/api/breeds/image/random")
    return r.json()


# ─── Tool: Trivia (Open Trivia DB) ───────────────────────────────────────────

@mcp.tool()
def trivia() -> Dict[str, Any]:
    """Return one multiple-choice trivia question."""
    r = _get_with_retry("https://opentdb.com/api.php?amount=1&type=multiple")
    data = r.json().get("results", [])
    if not data:
        return {"error": "No trivia available right now, try again later."}
    q = data[0]
    q["question"] = html.unescape(q["question"])
    q["correct_answer"] = html.unescape(q["correct_answer"])
    q["incorrect_answers"] = [html.unescape(x) for x in q["incorrect_answers"]]
    return q


if __name__ == "__main__":
    mcp.run()  # stdio transport
