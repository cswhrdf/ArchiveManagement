"""GUI 用例的 Tk 抖动重试: 行为与"必须写明原因"的守卫.

背景: 同一个进程里反复建/销窗口之后, 解释器偶尔连 Tcl 库数据都找不到, 建窗口那
一步直接抛 ``TclError: invalid command name "tcl_findLibrary"``。这是环境抖动而非
被测行为, 因此 ``gui_support.gui_app`` 会**重试**(而不是立刻跳过)。

纪律(本模块即守卫):

1. **重试必须写明原因** —— ``TK_RETRY_REASON`` 要指得出可观察的症状, 不能是
   "失败了就再跑一遍"这种话(占位词与过短的原因一律拦下);
2. **重试必须在报告里看得见** —— 记成 Allure 参数(可筛选)+ 附件(带原始异常),
   否则"靠重试过关"这件事会完全静默;
3. **重试有上限**, 到顶仍然是跳过: 环境问题不该被伪装成用例失败, 也不该无限重试;
4. **只允许已知症状跳过** —— 原因前缀是 "tk 环境不可用" 但症状不在
   :data:`tk_guard.KNOWN_TK_SKIP_MARKERS` 里时, 由 ``tests/conftest.py`` 改成失败。
   这条是补的: 2026-09-25 从报告里发现一条用例因 `image "pyimage1" does not exist`
   在**两个平台都**被跳过(见 tests/tk_guard.py)。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import gui_support
import tk_guard

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("界面用例基础设施"),
    pytest.mark.story("Tk 会话抖动的重试与留痕"),
    pytest.mark.layer("unit"),
]

_TESTS_ROOT = Path(__file__).resolve().parents[1]
# 原因至少要能说清"为什么可以重试"。
_MIN_REASON_LENGTH = 20
# 占位词(按整句比较)。
_PLACEHOLDERS = frozenset({"todo", "fixme", "无", "略", "……", "...", "待补", "稍后"})


class _FakeApp:
    """最小的窗口替身: 只需能被收尾逻辑销毁."""

    def destroy(self) -> None:
        """收尾时被调用."""


def _tcl_error() -> Exception:
    """构造一个与真实抖动同形的 TclError."""
    from tkinter import TclError

    return TclError('invalid command name "tcl_findLibrary"')


def test_gui_app_retries_the_tk_session_flake(monkeypatch: pytest.MonkeyPatch) -> None:
    """抖动两次后成功: 用例继续跑, 且每次失败都留下"为什么重试"的记录."""
    recorded: list[tuple[int, Exception]] = []
    monkeypatch.setattr(
        gui_support,
        "_record_retry",
        lambda attempt, error: recorded.append((attempt, error)),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    calls = {"count": 0}

    def builder() -> _FakeApp:
        calls["count"] += 1
        if calls["count"] < gui_support.TK_RETRY_ATTEMPTS:
            raise _tcl_error()
        return _FakeApp()

    app = gui_support.gui_app(builder)

    assert isinstance(app, _FakeApp)
    assert calls["count"] == gui_support.TK_RETRY_ATTEMPTS
    assert [
        attempt for attempt, error in recorded if not isinstance(error, RuntimeError)
    ] == [1, 2], "每一次失败都要记进报告"
    gui_support.close_gui_apps()


def test_gui_app_skips_only_after_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一直抖动: 重试到上限后仍然跳过(环境问题不该被伪装成用例失败)."""
    monkeypatch.setattr(gui_support, "_record_retry", lambda *_args: None)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    calls = {"count": 0}

    def builder() -> str:
        calls["count"] += 1
        raise _tcl_error()

    with pytest.raises(pytest.skip.Exception):
        gui_support.gui_app(builder)

    assert calls["count"] == gui_support.TK_RETRY_ATTEMPTS


