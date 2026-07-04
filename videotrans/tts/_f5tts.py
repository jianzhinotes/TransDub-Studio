from dataclasses import dataclass
import gc
import re
from pathlib import Path
from typing import List, Dict, Union

from gradio_client import  handle_file
from videotrans.configure.config import ROOT_DIR, logger
from videotrans.configure.excepts import DubbingSrtError
from videotrans.tts._gradio import GradioBase
from videotrans.util.help_misc import vail_file
from pydub import AudioSegment


@dataclass
class F5TTS(GradioBase):

    MAX_REF_AUDIO_MS=5000
    MAX_LANGUAGE_RETRIES=2

    def __post_init__(self):
        self.ainame = "f5tts"
        super().__post_init__()
        self.safe_ref_wav, self.safe_ref_text = self._select_safe_reference()

    @staticmethod
    def _reference_text_penalty(text: str) -> int:
        words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text or "")
        lowered = (text or "").lower()
        penalty = 0

        # Introductions, names, brands and calls to action are particularly
        # noticeable when F5 copies one word from the English reference.
        blocked_phrases = (
            "my name is", "welcome", "subscribe", "former cia", "officer",
            "today show", "fox news", "shark tank", "amazon", "youtube",
        )
        penalty += sum(12000 for phrase in blocked_phrases if phrase in lowered)

        # Ignore the first word because normal English sentences capitalize it.
        for word in words[1:]:
            plain = word.replace("'", "")
            if plain.isupper() and len(plain) >= 2:
                penalty += 6000
            elif plain[:1].isupper() and plain[1:].islower() and len(plain) >= 4:
                penalty += 9000
        return penalty

    def _select_safe_reference(self):
        candidates = []
        queue_len = max(len(self.queue_tts), 1)
        for index, item in enumerate(self.queue_tts):
            if item.get("role") != "clone":
                continue
            ref_wav = item.get("ref_wav", "")
            ref_text = (item.get("ref_text") or "").strip()
            if not ref_wav or not Path(ref_wav).is_file() or len(ref_text) < 15:
                continue
            try:
                duration_ms = len(AudioSegment.from_file(ref_wav))
            except Exception:
                continue
            if 2500 <= duration_ms <= self.MAX_REF_AUDIO_MS:
                position = index / queue_len
                # Prefer ordinary narration near the middle. Avoid intros,
                # outros, proper names and branded montage clips.
                edge_penalty = 0
                if position < 0.18 or position > 0.88:
                    edge_penalty = 8000
                position_penalty = int(abs(position - 0.5) * 2500)
                duration_penalty = abs(duration_ms - 3600)
                text_penalty = self._reference_text_penalty(ref_text)
                score = text_penalty + edge_penalty + position_penalty + duration_penalty
                candidates.append((score, ref_wav, ref_text, index, duration_ms))
        if not candidates:
            return None, None
        score, ref_wav, ref_text, index, duration_ms = min(candidates, key=lambda item: item[0])
        if ref_text[-1:] not in ".!?。！？":
            ref_text += "."
        logger.debug(
            "F5-TTS 使用安全短参考音频: index=%s duration=%sms score=%s "
            "ref_wav=%s ref_text=%s",
            index, duration_ms, score, ref_wav, ref_text
        )
        return ref_wav, ref_text

    def _exec(self) -> None:
        super()._exec()
        if self.is_test or not self.language or self.language[:2].lower() != "zh":
            return
        self._verify_chinese_outputs()

    @staticmethod
    def _latin_words(text: str) -> List[str]:
        return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text or "")

    def _has_unexpected_english(self, expected: str, transcript: str) -> bool:
        expected_words = {word.lower() for word in self._latin_words(expected)}
        unexpected = [
            word for word in self._latin_words(transcript)
            if word.lower() not in expected_words
        ]
        # Whisper occasionally renders one short Chinese sound as an English word.
        # A longer word or several words is a much stronger signal that F5 copied
        # speech from the English reference clip.
        latin_chars = sum(len(word.replace("'", "")) for word in unexpected)
        if latin_chars >= 8 or (len(unexpected) >= 2 and latin_chars >= 6):
            return True

        reference_words = {
            word.lower() for word in self._latin_words(self.safe_ref_text or "")
            if len(word.replace("'", "")) >= 5
        }
        # Catch a single leaked proper name such as "Hansen". This used to slip
        # through the general threshold because it is only six letters long.
        return any(
            word.lower() in reference_words and word.lower() not in expected_words
            for word in unexpected
        )

    def _get_validator_model_path(self) -> Path:
        candidates = [
            Path(ROOT_DIR) / "models/faster-whisper-tiny",
            Path(ROOT_DIR) / "models/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
            Path(ROOT_DIR) / "models/large-v3-turbo",
        ]
        for path in candidates:
            if (path / "model.bin").is_file():
                return path
        raise DubbingSrtError(
            "F5-TTS 中文配音验收需要已下载的 large-v3-turbo 识别模型，但没有找到该模型。"
        )

    def _transcribe_batch_for_validation(self, model) -> Dict[int, str]:
        batch_audio = AudioSegment.empty()
        boundaries = []
        gap_ms = 700
        for idx, item in enumerate(self.queue_tts):
            if not item.get("text", "").strip() or not vail_file(item.get("filename")):
                continue
            clip = AudioSegment.from_file(item["filename"])
            start_ms = len(batch_audio)
            batch_audio += clip
            boundaries.append((start_ms, len(batch_audio), idx))
            batch_audio += AudioSegment.silent(duration=gap_ms)

        if not boundaries:
            return {}

        batch_file = Path(ROOT_DIR) / "tmp" / f"f5-language-check-{self.uuid}.wav"
        batch_file.parent.mkdir(parents=True, exist_ok=True)
        batch_audio.export(batch_file, format="wav")
        transcripts = {idx: [] for _, _, idx in boundaries}
        try:
            segments, _ = model.transcribe(
                str(batch_file),
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
                temperature=0,
            )
            boundary_pos = 0
            for segment in segments:
                segment_start = int(segment.start * 1000)
                segment_end = int(segment.end * 1000)
                while (
                    boundary_pos < len(boundaries) - 1
                    and segment_start >= boundaries[boundary_pos][1]
                ):
                    boundary_pos += 1

                best_idx = None
                best_overlap = 0
                for pos in range(boundary_pos, min(boundary_pos + 2, len(boundaries))):
                    start_ms, end_ms, idx = boundaries[pos]
                    overlap = max(0, min(segment_end, end_ms) - max(segment_start, start_ms))
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_idx = idx
                if best_idx is not None:
                    transcripts[best_idx].append(segment.text)
            return {idx: "".join(parts).strip() for idx, parts in transcripts.items()}
        finally:
            batch_file.unlink(missing_ok=True)

    def _transcribe_one_for_validation(self, model, filename: str) -> str:
        segments, _ = model.transcribe(
            filename,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            temperature=0,
        )
        return "".join(segment.text for segment in segments).strip()

    def _verify_chinese_outputs(self) -> None:
        from faster_whisper import WhisperModel

        model = None
        try:
            self.signal(text="正在检查 F5-TTS 配音中是否混入英文原声…")
            logger.debug("开始对 F5-TTS 中文配音执行英文原声泄漏检查")
            model = WhisperModel(
                str(self._get_validator_model_path()),
                device="cpu",
                compute_type="int8",
            )

            transcripts = self._transcribe_batch_for_validation(model)
            failed = []
            for idx, item in enumerate(self.queue_tts):
                if not item.get("text", "").strip() or not vail_file(item.get("filename")):
                    continue
                transcript = transcripts.get(idx, "")
                if self._has_unexpected_english(item["text"], transcript):
                    failed.append((idx, item, transcript))

            for retry_index in range(self.MAX_LANGUAGE_RETRIES):
                if not failed:
                    break
                logger.warning(
                    "检测到 %s 段 F5-TTS 配音混入字幕之外的英文，开始第 %s 次重生成",
                    len(failed), retry_index + 1
                )
                self.signal(
                    text=f"检测到 {len(failed)} 段混入英文原声，正在自动重配 "
                         f"({retry_index + 1}/{self.MAX_LANGUAGE_RETRIES})…"
                )
                retry_failed = []
                for idx, item, old_transcript in failed:
                    Path(item["filename"]).unlink(missing_ok=True)
                    error = self._item_task(item, idx)
                    if error or not vail_file(item["filename"]):
                        retry_failed.append((idx, item, str(error or old_transcript)))
                        continue
                    transcript = self._transcribe_one_for_validation(model, item["filename"])
                    if self._has_unexpected_english(item["text"], transcript):
                        retry_failed.append((idx, item, transcript))
                failed = retry_failed

            if failed:
                details = "；".join(
                    f"第 {idx + 1} 段：{transcript[:80]}"
                    for idx, _, transcript in failed[:5]
                )
                raise DubbingSrtError(
                    f"F5-TTS 仍有 {len(failed)} 段混入字幕之外的英文原声，"
                    f"已停止合成，避免输出有问题的成品。{details}"
                )
            logger.debug("F5-TTS 英文原声泄漏检查通过")
            self.signal(text="F5-TTS 配音内容检查通过")
        finally:
            del model
            gc.collect()

    def _run(self, data_item: Union[Dict, List, None], idx: int = -1) -> Union[str, None]:
        ref_wav,ref_text=self.get_ref_wav(data_item)
        if data_item.get("role") == "clone" and self.safe_ref_wav:
            ref_wav, ref_text = self.safe_ref_wav, self.safe_ref_text
        speed_slider = 0.5 if ref_text  and len(ref_text) < 10 else self.get_speed()
        gen_text = data_item['text'].strip()
        if gen_text[-1:] not in ".!?。！？":
            gen_text += "。"
        kwargs={
            "ref_audio_input":handle_file(ref_wav),
            "ref_text_input":ref_text,
            "gen_text_input":gen_text,
            "remove_silence":True,
            "randomize_seed":True,
            "seed_input":0,  # 开启随机后，这个数字会被忽略，填多少都行
            "cross_fade_duration_slider":0.0, # 默认交叉淡入淡出时长
            "nfe_slider":16,            # Apple Silicon 轻量模式：降低发热并缩短推理时间
            "speed_slider":speed_slider,
            "api_name":'/basic_tts'
        }
        ref_wav_audio=AudioSegment.from_file(ref_wav)
        if len(ref_wav_audio)>self.MAX_REF_AUDIO_MS:
            raise DubbingSrtError(
                f"F5-TTS 参考音频超过 {self.MAX_REF_AUDIO_MS / 1000:.0f} 秒，"
                "已停止以避免复制英文原声。"
            )

        return self._send(kwargs,data_item)
