"""Flow UI 进度页：每任务一张 TaskCard（六阶段步进器 + 进度条 + 日志尾行）。

消息来源：win_action.flow_observer 镜像（覆盖 SignalHub 与 only_one uito 两条通道）。
Studio/编辑弹窗由既有 update_data 打开，本页只反映"等待编辑"状态。
"""
from pathlib import Path
import time

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from videotrans.configure.config import TEMP_ROOT, logger, tr
from videotrans.flowui import recent_tasks, stages

_EDIT_TYPES = {'edit_dubbing', 'edit_subtitle_source', 'edit_subtitle_target',
               'edit_subtitle_bilingual', 'edit_recogn2_subtitle'}

_QSS = """
#pageProgress QFrame#taskCard { border: 1px solid #2E3947; border-radius: 8px; background: #1C232D; }
#pageProgress QLabel#taskName { font-size: 14px; font-weight: bold; color: #E6E9EC; }
#pageProgress QLabel#lastLog { color: #9AA7B4; font-size: 12px; }
#pageProgress QLabel#stageDone { color: #2E7CF6; font-weight: bold; }
#pageProgress QLabel#stageCurrent { color: #E6E9EC; font-weight: bold; }
#pageProgress QLabel#stagePending { color: #2E3947; }
#pageProgress QLabel#editState { color: #f39c12; }
#pageProgress QLabel#errState { color: #ff4d4d; }
#pageProgress QLabel#doneBanner { color: #2ecc71; font-size: 15px; font-weight: bold; }
"""

_STAGE_KEYS = ['flow_stage_prepare', 'flow_stage_recogn', 'flow_stage_trans',
               'flow_stage_dubbing', 'flow_stage_align', 'flow_stage_assemble']

_JOURNAL_STAGE = {
    'prepare': stages.STAGE_PREPARE,
    'recognize': stages.STAGE_RECOGN,
    'diarize': stages.STAGE_RECOGN,
    'translate': stages.STAGE_TRANS,
    'dubbing': stages.STAGE_DUBBING,
    'quality_review': stages.STAGE_DUBBING,
    'align': stages.STAGE_ALIGN,
    'recognize_second_pass': stages.STAGE_ALIGN,
    'assemble': stages.STAGE_ASSEMBLE,
}


def _live_duration(payload, now=None):
    """Return a finished duration or derive an honest live elapsed value."""
    duration = payload.get('duration_s') if isinstance(payload, dict) else None
    if duration is not None:
        return max(float(duration), 0.0)
    if not isinstance(payload, dict) or payload.get('status') != 'running':
        return None
    started_at = payload.get('started_at')
    try:
        return max(float(now if now is not None else time.time()) - float(started_at), 0.0)
    except (TypeError, ValueError):
        return None


def _format_duration(seconds):
    if seconds is None:
        return '—'
    seconds = max(float(seconds), 0.0)
    if seconds < 60:
        return f'{seconds:.1f}s'
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f'{minutes}m {remainder:02d}s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h {minutes:02d}m'


def _diagnostics_message(report, *, now=None):
    resources = report.get('resources') or {}
    stages_data = report.get('stages') or {}
    stage_lines = []
    for name in ('prepare', 'recognize', 'diarize', 'translate', 'dubbing',
                 'align', 'recognize_second_pass', 'assemble'):
        stage = stages_data.get(name)
        if stage:
            stage_lines.append(
                f"{name}: {stage.get('status', '')}  "
                f"{_format_duration(_live_duration(stage, now=now))}")
    context = report.get('context') or {}
    dubbing_metrics = (stages_data.get('dubbing') or {}).get('metadata') or {}
    quality_known = any(
        key in dubbing_metrics for key in ('quality_passed', 'quality_failed'))
    quality_line = (
        f"质量核对: 通过 {dubbing_metrics.get('quality_passed', 0)} / "
        f"待处理 {dubbing_metrics.get('quality_failed', 0)}"
        if quality_known else "质量核对: 尚未开始"
    )
    cache_hits = dubbing_metrics.get('tts_cache_hits')
    cache_line = f"配音缓存命中: {cache_hits} 段" if cache_hits is not None else "配音缓存命中: 统计中"
    return '\n'.join([
        f"状态: {report.get('status', '')}",
        f"TTS: {context.get('tts_type', '')}  ASR: {context.get('recogn_model', '')}",
        f"总耗时: {_format_duration(_live_duration(report, now=now))}",
        f"峰值进程内存: {resources.get('peak_process_tree_rss_mb') or 0} MB",
        f"峰值系统内存占用: {resources.get('peak_system_memory_percent') or 0}%",
        f"最低可用内存: {resources.get('lowest_available_memory_mb') or 0} MB",
        f"最高资源压力: {resources.get('peak_pressure') or 'normal'}",
        f"配音实时率 RTF: {dubbing_metrics.get('real_time_factor', '—')}",
        cache_line,
        quality_line,
        '',
        *stage_lines,
    ])


