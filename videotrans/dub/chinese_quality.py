"""Shared Chinese dubbing content rules used by synthesis and Studio audits."""

from __future__ import annotations

import difflib
import re
from typing import List, Optional


_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_LARGE_UNITS = {"万": 10_000, "亿": 100_000_000}
_CN_NUMBER_CHARS = "零〇一二两三四五六七八九十百千万亿点"
_NUMBER_MARKER_RE = re.compile(r"\u00a4N:([^\u00a4]+)\u00a4")


def latin_words(text: str) -> List[str]:
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text or "")


def has_unexpected_english(
        expected: str, transcript: str, *, safe_reference_text: str = "",
        zero_unexpected_latin: bool = True) -> bool:
    expected_words = {word.lower() for word in latin_words(expected)}
    unexpected = [
        word for word in latin_words(transcript)
        if word.lower() not in expected_words
    ]
    if unexpected and zero_unexpected_latin:
        return True
    latin_chars = sum(len(word.replace("'", "")) for word in unexpected)
    if latin_chars >= 8 or (len(unexpected) >= 2 and latin_chars >= 6):
        return True
    reference_words = {
        word.lower() for word in latin_words(safe_reference_text)
        if len(word.replace("'", "")) >= 5
    }
    return any(word.lower() in reference_words for word in unexpected)


def has_pathological_repetition(transcript: str) -> bool:
    compact = re.sub(r"[^A-Za-z\u4e00-\u9fff]+", "", transcript or "")
    return bool(re.search(r"(.{1,4})\1{4,}", compact))


def _canonical_number(value: float | int | str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return (f"{number:.8f}").rstrip("0").rstrip(".")


def _chinese_number_value(text: str) -> Optional[str]:
    """Return a canonical Arabic representation for a spoken Chinese number."""
    if not text:
        return None
    if "点" in text:
        integer, fraction = text.split("点", 1)
        integer_value = _chinese_number_value(integer) if integer else "0"
        if integer_value is None or not fraction or any(
                char not in _CN_DIGITS for char in fraction):
            return None
        return f"{integer_value}." + "".join(
            str(_CN_DIGITS[char]) for char in fraction
        )
    if any(char not in _CN_DIGITS | _CN_SMALL_UNITS | _CN_LARGE_UNITS
           for char in text):
        return None
    if all(char in _CN_DIGITS for char in text):
        return str(int("".join(str(_CN_DIGITS[char]) for char in text)))

    total = 0
    section = 0
    digit = 0
    for char in text:
        if char in _CN_DIGITS:
            digit = _CN_DIGITS[char]
        elif char in _CN_SMALL_UNITS:
            section += (digit or 1) * _CN_SMALL_UNITS[char]
            digit = 0
        else:
            section += digit
            total += (section or 1) * _CN_LARGE_UNITS[char]
            section = 0
            digit = 0
    return str(total + section + digit)


def _semantic_text(text: str) -> str:
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from zhconv import convert
        text = convert(text or "", "zh-cn")
    except Exception:
        text = text or ""

    cn_number = rf"[{_CN_NUMBER_CHARS}]+"
    number_markers: List[str] = []

    def stash_number(value: str) -> str:
        marker = chr(0xE000 + len(number_markers))
        number_markers.append(f"\u00a4N:{value}\u00a4")
        return marker

    def percent_replacement(match: re.Match) -> str:
        raw = match.group(1).replace(",", "")
        value = (_chinese_number_value(raw)
                 if re.fullmatch(cn_number, raw) else _canonical_number(raw))
        return stash_number(f"{value}%")

    text = re.sub(
        rf"百分之\s*({cn_number}|\d[\d,]*(?:\.\d+)?)",
        percent_replacement,
        text,
    )
    text = re.sub(
        r"(\d[\d,]*(?:\.\d+)?)\s*[%％]",
        percent_replacement,
        text,
    )

    def chinese_replacement(match: re.Match) -> str:
        value = _chinese_number_value(match.group(0))
        return stash_number(value) if value is not None else match.group(0)

    text = re.sub(cn_number, chinese_replacement, text)
    text = re.sub(
        r"\d[\d,]*(?:\.\d+)?",
        lambda match: stash_number(
            _canonical_number(match.group(0).replace(',', ''))
        ),
        text,
    )
    for index, marker in enumerate(number_markers):
        text = text.replace(chr(0xE000 + index), marker)
    return text


def _semantic_tokens(text: str) -> List[str]:
    normalized = _semantic_text(text)
    return re.findall(r"\u00a4N:[^\u00a4]+\u00a4|[\u4e00-\u9fff]", normalized)


def _numeric_tokens(text: str) -> List[str]:
    return _NUMBER_MARKER_RE.findall(_semantic_text(text))


def chinese_similarity(expected: str, transcript: str) -> float:
    left = _semantic_tokens(expected)
    right = _semantic_tokens(transcript)
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def hard_quality_failures(
        expected: str, transcript: str, *, safe_reference_text: str = "",
        zero_unexpected_latin: bool = True) -> List[str]:
    failures = []
    if has_unexpected_english(
            expected, transcript,
            safe_reference_text=safe_reference_text,
            zero_unexpected_latin=zero_unexpected_latin):
        failures.append("unexpected_english")
    if has_pathological_repetition(transcript):
        failures.append("pathological_repetition")
    expected_numbers = _numeric_tokens(expected)
    transcript_numbers = _numeric_tokens(transcript)
    if expected_numbers != transcript_numbers:
        failures.append("numeric_content_mismatch")
    expected_cjk = len(_semantic_tokens(expected))
    transcript_cjk = len(_semantic_tokens(transcript))
    if expected_cjk >= 6 and transcript_cjk < 2:
        failures.append("missing_chinese_content")
    if expected_cjk >= 8 and transcript_cjk >= 2:
        allowed_delta = max(3, round(expected_cjk * 0.18))
        if transcript_cjk > expected_cjk + allowed_delta:
            failures.append("unexpected_chinese_content")
        elif transcript_cjk < expected_cjk - max(4, round(expected_cjk * 0.35)):
            failures.append("truncated_chinese_content")
        if chinese_similarity(expected, transcript) < 0.35:
            failures.append("chinese_content_mismatch")
    return failures


def quality_metrics(expected: str, transcript: str) -> dict:
    expected_cjk = len(_semantic_tokens(expected))
    transcript_cjk = len(_semantic_tokens(transcript))
    return {
        "expected_cjk_chars": expected_cjk,
        "transcript_cjk_chars": transcript_cjk,
        "cjk_length_ratio": round(transcript_cjk / max(expected_cjk, 1), 3),
        "cjk_similarity": round(chinese_similarity(expected, transcript), 3),
        "expected_numbers": _numeric_tokens(expected),
        "transcript_numbers": _numeric_tokens(transcript),
        "transcript_latin_chars": sum(
            len(word.replace("'", "")) for word in latin_words(transcript)
        ),
    }
