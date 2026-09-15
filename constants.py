"""插件级共享常量。

日志前缀在多个模块都要用（各模块各有一份 logger），集中放这里避免
同一字面量在三个文件里各写一遍、改一处漏两处。
"""

from __future__ import annotations

__all__ = ["DEFAULT_ICS_DIR", "LOG_PREFIX"]

#: 日志前缀，便于在麦麦主进程日志里筛出本插件的输出
LOG_PREFIX = "[ClassSchedule]"

#: 课表目录默认名（相对插件数据目录）
DEFAULT_ICS_DIR = "ics"
