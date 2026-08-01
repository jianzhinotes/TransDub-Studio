"""Shared Chinese dubbing content rules used by synthesis and Studio audits."""

from __future__ import annotations

import difflib
import re
from typing import List


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


def chinese_similarity(expected: str, transcript: str) -> float:
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from zhconv import convert
        expected = convert(expected or "", "zh-cn")
        transcript = convert(transcript or "", "zh-cn")
    except Exception:
        pass
    left = "".join(re.findall(r"[\u4e00-\u9fff]", expected or ""))
    right = "".join(re.findall(r"[\u4e00-\u9fff]", transcript or ""))
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
    expected_cjk = len(re.findall(r"[\u4e00-\u9fff]", expected or ""))
    transcript_cjk = len(re.findall(r"[\u4e00-\u9fff]", transcript or ""))
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
    expected_cjk = len(re.findall(r"[\u4e00-\u9fff]", expected or ""))
    transcript_cjk = len(re.findall(r"[\u4e00-\u9fff]", transcript or ""))
    return {
        "expected_cjk_chars": expected_cjk,
        "transcript_cjk_chars": transcript_cjk,
        "cjk_length_ratio": round(transcript_cjk / max(expected_cjk, 1), 3),
        "cjk_similarity": round(chinese_similarity(expected, transcript), 3),
        "transcript_latin_chars": sum(
            len(word.replace("'", "")) for word in latin_words(transcript)
        ),
    }
