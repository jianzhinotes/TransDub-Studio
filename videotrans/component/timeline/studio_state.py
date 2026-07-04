"""StudioState：Dubbing Studio 的唯一数据源。

持有 queue_tts 的同一 list 引用（不替换），卡片/时间轴/预览重建
全部经由本类方法修改数据并接收粒度信号。
"""
from PySide6.QtCore import QObject, Signal

from videotrans.component.timeline import edit_logic


class StudioState(QObject):
    textChanged = Signal(int)
    roleChanged = Signal(int)
    timesChanged = Signal(int)
    statusChanged = Signal(int)        # dubbing_s 或槽位时长变化
    dirtyChanged = Signal(int, bool)   # 该行是否待重新配音

    def __init__(self, queue_tts: list, duration_ms: int, parent=None):
        super().__init__(parent)
        self._items = queue_tts
        self.duration_ms = max(int(duration_ms), 1)
        self._dirty = set()

    @property
    def items(self) -> list:
        return self._items

    def __len__(self):
        return len(self._items)

    # ---- 修改入口 ----
    def set_text(self, idx: int, text: str):
        if self._items[idx]['text'] == text:
            return
        self._items[idx]['text'] = text
        self.textChanged.emit(idx)
        self._mark_dirty(idx)

    def set_role(self, idx: int, role: str):
        if self._items[idx].get('role') == role:
            return
        self._items[idx]['role'] = role
        self.roleChanged.emit(idx)
        self._mark_dirty(idx)

    def set_times(self, idx: int, start_ms: int, end_ms: int):
        it = self._items[idx]
        if int(it['start_time']) == int(start_ms) and int(it['end_time']) == int(end_ms):
            return
        edit_logic.sync_time_fields(it, start_ms, end_ms)
        self.timesChanged.emit(idx)
        self.statusChanged.emit(idx)   # 槽位时长变了，超时/缩短状态需重算

    def set_dubbing_s(self, idx: int, seconds: float):
        self._items[idx]['dubbing_s'] = float(seconds)
        self.statusChanged.emit(idx)

    # ---- 待重配追踪 ----
    def _mark_dirty(self, idx: int):
        if idx not in self._dirty:
            self._dirty.add(idx)
            self.dirtyChanged.emit(idx, True)

    def mark_clean(self, idx: int):
        if idx in self._dirty:
            self._dirty.discard(idx)
            self.dirtyChanged.emit(idx, False)

    def is_dirty(self, idx: int) -> bool:
        return idx in self._dirty

    def dirty_indices(self) -> set:
        return set(self._dirty)

    # ---- 查询/持久化 ----
    def status_for(self, idx: int) -> tuple:
        return edit_logic.compute_status(self._items[idx])

    def save(self, cache_folder: str) -> str:
        return edit_logic.dump_queue(self._items, cache_folder)
