"""安全测试的共享夹具.

安全测试只在 GitHub Actions 执行: 默认 ``testpaths`` 不含本目录, 需要显式指定
``pytest tests/security -m security``。每条结论都记录"输入场景 / 期望拦截 /
实际情况", 会话结束时写入 ``security-results.json``, 并由 Allure 汇总任务作为
附件上传(见 ``scripts/create_allure_summary.py``)。

约定: 不得使用真实凭据或真实用户文件 —— 所有恶意输入都是临时目录里构造出来的
数据, 涉及"用户主目录/盘符根"的用例只做**判定**, 不执行写入或删除。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import allure
import pytest

from reporting import SecurityRecorder

# 机器可读的安全结论(CI 汇总 job 会把它作为 Allure 附件上传).
RESULTS_JSON = Path("security-results.json")


@pytest.fixture(scope="session")
def security_recorder() -> Iterator[SecurityRecorder]:
    """会话级安全结论记录器: 结束后把结论写到工作区根目录."""
    recorder = SecurityRecorder()
    try:
        yield recorder
    finally:
        recorder.write(RESULTS_JSON)


@pytest.fixture(autouse=True)
def _attach_findings(security_recorder: SecurityRecorder) -> Iterator[None]:
    """把当前用例产生的安全结论作为 Allure 附件(期望 vs 实际)."""
    start = len(security_recorder.findings)
    yield
    fresh = security_recorder.since(start)
    if fresh:
        allure.attach(
            json.dumps([asdict(item) for item in fresh], ensure_ascii=False, indent=2),
            name="安全结论(场景/期望/实际/是否拦截)",
            attachment_type=allure.attachment_type.JSON,
        )