class TaskCard(QFrame):
    editRequested = Signal(str)   # 携带工程目录，请求打开工作台重新编辑

    def __init__(self, *, uuid: str, video_path: str, target_dir: str, parent=None):
        super().__init__(parent)
        self.uuid = uuid
        self.video_path = video_path
        self.target_dir = target_dir
        self.stage = stages.STAGE_PREPARE
        self.done = False
        self._run_state_file = None
        self._output_video = ''
        self.setObjectName('taskCard')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        head = QHBoxLayout()
        name = QLabel(Path(video_path).name)
        name.setObjectName('taskName')
        head.addWidget(name)
        head.addStretch(1)
        self.state_label = QLabel('')
        head.addWidget(self.state_label)
        layout.addLayout(head)

        stepper = QHBoxLayout()
        self.stage_labels = []
        for i, key in enumerate(_STAGE_KEYS):
            lbl = QLabel(('● ' if i == 0 else '○ ') + tr(key))
            lbl.setObjectName('stageCurrent' if i == 0 else 'stagePending')
            stepper.addWidget(lbl)
            self.stage_labels.append(lbl)
            if i < len(_STAGE_KEYS) - 1:
                sep = QLabel('—')
                sep.setObjectName('stagePending')
                stepper.addWidget(sep)
        stepper.addStretch(1)
        layout.addLayout(stepper)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setMaximumHeight(10)
        self.bar.setTextVisible(False)
        layout.addWidget(self.bar)

        self.last_log = QLabel('')
        self.last_log.setObjectName('lastLog')
        layout.addWidget(self.last_log)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.diagnostics_btn = QPushButton('诊断信息')
        self.diagnostics_btn.clicked.connect(self._open_diagnostics)
        self.diagnostics_btn.setVisible(False)
        btns.addWidget(self.diagnostics_btn)
        self.open_btn = QPushButton(tr('flow_open_folder'))
        self.open_btn.clicked.connect(self._open_folder)
        self.open_btn.setVisible(False)
        btns.addWidget(self.open_btn)
        self.preview_btn = QPushButton(tr('flow_timeline_preview'))
        self.preview_btn.clicked.connect(self._open_preview)
        self.preview_btn.setVisible(False)
        btns.addWidget(self.preview_btn)
        self.edit_btn = QPushButton(tr('flow_reedit'))
        self.edit_btn.setObjectName('startBtn')
        self.edit_btn.clicked.connect(self._on_edit)
        self.edit_btn.setVisible(False)
        btns.addWidget(self.edit_btn)
        layout.addLayout(btns)

    # ---- 状态更新 ----
    def set_stage(self, stage: int):
        stage = max(stage, self.stage)
        if stage == self.stage and stage != stages.STAGE_PREPARE:
            return
        self.stage = stage
        for i, lbl in enumerate(self.stage_labels):
            text = tr(_STAGE_KEYS[i])
            if i < stage:
                lbl.setText('● ' + text)
                lbl.setObjectName('stageDone')
            elif i == stage:
                lbl.setText('● ' + text)
                lbl.setObjectName('stageCurrent')
            else:
                lbl.setText('○ ' + text)
                lbl.setObjectName('stagePending')
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)

    def set_percent(self, percent: int):
        self.bar.setValue(max(self.bar.value(), int(percent)))

    def set_log(self, text: str):
        if text:
            self.last_log.setText(str(text)[:160])

    def set_state(self, text: str, obj_name: str = ''):
        self.state_label.setText(text)
        self.state_label.setObjectName(obj_name)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)

    def output_video_path(self, reported_path=None):
        """Find this task's final render without mistaking cache media for output."""
        if not self.target_dir or not self.video_path:
            return None
        name = Path(self.video_path).stem
        for raw_path in (reported_path, self._output_video):
            if not raw_path:
                continue
            candidate = Path(raw_path)
            try:
                if (candidate.is_file() and candidate.stat().st_size > 0
                        and candidate.resolve() != Path(self.video_path).resolve()):
                    return candidate.as_posix()
            except OSError:
                continue
        # Normal output lives inside the dedicated child folder. Do not search
        # its parent: it can be the source directory, where finding the input
        # clip would turn another failed export into a false success.
        root = Path(self.target_dir)
        for suffix in ('.mp4', '.mkv', '.mov', '.webm', '.avi'):
            candidate = root / f'{name}{suffix}'
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate.as_posix()
            except OSError:
                continue
        return None

    def set_done(self, ok: bool, err: str = '', expect_video=None, output_video=None):
        self.done = True
        if ok:
            output_video = self.output_video_path(output_video)
            # A success signal from a legacy worker, an overall queue "end",
            # or a 100% progress tick is not proof that FFmpeg copied a file.
            # Only explicit subtitle/audio-only jobs may complete without one.
            if expect_video is not False and not output_video:
                return self.set_done(
                    False,
                    '导出核验失败：任务没有生成有效成品视频。字幕和工程文件仍已保留，'
                    '请重新导出或查看诊断信息。')
            self._output_video = output_video or ''
            self.set_stage(stages.STAGE_ASSEMBLE)
            for lbl, key in zip(self.stage_labels, _STAGE_KEYS):
                lbl.setText('● ' + tr(key))
                lbl.setObjectName('stageDone')
                lbl.style().unpolish(lbl)
                lbl.style().polish(lbl)
            self.bar.setValue(100)
            state = ('✨ ' + tr('flow_status_succeed') if output_video
                     else '✨ ' + tr('flow_status_subtitle_succeed'))
            self.set_state(state, 'doneBanner')
            self.open_btn.setVisible(True)
            self.preview_btn.setVisible(bool(output_video))
            self.edit_btn.setVisible(bool(self._project_dir()))
            return True
        else:
            self.set_state(tr('flow_status_error'), 'errState')
            self.set_log(err)
            self.open_btn.setVisible(bool(self.target_dir))
            return False

    def reset_for_run(self):
        """Clear terminal visuals when the same project starts a new attempt."""
        self.done = False
        self.stage = stages.STAGE_PREPARE
        self._output_video = ''
        self.bar.setValue(0)
        self.set_state('')
        self.last_log.setText('')
        self.open_btn.setVisible(False)
        self.preview_btn.setVisible(False)
        self.edit_btn.setVisible(False)
        for index, (label, key) in enumerate(zip(self.stage_labels, _STAGE_KEYS)):
            label.setText(('● ' if index == 0 else '○ ') + tr(key))
            label.setObjectName('stageCurrent' if index == 0 else 'stagePending')
            label.style().unpolish(label)
            label.style().polish(label)

    def sync_run_state(self):
        """Apply the durable stage journal; return its effective status."""
        from videotrans.dub.run_state import (
            effective_status, find_run_state, load_run_state)
        if not self._run_state_file:
            self._run_state_file = find_run_state(
                self.target_dir, Path(self.video_path).stem)
        payload = load_run_state(self._run_state_file) if self._run_state_file else None
        if not payload:
            return ''
        status = effective_status(payload)
        if status == 'running' and self.done:
            self.reset_for_run()
        report_path = Path(self._run_state_file).parent / 'performance_report.json'
        self.diagnostics_btn.setVisible(report_path.is_file())
        current = payload.get('current_stage') or ''
        if current in _JOURNAL_STAGE:
            self.set_stage(_JOURNAL_STAGE[current])
        else:
            completed = [
                _JOURNAL_STAGE[name]
                for name, detail in (payload.get('stages') or {}).items()
                if name in _JOURNAL_STAGE and detail.get('status') == 'completed'
            ]
            if completed:
                self.set_stage(max(completed))
        if status == 'completed' and not self.done:
            artifacts = payload.get('artifacts') or {}
            self.set_done(
                True,
                expect_video=artifacts.get('expect_video'),
                output_video=artifacts.get('output_video'))
        elif status == 'failed' and not self.done:
            self.set_done(False, payload.get('last_error') or '')
        elif status == 'interrupted' and not self.done:
            self.done = True
            self.set_state(tr('flow_status_stopped'), 'lastLog')
            self.set_log(payload.get('last_error') or '')
            self.open_btn.setVisible(bool(self.target_dir))
        return status

    def _open_diagnostics(self):
        if not self._run_state_file:
            return
        from videotrans.dub.performance_report import load_performance_report
        report = load_performance_report(Path(self._run_state_file).parent)
        if not report:
            return
        message = _diagnostics_message(report)
        QMessageBox.information(self, 'TransDub Studio 诊断信息', message)

    # ---- 完成态动作 ----
    def _project_dir(self):
        """该任务的可编辑工程目录（存在才返回）。"""
        if not self.target_dir or not self.video_path:
            return None
        from videotrans.task.project import find_project
        return find_project(self.target_dir, Path(self.video_path).stem)

    def _on_edit(self):
        pd = self._project_dir()
        if pd:
            self.editRequested.emit(pd)

    def _open_folder(self):
        if self.target_dir and Path(self.target_dir).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.target_dir))

    def _open_preview(self):
        # 在输出目录找最新的视频+字幕做只读时间轴预览（v1 取最新修改时间者）
        try:
            tdir = Path(self.target_dir)
            videos = sorted(tdir.glob('*.mp4'), key=lambda p: p.stat().st_mtime)
            srts = sorted(tdir.glob('*.srt'), key=lambda p: p.stat().st_mtime)
            if not videos:
                return
            from videotrans.util import tools
            items = tools.get_subtitle_from_srt(srts[-1].as_posix()) if srts else []
            from videotrans.component.timeline import TimelinePreviewDialog
            dlg = TimelinePreviewDialog(
                video_path=videos[-1].as_posix(),
                subtitle_items=items,
                cache_folder=f'{TEMP_ROOT}/timeline_cache',
                parent=self)
            dlg.show()
            self._preview_dlg = dlg
        except Exception as e:
            logger.exception(f'打开时间轴预览失败: {e}', exc_info=True)


