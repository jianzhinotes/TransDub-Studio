"""Cheap, deterministic translation checks that run before expensive TTS.

These checks intentionally focus on high-confidence corruption signals.  They
do not pretend to replace a bilingual judge; their job is to catch mapping
drift (for example, a sentence suddenly acquiring ``1400 W/m²``) without a
model download or an API call.
"""

from __future__ import annotations

import re
import unicodedata


_NUMBER_RE = re.compile(r"(?<![\d.])\d[\d,]*(?:\.\d+)?%?")
_SMALL_ENGLISH_NUMBERS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16",
    "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20",
}
_UNIT_RULES = (
    (re.compile(r"平方米|平方公尺|m²", re.I), re.compile(r"square\s+met(?:er|re)s?|m²", re.I), "square_meter"),
    (
        re.compile(r"瓦(?:特)?", re.I),
        re.compile(r"\b(?:kilo|mega|giga|tera)?watts?\b|\b(?:k|m|g|t)w\b", re.I),
        "watt",
    ),
    (re.compile(r"百分之|%"), re.compile(r"\bpercent(?:age)?\b|%", re.I), "percent"),
)


def _numbers(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    found = {match.group(0).replace(",", "").rstrip("%") for match in _NUMBER_RE.finditer(normalized)}
    for word, value in _SMALL_ENGLISH_NUMBERS.items():
        if re.search(rf"\b{word}\b", normalized):
            found.add(value)
    return found


def audit_translation_pair(source_text: str, target_text: str) -> list[str]:
    """Return only high-confidence semantic-integrity failures."""
    source = str(source_text or "")
    target = str(target_text or "")
    failures: list[str] = []

    source_numbers = _numbers(source)
    target_numbers = _numbers(target)
    unexpected = sorted(target_numbers - source_numbers)
    # Large values, decimals and percentages are strong anchors.  Small digits
    # are often harmless renderings of words such as "type one".
    high_signal = [value for value in unexpected if "." in value or int(value or 0) >= 21]
    if high_signal:
        failures.append("unexpected_number:" + ",".join(high_signal))

    for target_pattern, source_pattern, name in _UNIT_RULES:
        if target_pattern.search(target) and not source_pattern.search(source):
            failures.append("unexpected_unit:" + name)
    return failures


def audit_translation_queue(queue: list[dict]) -> list[dict]:
    issues = []
    for index, item in enumerate(queue):
        failures = audit_translation_pair(item.get("ref_text", ""), item.get("text", ""))
        if failures:
            issues.append({
                "index": index,
                "line": item.get("line", index + 1),
                "failures": failures,
                "source": str(item.get("ref_text") or ""),
                "target": str(item.get("text") or ""),
            })
    return issues
