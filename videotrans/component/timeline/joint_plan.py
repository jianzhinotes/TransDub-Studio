"""Dubbing Studio 的联合编排后台任务与只读结果窗口。"""

import traceback
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHeaderView, QLabel,
    QHBoxLayout, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from videotrans.configure.config import logger, tr
from videotrans.dub.presentation import build_plan_view
from videotrans.task.joint_dub import run_joint_preview, synthesize_joint_plan
from videotrans.util import tools


class _DaemonWorker(QObject):
    """用 daemon Python 线程承载长网络/TTS任务，关闭应用时不触发 QThread fatal。"""

    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._thread = None
        self._cancelled = threading.Event()

    def start(self):
        if self.isRunning():
            return
        self._thread = threading.Thread(
            target=self._run_and_finish,
            name=self.__class__.__name__, daemon=True)
        self._thread.start()

    def _run_and_finish(self):
        try:
            self.run()
        finally:
            if not self._cancelled.is_set():
                self.finished.emit()

    def cancel(self):
        self._cancelled.set()

    def cancelled(self):
        return self._cancelled.is_set()

    def isRunning(self):
        return bool(self._thread and self._thread.is_alive())

    def wait(self, milliseconds=None):
        if not self._thread:
            return True
        timeout = None if milliseconds is None else max(milliseconds, 0) / 1000.0
        self._thread.join(timeout)
        return not self._thread.is_alive()


class JointPlanningWorker(_DaemonWorker):
    done = Signal(object, object)
    failed = Signal(str)

    def __init__(self, *, queue_tts, source_video, source_language,
                 target_language, name, candidate_dir, project_dir,
                 candidate_backend, limit=20, parent=None):
        super().__init__(parent=parent)
        self._kwargs = {
            "queue_tts": queue_tts,
            "source_video": source_video,
            "source_language": source_language,
            "target_language": target_language,
            "name": name,
            "candidate_dir": candidate_dir,
            "project_dir": project_dir,
            "candidate_backend": candidate_backend,
            "limit": limit,
            "synthesize": False,
        }

    def run(self):
        try:
            project, plan = run_joint_preview(**self._kwargs)
            if not self.cancelled():
                self.done.emit(project, plan)
        except Exception as error:
            logger.exception(f"联合编排预览失败: {error}", exc_info=True)
            if not self.cancelled():
                self.failed.emit(f"{error}\n{traceback.format_exc()}")


class JointSynthesisWorker(_DaemonWorker):
    done = Signal(object, object)
    failed = Signal(str)

    def __init__(self, *, project, plan_id, candidate_dir, tts_type,
                 language, project_dir=None, uuid=None, is_cuda=False,
                 parent=None):
        super().__init__(parent=parent)
        self._kwargs = {
            "project": project,
            "plan_id": plan_id,
            "candidate_dir": candidate_dir,
            "tts_type": tts_type,
            "language": language,
            "project_dir": project_dir,
            "uuid": uuid,
            "is_cuda": is_cuda,
        }

    def run(self):
        try:
            project, plan = synthesize_joint_plan(**self._kwargs)
            if not self.cancelled():
                self.done.emit(project, plan)
        except Exception as error:
            logger.exception(f"联合编排 A/B 音频生成失败: {error}", exc_info=True)
            if not self.cancelled():
                self.failed.emit(f"{error}\n{traceback.format_exc()}")


