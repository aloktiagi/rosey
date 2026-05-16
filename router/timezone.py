"""Resolve a free-form timezone answer to an IANA timezone name.

Used by the onboarding FSM to translate whatever the user typed into the
``SCHEDULER_TZ`` value injected as a per-household Fly secret.

Tiered resolution:

1. **IANA passthrough** — "America/Los_Angeles" → "America/Los_Angeles"
2. **Abbreviations** — "PST", "ET", "IST" → mapped IANA
3. **City names** — "NYC", "Bangalore", "London" → IANA via a bundled
   table of ~150 common cities
4. **UTC offset** — "UTC-5", "+05:30" → Etc/GMT±N (no DST)

Returns None when nothing matches. The caller can then either re-ask
the user or fall back to an LLM resolver. The launch path keeps the
fallback simple (re-ask once, default to America/Los_Angeles).
"""
from __future__ import annotations

import re
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

# Common abbreviations → IANA. Maps to a representative city in that zone.
# Where an abbreviation collides (e.g. "BST" = British Summer Time vs.
# Bangladesh Standard Time, "IST" = India vs. Israel vs. Irish), we pick
# the more common interpretation for an English-speaking audience.
_ABBREVIATIONS = {
    # North America
    "PT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "MT": "America/Denver",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "CT": "America/Chicago",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "ET": "America/New_York",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "AT": "America/Halifax",
    "AST": "America/Halifax",
    "ADT": "America/Halifax",
    "AKT": "America/Anchorage",
    "AKST": "America/Anchorage",
    "AKDT": "America/Anchorage",
    "HT": "Pacific/Honolulu",
    "HST": "Pacific/Honolulu",
    # Europe / UK
    "GMT": "Europe/London",
    "BST": "Europe/London",
    "WET": "Europe/Lisbon",
    "WEST": "Europe/Lisbon",
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "EET": "Europe/Athens",
    "EEST": "Europe/Athens",
    # Asia
    "IST": "Asia/Kolkata",
    "PKT": "Asia/Karachi",
    "JST": "Asia/Tokyo",
    "KST": "Asia/Seoul",
    "HKT": "Asia/Hong_Kong",
    "SGT": "Asia/Singapore",
    "ICT": "Asia/Bangkok",
    "WIB": "Asia/Jakarta",
    # Australia / NZ
    "AEDT": "Australia/Sydney",
    "AEST": "Australia/Sydney",
    "ACDT": "Australia/Adelaide",
    "ACST": "Australia/Adelaide",
    "AWST": "Australia/Perth",
    "NZDT": "Pacific/Auckland",
    "NZST": "Pacific/Auckland",
    # UTC
    "UTC": "UTC",
    "GMT0": "UTC",
    "Z": "UTC",
    "ZULU": "UTC",
}

