"""工作区页：导入→配置→处理→内嵌编辑工作台→导出，一体连贯（ElevenLabs 式）。

顶层 QStackedWidget：
  normal 视图 = 左视频预览 + 右栈（配置 / 进度）
  editing 视图 = 全区内嵌的配音/字幕校对工作台（非弹窗）
处理完成后自动进入编辑工作台校对，「导出成品」只重跑对齐+合成，「返回」回到完成态。
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QMessageBox, QStackedWidget, QVBoxLayout, QWidget,
)

from videotrans.configure.config import tr
from videotrans.flowui.config_page import ConfigPage
from videotrans.flowui.progress_page import ProgressPage
from videotrans.flowui.video_preview_panel import VideoPreviewPanel


class WorkspacePage(QWidget):
    back_requested = Signal()

    def __init__(self, *, flow, parent=None):
        super().__init__(parent)
        self.flow = flow
        self.setObjectName('pageWorkspace')
        self._realign = None
        self._editor = None
        self._proof = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._top = QStackedWidget()
        outer.addWidget(self._top)

        # normal 视图：左预览 + 右栈
        self._normal = QWidget()
        nlay = QHBoxLayout(self._normal)
        nlay.setContentsMargins(0, 0, 0, 0)
        nlay.setSpacing(0)
        self.preview = VideoPreviewPanel()
        nlay.addWidget(self.preview, stretch=52)
        self.right_stack = QStackedWidget()
        self.config_page = ConfigPage(flow=flow)
        self.progress_page = ProgressPage(flow=flow)
        self.right_stack.addWidget(self.config_page)
        self.right_stack.addWidget(self.progress_page)
        nlay.addWidget(self.right_stack, stretch=48)
        self._top.addWidget(self._normal)   # index 0

        # 态切换接线
        self.config_page.started.connect(self.show_processing)
        self.config_page.start_failed.connect(self.show_configure)
        self.config_page.back_requested.connect(self._on_back)
        self.progress_page.back_home.connect(self._on_back)
        self.progress_page.editRequested.connect(self.open_editor)

    # ---- 载入 ----
    def load(self, files: list):
        self._top.setCurrentWidget(self._normal)
        self.preview.load(files)
        self.config_page.load(files)
        self.show_configure()

    def set_workers_ready(self, ready: bool):
        self.config_page.set_workers_ready(ready)

    # ---- normal 视图三态 ----
    def show_configure(self):
        self._top.setCurrentWidget(self._normal)
        self.right_stack.setCurrentWidget(self.config_page)

    def show_processing(self):
        self._top.setCurrentWidget(self._normal)
        self.right_stack.setCurrentWidget(self.progress_page)

    def show_done(self):
        self._top.setCurrentWidget(self._normal)
        self.right_stack.setCurrentWidget(self.progress_page)

    # ---- 任务消息（FlowWidget 把 win_action.flow_observer 指向这里） ----
    def on_message(self, uuid: str, d: dict):
        t = d.get('type')
        # 分步内嵌校对：识别后/翻译后/二次识别后校字幕，配音后校对齐
        if t in ('edit_subtitle_source', 'edit_subtitle_target',
                 'edit_recogn2_subtitle', 'edit_dubbing'):
            self._enter_proof(t, d)
            return
        self.progress_page.on_message(uuid, d)
        if t == 'succeed' and uuid:
            self._load_result(uuid)
            self._backfill_project_dir(uuid)

    def _backfill_project_dir(self, uuid: str):
        """完成后把真实工程目录回填进最近任务，供下次从首页重新打开编辑。"""
        card = self.progress_page.cards.get(uuid)
        pd = card._project_dir() if card else None
        if pd:
            from videotrans.flowui import recent_tasks
            recent_tasks.update_fields(card.video_path, project_dir=pd)

    def _load_result(self, uuid: str):
        """把成品视频加载进左侧预览区（返回完成态时可与原片切换对比）。
        用进度卡片的 target_dir（视频名子文件夹，非 uuid_queue_mp4 的父级目录）。"""
        card = self.progress_page.cards.get(uuid)
        info = getattr(self.flow.win_action, 'uuid_queue_mp4', {}).get(uuid)
        source = info[0] if info else ''
        target_dir = card.target_dir if card else (info[1] if info else '')
        output = None
        if target_dir and Path(target_dir).is_dir():
            vids = sorted(Path(target_dir).rglob('*.mp4'), key=lambda p: p.stat().st_mtime)
            output = vids[-1].as_posix() if vids else None
        self.preview.show_result(source, output)

    # ---- 分步内嵌校对 ----
    def _enter_proof(self, mtype: str, d: dict):
        from videotrans.configure.config import app_cfg
        wa = self.flow.win_action
        if mtype == 'edit_dubbing':
            # 配音校对：内嵌配音工作台（时间轴/卡片/重配/A-B）
            parts = d['text'].split('<|>')
            cache_folder = parts[0]
            language = parts[1] if len(parts) > 1 else ''
            video_path = parts[2] if len(parts) > 2 and parts[2] else None
            source_wav = parts[3] if len(parts) > 3 and parts[3] else None
            source_language = parts[4] if len(parts) > 4 and parts[4] else None
            self.preview.stop()
            self._destroy_editor()
            from videotrans.component.timeline.studio import DubbingStudioDialog
            self._editor = DubbingStudioDialog(
                cache_folder=cache_folder, language=language, video_path=video_path,
                source_wav=source_wav, source_language=source_language,
                embedded=True, parent=self)
            # 内嵌配音校对：主按钮"下一步"继续流水线（保存 queue_tts.json 已在 studio 内）
            self._editor.proof_done.connect(self._resume_pipeline)
            self._editor.back_requested.connect(self._terminate_pipeline)
            self._top.addWidget(self._editor)
            self._top.setCurrentWidget(self._editor)
            return

        # 字幕/译文校对
        from videotrans.flowui.inline_subtitle_editor import (
            InlineSubtitleEditor, MODE_SOURCE, MODE_TARGET)
        self._destroy_proof()
        if mtype == 'edit_subtitle_source':
            self._proof = InlineSubtitleEditor(
                mode=MODE_SOURCE, sub_path=app_cfg.onlyone_source_sub)
        elif mtype == 'edit_recogn2_subtitle':
            self._proof = InlineSubtitleEditor(
                mode=MODE_SOURCE, sub_path=app_cfg.onlyone_target_sub)
        else:  # edit_subtitle_target
            main = self.flow.main
            self._proof = InlineSubtitleEditor(
                mode=MODE_TARGET, sub_path=app_cfg.onlyone_target_sub,
                source_sub=app_cfg.onlyone_source_sub if app_cfg.onlyone_trans else None,
                translate_type=main.translate_type.currentIndex(),
                source_code=main.source_language.currentText(),
                target_code=main.target_language.currentText())
        self._proof.proofDone.connect(self._resume_pipeline)
        self._proof.proofTerminate.connect(self._terminate_pipeline)
        self._top.addWidget(self._proof)
        self._top.setCurrentWidget(self._proof)

    def _destroy_proof(self):
        p = getattr(self, '_proof', None)
        if p:
            self._top.removeWidget(p)
            p.deleteLater()
            self._proof = None

    def _resume_pipeline(self):
        """校对完：结束 worker 的 countdown 等待，切回进度态。"""
        self._destroy_proof()
        self._destroy_editor()
        self.show_processing()
        self.flow.win_action.set_djs_timeout()

    def _terminate_pipeline(self):
        self._destroy_proof()
        self._destroy_editor()
        self.flow.win_action.update_status('stop')
        self.show_processing()

    def _enter_editing_for(self, uuid: str):
        # 复用进度卡片计算的工程目录（其 target_dir 是正确的视频名子文件夹）
        card = self.progress_page.cards.get(uuid)
        pd = card._project_dir() if card else None
        if pd:
            self.show_editing(pd)

    # ---- 内嵌编辑工作台 ----
    def open_editor(self, proj_dir: str):
        self.show_editing(proj_dir)

    def show_editing(self, proj_dir: str):
        if not proj_dir or not Path(proj_dir).is_dir():
            return
        # 解绑工作区预览的视频层，避免与工作台的 QVideoWidget 在 macOS 冲突段错误
        self.preview.release_video()
        self._destroy_editor()
        from videotrans.component.timeline.studio import DubbingStudioDialog
        self._editor = DubbingStudioDialog(project_dir=proj_dir, embedded=True, parent=self)
        self._editor.regenerate_requested.connect(self._start_realign)
        self._editor.back_requested.connect(self._exit_editing)
        self._top.addWidget(self._editor)
        self._top.setCurrentWidget(self._editor)

    def _destroy_editor(self):
        if self._editor:
            self._top.removeWidget(self._editor)
            self._editor.deleteLater()
            self._editor = None

    def _exit_editing(self):
        self._destroy_editor()
        self.preview.resume_video()
        self.show_done()

    # ---- 导出（只重跑对齐+合成） ----
    def _start_realign(self, proj_dir: str):
        from videotrans.task.realign import RealignWorker
        if self._realign and self._realign.isRunning():
            return
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        self._realign = RealignWorker(proj_dir)
        self._realign.succeeded.connect(self._on_realign_done)
        self._realign.failed.connect(self._on_realign_failed)
        self._realign.start()

    def _on_realign_done(self, new_mp4: str):
        QApplication.restoreOverrideCursor()
        self._exit_editing()
        self.preview.set_video_hidden(False)
        self.preview.load_output(new_mp4)
        QMessageBox.information(self, tr('Dubbing Studio'), tr('flow_regenerate_done'))

    def _on_realign_failed(self, msg: str):
        QApplication.restoreOverrideCursor()
        QMessageBox.warning(self, tr('anerror'), msg)

    def _on_back(self):
        self.preview.stop()
        self.back_requested.emit()
