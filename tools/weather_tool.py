"""Weather tool — current conditions and forecasts via Open-Meteo (free, no API key)."""

from __future__ import annotations

import logging
from typing import Any

import requests
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry

logger = logging.getLogger(__name__)

_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes → human-readable descriptions
_WMO_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _wind_direction_label(degrees: float | int | None) -> str:
    """Convert wind direction in degrees to a compass label."""
    if degrees is None:
        return "N/A"
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = round(degrees / 22.5) % 16
    return dirs[idx]


# ---------------------------------------------------------------------------
#  Geocode helper
# ---------------------------------------------------------------------------

def _geocode(location: str) -> dict[str, Any] | str:
    """Resolve a location name to lat/lon/timezone. Returns dict or error str."""
    try:
        resp = requests.get(
            _GEO_URL,
            params={"name": location, "count": 1, "language": "en"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return f"Geocoding request failed: {exc}"

    results = data.get("results")
    if not results:
        return f"Could not find location: {location}"

    r = results[0]
    return {
        "name": r.get("name", location),
        "country": r.get("country", ""),
        "admin1": r.get("admin1", ""),  # state / region
        "latitude": r["latitude"],
        "longitude": r["longitude"],
        "timezone": r.get("timezone", "auto"),
    }


# ---------------------------------------------------------------------------
#  Current weather
# ---------------------------------------------------------------------------

def _get_current_weather(location: str) -> str:
    """Return current weather conditions for *location*."""
    geo = _geocode(location)
    if isinstance(geo, str):
        return geo

    try:
        resp = requests.get(
            _FORECAST_URL,
            params={
                "latitude": geo["latitude"],
                "longitude": geo["longitude"],
                "current": (
                    "temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "precipitation,weather_code,wind_speed_10m,"
                    "wind_direction_10m,wind_gusts_10m,uv_index"
                ),
                "timezone": geo["timezone"],
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return f"Weather request failed: {exc}"

    cur = data.get("current", {})
    units = data.get("current_units", {})

    place_parts = [geo["name"]]
    if geo["admin1"]:
        place_parts.append(geo["admin1"])
    if geo["country"]:
        place_parts.append(geo["country"])
    place = ", ".join(place_parts)

    wmo = cur.get("weather_code", -1)
    condition = _WMO_CODES.get(wmo, f"Unknown ({wmo})")
    wind_dir = _wind_direction_label(cur.get("wind_direction_10m"))

    lines = [
        f"Current weather for {place}:",
        f"  Condition: {condition}",
        f"  Temperature: {cur.get('temperature_2m')}{units.get('temperature_2m', '°C')}",
        f"  Feels like: {cur.get('apparent_temperature')}{units.get('apparent_temperature', '°C')}",
        f"  Humidity: {cur.get('relative_humidity_2m')}{units.get('relative_humidity_2m', '%')}",
        f"  Wind: {cur.get('wind_speed_10m')} {units.get('wind_speed_10m', 'km/h')} {wind_dir}"
        f" (gusts {cur.get('wind_gusts_10m')} {units.get('wind_gusts_10m', 'km/h')})",
        f"  Precipitation: {cur.get('precipitation')} {units.get('precipitation', 'mm')}",
        f"  UV index: {cur.get('uv_index')}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Forecast
# ---------------------------------------------------------------------------

def _get_weather_forecast(location: str, days: int = 3) -> str:
    """Return a daily weather forecast for *location* (1-14 days)."""
    days = max(1, min(days, 14))

    geo = _geocode(location)
    if isinstance(geo, str):
        return geo

    try:
        resp = requests.get(
            _FORECAST_URL,
            params={
                "latitude": geo["latitude"],
                "longitude": geo["longitude"],
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,precipitation_probability_max,"
                    "wind_speed_10m_max,uv_index_max,sunrise,sunset"
                ),
                "timezone": geo["timezone"],
                "forecast_days": days,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return f"Weather request failed: {exc}"

    daily = data.get("daily", {})
    units = data.get("daily_units", {})
    dates = daily.get("time", [])
    if not dates:
        return f"No forecast data available for {location}."

    place_parts = [geo["name"]]
    if geo["admin1"]:
        place_parts.append(geo["admin1"])
    if geo["country"]:
        place_parts.append(geo["country"])
    place = ", ".join(place_parts)

    lines = [f"{days}-day forecast for {place}:\n"]
    for i, date in enumerate(dates):
        wmo = daily.get("weather_code", [None])[i]
        condition = _WMO_CODES.get(wmo, f"Unknown ({wmo})") if wmo is not None else "N/A"
        hi = daily.get("temperature_2m_max", [None])[i]
        lo = daily.get("temperature_2m_min", [None])[i]
        precip = daily.get("precipitation_sum", [None])[i]
        rain_pct = daily.get("precipitation_probability_max", [None])[i]
        wind = daily.get("wind_speed_10m_max", [None])[i]
        uv = daily.get("uv_index_max", [None])[i]
        sunrise = daily.get("sunrise", [None])[i]
        sunset = daily.get("sunset", [None])[i]

        temp_unit = units.get("temperature_2m_max", "°C")
        parts = [
            f"  {date}:",
            f"    Condition: {condition}",
            f"    High: {hi}{temp_unit}  Low: {lo}{temp_unit}",
        ]
        if rain_pct is not None:
            parts.append(f"    Precipitation: {precip} {units.get('precipitation_sum', 'mm')} (chance: {rain_pct}%)")
        if wind is not None:
            parts.append(f"    Max wind: {wind} {units.get('wind_speed_10m_max', 'km/h')}")
        if uv is not None:
            parts.append(f"    UV index: {uv}")
        if sunrise and sunset:
            # Strip date prefix from sunrise/sunset (e.g. "2026-03-05T06:30" → "06:30")
            sr = sunrise.split("T")[-1] if "T" in str(sunrise) else sunrise
            ss = sunset.split("T")[-1] if "T" in str(sunset) else sunset
            parts.append(f"    Sunrise: {sr}  Sunset: {ss}")
        lines.append("\n".join(parts))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Tool class
# ---------------------------------------------------------------------------

class WeatherTool(BaseTool):

    @property
    def name(self) -> str:
        return "weather"

    @property
    def display_name(self) -> str:
        return "🌤️ Weather"

    @property
    def description(self) -> str:
        return (
            "Get current weather conditions and multi-day forecasts for any "
            "location worldwide using the free Open-Meteo API. No API key required."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    def as_langchain_tools(self) -> list:

        class _CurrentInput(BaseModel):
            location: str = Field(
                description=(
                    "City or place name to get current weather for. "
                    "Examples: 'London', 'New York', 'Tokyo, Japan'."
                )
            )

        class _ForecastInput(BaseModel):
            location: str = Field(
                description=(
                    "City or place name to get the forecast for. "
                    "Examples: 'London', 'New York', 'Tokyo, Japan'."
                )
            )
            days: int = Field(
                default=3,
                description="Number of forecast days (1-14). Default: 3.",
            )

        return [
            StructuredTool.from_function(
                func=_get_current_weather,
                name="get_current_weather",
                description=(
                    "Get current weather conditions for a location — temperature, "
                    "feels-like, humidity, wind, precipitation, UV index."
                ),
                args_schema=_CurrentInput,
            ),
            StructuredTool.from_function(
                func=_get_weather_forecast,
                name="get_weather_forecast",
                description=(
                    "Get a daily weather forecast for a location (1-14 days). "
                    "Includes highs/lows, precipitation chance, wind, UV, "
                    "sunrise/sunset times."
                ),
                args_schema=_ForecastInput,
            ),
        ]

    def execute(self, query: str) -> str:
        return _get_current_weather(query)


registry.register(WeatherTool())
