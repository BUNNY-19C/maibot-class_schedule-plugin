"""pytest 兼容入口：复用 unittest 那边的包注册逻辑。

插件内部使用相对导入（``from .ics_parser import ...``），直接 import 会失败，
所以测试收集前必须先把插件目录注册成包。
"""

import _bootstrap  # noqa: F401  —— 导入即完成包注册，勿删
