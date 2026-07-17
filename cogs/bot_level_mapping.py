"""Shared game-data constants used across cogs.

Single source of truth for the Town Center (TC) level table so cogs reference
one mapping instead of each duplicating it. The raw API field is still named
"stove"/"furnace" upstream; only the display labels are Kingshot's TC scheme.
"""

# Raw API stove level -> in-game display name. Levels <= 30 are plain numbers
# and intentionally aren't listed (callers fall back to "Level N").
LEVEL_MAPPING = {
    31: "30-1", 32: "30-2", 33: "30-3", 34: "30-4",
    35: "TC 1", 36: "TC 1-1", 37: "TC 1-2", 38: "TC 1-3", 39: "TC 1-4",
    40: "TC 2", 41: "TC 2-1", 42: "TC 2-2", 43: "TC 2-3", 44: "TC 2-4",
    45: "TC 3", 46: "TC 3-1", 47: "TC 3-2", 48: "TC 3-3", 49: "TC 3-4",
    50: "TC 4", 51: "TC 4-1", 52: "TC 4-2", 53: "TC 4-3", 54: "TC 4-4",
    55: "TC 5", 56: "TC 5-1", 57: "TC 5-2", 58: "TC 5-3", 59: "TC 5-4",
    60: "TC 6", 61: "TC 6-1", 62: "TC 6-2", 63: "TC 6-3", 64: "TC 6-4",
    65: "TC 7", 66: "TC 7-1", 67: "TC 7-2", 68: "TC 7-3", 69: "TC 7-4",
    70: "TC 8", 71: "TC 8-1", 72: "TC 8-2", 73: "TC 8-3", 74: "TC 8-4",
    75: "TC 9", 76: "TC 9-1", 77: "TC 9-2", 78: "TC 9-3", 79: "TC 9-4",
    80: "TC 10", 81: "TC 10-1", 82: "TC 10-2", 83: "TC 10-3", 84: "TC 10-4",
}


def format_furnace_level(raw) -> str:
    """Display name for a raw API stove level (e.g. 80 -> 'TC 10'); <= 30 -> 'Level N'."""
    try:
        lv = int(raw)
    except (TypeError, ValueError):
        return str(raw)
    if lv > 30:
        return LEVEL_MAPPING.get(lv, f"Level {lv}")
    return f"Level {lv}"
