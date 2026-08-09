"""引擎摘要条：在主面板一眼看到本次用什么识别/翻译/配音引擎、配好没有。

智能模式的叙事是"已经替你选好了"，所以主面板要回答的是**信息**问题
（用的是什么、能不能跑），而不是把选择器搬回来。真正的更换入口仍在
高级设置的 ChannelCard 里，点摘要行即可展开定位过去。

本组件只持有 ChannelCard 的引用做只读镜像，绝不 setParent、不改其状态。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from videotrans.configure.config import tr
from videotrans.flowui import curated
from videotrans.styles import tokens

_KIND_TITLE = {
    curated.KIND_RECOGN: 'flow_recogn_card',
    curated.KIND_TRANS: 'flow_trans_card',
    curated.KIND_TTS: 'flow_tts_card',
}
_EMPTY_SECONDARY = (None, 'No', '', ' ')


class EngineSummaryRow(QFrame):
    """一行：状态点 + 类型 + 引擎名(·模型/音色) + 徽章 + 「去配置」。"""

    expandRequested = Signal(object)     # 携带对应 ChannelCard

    def __init__(self, *, card, parent=None):
        super().__init__(parent)
        self._card = card
        self.setObjectName('engineRow')
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        row = QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(8)
        self.dot = QLabel('●')
        self.kind_label = QLabel(tr(_KIND_TITLE[card.kind]))
        self.kind_label.setObjectName('engineKind')
        self.kind_label.setMinimumWidth(52)
        self.name_label = QLabel('')
        self.name_label.setObjectName('engineName')
        self.badge = QLabel('')
        self.badge.setVisible(False)
        for w in (self.dot, self.kind_label, self.name_label, self.badge):
            row.addWidget(w)
        row.addStretch(1)
        # 未配置时不必先展开高级设置再找卡片，这里直接开 API Key 窗口
        self.fix_btn = QPushButton(tr('flow_configure'))
        self.fix_btn.setObjectName('linkBtn')
        self.fix_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fix_btn.clicked.connect(card._open_config)
        self.fix_btn.setVisible(False)
        row.addWidget(self.fix_btn)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.expandRequested.emit(self._card)
        super().mousePressEvent(event)

    def refresh(self):
        provider = self._card.provider()
        ok = self._card.is_ready()
        self.dot.setStyleSheet(f'color:{tokens.SUCCESS if ok else tokens.WARNING};')
        secondary = self._card.current_secondary()
        detail = '' if secondary in _EMPTY_SECONDARY else f'  ·  {secondary}'
        self.name_label.setText(provider.name + detail)
        warn = self._card.warn_label.text()
        if not ok:
            self._set_badge(tr('flow_need_key'), tokens.WARNING)
        elif warn:
            self._set_badge('!', tokens.WARNING)
        elif curated.is_free(provider):
            self._set_badge(tr('flow_free'), tokens.ACCENT)
        else:
            self.badge.setVisible(False)
        self.fix_btn.setVisible(not ok and bool(provider.win))
        self.setToolTip(warn or tr('flow_engine_row_tip'))

    def _set_badge(self, text: str, color: str):
        self.badge.setText(text)
        self.badge.setStyleSheet(
            f'color:#fff;background:{color};border-radius:3px;padding:1px 6px;font-size:11px;')
        self.badge.setVisible(True)


class EngineSummary(QFrame):
    expandRequested = Signal(object)

    def __init__(self, *, cards, parent=None):
        super().__init__(parent)
        self.setObjectName('engineSummary')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(2)
        title = QLabel(tr('flow_engine_summary'))
        title.setObjectName('secTitle')
        lay.addWidget(title)
        self.rows = {}
        for card in cards:
            row = EngineSummaryRow(card=card)
            row.expandRequested.connect(self.expandRequested)
            lay.addWidget(row)
            self.rows[card.kind] = row

    def refresh(self):
        for row in self.rows.values():
            row.refresh()

    def set_kind_visible(self, kind: str, visible: bool):
        row = self.rows.get(kind)
        if row:
            row.setVisible(visible)
