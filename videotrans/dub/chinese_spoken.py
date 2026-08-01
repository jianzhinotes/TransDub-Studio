"""Stable Chinese speech rendering, separate from viewer-facing subtitles.

Subtitles should keep compact factual notation (``6.5 英寸``), while a cloned
Chinese voice needs an unambiguous pronounceable form (``六点五英寸``).  This
module is deliberately deterministic: it is used before synthesis, in cache
keys, and as the expected text for audio QA.
"""

from __future__ import annotations

import re


SPOKEN_TEXT_VERSION = "zh-spoken-numbers-v1"
_DIGITS = "零一二三四五六七八九"
_UNITS = ((100_000_000, "亿"), (10_000, "万"), (1_000, "千"), (100, "百"), (10, "十"))
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])(-?\d[\d,]*(?:\.\d+)?)(?![A-Za-z0-9])")
_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9])(-?\d[\d,]*(?:\.\d+)?)\s*[-–—~～]\s*"
    r"(-?\d[\d,]*(?:\.\d+)?)(?![A-Za-z0-9])"
)
_PERCENT_RE = re.compile(r"(?<![A-Za-z0-9])(-?\d[\d,]*(?:\.\d+)?)\s*[%％]")


def _integer_to_chinese(value: int) -> str:
    if value == 0:
        return _DIGITS[0]
    if value < 0:
        return "负" + _integer_to_chinese(-value)
    if value < 10:
        return _DIGITS[value]

    def under_ten_thousand(number: int) -> str:
        pieces = []
        digits = ((1000, "千"), (100, "百"), (10, "十"), (1, ""))
        zero_pending = False
        for divisor, unit in digits:
            digit, number = divmod(number, divisor)
            if digit:
                if zero_pending:
                    pieces.append("零")
                    zero_pending = False
                if divisor == 10 and digit == 1 and not pieces:
                    pieces.append("十")
                else:
                    pieces.append(_DIGITS[digit] + unit)
            elif pieces and number:
                zero_pending = True
        return "".join(pieces)

    chunks = []
    remaining = value
    for divisor, unit in _UNITS[:2]:
        chunk, remaining = divmod(remaining, divisor)
        if chunk:
            chunks.append((chunk, unit))
    if remaining:
        chunks.append((remaining, ""))
    result = ""
    previous_chunk = None
    for chunk, unit in chunks:
        if previous_chunk is not None and chunk < 1000:
            result += "零"
        if chunk >= 10_000:
            result += _integer_to_chinese(chunk)
        else:
            result += under_ten_thousand(chunk)
        result += unit
        previous_chunk = chunk
    return result


def number_to_chinese_spoken(raw: str) -> str:
    """Render an Arabic integer or decimal in natural, audit-equivalent speech."""
    value = str(raw or "").replace(",", "").strip()
    if not value:
        return value
    negative = value.startswith("-")
    if negative:
        value = value[1:]
    if "." in value:
        integer, fraction = value.split(".", 1)
        spoken = _integer_to_chinese(int(integer or "0")) + "点" + "".join(
            _DIGITS[int(char)] for char in fraction if char.isdigit())
    elif len(value) > 1 and value.startswith("0"):
        # Identifiers such as 007 are normally spoken digit by digit.
        spoken = "".join(_DIGITS[int(char)] for char in value)
    else:
        spoken = _integer_to_chinese(int(value))
    return ("负" if negative else "") + spoken


def prepare_chinese_spoken_text(text: str) -> str:
    """Create a TTS-only Chinese rendering while leaving the subtitle intact."""
    value = str(text or "")
    if not value:
        return value
    value = _PERCENT_RE.sub(
        lambda match: "百分之" + number_to_chinese_spoken(match.group(1)), value)
    value = _RANGE_RE.sub(
        lambda match: number_to_chinese_spoken(match.group(1)) + "到"
        + number_to_chinese_spoken(match.group(2)), value)
    value = _NUMBER_RE.sub(lambda match: number_to_chinese_spoken(match.group(1)), value)
    return re.sub(r"\s+", "", value)