class ProgressPage(QWidget):
    back_home = Signal()
    editRequested = Signal(str)   # 转发某任务卡片的"重新编辑"，携带工程目录

    def __init__(self, *, flow, parent=None):
        super().__init__(parent)
        self.flow = flow
        self.cards = {}
        self._markers = None
        self._state_timer = QTimer(self)
        self._state_timer.setInterval(5000)
        self._state_timer.timeout.connect(self._sync_run_states)
        self.setObjectName('pageProgress')
        self.setStyleSheet(_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel(tr('flow_progress_title'))
        title.setStyleSheet('font-size:16px;font-weight:bold;color:#E6E9EC;')
        head.addWidget(title)
        head.addStretch(1)
        self.cancel_btn = QPushButton(tr('flow_cancel'))
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self._cancel)
        head.addWidget(self.cancel_btn)
        self.home_btn = QPushButton(tr('flow_back_home'))
        self.home_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.home_btn.clicked.connect(self.back_home)
        head.addWidget(self.home_btn)
        layout.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.cards_layout = QVBoxLayout(container)
        self.cards_layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, stretch=1)

    # ---- 卡片管理 ----
    def _ensure_card(self, uuid: str) -> TaskCard:
        card = self.cards.get(uuid)
        if card:
            return card
        wa = self.flow.win_action
        video_path, target_dir = '', ''
        info = getattr(wa, 'uuid_queue_mp4', {}).get(uuid)
        if info:
            # uuid_queue_mp4 保存的是重试用输出根目录；优先取视频专属目录，
            # 否则“打开文件夹”会只打开 _video_out 而不是成品所在子目录。
            video_path = info[0]
            target_dir = getattr(wa, 'uuid_output_dirs', {}).get(uuid, info[1])
        card = TaskCard(uuid=uuid, video_path=video_path or uuid, target_dir=target_dir)
        card.editRequested.connect(self.editRequested)
        self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
        self.cards[uuid] = card
        card.sync_run_state()
        if not card.done and not self._state_timer.isActive():
            self._state_timer.start()
        return card

    def _sync_run_states(self):
        active = False
        for card in self.cards.values():
            if not card.done:
                card.sync_run_state()
            active = active or not card.done
        if not active:
            self._state_timer.stop()

    def clear_done(self):
        for uuid in list(self.cards):
            card = self.cards.pop(uuid)
            card.setParent(None)
            card.deleteLater()

    # ---- 消息镜像入口（GUI 线程，由 update_data 顶部调用） ----
    def on_message(self, uuid: str, d: dict):
        mtype = d.get('type') or 'logs'
        text = d.get('text') or ''

        if mtype == 'end':
            # 全部任务完成：无 uuid 的整体信号
            self.cancel_btn.setVisible(False)
            for card in self.cards.values():
                if not card.done:
                    # ``end`` is a queue-level notification, not a per-video
                    # render receipt. Recover a journal if available; without
                    # one, surface an actionable failure instead of a lie.
                    card.sync_run_state()
                    if not card.done:
                        card.set_done(
                            False,
                            '任务队列已结束，但未收到该视频的有效导出结果。'
                            '请打开输出文件夹查看保留的字幕/工程并重新导出。')
            return
        if not uuid:
            return
        card = self._ensure_card(uuid)

        if card.done and mtype not in {'error', 'succeed', 'stop'}:
            card.reset_for_run()

        if self._markers is None:
            self._markers = stages.stage_markers()

        if mtype == 'set_precent':
            _secs, pct = stages.parse_percent(text)
            if pct is not None:
                card.set_percent(pct)
                card.set_stage(stages.stage_from_percent(pct, card.stage))
        elif mtype == 'logs':
            card.set_log(text)
            card.set_stage(stages.stage_from_text(text, card.stage, self._markers))
        elif mtype in _EDIT_TYPES:
            card.set_state(tr('flow_waiting_edit'), 'editState')
        elif mtype == 'replace_subtitle':
            pass
        elif mtype == 'succeed':
            succeeded = card.set_done(
                True,
                expect_video=d.get('expect_video'),
                output_video=d.get('output_video'))
            if succeeded and card.video_path:
                recent_tasks.update_status(card.video_path, recent_tasks.STATUS_SUCCEED)
            elif card.video_path:
                recent_tasks.update_status(card.video_path, recent_tasks.STATUS_ERROR)
        elif mtype == 'error':
            card.set_done(False, err=text)
            if card.video_path:
                recent_tasks.update_status(card.video_path, recent_tasks.STATUS_ERROR)
        elif mtype == 'stop':
            card.set_state(tr('flow_status_stopped'), 'lastLog')
            if card.video_path:
                recent_tasks.update_status(card.video_path, recent_tasks.STATUS_STOPPED)

    def _cancel(self):
        self.flow.win_action.update_status('stop')
        for card in self.cards.values():
            if not card.done:
                card.set_state(tr('flow_status_stopped'), 'lastLog')
                if card.video_path:
                    recent_tasks.update_status(card.video_path, recent_tasks.STATUS_STOPPED)
