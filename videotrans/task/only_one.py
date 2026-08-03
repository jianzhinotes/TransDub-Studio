# 执行单个视频翻译任务时 暂停等待
import hashlib
import json
import time
import traceback
from pathlib import Path
from typing import Optional,  Dict, Any

from PySide6.QtCore import QThread, Signal, QObject
from pydub import AudioSegment

from videotrans.configure.config import tr, settings, app_cfg, logger

from videotrans.task.taskcfg import TaskCfgVTT, SignMsg, InputFile
from videotrans.task.trans_create import TransCreate
from videotrans.util.tools import vail_file
from videotrans.util.help_srt import get_subtitle_from_srt
from videotrans.configure.excepts import DubbingTextReviewRequired


def is_translated_subtitle_only(trk) -> bool:
    """Return true for a translated video that deliberately has no TTS."""
    return bool(
        getattr(trk, 'should_trans', False)
        and not getattr(trk, 'should_dubbing', False)
        and getattr(trk, 'should_hebing', False)
    )


def _subtitle_signature(path: str) -> str:
    try:
        rows = get_subtitle_from_srt(path, is_file=True)
        canonical = [{
            'line': int(row.get('line', 0) or 0),
            'start': int(row.get('start_time', 0) or 0),
            'end': int(row.get('end_time', 0) or 0),
            'text': str(row.get('text') or '').strip(),
        } for row in rows]
        return hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode('utf-8')
        ).hexdigest()
    except (OSError, TypeError, ValueError):
        return ''


def should_pause_for_subtitle_proof(trk) -> bool:
    """Whether a translated, non-dubbed video needs the subtitle editor.

    The old pipeline only opened ``edit_subtitle_target`` inside the dubbing
    branch.  A bilingual subtitle-only job therefore went straight from
    translation to FFmpeg, leaving no way to correct the translated lines
    before the final video was rendered.
    """
    return bool(
        is_translated_subtitle_only(trk)
        and getattr(getattr(trk, 'cfg', None), 'target_sub', '')
        and Path(trk.cfg.target_sub).is_file()
    )


