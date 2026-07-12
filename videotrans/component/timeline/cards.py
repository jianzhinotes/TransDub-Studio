"""说话人卡片列表：每行字幕一张卡（原文+译文并排、音色、试听、重配、状态）。

卡片不直接改数据：所有编辑经 StudioState 提交；列表订阅 state 信号刷新对应卡。
"""
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from videotrans.configure.config import tr
from videotrans.component.timeline.edit_logic import (
    STATUS_EXCEEDED, STATUS_NO_AUDIO, STATUS_OK, STATUS_SHORTENED,
)

_BATCH_SIZE = 50

_STATUS_COLOR = {
    STATUS_NO_AUDIO: '#ff4d4d',
    STATUS_EXCEEDED: '#ff6600',
    STATUS_SHORTENED: '#E6E9EC',
    STATUS_OK: '#66ff66',
}


class _CommitOnFocusOutEdit(QPlainTextEdit):
    """失焦且内容变化时才提交，避免每击键一次信号风暴。"""

    def __init__(self, text, on_commit, parent=None):
        super().__init__(text, parent)
        self._on_commit = on_commit
        self._last = text
        self.setMaximumHeight(72)
        self.setTabChangesFocus(True)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        text = self.toPlainText().strip()
        if text != self._last:
            self._last = text
            self._on_commit(text)

    def sync_text(self, text):
        self._last = text
        if self.toPlainText().strip() != text:
            self.setPlainText(text)


class SpeakerCard(QFrame):
    playRequested = Signal(int)    # idx
    redubRequested = Signal(int)   # idx
    seekRequested = Signal(int)    # ms

    def __init__(self, idx: int, state, roles, parent=None):
        super().__init__(parent)
        self.idx = idx
        self._state = state
        self.setObjectName('speaker_card')
        self.setStyleSheet(
            '#speaker_card{border:1px solid #2E3947;border-radius:4px;background:#161B22;}')

        item = state.items[idx]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        # 头行：#行号 时间（点击跳转） | 状态 | 待重配徽标
        head = QHBoxLayout()
        self.time_label = QLabel()
        self.time_label.setStyleSheet('color:#E6E9EC;')
        self.time_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.time_label.mousePressEvent = self._on_head_click
        head.addWidget(self.time_label)
        head.addStretch(1)
        self.leak_badge = QLabel('⚠ ' + tr('Suspected original audio'))
        self.leak_badge.setStyleSheet(
            'color:#fff;background:#c9463d;border-radius:3px;padding:1px 6px;')
        self.leak_badge.setVisible(False)
        head.addWidget(self.leak_badge)
        self.dirty_badge = QLabel(tr('Needs re-dub'))
        self.dirty_badge.setStyleSheet(
            'color:#161B22;background:#e0a94f;border-radius:3px;padding:1px 6px;')
        self.dirty_badge.setVisible(False)
        head.addWidget(self.dirty_badge)
        self.status_label = QLabel()
        head.addWidget(self.status_label)
        layout.addLayout(head)

        # 主体：原文（只读、暗色） | 译文（可编辑）
        body = QHBoxLayout()
        ref = QLabel(str(item.get('ref_text') or ''))
        ref.setWordWrap(True)
        ref.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        ref.setStyleSheet('color:#9AA7B4;')
        ref.setToolTip(tr('Source text'))
        body.addWidget(ref, stretch=1)
        self.text_edit = _CommitOnFocusOutEdit(
            str(item.get('text') or ''),
            lambda t: self._state.set_text(self.idx, t))
        self.text_edit.setToolTip(tr('Translated text'))
        body.addWidget(self.text_edit, stretch=1)
        layout.addLayout(body)

        # 脚行：音色 | 试听 | 重配
        foot = QHBoxLayout()
        self.role_box = QComboBox()
        self.role_box.setToolTip(tr('Voice role'))
        current_role = str(item.get('role') or '')
        role_list = list(roles or [])
        if current_role and current_role not in role_list:
            role_list.insert(0, current_role)
        self.role_box.addItems(role_list)
        if current_role:
            self.role_box.setCurrentText(current_role)
        self.role_box.currentTextChanged.connect(
            lambda r: self._state.set_role(self.idx, r))
        foot.addWidget(self.role_box, stretch=1)

        self.play_btn = QPushButton('▶ ' + tr('Trial dubbing'))
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.clicked.connect(lambda: self.playRequested.emit(self.idx))
        foot.addWidget(self.play_btn)

        self.redub_btn = QPushButton('↻ ' + tr('Re-dubbed'))
        self.redub_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.redub_btn.clicked.connect(lambda: self.redubRequested.emit(self.idx))
        foot.addWidget(self.redub_btn)
        layout.addLayout(foot)

        self.refresh()

    def _on_head_click(self, event):
        self.seekRequested.emit(int(self._state.items[self.idx]['start_time']))

    def mousePressEvent(self, event):
        # 点卡片任意空白处即选中并跳转到该句（译文框/下拉/按钮等子控件各自照常）
        if event.button() == Qt.MouseButton.LeftButton:
            self.seekRequested.emit(int(self._state.items[self.idx]['start_time']))
        super().mousePressEvent(event)

    def set_active(self, active: bool):
        border = '#2E7CF6' if active else '#2E3947'
        self.setStyleSheet(
            f'#speaker_card{{border:1px solid {border};border-radius:4px;background:#161B22;}}')

    def set_busy(self, busy: bool, queued: bool = False):
        self.redub_btn.setDisabled(busy)
        if busy:
            self.redub_btn.setText(tr('Queued for dubbing') if queued else tr('Dubbing in progress'))
        else:
            self.redub_btn.setText('↻ ' + tr('Re-dubbed'))

    def refresh(self):
        item = self._state.items[self.idx]
        self.time_label.setText(f"#{item.get('line', self.idx + 1)}  "
                                f"{item.get('startraw', '')} → {item.get('endraw', '')}")
        kind, dubbing, diff = self._state.status_for(self.idx)
        if kind == STATUS_NO_AUDIO:
            msg = tr('No audio')
        elif kind == STATUS_EXCEEDED:
            msg = f'[{dubbing}s]{tr("Exceeded")}{diff}s'
        elif kind == STATUS_SHORTENED:
            msg = f'[{dubbing}s]{tr("Shortened")}{abs(diff)}s'
        else:
            msg = f'{dubbing}s'
        self.status_label.setText(msg)
        self.status_label.setStyleSheet(f'color:{_STATUS_COLOR[kind]};')
        leak = str(item.get('lang_leak') or '')
        self.leak_badge.setVisible(bool(leak))
        if leak:
            self.leak_badge.setToolTip(
                tr('Auto-check heard unexpected speech in this line') + f':\n{leak}')
        self.dirty_badge.setVisible(self._state.is_dirty(self.idx))
        self.text_edit.sync_text(str(item.get('text') or ''))