class JointPlanPreviewDialog(QDialog):
    seekRequested = Signal(int)
    synthesisRequested = Signal(str)

    def __init__(self, plan, project=None, can_synthesize=True, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.project = project
        self.view = build_plan_view(plan, project)
        self.setWindowTitle(tr("Smart version"))
        self.setMinimumSize(760, 360)
        self.resize(900, 420)

        layout = QVBoxLayout(self)
        risks = self.view["risk_counts"]
        diagnostics = self.view["diagnostic_counts"]
        heading = QLabel(tr("Smart optimization is ready"))
        heading.setStyleSheet("font-size:22px;font-weight:600;")
        layout.addWidget(heading)
        summary = QLabel(tr(
            "{0} segments are ready; {1} need review after voice generation."
        ).format(
            len(self.view["rows"]), risks["stretch"] + risks["overflow"]))
        summary.setWordWrap(True)
        layout.addWidget(summary)

        if diagnostics["partial"] or diagnostics["fallback"]:
            warning = QLabel(tr(
                "Some segments automatically used the local fallback; no action is required."
            ))
            warning.setStyleSheet("color:#E0A94F;")
            layout.addWidget(warning)

        note = QLabel(tr("Your subtitles and audio stay unchanged until you confirm."))
        note.setStyleSheet("color:#9AA7B4;")
        layout.addWidget(note)

        self.details_btn = QPushButton(tr("Show details"))
        self.details_btn.setCheckable(True)
        self.details_btn.setMaximumWidth(150)
        self.details_btn.toggled.connect(self._toggle_details)
        layout.addWidget(self.details_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        headers = [
            tr("Segment"), tr("Window"), tr("Source text"), tr("Current text"),
            tr("Planned text"), tr("Predicted"), tr("Stretch"),
        ]
        self.table = QTableWidget(len(self.view["rows"]), len(headers), self)
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for column in (2, 3, 4):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch)

        self._populate_table()
        self.table.cellDoubleClicked.connect(self._seek_row)
        self.table.currentCellChanged.connect(self._on_current_row_changed)
        self.table.setVisible(False)
        layout.addWidget(self.table, stretch=1)

        audio = QHBoxLayout()
        self.audio_status = QLabel(tr("The smart text is ready. Generate voice to listen."))
        self.audio_status.setStyleSheet("color:#9AA7B4;")
        audio.addWidget(self.audio_status, stretch=1)
        self.play_current_btn = QPushButton(tr("Play original version"))
        self.play_current_btn.clicked.connect(self._play_current)
        audio.addWidget(self.play_current_btn)
        self.play_planned_btn = QPushButton(tr("Play smart version"))
        self.play_planned_btn.clicked.connect(self._play_planned)
        audio.addWidget(self.play_planned_btn)
        self.synthesize_btn = QPushButton(tr("Generate smart voice"))
        self.synthesize_btn.setObjectName('startBtn')
        self.synthesize_btn.setMinimumWidth(220)
        self.synthesize_btn.setEnabled(bool(can_synthesize))
        self.synthesize_btn.clicked.connect(
            lambda: self.synthesisRequested.emit(self.plan.id))
        audio.addWidget(self.synthesize_btn)
        self.close_btn = QPushButton(tr("Close"))
        self.close_btn.clicked.connect(self.reject)
        audio.addWidget(self.close_btn)
        layout.addLayout(audio)
        if self.view["rows"]:
            self.table.selectRow(0)
            self._on_current_row_changed(0, 0, -1, -1)

    def _toggle_details(self, visible):
        self.table.setVisible(visible)
        self.details_btn.setText(tr("Hide details") if visible else tr("Show details"))
        if visible:
            self.resize(max(self.width(), 1180), max(self.height(), 680))
        else:
            self.resize(900, 420)

    def _populate_table(self):
        self.table.setRowCount(len(self.view["rows"]))
        for row_index, row in enumerate(self.view["rows"]):
            values = [
                f"#{row['index']} ({row['unit_count']})",
                f"{row['window_ms'] / 1000:.2f}s",
                row["source_text"],
                row["baseline_text"],
                row["selected_text"],
                f"{row['predicted_duration_ms'] / 1000:.2f}s\n{row['selected_kind']}",
                f"{row['stretch_ratio']:.2f}×",
            ]
            color = None
            if row["risk"] == "overflow":
                color = QColor("#5A2424")
            elif row["risk"] == "stretch":
                color = QColor("#59461F")
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(row["candidate_details"])
                if color is not None:
                    item.setBackground(color)
                self.table.setItem(row_index, column, item)
            self.table.setRowHeight(row_index, 72)

    def update_result(self, project, plan):
        self.project = project
        self.plan = plan
        self.view = build_plan_view(plan, project)
        self._populate_table()
        ready = sum(1 for row in self.view["rows"] if row["audio_validated"])
        self.audio_status.setText(
            tr("A/B audio ready: {0}/{1} segments").format(
                ready, len(self.view["rows"])))
        self.set_synthesis_busy(False)
        self.synthesize_btn.setText(tr("Regenerate smart voice"))
        if self.view["rows"]:
            self.table.selectRow(0)
            self._on_current_row_changed(0, 0, -1, -1)

    def set_synthesis_busy(self, busy, message=""):
        self.synthesize_btn.setDisabled(bool(busy))
        if message:
            self.audio_status.setText(message)

    def _row(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.view["rows"]):
            return self.view["rows"][row]
        return None

    def _on_current_row_changed(self, current_row, _current_column,
                                _previous_row, _previous_column):
        row = self.view["rows"][current_row] if 0 <= current_row < len(self.view["rows"]) else None
        self.play_current_btn.setEnabled(bool(row and row["current_audio_paths"]))
        self.play_planned_btn.setEnabled(bool(
            row and row["planned_audio_path"] and Path(row["planned_audio_path"]).is_file()))

    @staticmethod
    def _play_paths(paths):
        for path in paths:
            if Path(path).is_file():
                tools.pygameaudio(path)

    def _play_current(self):
        row = self._row()
        if row:
            threading.Thread(
                target=self._play_paths,
                args=(list(row["current_audio_paths"]),), daemon=True).start()

    def _play_planned(self):
        row = self._row()
        if row and row["planned_audio_path"]:
            threading.Thread(
                target=self._play_paths,
                args=([row["planned_audio_path"]],), daemon=True).start()

    def _seek_row(self, row, _column):
        if 0 <= row < len(self.view["rows"]):
            self.seekRequested.emit(self.view["rows"][row]["start_ms"])
