---
name: weather
description: "Get current weather and forecasts via Open-Meteo. Use when: user asks about weather, temperature, or forecasts for any location. NOT for: historical weather data, severe weather alerts, or detailed meteorological analysis. No API key needed."
---

# Weather Skill

Get current weather conditions and forecasts using the Open-Meteo API.

## When to Use

**USE this skill when:**

- "What's the weather?"
- "Will it rain today/tomorrow?"
- "Temperature in [city]"
- "Weather forecast for the week"
- Travel planning weather checks

## When NOT to Use

**DON'T use this skill when:**

- Historical weather data — use weather archives/APIs
- Climate analysis or trends — use specialized data sources
- Hyper-local microclimate data — use local sensors
- Severe weather alerts — check official NWS sources
- Aviation/marine weather — use specialized services (METAR, etc.)

## Two-Step Process

Open-Meteo uses coordinates, not city names. Always geocode first.

### Step 1: Geocode the location

```bash
curl -s "https://geocoding-api.open-meteo.com/v1/search?name=Austin&count=1" | jq '.results[0] | {name, country, latitude, longitude}'
```

### Step 2: Fetch weather using coordinates

```bash
curl -s "https://api.open-meteo.com/v1/forecast?latitude=30.27&longitude=-97.74&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m,precipitation&temperature_unit=fahrenheit&wind_speed_unit=mph" | jq '.current'
```

## Commands

### Current Weather

```bash
# Geocode + current conditions (replace city name and coordinates)
curl -s "https://api.open-meteo.com/v1/forecast?latitude=30.27&longitude=-97.74&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m,precipitation&temperature_unit=fahrenheit&wind_speed_unit=mph"
```

### Forecasts

```bash
# 7-day daily forecast
curl -s "https://api.open-meteo.com/v1/forecast?latitude=30.27&longitude=-97.74&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=auto"

# Hourly forecast (next 24h)
curl -s "https://api.open-meteo.com/v1/forecast?latitude=30.27&longitude=-97.74&hourly=temperature_2m,precipitation_probability,weather_code&temperature_unit=fahrenheit&forecast_hours=24&timezone=auto"
```

### Current Parameters

- `temperature_2m` — Temperature
- `apparent_temperature` — "Feels like"
- `weather_code` — WMO weather code (see below)
- `wind_speed_10m` — Wind speed
- `relative_humidity_2m` — Humidity
- `precipitation` — Precipitation (mm)

### WMO Weather Codes

- 0: Clear sky
- 1-3: Mainly clear / Partly cloudy / Overcast
- 45, 48: Fog
- 51, 53, 55: Drizzle (light/moderate/dense)
- 61, 63, 65: Rain (slight/moderate/heavy)
- 71, 73, 75: Snow (slight/moderate/heavy)
- 80, 81, 82: Rain showers
- 95: Thunderstorm
- 96, 99: Thunderstorm with hail

## Notes

- No API key needed
- Free for non-commercial use
- Always geocode first — Open-Meteo needs lat/lon, not city names
- Use `&timezone=auto` for local times in forecasts
- Use `&temperature_unit=fahrenheit` and `&wind_speed_unit=mph` for US users
- Check user's timezone (USER.md) to decide units
