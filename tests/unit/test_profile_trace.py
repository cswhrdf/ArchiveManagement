"""``@pytest.mark.profile`` 的耗时追踪: 只对标记用例生效, 体积超限只挂摘要.

这里锁住四件事:

- **门槛**: 没标记的用例一行开销都没有; 默认只在 Linux 追踪(可用环境变量临时开);
- **降级**: viztracer 没装时只打印提示, 用例照常跑;
- **摘要口径**: self time = 自身时长减去子调用, 与 trace 查看器一致;
- **体积**: 超过上限就不挂 trace, 只挂摘要与落点。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import allure
import pytest

import profile_trace

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("耗时追踪"),
    pytest.mark.story("只对标记的用例挂 trace"),
    pytest.mark.layer("unit"),
]


class _Item:
    """最小的 pytest.Item 替身: 只回答"有没有 profile 标记"这几件事."""

    def __init__(
        self, *, marked: bool = True, nodeid: str = "tests/x.py::test_x"
    ) -> None:
        """记录"有没有标记"与 nodeid(其余 pytest.Item 属性用例都不碰)."""
        self.nodeid = nodeid
        self.path = Path("tests/x.py")
        self._marked = marked

    def get_closest_marker(self, name: str) -> object | None:
        """只有 ``profile`` 这一种标记会被认出来."""
        return object() if self._marked and name == profile_trace.MARKER else None


def _item(*, marked: bool = True) -> pytest.Item:
    """把替身转成 ``pytest.Item``(用例不关心其余属性)."""
    return cast(pytest.Item, _Item(marked=marked))


def _trace(*spans: tuple[str, float, float]) -> dict[str, object]:
    """按 ``(名字, 起始µs, 时长µs)`` 造一份最小 trace."""
    return {
        "traceEvents": [
            {"ph": "X", "name": name, "ts": start, "dur": duration}
            for name, start, duration in spans
        ]
    }


# ------------------------------------------------------------------ 门槛


def test_only_marked_tests_are_traced() -> None:
    assert profile_trace.should_trace(_item(), platform="linux") is True
    assert profile_trace.should_trace(_item(marked=False), platform="linux") is False


def test_tracing_stays_off_other_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三平台各挂一份会把报告撑大: 默认只在 Linux 开, 可用环境变量临时覆盖."""
    monkeypatch.delenv(profile_trace.ENV_OVERRIDE, raising=False)
    assert profile_trace.should_trace(_item(), platform="win32") is False
    assert profile_trace.should_trace(_item(), platform="darwin") is False
    monkeypatch.setenv(profile_trace.ENV_OVERRIDE, "1")
    assert profile_trace.should_trace(_item(), platform="win32") is True


# ------------------------------------------------------------------ 降级


def test_start_degrades_when_viztracer_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """viztracer 装不上时: 不挂附件、只提示一句, 用例不会因此变红."""
    monkeypatch.setattr(profile_trace, "should_trace", lambda item: True)
    monkeypatch.setitem(sys.modules, "viztracer", None)

    trace = profile_trace.start(_item())

    assert trace is None
    assert "viztracer 不可用" in capsys.readouterr().out
    profile_trace.finish(None)


# ------------------------------------------------------------------ 摘要口径


def test_summary_uses_self_time_not_inclusive_time() -> None:
    """父函数 10 ms, 内层两个子 2 ms + 0.5 ms ⇒ 父的 self 应是 7.5 ms."""
    text = profile_trace.render_summary(
        _trace(
            ("outer", 0, 10_000),
            ("inner_a", 1_000, 2_000),
            ("inner_b", 4_000, 500),
        ),
        top=5,
    )

    lines = text.splitlines()
    assert "outer" in lines[1], f"self time 最大的应排在最前: {lines}"
    assert "7.5 ms" in lines[1]
    assert "2.0 ms" in lines[2]
    assert "0.5 ms" in lines[3]
    assert "合计 10.0 ms" in lines[0]


def test_summary_reports_an_unreadable_trace(tmp_path: Path) -> None:
    text = profile_trace.summarize_text(tmp_path / "gone.json")
    assert "读不出来" in text


# ------------------------------------------------------------------ 体积与附件


def _trace_file(tmp_path: Path) -> Path:
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(_trace(("outer", 0, 10_000))), encoding="utf-8")
    return path


def test_a_small_trace_is_attached_as_a_download_only_file(tmp_path: Path) -> None:
    evidence = profile_trace.evidence(_trace_file(tmp_path))

    assert [item.name for item in evidence] == [
        "耗时追踪 trace · trace.json",
        "耗时追踪摘要 · Top self time",
    ]
    # Allure 认识的大类型会在打开详情页时卡死: trace 只给下载链接.
    assert evidence[0].attachment_type == "application/octet-stream"
    assert evidence[0].path is not None
    assert evidence[1].attachment_type == allure.attachment_type.TEXT
    assert evidence[1].body is not None
    assert b"outer" in evidence[1].body


def test_an_oversized_trace_falls_back_to_the_summary_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过上限时只挂摘要, 并在摘要里写清落点与大小(报告不能被几个 MB 拖死)."""
    monkeypatch.setattr(profile_trace, "MAX_TRACE_BYTES", 8)

    evidence = profile_trace.evidence(_trace_file(tmp_path))

    assert len(evidence) == 1, "超限时不该挂 trace 本体"
    assert evidence[0].body is not None
    assert "超过" in evidence[0].body.decode("utf-8")
    assert "outer" in evidence[0].body.decode("utf-8")


def test_the_output_path_is_derived_from_the_node_id() -> None:
    """trace 文件名由 nodeid 派生: 只保留字母数字与短横, 不含路径分隔符."""
    nodeid = "tests/integration/test_pipeline_full_flow.py::test_x"

    slug = profile_trace._slug(nodeid)

    assert slug.endswith("test-x")
    assert "/" not in slug
    assert "\\" not in slug
    assert ":" not in slug