# City → IANA. Lookup is case-insensitive on a normalized form (lowercased,
# trailing punctuation trimmed). Aim for cities most likely to come up.
_CITIES = {
    # US — Pacific
    "los angeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "sf": "America/Los_Angeles",
    "sf bay area": "America/Los_Angeles",
    "bay area": "America/Los_Angeles",
    "san jose": "America/Los_Angeles",
    "oakland": "America/Los_Angeles",
    "san diego": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "portland": "America/Los_Angeles",
    "las vegas": "America/Los_Angeles",
    # US — Mountain
    "denver": "America/Denver",
    "salt lake city": "America/Denver",
    "boise": "America/Boise",
    "phoenix": "America/Phoenix",
    "albuquerque": "America/Denver",
    # US — Central
    "chicago": "America/Chicago",
    "houston": "America/Chicago",
    "dallas": "America/Chicago",
    "austin": "America/Chicago",
    "san antonio": "America/Chicago",
    "minneapolis": "America/Chicago",
    "kansas city": "America/Chicago",
    "saint louis": "America/Chicago",
    "st louis": "America/Chicago",
    "nashville": "America/Chicago",
    "memphis": "America/Chicago",
    "new orleans": "America/Chicago",
    # US — Eastern
    "new york": "America/New_York",
    "new york city": "America/New_York",
    "nyc": "America/New_York",
    "ny": "America/New_York",
    "manhattan": "America/New_York",
    "brooklyn": "America/New_York",
    "queens": "America/New_York",
    "boston": "America/New_York",
    "philadelphia": "America/New_York",
    "philly": "America/New_York",
    "washington": "America/New_York",
    "washington dc": "America/New_York",
    "dc": "America/New_York",
    "atlanta": "America/New_York",
    "miami": "America/New_York",
    "orlando": "America/New_York",
    "tampa": "America/New_York",
    "detroit": "America/Detroit",
    "pittsburgh": "America/New_York",
    "charlotte": "America/New_York",
    "raleigh": "America/New_York",
    "baltimore": "America/New_York",
    # US — other
    "honolulu": "Pacific/Honolulu",
    "hawaii": "Pacific/Honolulu",
    "anchorage": "America/Anchorage",
    "alaska": "America/Anchorage",
    # Canada
    "toronto": "America/Toronto",
    "montreal": "America/Toronto",
    "ottawa": "America/Toronto",
    "vancouver": "America/Vancouver",
    "calgary": "America/Edmonton",
    "edmonton": "America/Edmonton",
    "winnipeg": "America/Winnipeg",
    "halifax": "America/Halifax",
    # UK / Ireland
    "london": "Europe/London",
    "edinburgh": "Europe/London",
    "manchester": "Europe/London",
    "glasgow": "Europe/London",
    "dublin": "Europe/Dublin",
    "belfast": "Europe/London",
    # Western Europe
    "paris": "Europe/Paris",
    "marseille": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "munich": "Europe/Berlin",
    "hamburg": "Europe/Berlin",
    "frankfurt": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam",
    "brussels": "Europe/Brussels",
    "madrid": "Europe/Madrid",
    "barcelona": "Europe/Madrid",
    "rome": "Europe/Rome",
    "milan": "Europe/Rome",
    "lisbon": "Europe/Lisbon",
    "zurich": "Europe/Zurich",
    "geneva": "Europe/Zurich",
    "vienna": "Europe/Vienna",
    "stockholm": "Europe/Stockholm",
    "copenhagen": "Europe/Copenhagen",
    "oslo": "Europe/Oslo",
    "helsinki": "Europe/Helsinki",
    "reykjavik": "Atlantic/Reykjavik",
    # Eastern Europe / Middle East
    "warsaw": "Europe/Warsaw",
    "prague": "Europe/Prague",
    "budapest": "Europe/Budapest",
    "athens": "Europe/Athens",
    "bucharest": "Europe/Bucharest",
    "kyiv": "Europe/Kyiv",
    "kiev": "Europe/Kyiv",
    "moscow": "Europe/Moscow",
    "saint petersburg": "Europe/Moscow",
    "istanbul": "Europe/Istanbul",
    "tel aviv": "Asia/Jerusalem",
    "jerusalem": "Asia/Jerusalem",
    "dubai": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
    "doha": "Asia/Qatar",
    "riyadh": "Asia/Riyadh",
    "tehran": "Asia/Tehran",
    # India / Pakistan / Bangladesh
    "mumbai": "Asia/Kolkata",
    "bombay": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata",
    "ncr": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata",
    "chennai": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata",
    "calcutta": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata",
    "pune": "Asia/Kolkata",
    "ahmedabad": "Asia/Kolkata",
    "jaipur": "Asia/Kolkata",
    "lucknow": "Asia/Kolkata",
    "kochi": "Asia/Kolkata",
    "karachi": "Asia/Karachi",
    "lahore": "Asia/Karachi",
    "islamabad": "Asia/Karachi",
    "dhaka": "Asia/Dhaka",
    # East / Southeast Asia
    "tokyo": "Asia/Tokyo",
    "osaka": "Asia/Tokyo",
    "kyoto": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "shenzhen": "Asia/Shanghai",
    "guangzhou": "Asia/Shanghai",
    "hong kong": "Asia/Hong_Kong",
    "hk": "Asia/Hong_Kong",
    "taipei": "Asia/Taipei",
    "singapore": "Asia/Singapore",
    "bangkok": "Asia/Bangkok",
    "jakarta": "Asia/Jakarta",
    "manila": "Asia/Manila",
    "kuala lumpur": "Asia/Kuala_Lumpur",
    "kl": "Asia/Kuala_Lumpur",
    "ho chi minh": "Asia/Ho_Chi_Minh",
    "ho chi minh city": "Asia/Ho_Chi_Minh",
    "saigon": "Asia/Ho_Chi_Minh",
    "hanoi": "Asia/Ho_Chi_Minh",
    # Australia / NZ
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane",
    "perth": "Australia/Perth",
    "adelaide": "Australia/Adelaide",
    "canberra": "Australia/Sydney",
    "hobart": "Australia/Hobart",
    "auckland": "Pacific/Auckland",
    "wellington": "Pacific/Auckland",
    "christchurch": "Pacific/Auckland",
    # Latin America
    "mexico city": "America/Mexico_City",
    "mexico": "America/Mexico_City",
    "guadalajara": "America/Mexico_City",
    "monterrey": "America/Monterrey",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "sao paulo": "America/Sao_Paulo",
    "rio": "America/Sao_Paulo",
    "rio de janeiro": "America/Sao_Paulo",
    "santiago": "America/Santiago",
    "bogota": "America/Bogota",
    "lima": "America/Lima",
    "caracas": "America/Caracas",
    "panama": "America/Panama",
    "panama city": "America/Panama",
    # Africa
    "cairo": "Africa/Cairo",
    "lagos": "Africa/Lagos",
    "johannesburg": "Africa/Johannesburg",
    "cape town": "Africa/Johannesburg",
    "nairobi": "Africa/Nairobi",
    "casablanca": "Africa/Casablanca",
    "accra": "Africa/Accra",
    "addis ababa": "Africa/Addis_Ababa",
}