class Worker(QThread):
    uito = Signal(str, SignMsg)

    def __init__(self, *,
                 parent: Optional[QObject] = None,
                 file: InputFile = None,
                 cfg: Optional[Dict[str, Any]] = None):
        super().__init__(parent=parent)
        self.cfg = cfg
        # 存放处理好的 视频路径等信息
        self.file = file
        self.uuid = None

    def run(self) -> None:
        self.uuid = self.file['uuid']
        if not app_cfg.acquire_project_task(self.uuid):
            logger.warning("阻止同一项目重复启动: %s", self.uuid)
            self.uito.emit(self.uuid, SignMsg(**{
                "text": "这个视频已经有一个任务在运行，请等待当前任务结束或先停止它。",
                "type": "duplicate",
                "uuid": self.uuid,
            }))
            return
        # 从停止队列中移出，以便重新开始
        app_cfg.rm_uuid(self.uuid)
        trk=None
        run_state = None
        performance = None
        run_finished = False
        try:
            trk = TransCreate(cfg=TaskCfgVTT(**self.cfg | self.file))
            from videotrans.dub.run_state import RunStateStore
            from videotrans.task.project import project_dir_for
            run_state = RunStateStore(project_dir_for(trk.cfg.target_dir, trk.cfg.noextname))
            run_state.begin_run(self.uuid)
            from videotrans.dub.performance_report import PerformanceReporter
            performance = PerformanceReporter(
                project_dir_for(trk.cfg.target_dir, trk.cfg.noextname))
            performance.start(self.uuid, {
                'source_language': trk.cfg.source_language_code,
                'target_language': trk.cfg.target_language_code,
                'tts_type': getattr(trk.cfg, 'tts_type', None),
                'recogn_type': getattr(trk.cfg, 'recogn_type', None),
                'recogn_model': getattr(trk.cfg, 'model_name', ''),
                'smart_orchestration': bool(
                    getattr(trk.cfg, 'smart_orchestration', False)),
                'reference_mode': settings.get(
                    'f5tts_reference_mode', 'youtube_hybrid'),
            })

            def run_stage(name, callback):
                run_state.start_stage(name)
                performance.start_stage(name)
                try:
                    result = callback()
                except BaseException as error:
                    run_state.fail_stage(name, error)
                    performance.finish_stage(name, status='failed', error=error)
                    raise
                if self._exit():
                    run_state.finish_stage(name, status='interrupted')
                    performance.finish_stage(name, status='interrupted')
                else:
                    metadata = {}
                    if name == 'dubbing':
                        metadata['segments_total'] = len(trk.queue_tts)
                        metadata['reference_mode'] = settings.get(
                            'f5tts_reference_mode', 'youtube_hybrid')
                        try:
                            import soundfile as sf
                            metadata['audio_duration_s'] = round(sum(
                                sf.info(item['filename']).duration
                                for item in trk.queue_tts
                                if item.get('filename') and Path(item['filename']).is_file()
                            ), 3)
                        except Exception:
                            pass
                        try:
                            from videotrans.dub.performance_report import TTS_RUN_STATS_FILE
                            stats_file = Path(trk.cfg.cache_folder) / TTS_RUN_STATS_FILE
                            tts_stats = json.loads(stats_file.read_text(encoding='utf-8'))
                            metadata.update({
                                'tts_cache_hits': int(tts_stats.get('cache_hits') or 0),
                                'tts_succeeded': int(tts_stats.get('succeeded') or 0),
                                'tts_failed': int(tts_stats.get('failed') or 0),
                            })
                        except (OSError, json.JSONDecodeError, TypeError, ValueError):
                            pass
                        quality_file = Path(trk.cfg.cache_folder) / 'quality_manifest.json'
                        try:
                            quality = json.loads(quality_file.read_text(encoding='utf-8'))
                            entries = (quality or {}).get('entries') or {}
                            metadata['quality_passed'] = sum(
                                1 for entry in entries.values() if entry.get('passed'))
                            metadata['quality_failed'] = sum(
                                1 for entry in entries.values() if not entry.get('passed'))
                        except (OSError, json.JSONDecodeError, TypeError):
                            pass
                    run_state.complete_stage(name, metadata)
                    performance.finish_stage(name, metadata=metadata)
                return result

            def run_dubbing_with_text_review():
                """Pause at a recoverable wording gate instead of failing.

                Smart planning has already persisted its queue before raising
                ``DubbingTextReviewRequired``.  The embedded editor changes
                that queue in place; after the user continues we replace only
                the smart-plan checkpoint and retry the dubbing stage.
                """
                review = None
                while True:
                    if review is None:
                        try:
                            return run_stage('dubbing', trk.dubbing)
                        except DubbingTextReviewRequired as pending:
                            review = pending
                    if review is not None:
                        issue_count = len(review.issues)
                        run_state.finish_stage(
                            'dubbing', status='waiting_review',
                            metadata={'text_review_segments': issue_count},
                            error=str(review))
                        performance.finish_stage(
                            'dubbing', status='waiting_review',
                            metadata={'text_review_segments': issue_count},
                            error=str(review))
                        self._post(
                            text=(str(review) + '\n已保存智能编排断点：请编辑红色“中文口播待处理”'
                                  '的句子，然后点击“继续合成”。无需重新识别或翻译。'))
                        app_cfg.set_countdown(86400)
                        self._post(
                            text=(f'{trk.cfg.cache_folder}<|>{trk.cfg.target_language_code}'
                                  f'<|>{trk.cfg.name}<|>{trk.cfg.source_wav}'
                                  f'<|>{trk.cfg.source_language_code}<|>{trk.cfg.translate_type}'
                                  f'<|>{trk.cfg.target_language_code}<|>{trk.cfg.source_sub}'),
                            type='edit_dubbing')
                        self._post(tr('The subtitle editing interface is rendering'))
                        while app_cfg.task_countdown > 0:
                            if self._exit():
                                return None
                            time.sleep(1)
                            app_cfg.set_countdown(app_cfg.task_countdown - 1)
                        qfile = Path(f'{trk.cfg.cache_folder}/queue_tts.json')
                        try:
                            reviewed_queue = json.loads(qfile.read_text(encoding='utf-8'))
                            trk.accept_smart_text_review(reviewed_queue)
                        except DubbingTextReviewRequired as unresolved:
                            # The editor remains the recovery path even when a
                            # user clicked continue without changing all flags.
                            # No terminal error or expensive rerun is emitted.
                            review = unresolved
                            continue
                        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                            raise DubbingTextReviewRequired(
                                f'无法读取中文口播编辑结果：{error}。请在工作台保存后继续。')
                        review = None

            manual_proof = (
                not trk.cfg.smart_orchestration
                and float(settings.get('countdown_sec', 0)) > 0)
            subtitle_only_translation = is_translated_subtitle_only(trk)
            # 原始语言字幕文件
            app_cfg.onlyone_source_sub = trk.cfg.source_sub
            # 目标语言字幕文件
            app_cfg.onlyone_target_sub = trk.cfg.target_sub
            if self._exit(): return
            app_cfg.set_countdown(0)
            run_stage('prepare', trk.prepare)
            if self._exit(): return
            run_stage('recognize', trk.recogn)
            if self._exit(): return
            run_stage('diarize', trk.diariz)
            if self._exit(): return
            self._post(text=Path(trk.cfg.source_sub).read_text(encoding='utf-8'), type='replace_subtitle')

            if manual_proof or subtitle_only_translation:
                # A source edit invalidates the previous target translation;
                # remember the exact file so an unchanged source can still
                # resume its translation checkpoint without extra work.
                source_before = (_subtitle_signature(trk.cfg.source_sub)
                                 if subtitle_only_translation else '')
                app_cfg.set_countdown(86400)
                # 等待修改识别出的字幕
                self._post(text=f'{trk.cfg.source_sub}<|>{trk.cfg.name}',
                           type='edit_subtitle_source')
                self._post(tr('The subtitle editing interface is rendering'))
                while app_cfg.task_countdown > 0:
                    time.sleep(1)
                    app_cfg.set_countdown(app_cfg.task_countdown - 1)
                    if self._exit(): return
                if (subtitle_only_translation
                        and _subtitle_signature(trk.cfg.source_sub) != source_before):
                    # ``trans()`` otherwise may accept an old target SRT with
                    # the same row/timeline shape.  Removing it forces a new
                    # translation against the edited English text.
                    Path(trk.cfg.target_sub).unlink(missing_ok=True)

            if trk.should_trans:
                app_cfg.onlyone_trans = True
                # TransCreate owns translation-cache validation.  A target SRT
                # may be the resegmented dubbing output rather than a reusable
                # source-aligned translation, so file existence alone is never
                # sufficient evidence for skipping this stage.
                run_stage('translate', trk.trans)

            if self._exit(): return

            # Subtitle-only translated videos (including the bilingual
            # delivery mode) must have the same recoverable proof step as a
            # dubbed video.  Previously this editor lived only inside the
            # ``should_dubbing`` branch, so the job appeared to complete but
            # its translation could not be edited before assembly.
            if should_pause_for_subtitle_proof(trk):
                # Keep the three-way editor as the source of truth. If the
                # user changes the source column, retranslate first and open
                # the editor again so a stale target can never be rendered.
                while True:
                    source_before = _subtitle_signature(trk.cfg.source_sub)
                    self._post(text=tr('flow_subtitle_review'), type='logs')
                    self._post(text=Path(trk.cfg.target_sub).read_text(
                        encoding='utf-8', errors='ignore'), type='replace_subtitle')
                    app_cfg.set_countdown(86400)
                    self._post(text=f'{trk.cfg.target_sub}<|>{trk.cfg.name}',
                               type='edit_subtitle_bilingual')
                    self._post(text=tr('The subtitle editing interface is rendering'))
                    while app_cfg.task_countdown > 0:
                        if self._exit(): return
                        time.sleep(1)
                        app_cfg.set_countdown(app_cfg.task_countdown - 1)
                    if _subtitle_signature(trk.cfg.source_sub) == source_before:
                        break
                    Path(trk.cfg.target_sub).unlink(missing_ok=True)
                    run_stage('translate', trk.trans)

            # 需要配音时
            if trk.should_dubbing:

                self._post(text=Path(trk.cfg.target_sub).read_text(encoding='utf-8'), type='replace_subtitle')
                if manual_proof:
                    app_cfg.set_countdown(86400)
                    # 传递过去临时目录，用于获取 speaker.json，等待修改待配音的字幕
                    self._post(text=(f'{trk.cfg.cache_folder}<|>{trk.cfg.target_language_code}'
                                     f'<|>{trk.cfg.tts_type}<|>{trk.cfg.name}'),
                               type="edit_subtitle_target")
                    self._post(tr('The subtitle editing interface is rendering'))
                    while app_cfg.task_countdown > 0:
                        if self._exit(): return
                        time.sleep(1)
                        app_cfg.set_countdown(app_cfg.task_countdown - 1)

                if self._exit(): return
                run_dubbing_with_text_review()

                from videotrans.dub.quality_manifest import unresolved_queue_indices
                quality_failed = unresolved_queue_indices(trk.queue_tts)
                needs_dubbing_proof = (
                    not trk.ignore_align and (manual_proof or bool(quality_failed)))
                if needs_dubbing_proof:
                    for it in trk.queue_tts:
                        if self._exit(): return
                        # 当前配音时长,0=不存在配音文件
                        it['dubbing_s'] = (len(AudioSegment.from_file(it['filename'])) if vail_file(
                            it['filename']) else 0) / 1000.0

                    # 存入临时目录
                    Path(f'{trk.cfg.cache_folder}/queue_tts.json').write_text(
                        json.dumps(trk.queue_tts, ensure_ascii=False), encoding='utf-8')

                    app_cfg.set_countdown(86400)
                    if quality_failed:
                        run_state.start_stage(
                            'quality_review', {'failed_segments': len(quality_failed)})
                        performance.start_stage(
                            'quality_review', {'failed_segments': len(quality_failed)})
                        self._post(
                            text=(f"质量核对已隔离 {len(quality_failed)} 个异常片段；"
                                  "成功片段均已保留，请只返工红色标记片段。"))
                    # 等待修改配音结果或重新配音
                    # 追加视频路径与原声 wav，供时间轴预览使用（旧字段顺序保持兼容）
                    self._post(
                        text=(f"{trk.cfg.cache_folder}<|>{trk.cfg.target_language_code}"
                              f"<|>{trk.cfg.name}<|>{trk.cfg.source_wav}"
                              f"<|>{trk.cfg.source_language_code}<|>{trk.cfg.translate_type}"
                              f"<|>{trk.cfg.target_language_code}<|>{trk.cfg.source_sub}"),
                        type='edit_dubbing')
                    self._post(text=tr('The subtitle editing interface is rendering'))
                    while app_cfg.task_countdown > 0:
                        if self._exit(): return
                        time.sleep(1)
                        app_cfg.set_countdown(app_cfg.task_countdown - 1)

                    # 编辑界面把修改写回了 queue_tts.json，重载后 align/最终字幕才能反映编辑
                    qfile = Path(f'{trk.cfg.cache_folder}/queue_tts.json')
                    try:
                        data = json.loads(qfile.read_text(encoding='utf-8'))
                        if isinstance(data, list) and data:
                            trk.queue_tts = data
                        else:
                            logger.warning('queue_tts.json 为空或格式异常，保留内存中的原列表')
                    except Exception as e:
                        logger.warning(f'重载 queue_tts.json 失败，保留原列表: {e}')
                    if quality_failed:
                        unresolved = unresolved_queue_indices(trk.queue_tts)
                        if unresolved:
                            error = f'仍有 {len(unresolved)} 个质量异常片段未处理'
                            run_state.fail_stage('quality_review', error)
                            performance.finish_stage(
                                'quality_review', status='failed', error=error)
                            raise DubbingSrtError(error)
                        run_state.complete_stage(
                            'quality_review', {'repaired_segments': len(quality_failed)})
                        performance.finish_stage(
                            'quality_review',
                            metadata={'repaired_segments': len(quality_failed)})

            # 保存可重开编辑工程（此刻逐行配音尚未被 align 变速，是原始未变速版本），
            # 供任务完成后从最近任务反复打开工作台编辑、仅重跑对齐+合成
            if not trk.cfg.only_out_mp4 and getattr(trk, 'should_dubbing', False):
                try:
                    from videotrans.task.project import save_project
                    save_project(trk.cfg, trk.queue_tts, trk.cfg.cache_folder)
                except Exception as e:
                    logger.warning(f'保存编辑工程失败，跳过: {e}')

            if self._exit(): return
            run_stage('align', trk.align)

            if self._exit(): return
            run_stage('recognize_second_pass', trk.recogn2pass)
            if trk.should_recogn2 and manual_proof:
                app_cfg.set_countdown(86400)
                # 等待修改二次识别出的字幕
                self._post(text=f'{trk.cfg.target_sub}<|>{trk.cfg.name}',
                           type="edit_recogn2_subtitle")
                self._post(text=tr('The subtitle editing interface is rendering'))
                while app_cfg.task_countdown > 0:
                    if self._exit(): return
                    time.sleep(1)
                    app_cfg.set_countdown(app_cfg.task_countdown - 1)

            if self._exit(): return
            run_stage('assemble', trk.assembling)

            if self._exit(): return
            trk.task_done()
            output_video = trk.final_output_video()
            run_state.finish_run('completed', artifacts={
                'expect_video': bool(trk.should_hebing and not trk.is_audio_trans),
                'output_video': output_video or '',
            })
            performance.finish('completed')
            run_finished = True
            self._post(text="", type='end')
        except Exception as e:
            from videotrans.configure.excepts import get_msg_from_except
            logger.exception(f'单视频模式翻译失败{e}',exc_info=True)
            except_msg = get_msg_from_except(e)
            msg=f"{except_msg}\n{traceback.format_exc()}\n"
            if trk:
                msg+=f'cfg={trk.cfg}'
            if run_state:
                run_state.finish_run('failed', except_msg)
                if performance:
                    performance.finish('failed', except_msg)
                run_finished = True
            self._post(text=msg, type='error')
        finally:
            if run_state and not run_finished:
                run_state.finish_run('interrupted')
                if performance:
                    performance.finish('interrupted')
            app_cfg.release_project_task(self.uuid)

    def _post(self, text='', type='logs'):
        try:
            if self.uuid in app_cfg.stoped_uuid_set: return
            self.uito.emit(self.uuid, SignMsg(**{"text": text, "type": type, 'uuid': self.uuid}))
        except (ValueError,IndexError,TypeError):
            pass

    def _exit(self):
        if app_cfg.exit_soft or app_cfg.current_status != 'ing':
            return True
        return False
