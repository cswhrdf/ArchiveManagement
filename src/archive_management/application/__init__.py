"""应用层:备份、恢复、导入导出、Steam 发现等用例.

用例仅依赖领域模型与注入的基础设施接口,不直接调用 UI、HTTP 或文件
系统(PLAN 4).各用例在后续里程碑(阶段 B-E)逐步实现
"""

from __future__ import annotations