def test_retry_is_recorded_in_allure_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重试必须在 Allure 里可见: 一个参数(可筛选)+ 一份带原始异常与原因的附件."""
    import allure

    attachments: list[tuple[str, str, str]] = []
    parameters: list[tuple[str, str]] = []
    monkeypatch.setattr(
        allure,
        "attach",
        lambda body, name, attachment_type: attachments.append(
            (str(body), str(name), str(attachment_type))
        ),
    )
    monkeypatch.setattr(
        allure.dynamic,
        "parameter",
        lambda name, value: parameters.append((str(name), str(value))),
    )

    gui_support._record_retry(1, _tcl_error())

    assert attachments, "重试没有在报告里留附件"
    body, _name, _kind = attachments[0]
    assert "tcl_findLibrary" in body, f"附件里要带原始异常: {body}"
    assert gui_support.TK_RETRY_REASON in body, "附件里要写明重试原因"
    assert parameters, "重试没有记成可筛选的参数"
    assert parameters[0][0] == "重试"
    assert gui_support.TK_RETRY_REASON in parameters[0][1]


def test_retry_budget_and_delay_are_bounded() -> None:
    """重试次数与等待都有上限: 无上限的重试会把一次抖动放大成几分钟的等待."""
    assert gui_support.TK_RETRY_ATTEMPTS >= 2, "至少要重试一次才有意义"
    assert 0 < gui_support.TK_RETRY_DELAY_SECONDS <= 1.0, "等待要短, 抖动的恢复是瞬时的"


def test_the_retry_reason_names_the_observable_symptom() -> None:
    """原因必须指得出可观察的症状与处理方式, 而不是"失败了就再跑一遍"这类空话.

    (仓库目前只有这一处重试; 将来再加第二处时, 请把这条守卫扩展成"逐个检查",
    而不是让新原因绕过它。)
    """
    reason = gui_support.TK_RETRY_REASON
    normalized = reason.strip().removesuffix("。").removesuffix(".").lower()

    assert len(reason) >= _MIN_REASON_LENGTH, f"原因太短: {reason}"
    assert normalized not in _PLACEHOLDERS, f"原因是占位词: {reason}"
    assert "tcl_findLibrary" in reason, f"原因里要写清可观察的症状: {reason}"
    assert "重试" in reason, f"原因里要说清处理方式: {reason}"


def test_gui_support_passes_the_reason_into_the_report() -> None:
    """守卫本身的落点: 记录重试的那个函数必须真的用上 TK_RETRY_REASON."""
    source = (_TESTS_ROOT / "gui_support.py").read_text(encoding="utf-8")
    recorder = source.split("def _record_retry", 1)[-1].split("def gui_app", 1)[0]

    assert "TK_RETRY_REASON" in recorder, "记录重试时没有带原因"
    assert "allure.attach(" in recorder, "重试细节要留附件"
    assert "allure.dynamic.parameter(" in recorder, "重试要记成可筛选的参数"


# --- 只允许"已知环境问题"跳过 ------------------------------------------------


def test_only_the_known_tk_symptoms_count_as_an_environment_problem() -> None:
    """已知症状(Tcl 库数据缺失 / 没有显示环境)才算环境问题; 其它一律不算.

    反例就是这次踩到的: `image "pyimage1" does not exist` 是**图像名失效**,
    与解释器找不到 Tcl 库毫无关系, 却被统一写成"tk 环境不可用"跳过了。
    """
    assert tk_guard.is_known_tk_skip('invalid command name "tcl_findLibrary"')
    assert tk_guard.is_known_tk_skip(
        "Can't find a usable init.tcl in the following ..."
    )
    assert tk_guard.is_known_tk_skip(
        "no display name and no $DISPLAY environment variable"
    )
    assert tk_guard.is_known_tk_skip("couldn't connect to display ':99'")
    assert not tk_guard.is_known_tk_skip('image "pyimage1" does not exist')
    assert not tk_guard.is_known_tk_skip("bad window path name")


def test_an_unknown_tk_skip_reason_becomes_a_failure() -> None:
    """原因不在已知清单里时必须给出失败说明(而不是让它安静地跳过)."""
    unknown = 'tk 环境不可用: image "pyimage1" does not exist'
    message = tk_guard.unknown_tk_skip_message(unknown)

    assert message is not None, "这条跳过必须被拦下"
    assert 'image "pyimage1" does not exist' in message, "失败说明要带原始原因"
    assert "tcl_findlibrary" in message.casefold(), "要说清判定依据"
    assert "删掉这条用例" in message, "要给出两条出路(修掉或删掉)"


def test_other_skips_are_left_alone() -> None:
    """与 Tk 无关的跳过(环境不支持的符号链接、受限屏幕尺寸等)不受这条规则影响."""
    assert tk_guard.unknown_tk_skip_message("当前环境不允许创建符号链接") is None
    assert tk_guard.unknown_tk_skip_message("窗口没法变宽: 屏幕只有 1024px") is None
    assert (
        tk_guard.unknown_tk_skip_message(
            "tk 环境不可用(重试 3 次): Can't find a usable init.tcl"
        )
        is None
    ), "已知环境问题照旧跳过"


def test_gui_app_refuses_to_hide_an_unrelated_tcl_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非已知抖动的 TclError 不重试、不跳过, 直接让用例红(否则用例等于不存在)."""
    from tkinter import TclError

    calls: list[int] = []

    def builder() -> _FakeApp:
        calls.append(1)
        raise TclError('image "pyimage1" does not exist')

    with pytest.raises(TclError):
        gui_support.gui_app(builder)

    assert len(calls) == 1, "不是已知抖动就不该重试(重试也修不好)"


def test_conftest_turns_an_unknown_tk_skip_into_a_failure() -> None:
    """兜底规则必须真的接在 conftest 的用例报告钩子上(守卫漏写也拦得住)."""
    source = (_TESTS_ROOT / "conftest.py").read_text(encoding="utf-8")
    hook = source.split("def pytest_runtest_makereport", 1)[-1]

    assert "tk_guard.unknown_tk_skip_message" in hook, "钩子没有接上跳过纪律"
    assert 'report.outcome = "failed"' in hook, "跳过要真的改成失败"
