"""Normalize loosely typed US delivery addresses for reliable lookup."""

from __future__ import annotations

import re


_STREET_SUFFIXES = {
    "st", "street", "rd", "road", "ave", "avenue", "av", "blvd", "boulevard",
    "dr", "drive", "ln", "lane", "way", "ct", "court", "pkwy", "parkway",
    "hwy", "highway", "cir", "circle", "ter", "terrace", "pl", "place",
    "trl", "trail", "loop", "run", "path", "pike", "plz", "plaza", "sq",
    "square", "xing", "crossing", "cv", "cove", "bnd", "bend", "aly", "alley",
}
_SUFFIX_NORMALIZATION = {
    "street": "St", "st": "St", "road": "Rd", "rd": "Rd",
    "avenue": "Ave", "ave": "Ave", "av": "Ave", "boulevard": "Blvd", "blvd": "Blvd",
    "drive": "Dr", "dr": "Dr", "lane": "Ln", "ln": "Ln", "court": "Ct", "ct": "Ct",
    "parkway": "Pkwy", "pkwy": "Pkwy", "highway": "Hwy", "hwy": "Hwy",
    "circle": "Cir", "cir": "Cir", "terrace": "Ter", "ter": "Ter",
    "place": "Pl", "pl": "Pl", "trail": "Trl", "trl": "Trl",
    "square": "Sq", "sq": "Sq", "crossing": "Xing", "xing": "Xing",
    "cove": "Cv", "cv": "Cv", "bend": "Bnd", "bnd": "Bnd",
    "alley": "Aly", "aly": "Aly",
}
_UNIT_WORDS = {
    "apt", "apartment", "suite", "ste", "unit", "bldg", "building",
    "fl", "floor", "rm", "room", "lot", "trlr", "#",
}
_DIRECTIONALS = {
    "n", "s", "e", "w", "ne", "nw", "se", "sw",
    "north", "south", "east", "west",
}
_DIRECTION_NORMALIZATION = {
    "north": "N", "south": "S", "east": "E", "west": "W",
    "n": "N", "s": "S", "e": "E", "w": "W",
    "ne": "NE", "nw": "NW", "se": "SE", "sw": "SW",
}

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
    "washington dc": "DC", "puerto rico": "PR",
}
_STATE_CODES = set(US_STATES.values())

