"""CustomTkinter 用户界面.

UI 通过线程安全消息队列接收后台任务状态;工作线程禁止直接更新 Tk
控件(PLAN 阶段 F). 界面在后续里程碑实现
"""

from __future__ import annotations
