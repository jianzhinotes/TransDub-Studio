# 时间轴预览包：波形数据层(peaks/dub_preview/edit_logic)无 Qt 依赖，可独立单测；
# UI 部件通过 __getattr__ 懒导入，避免在无显示环境下 import 本包时拉起 Qt
__all__ = ['TimelinePreviewDialog', 'DubbingStudioDialog']


def __getattr__(name):
    if name == 'TimelinePreviewDialog':
        from videotrans.component.timeline.dialog import TimelinePreviewDialog
        return TimelinePreviewDialog
    if name == 'DubbingStudioDialog':
        from videotrans.component.timeline.studio import DubbingStudioDialog
        return DubbingStudioDialog
    raise AttributeError(name)
