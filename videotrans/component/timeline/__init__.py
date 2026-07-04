# 时间轴预览包：波形数据层(peaks/dub_preview)无 Qt 依赖，可独立单测；
# UI 部件通过 __getattr__ 懒导入，避免在无显示环境下 import 本包时拉起 Qt
__all__ = ['TimelinePreviewDialog']


def __getattr__(name):
    if name == 'TimelinePreviewDialog':
        from videotrans.component.timeline.dialog import TimelinePreviewDialog
        return TimelinePreviewDialog
    raise AttributeError(name)
