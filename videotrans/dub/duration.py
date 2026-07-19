"""轻量级、可按说话人校准的中文发音时长模型。"""

import re
import statistics


def spoken_units(text: str) -> float:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text or ""))
    latin = len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text or ""))
    numbers = len(re.findall(r"\d+(?:\.\d+)?", text or ""))
    return max(cjk + latin * 1.7 + numbers * 1.2, 1.0)


class DurationModel:
    def __init__(self, *, default_ms_per_unit=220.0, overhead_ms=180):
        self.default_ms_per_unit = float(default_ms_per_unit)
        self.overhead_ms = int(overhead_ms)
        self.speaker_rates = {}

    @classmethod
    def from_project(cls, project):
        model = cls()
        samples = {}
        all_rates = []
        for unit in project.units:
            text = next(
                (c.text for c in unit.text_candidates
                 if c.id == unit.selected_text_candidate_id),
                str(unit.legacy_payload.get("text") or ""),
            )
            audio = next(
                (c for c in unit.audio_candidates
                 if c.id == unit.selected_audio_candidate_id), None)
            if not audio or not audio.duration_ms or not text.strip():
                continue
            rate = (audio.duration_ms - model.overhead_ms) / spoken_units(text)
            if 80 <= rate <= 500:
                samples.setdefault(unit.speaker_id, []).append(rate)
                all_rates.append(rate)
        if all_rates:
            model.default_ms_per_unit = float(statistics.median(all_rates))
        for speaker, values in samples.items():
            if len(values) >= 2:
                model.speaker_rates[speaker] = float(statistics.median(values))
        return model

    def estimate(self, text: str, speaker_id: str = "") -> int:
        rate = self.speaker_rates.get(speaker_id, self.default_ms_per_unit)
        return max(int(self.overhead_ms + spoken_units(text) * rate), 120)
