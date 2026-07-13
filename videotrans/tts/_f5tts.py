from dataclasses import dataclass
import gc
import re
from pathlib import Path
from typing import List, Dict, Union

from gradio_client import  handle_file
from videotrans.configure.config import ROOT_DIR, logger, settings
from videotrans.configure.excepts import DubbingSrtError
from videotrans.tts._gradio import GradioBase
from videotrans.util.help_misc import vail_file
from pydub import AudioSegment


@dataclass
class F5TTS(GradioBase):

    # F5-TTS 参考音频越接近 6-10s 干净人声，克隆音色越像；过短(3s 级)音色明显失真。
    # 上限 12s（F5 官方建议 <15s），选择时以 8s 为最优目标。
    MAX_REF_AUDIO_MS=12000
    BEST_REF_AUDIO_MS=8000
    MAX_LANGUAGE_RETRIES=2

    def __post_init__(self):
        self.ainame = "f5tts"
        super().__post_init__()
        # 参考质检模型（tiny whisper）与泄漏检查共用；不可用则跳过回读验证
        validator = self._load_validator()
        try:
            self.safe_ref_wav, self.safe_ref_text = self._select_safe_reference(validator)
            self._build_cluster_refs(validator)
        finally:
            del validator
            gc.collect()
        # nfe/seed 影响输出音质，纳入配音缓存键，防止调参后命中旧缓存
        self.dubb_cache_extra = (
            f"nfe{int(settings.get('f5tts_nfe') or 32)}-seed{int(settings.get('f5tts_seed', 42))}")

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
            # 访谈对话口癖：这类句子多半是主持人/嘉宾互相对话，容易选中
            # 非主讲人的声音当克隆模板，导致成品音色不像
            "as you mentioned", "as you said", "thank you", "thanks for",
            "great question", "welcome back", "joining us",
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

    # ---- 参考音频自动质检与构建（全自动，无需人工指定） ----
    @staticmethod
    def _punct_ok(text: str) -> bool:
        return (text or "")[-1:] in ".!?。！？"

    @staticmethod
    def _ensure_punct(text: str) -> str:
        text = (text or "").strip()
        return text if F5TTS._punct_ok(text) else text + "."

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """转写与字幕文本的相似度（跨语言鲁棒：拉丁词 + CJK 单字为 token）。"""
        import difflib

        def norm(s):
            tokens = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[一-鿿]", (s or "").lower())
            return " ".join(tokens)

        na, nb = norm(a), norm(b)
        if not na or not nb:
            return 0.0
        return difflib.SequenceMatcher(None, na, nb).ratio()

    def _load_validator(self):
        try:
            from faster_whisper import WhisperModel
            return WhisperModel(str(self._get_validator_model_path()),
                                device="cpu", compute_type="int8")
        except Exception as e:
            logger.warning(f"参考质检模型不可用，跳过回读验证: {e}")
            return None

    def _collect_candidates(self, allowed=None):
        """收集克隆参考候选并打分。allowed 为 None 时考虑全部行，否则只看指定下标。

        半句文本重罚：ref_text 掐在半句上时 F5 会把参考文本"续写"进生成结果
        （曾导致 49 段串音 "First you've got..."）。
        """
        candidates = []
        queue_len = max(len(self.queue_tts), 1)
        for index, item in enumerate(self.queue_tts):
            if allowed is not None and index not in allowed:
                continue
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
                # 以 BEST_REF_AUDIO_MS(8s) 为最优：长参考音色更像，且长句
                # 多为主讲人连续陈述，降低选中访谈另一方声音的概率
                duration_penalty = abs(duration_ms - self.BEST_REF_AUDIO_MS)
                text_penalty = self._reference_text_penalty(ref_text)
                punct_penalty = 0 if self._punct_ok(ref_text) else 7000
                score = (text_penalty + edge_penalty + position_penalty
                         + duration_penalty + punct_penalty)
                candidates.append((score, ref_wav, ref_text, index, duration_ms))
        return candidates

    def _validate_candidates(self, ranked, validator, need=4, max_try=8):
        """回读验证：用 tiny whisper 把候选转写一遍，与字幕文本对不上的淘汰。

        这一步专杀"文本与音频错位"的毒参考。validator 不可用时返回空列表，
        调用方退回按分数排序的旧行为。
        """
        if not validator:
            return []
        passed = []
        for cand in ranked[:max_try]:
            try:
                transcript = self._transcribe_one_for_validation(validator, cand[1])
            except Exception as e:
                logger.debug(f"参考回读失败,跳过候选: {e}")
                continue
            sim = self._text_similarity(transcript, cand[2])
            if sim >= 0.5:
                passed.append(cand)
            else:
                logger.debug(f"参考回读不匹配(sim={sim:.2f}),淘汰: {cand[2][:50]!r} vs {transcript[:50]!r}")
            if len(passed) >= need:
                break
        return passed

    def _compose_reference(self, pool, tag="main"):
        """聚合式参考（ElevenLabs 思路）：主片段不足 7s 时拼接次优片段到 ~8-12s。

        单一坏片段不再决定全片音色。pool 元素: (score, wav, text, index, duration_ms)。
        返回 (wav_path, ref_text)。
        """
        _s, wav, text, _i, duration_ms = pool[0]
        text = self._ensure_punct(text)
        if duration_ms >= 7000 or len(pool) < 2:
            return wav, text
        try:
            combined = AudioSegment.from_file(wav)
            parts_text = [text]
            for _s2, w2, t2, _i2, d2 in pool[1:]:
                if len(combined) + d2 > self.MAX_REF_AUDIO_MS:
                    continue
                combined += AudioSegment.silent(duration=200) + AudioSegment.from_file(w2)
                parts_text.append(self._ensure_punct(t2))
                if len(combined) >= 7000:
                    break
            if len(parts_text) == 1:
                return wav, text
            out = Path(wav).parent / f"f5-composite-ref-{tag}.wav"
            combined.export(out, format="wav")
            logger.debug(f"F5-TTS 复合参考[{tag}]: {len(parts_text)} 段, {len(combined)}ms")
            return out.as_posix(), " ".join(parts_text)
        except Exception as e:
            logger.warning(f"复合参考构建失败,退回单片段: {e}")
            return wav, text

    def _select_safe_reference(self, validator=None):
        candidates = self._collect_candidates()
        if not candidates:
            return None, None
        candidates = self._keep_dominant_speaker(candidates)
        ranked = sorted(candidates, key=lambda item: item[0])
        validated = self._validate_candidates(ranked, validator)
        pool = validated or ranked[:4]
        ref_wav, ref_text = self._compose_reference(pool, tag="main")
        # 备选参考（同簇次优）：主参考仍导致大面积串音时，泄漏重试第 2 轮起换用
        self.ref_backups = []
        for _s, w, t, _i, _d in pool[1:]:
            if w != ref_wav:
                self.ref_backups.append((w, self._ensure_punct(t)))
            if len(self.ref_backups) >= 3:
                break
        logger.debug(
            "F5-TTS 参考选择: 候选=%s 回读通过=%s ref_wav=%s ref_text=%s 备选=%s",
            len(ranked), len(validated), ref_wav, ref_text, len(self.ref_backups)
        )
        return ref_wav, ref_text

    def _build_cluster_refs(self, validator=None):
        """多说话人模式：逐句归属说话人簇，各簇构建独立参考（各说各的音色）。

        置信门槛：聚类可靠且次要说话人时长占比 ≥12% 才启用；否则维持
        单一主讲人参考（旧行为）。可用 settings['f5tts_multi_speaker']=false 关闭。
        """
        if str(settings.get('f5tts_multi_speaker', True)).lower() == 'false':
            return
        lines = [(i, it) for i, it in enumerate(self.queue_tts)
                 if it.get('role') == 'clone' and it.get('ref_wav')
                 and Path(it.get('ref_wav', '')).is_file()]
        if len(lines) < 12:
            return
        try:
            from videotrans.util.speaker_cluster import label_speakers
            labels = label_speakers([it['ref_wav'] for _, it in lines])
        except Exception as e:
            logger.warning(f'逐句声纹归属失败,维持单参考: {e}')
            return
        if not labels:
            return
        # 各簇时长占比
        totals = {}
        for pos, (i, it) in enumerate(lines):
            if pos not in labels:
                continue
            d = max(int(it.get('end_time', 0) or 0) - int(it.get('start_time', 0) or 0), 0)
            totals[labels[pos]] = totals.get(labels[pos], 0) + d
        if len(totals) < 2 or sum(totals.values()) <= 0:
            return
        if min(totals.values()) / sum(totals.values()) < 0.12:
            logger.debug('次要说话人占比过低,视为单说话人,维持单参考')
            return
        # 每簇独立选参考（同样走打分+回读验证+复合）
        cluster_refs = {}
        for label in totals:
            allowed = {i for pos, (i, _it) in enumerate(lines) if labels.get(pos) == label}
            cands = self._collect_candidates(allowed=allowed)
            if not cands:
                continue
            ranked = sorted(cands, key=lambda item: item[0])
            validated = self._validate_candidates(ranked, validator, need=3, max_try=6)
            pool = validated or ranked[:3]
            cluster_refs[label] = self._compose_reference(pool, tag=f"spk{label}")
        if len(cluster_refs) < 2:
            return
        # 把归属写进条目：_run 按行取所属簇的参考；缓存键含 cluster_ref 指纹
        assigned = 0
        for pos, (i, it) in enumerate(lines):
            label = labels.get(pos)
            if label in cluster_refs:
                it['cluster_ref'], it['cluster_ref_text'] = cluster_refs[label]
                assigned += 1
        logger.debug(f'多说话人参考启用: {len(cluster_refs)} 簇, 覆盖 {assigned}/{len(lines)} 行, 时长占比={totals}')

    @staticmethod
    def _keep_dominant_speaker(candidates):
        """多说话人视频（访谈等）里只保留主讲人的片段做克隆参考。

        对候选片段做声纹聚类，按说话总时长判定主讲人簇（访谈里说得最多的
        通常就是被采访者），其余簇的片段剔除。聚类不可靠（单说话人/样本少/
        依赖异常）时原样返回，不影响原有选择逻辑。
        candidates 元素: (score, ref_wav, ref_text, index, duration_ms)
        """
        try:
            from videotrans.util.speaker_cluster import cluster_speakers
            labels = cluster_speakers([c[1] for c in candidates])
            if not labels:
                return candidates
            totals = {}
            for pos, label in labels.items():
                totals[label] = totals.get(label, 0) + candidates[pos][4]
            dominant = max(totals, key=totals.get)
            kept = [c for pos, c in enumerate(candidates)
                    if labels.get(pos, dominant) == dominant]
            logger.debug(
                "声纹聚类保留主讲人片段 %s/%s (各簇时长 %s)",
                len(kept), len(candidates), totals,
            )
            return kept or candidates
        except Exception as e:
            logger.warning(f"声纹聚类失败,退回原候选: {e}")
            return candidates

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

    def _write_leak_sidecar(self, failed) -> None:
        """BaseTTS 对 queue_tts 做了 deepcopy，直接改条目传不回调用方。
        把 {文件名: 转写} 写到配音目录的 lang_leak.json，由 trans_create 合并回真正的队列。"""
        try:
            import json
            marks = {}
            for _, item, transcript in failed:
                name = Path(item.get("filename") or "").name
                if name:
                    marks[name] = transcript[:120]
            if not marks:
                return
            first = next((it for it in self.queue_tts if it.get("filename")), None)
            if first:
                sidecar = Path(first["filename"]).parent / "lang_leak.json"
                sidecar.write_text(json.dumps(marks, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"写配音泄漏标记文件失败,忽略: {e}")

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
                    # 标记重试轮次：_run 据此偏移种子（第 2 轮起换备选参考），
                    # 否则固定种子下重新生成的结果与上次完全相同
                    item['lang_leak_retry'] = retry_index + 1
                    error = self._item_task(item, idx)
                    item.pop('lang_leak_retry', None)
                    if error or not vail_file(item["filename"]):
                        retry_failed.append((idx, item, str(error or old_transcript)))
                        continue
                    transcript = self._transcribe_one_for_validation(model, item["filename"])
                    if self._has_unexpected_english(item["text"], transcript):
                        retry_failed.append((idx, item, transcript))
                failed = retry_failed

            if failed:
                # 重试后仍可疑的段落不再终止整个任务（检测本身会误报，
                # 例如 whisper 把中文幻听成法语/英语短句）。改为标记该行，
                # 流程继续走到配音校对步，由用户在工作台试听后决定单句重配或放行。
                for idx, item, transcript in failed:
                    item["lang_leak"] = transcript[:120]
                self._write_leak_sidecar(failed)
                details = "；".join(
                    f"第 {idx + 1} 段：{transcript[:80]}"
                    for idx, _, transcript in failed[:5]
                )
                logger.warning(
                    "F5-TTS 仍有 %s 段疑似混入字幕之外的原声，已标记待人工校对：%s",
                    len(failed), details,
                )
                self.signal(
                    text=f"⚠️ {len(failed)} 段配音疑似混入原声，已标记，"
                         f"请在配音校对步试听并按需单句重配（{details}）"
                )
            else:
                logger.debug("F5-TTS 英文原声泄漏检查通过")
                self.signal(text="F5-TTS 配音内容检查通过")
        finally:
            del model
            gc.collect()

    def _run(self, data_item: Union[Dict, List, None], idx: int = -1) -> Union[str, None]:
        ref_wav,ref_text=self.get_ref_wav(data_item)
        if data_item.get("role") == "clone":
            if data_item.get('cluster_ref'):
                # 多说话人：该行所属说话人簇的参考（各说各的音色）
                ref_wav, ref_text = data_item['cluster_ref'], data_item.get('cluster_ref_text') or ref_text
            elif self.safe_ref_wav:
                ref_wav, ref_text = self.safe_ref_wav, self.safe_ref_text
        # 泄漏重试：第 2 轮起换备选参考——主参考自身导致大面积串音时，换参考才有救
        retry_no = int(data_item.get('lang_leak_retry') or 0)
        if (retry_no >= 2 and data_item.get("role") == "clone"
                and getattr(self, 'ref_backups', None)):
            ref_wav, ref_text = self.ref_backups[(retry_no - 2) % len(self.ref_backups)]
        speed_slider = 0.5 if ref_text  and len(ref_text) < 10 else self.get_speed()
        gen_text = data_item['text'].strip()
        if gen_text[-1:] not in ".!?。！？":
            gen_text += "。"
        # nfe: F5 默认 32 步；16 是曾经的 Apple Silicon 轻量模式（省一半时间但损失音质细节）。
        # seed: 固定种子保证全片音色一致，逐句随机会导致音色漂移；设为负数恢复随机。
        nfe = int(settings.get('f5tts_nfe') or 32)
        seed = int(settings.get('f5tts_seed', 42))
        if seed >= 0 and retry_no:
            # 固定种子下重试必须偏移种子，否则重新生成的结果与上次完全相同，重试形同虚设
            seed += 9973 * retry_no
        kwargs={
            "ref_audio_input":handle_file(ref_wav),
            "ref_text_input":ref_text,
            "gen_text_input":gen_text,
            "remove_silence":True,
            "randomize_seed":seed < 0,
            "seed_input":max(seed, 0),
            "cross_fade_duration_slider":0.0, # 默认交叉淡入淡出时长
            "nfe_slider":nfe,
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
