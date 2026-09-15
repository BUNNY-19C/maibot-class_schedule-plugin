"""测试引导：把插件目录注册成包，使插件内部的相对导入可用。

MaiBot Runner 加载插件时也是「把插件目录当作一个包」来导入 ``plugin.py``，
这里复刻同样的环境，测试才能和运行时走同一套导入路径。

各个测试文件开头写::

    import _bootstrap  # noqa: F401

即可（``python -m unittest discover -s tests`` 会把 tests 目录加入 sys.path）。
"""

from __future__ import annotations

import os
import sys
import types

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_NAME = "class_schedule"

if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [PLUGIN_DIR]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = package
