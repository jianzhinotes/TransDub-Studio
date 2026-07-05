"""设计令牌：全应用统一的颜色常量（苹果风深色系）。

纯常量、无 Qt 依赖。styles/style.qss 的注释头与此保持一一对应；
Python 侧的页面局部 QSS / QPainter 颜色一律从这里取值。
"""
WINDOW_BG = '#161B22'        # 窗口最底层
SURFACE = '#1C232D'          # 面板/输入类底
ELEVATED = '#232B37'         # 浮起元素（按钮/卡片/菜单）
BORDER = '#2E3947'           # 细边框
TEXT = '#E6E9EC'             # 主文本
TEXT_SECONDARY = '#9AA7B4'   # 次级文本
ACCENT = '#2E7CF6'           # 强调色（苹果蓝）
ACCENT_HOVER = '#4A90F7'
SELECTION = '#2A4A73'        # 选区/选中底
SUCCESS = '#2ecc71'
WARNING = '#f39c12'
ERROR = '#ff4d4d'
