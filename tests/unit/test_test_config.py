"""测试基础设施自身的配置校验.

用一条"配置即断言"的用例锁住两道容易被误删的门槛:

- 每个用例都有最长执行时间(``--timeout``), 卡住的用例不会让整套测试挂死;
- 严重等级过滤仍与 Allure 元数据挂钩(``--min-severity`` 由 conftest 提供).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.critical]

# 与 pyproject.toml 的 addopts 保持一致: 单个用例最长 60 秒.
_EXPECTED_TIMEOUT_SECONDS = 60


def test_every_test_has_a_maximum_runtime(pytestconfig: pytest.Config) -> None:
    """全局超时必须开启, 否则卡住的用例会一直等下去."""
    option = pytestconfig.getoption("timeout")
    assert option is not None, "缺少 --timeout: 测试可能永久挂起"
    assert float(option) == _EXPECTED_TIMEOUT_SECONDS
    assert pytestconfig.getoption("timeout_method") == "thread"


def test_timeout_marker_can_override_default(pytestconfig: pytest.Config) -> None:
    """插件需支持按用例覆盖超时(慢用例可用 @pytest.mark.timeout 放宽)."""
    assert pytestconfig.pluginmanager.has_plugin("timeout")