class SpeakerCardList(QScrollArea):
    playRequested = Signal(int)
    redubRequested = Signal(int)
    seekRequested = Signal(int)

    def __init__(self, state, roles, parent=None):
        super().__init__(parent)
        self._state = state
        self._roles = roles
        self._cards = {}
        self._active = -1

        self.setWidgetResizable(True)
        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        self.setWidget(container)

        # 订阅 state：任何行级变更刷新对应卡
        for sig in (state.textChanged, state.roleChanged,
                    state.timesChanged, state.statusChanged):
            sig.connect(self._refresh_card)
        state.dirtyChanged.connect(lambda idx, _d: self._refresh_card(idx))

        # 分批建卡，避免长任务卡死打开。定时器带 receiver 上下文：
        # 本控件销毁后未触发的批次自动取消，不会摸到已删对象（工作台中途退出时曾崩溃）
        self._next_idx = 0
        QTimer.singleShot(0, self, self._build_batch)

    def _build_batch(self):
        from shiboken6 import isValid
        if not isValid(self):
            # 兜底：本批回调与延迟删除同 tick 触发时，receiver 取消可能来不及
            return
        end = min(self._next_idx + _BATCH_SIZE, len(self._state.items))
        for idx in range(self._next_idx, end):
            card = SpeakerCard(idx, self._state, self._roles)
            card.playRequested.connect(self.playRequested)
            card.redubRequested.connect(self.redubRequested)
            card.seekRequested.connect(self.seekRequested)
            self._layout.insertWidget(self._layout.count() - 1, card)
            self._cards[idx] = card
        self._next_idx = end
        if end < len(self._state.items):
            QTimer.singleShot(0, self, self._build_batch)

    def card(self, idx: int):
        return self._cards.get(idx)

    def _refresh_card(self, idx: int):
        card = self._cards.get(idx)
        if card:
            card.refresh()

    def scroll_to(self, idx: int):
        card = self._cards.get(idx)
        if card:
            self.ensureWidgetVisible(card, 0, 40)

    def set_active(self, idx: int):
        if idx == self._active:
            return
        old = self._cards.get(self._active)
        if old:
            old.set_active(False)
        self._active = idx
        new = self._cards.get(idx)
        if new:
            new.set_active(True)
