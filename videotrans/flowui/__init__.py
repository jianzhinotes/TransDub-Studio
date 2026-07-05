# Flow UI：剪映/ElevenLabs 式简洁流程界面（首页→配置→进度）。
# 无 Qt 的数据模块(curated/recent_tasks/stages)可独立单测；
# FlowWidget 懒导入，避免无显示环境 import 本包时拉起 Qt。
__all__ = ['FlowWidget']


def __getattr__(name):
    if name == 'FlowWidget':
        from videotrans.flowui.flow_widget import FlowWidget
        return FlowWidget
    raise AttributeError(name)
