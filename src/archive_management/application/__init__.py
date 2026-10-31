"""应用层: 备份、恢复、导入导出、Steam 发现等用例.

用例仅依赖领域模型与注入的基础设施接口, 不直接调用 UI、HTTP 或文件系统,
因此可以脱离界面单独测试。
"""

from __future__ import annotations