# Phrases users sometimes type before naming a place.
_COMMON_PREFIXES = (
    "i'm in ",
    "i am in ",
    "im in ",
    "i live in ",
    "we live in ",
    "based in ",
    "based out of ",
    "we're in ",
    "we are in ",
    "were in ",
    "from ",
)

# Country / region suffixes to strip when scanning for cities.
_COMMON_SUFFIXES = (
    ", usa",
    ", us",
    ", uk",
    ", india",
    ", canada",
    ", australia",
    " usa",
    " us",
    " uk",
    " india",
)

_UTC_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.I)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve(text: str) -> Optional[str]:
    """Return an IANA timezone name, or None if unresolvable.

    Tier order: IANA passthrough → abbreviations → cities → UTC offset.
    Punctuation/whitespace are tolerated; common prefixes ("i'm in NYC")
    are stripped before lookup.
    """
    if not text or not text.strip():
        return None

    raw = text.strip()
    cleaned = _strip_prefix(raw)

    # Tier 1: IANA passthrough. Only accept full Continent/City names
    # (or "UTC") — otherwise legacy single-token aliases like "EST" and
    # "GMT" would short-circuit and skip the abbreviation table, which
    # maps them to DST-aware city zones (America/New_York, Europe/London).
    for candidate in (cleaned, raw):
        if "/" not in candidate and candidate.upper() != "UTC":
            continue
        try:
            ZoneInfo(candidate)
            return candidate
        except (ZoneInfoNotFoundError, ValueError):
            pass

    norm = _normalize(cleaned)
    if not norm:
        return None

    # Tier 2: Abbreviation (case-insensitive, whitespace-stripped).
    upper = norm.upper().replace(" ", "")
    if upper in _ABBREVIATIONS:
        return _ABBREVIATIONS[upper]

    # Tier 3: City. Try the normalized form, plus suffix-stripped variants.
    if norm in _CITIES:
        return _CITIES[norm]
    for suffix in _COMMON_SUFFIXES:
        if norm.endswith(suffix):
            city = norm[: -len(suffix)].strip().rstrip(",").strip()
            if city in _CITIES:
                return _CITIES[city]

    # Tier 4: UTC offset.
    m = _UTC_OFFSET_RE.match(cleaned.upper().replace(" ", ""))
    if m:
        sign = m.group(1)
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        if minutes != 0:
            # Etc/GMT zones don't support fractional hours.
            return None
        if 0 <= hours <= 14:
            if hours == 0:
                return "UTC"
            # Etc/GMT signs are inverted relative to common usage:
            #   "UTC-5" means 5 hours behind UTC → Etc/GMT+5.
            inverted = "-" if sign == "+" else "+"
            name = f"Etc/GMT{inverted}{hours}"
            try:
                ZoneInfo(name)
                return name
            except ZoneInfoNotFoundError:
                return None

    return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return text.strip().rstrip(".,!?").strip().lower()


def _strip_prefix(text: str) -> str:
    lower = text.lower()
    for prefix in _COMMON_PREFIXES:
        if lower.startswith(prefix):
            return text[len(prefix):].strip()
    return text
