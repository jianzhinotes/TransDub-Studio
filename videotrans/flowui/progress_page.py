"""Flow UI 进度页：每任务一张 TaskCard（六阶段步进器 + 进度条 + 日志尾行）。

消息来源：win_action.flow_observer 镜像（覆盖 SignalHub 与 only_one uito 两条通道）。
Studio/编辑弹窗由既有 update_data 打开，本页只反映"等待编辑"状态。
"""
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from videotrans.configure.config import TEMP_ROOT, logger, tr
from videotrans.flowui import recent_tasks, stages

_EDIT_TYPES = {'edit_dubbing', 'edit_subtitle_source', 'edit_subtitle_target',
               'edit_recogn2_subtitle'}

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


class TaskCard(QFrame):
    editRequested = Signal(str)   # 携带工程目录，请求打开工作台重新编辑

    def __init__(self, *, uuid: str, video_path: str, target_dir: str, parent=None):
        super().__init__(parent)
        self.uuid = uuid
        self.video_path = video_path
        self.target_dir = target_dir
        self.stage = stages.STAGE_PREPARE
        self.done = False
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

    def set_done(self, ok: bool, err: str = ''):
        self.done = True
        if ok:
            self.set_stage(stages.STAGE_ASSEMBLE)
            for lbl, key in zip(self.stage_labels, _STAGE_KEYS):
                lbl.setText('● ' + tr(key))
                lbl.setObjectName('stageDone')
                lbl.style().unpolish(lbl)
                lbl.style().polish(lbl)
            self.bar.setValue(100)
            self.set_state('✨ ' + tr('flow_status_succeed'), 'doneBanner')
            self.open_btn.setVisible(True)
            self.preview_btn.setVisible(True)
            self.edit_btn.setVisible(bool(self._project_dir()))
        else:
            self.set_state(tr('flow_status_error'), 'errState')
            self.set_log(err)
            self.open_btn.setVisible(bool(self.target_dir))

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
            # uuid_queue_mp4: [name, target_dir]（_actions.py create_btns 填充）
            video_path, target_dir = info[0], info[1]
        card = TaskCard(uuid=uuid, video_path=video_path or uuid, target_dir=target_dir)
        card.editRequested.connect(self.editRequested)
        self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
        self.cards[uuid] = card
        return card

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
                    card.set_done(True)
            return
        if not uuid:
            return
        card = self._ensure_card(uuid)

        if self._markers is None:
            self._markers = stages.stage_markers()

        if mtype == 'set_precent':
            _secs, pct = stages.parse_percent(text)
            if pct is not None:
                card.set_percent(pct)
                card.set_stage(stages.stage_from_percent(pct, card.stage))
                if pct >= 100 and not card.done:
                    card.set_done(True)
        elif mtype == 'logs':
            card.set_log(text)
            card.set_stage(stages.stage_from_text(text, card.stage, self._markers))
        elif mtype in _EDIT_TYPES:
            card.set_state(tr('flow_waiting_edit'), 'editState')
        elif mtype == 'replace_subtitle':
            pass
        elif mtype == 'succeed':
            card.set_done(True)
            if card.video_path:
                recent_tasks.update_status(card.video_path, recent_tasks.STATUS_SUCCEED)
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