# Only ZIP prefixes that resolve to one state are used. Ambiguous ranges are
# deliberately omitted so a pasted address is never silently routed elsewhere.
_ZIP3_BANDS: tuple[tuple[int, int, str], ...] = (
    (5, 5, "NY"), (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"),
    (39, 49, "ME"), (50, 54, "VT"), (56, 59, "VT"), (60, 62, "CT"),
    (64, 69, "CT"), (70, 89, "NJ"), (100, 149, "NY"), (150, 196, "PA"),
    (197, 199, "DE"), (207, 219, "MD"), (220, 246, "VA"), (247, 268, "WV"),
    (270, 289, "NC"), (290, 299, "SC"), (300, 319, "GA"), (320, 349, "FL"),
    (350, 369, "AL"), (370, 385, "TN"), (386, 397, "MS"), (398, 399, "GA"),
    (400, 427, "KY"), (430, 459, "OH"), (460, 479, "IN"), (480, 499, "MI"),
    (500, 528, "IA"), (530, 549, "WI"), (550, 567, "MN"), (570, 577, "SD"),
    (580, 588, "ND"), (590, 599, "MT"), (600, 629, "IL"), (630, 658, "MO"),
    (660, 679, "KS"), (680, 693, "NE"), (700, 715, "LA"), (716, 729, "AR"),
    (730, 731, "OK"), (734, 749, "OK"), (750, 799, "TX"), (800, 816, "CO"),
    (820, 831, "WY"), (832, 833, "ID"), (835, 838, "ID"), (840, 847, "UT"),
    (850, 865, "AZ"), (870, 884, "NM"), (885, 885, "TX"), (889, 898, "NV"),
    (900, 961, "CA"), (967, 968, "HI"), (970, 979, "OR"), (980, 994, "WA"),
    (995, 999, "AK"),
)
_ZIP_EXACT = {
    "05501": "MA", "05544": "MA", "06390": "NY",
    "73301": "TX", "73344": "TX", "83414": "WY",
}


def normalize_state(value: str) -> str:
    token = (value or "").strip().strip(",.").lower()
    if token in US_STATES:
        return US_STATES[token]
    upper = token.upper()
    return upper if upper in _STATE_CODES else ""


def state_from_zip(zip_code: str) -> str:
    digits = (zip_code or "").strip().split("-", 1)[0]
    if not re.fullmatch(r"\d{5}", digits):
        return ""
    if digits in _ZIP_EXACT:
        return _ZIP_EXACT[digits]
    prefix = int(digits[:3])
    for low, high, state in _ZIP3_BANDS:
        if low <= prefix <= high:
            return state
    return ""


def _take_state(words: list[str]) -> tuple[str, list[str]]:
    if len(words) >= 2:
        code = normalize_state(f"{words[-2]} {words[-1]}")
        if code:
            return code, words[:-2]
    if words:
        code = normalize_state(words[-1])
        if code:
            return code, words[:-1]
    return "", words


def split_street_city(blob: str) -> tuple[str, str]:
    words = blob.split()
    if not words:
        return "", ""
    last_suffix = max(
        (index for index, word in enumerate(words) if word.strip(".,").lower() in _STREET_SUFFIXES),
        default=-1,
    )
    if last_suffix == -1 or last_suffix == len(words) - 1:
        return blob.strip(), ""
    cut = last_suffix + 1
    if words[cut].strip(".,").lower() in _DIRECTIONALS and cut + 1 < len(words):
        cut += 1
    if words[cut].strip(".,#").lower() in _UNIT_WORDS or words[cut].startswith("#"):
        cut += 1
        if cut < len(words) and any(character.isdigit() for character in words[cut]):
            cut += 1
    street = " ".join(words[:cut]).strip()
    city = " ".join(words[cut:]).strip()
    return (street, city) if city else (blob.strip(), "")


def _normalize_street(value: str) -> str:
    output = []
    for word in value.split():
        prefix = word[: len(word) - len(word.lstrip("#"))]
        core = word[len(prefix):]
        punctuation = "," if core.endswith(",") else ""
        bare = core.rstrip(",.").lower()
        normalized = _DIRECTION_NORMALIZATION.get(bare) or _SUFFIX_NORMALIZATION.get(bare) or core.rstrip(",.")
        output.append(f"{prefix}{normalized}{punctuation}")
    return " ".join(output).strip(" ,")


def parse_address(raw: str) -> dict[str, str]:
    text = re.sub(r"\s*[;\n]+\s*", ", ", (raw or "").strip())
    text = re.sub(r"(?:,?\s+)(?:usa|u\.s\.a\.|united states(?: of america)?)\s*$", "", text, flags=re.I)
    output = {"street": text, "city": "", "state": "", "zip": ""}
    if not text:
        return output

    if "|" in text:
        parts = [part.strip() for part in text.split("|")]
        output["street"] = parts[0] if parts else text
        output["city"] = parts[1] if len(parts) > 1 else ""
        state_part = parts[2] if len(parts) > 2 else ""
        state_words = state_part.split()
        if state_words and re.fullmatch(r"\d{5}(?:-\d{4})?", state_words[-1]):
            output["zip"] = state_words.pop()
        output["state"], _ = _take_state(state_words)
        if len(parts) > 3:
            output["zip"] = parts[3]
        if not output["state"]:
            output["state"] = state_from_zip(output["zip"])
        return output

    if "," in text:
        parts = [part.strip() for part in text.split(",") if part.strip()]
        if parts:
            tail = parts[-1].split()
            if tail and re.fullmatch(r"\d{5}(?:-\d{4})?", tail[-1]):
                output["zip"] = tail.pop()
                if tail:
                    parts[-1] = " ".join(tail)
                else:
                    parts.pop()

        if parts:
            tail = parts[-1].split()
            output["state"], tail = _take_state(tail)
            if output["state"]:
                if tail:
                    parts[-1] = " ".join(tail)
                else:
                    parts.pop()

        if not output["state"]:
            output["state"] = state_from_zip(output["zip"])

        if len(parts) >= 2:
            output["street"] = ", ".join(parts[:-1])
            output["city"] = parts[-1]
        elif len(parts) == 1:
            street, city = split_street_city(parts[0])
            output["street"], output["city"] = street, city
        return output

    words = text.split()
    if words and re.fullmatch(r"\d{5}(?:-\d{4})?", words[-1]):
        output["zip"] = words.pop()
    output["state"], words = _take_state(words)
    if not output["state"]:
        output["state"] = state_from_zip(output["zip"])
    if words:
        street, city = split_street_city(" ".join(words))
        output["street"] = street
        output["city"] = city
        if not city and output["state"] and len(words) > 1:
            output["street"] = " ".join(words[:-1])
            output["city"] = words[-1]
    return output


def normalize_address(raw: str) -> str:
    """Return a lookup-friendly address while preserving unparseable input."""
    cleaned = re.sub(r"\s+", " ", (raw or "").strip()).strip(" ,|")
    parsed = parse_address(cleaned)
    street = _normalize_street(parsed["street"])
    city = parsed["city"].strip(" ,")
    state = normalize_state(parsed["state"])
    zip_code = parsed["zip"].strip(" ,")
    if street and city and state and zip_code:
        if city.islower() or city.isupper():
            city = city.title()
        return f"{street}, {city}, {state} {zip_code}, USA"
    return cleaned
