"""Release 说明取自 PR 里**手写**的「本次变更」一节(2026-10-31 定).

为什么这条链要有守卫: 发布页的内容改动没有任何用例会变红 —— 说明空了、多带一段 marker、
或者干脆又退回"版本/提交/构建号"三行, 表现都是"Release 建出来了"。所以这里钉两件事:

① ``scripts/release_notes.py`` 的取文口径(标题起、下一个二级标题止、剥 HTML 注释);
② ``release.yml`` 真的调它, 而且用的是**合入提交**(``github.sha``)去找 PR —— 用
   ``RELEASE_TARGET`` 会找不到: 那是手动档可以任意填的 ref, 不一定关联着某个 PR。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("发布流水线"),
    pytest.mark.story("发布说明取自 PR"),
    pytest.mark.layer("unit"),
]

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "release_notes.py"


def _load_script() -> ModuleType:
    """按路径加载 ``scripts/release_notes.py``(scripts 不是包, 与别的报告脚本同一手法)."""
    spec = importlib.util.spec_from_file_location("release_notes", _SCRIPT)
    assert spec is not None, f"加载不到 {_SCRIPT}"
    assert spec.loader is not None, f"{_SCRIPT} 没有 loader"
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = module
    spec.loader.exec_module(module)
    return module


def test_extract_takes_the_section_and_stops_at_the_next_heading() -> None:
    """一节 = 从「本次变更」到下一个二级标题; 后面的章节一个字都不该带出来."""
    notes = _load_script()
    body = "## 本次变更\n\n修复了恢复时的覆盖顺序。\n\n## 动机\n\n这里不该进发布页。\n"

    text = notes.extract(body)

    assert text == "修复了恢复时的覆盖顺序。", f"取文要停在下一个二级标题: {text!r}"
    assert "动机" not in text, "后面的章节不能被带进发布说明"


def test_extract_strips_the_guidance_comment() -> None:
    """模板里给发起者的 HTML 注释是对内说明, 发布页上不该出现(它在渲染时是空的)."""
    notes = _load_script()
    body = (
        "## 本次变更\n\n"
        "<!-- 这一段是给发起者看的引导话 -->\n\n"
        "备份列表现在会记住上次的排序。\n"
    )

    text = notes.extract(body)

    assert text == "备份列表现在会记住上次的排序。", f"注释没剥干净: {text!r}"


def test_extract_collapses_extra_blank_lines() -> None:
    """手写时多敲的空行只是噪音: 三行以上压成一行, 首尾空行去掉."""
    notes = _load_script()

    text = notes.extract("## 本次变更\n\n\n\n第一句。\n\n\n\n第二句。\n\n\n")

    assert text == "第一句。\n\n第二句。", f"空行没收敛: {text!r}"


def test_extract_without_the_heading_is_empty() -> None:
    """没有这一节时返回空串 —— 由调用方决定回退, 这里不编内容."""
    notes = _load_script()

    assert notes.extract("## 动机\n\n只有动机。\n") == ""
    assert notes.extract("") == ""


def test_pick_pull_prefers_the_latest_merged_one() -> None:
    """``commits/{sha}/pulls`` 可能给多个 PR: 取最近合入的那个(与这次发布对得上)."""
    notes = _load_script()
    pulls = [
        {"number": 9, "merged_at": "2026-10-01T00:00:00Z", "body": "旧的"},
        {"number": 10, "merged_at": None, "body": "没合入"},
        {"number": 11, "merged_at": "2026-10-31T00:00:00Z", "body": "新的"},
    ]

    picked = notes.pick_pull(pulls)

    assert picked is not None
    assert picked["number"] == 11, "要取最近合入的那个 PR"
    assert notes.pick_pull([]) is None, "空列表要能安全返回"


def test_notes_fall_back_when_there_is_nothing_to_say() -> None:
    """没有 PR、或那一节是空的: 回退成一句说明, 不写版本/提交号."""
    notes = _load_script()

    assert notes.notes_from_pulls([]) == notes.FALLBACK
    assert notes.notes_from_pulls(
        [{"number": 1, "body": "## 动机\n\n只写了动机\n"}]
    ) == (notes.FALLBACK)
    hint = "回退文案不该回到'版本/提交/构建号'那三行"
    assert notes.FALLBACK not in ("", "详见提交记录。"), hint
    for word in ("版本:", "提交:", "构建:"):
        assert word not in notes.FALLBACK, hint


def test_notes_use_the_hand_written_section() -> None:
    """手写内容原样成为发布说明(这才是这条链的目的)."""
    notes = _load_script()
    pulls = [
        {
            "number": 4,
            "merged_at": "2026-10-31T00:00:00Z",
            "body": "## 本次变更\n\n现在发布页只挂一个 `ArchiveManagement.exe`。\n\n## 动机\n\n略。\n",
        }
    ]

    text = notes.notes_from_pulls(pulls)

    assert text == "现在发布页只挂一个 `ArchiveManagement.exe`。", (
        f"取到了别的东西: {text!r}"
    )


def test_main_writes_the_notes_file(tmp_path: Path) -> None:
    """入口要把结果写进 ``--out``(交给 ``gh release create --notes-file``), 末尾留一个换行."""
    notes = _load_script()
    pulls = tmp_path / "pulls.json"
    pulls.write_text(
        json.dumps(
            [
                {
                    "number": 7,
                    "merged_at": "2026-10-31T00:00:00Z",
                    "body": "## 本次变更\n\n内容。\n",
                }
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "release-notes.md"

    assert notes.main(["--pulls", str(pulls), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == "内容。\n", "写出的是段落 + 一个换行"


def test_main_survives_an_unreadable_payload(tmp_path: Path) -> None:
    """读不到/解析不了时用回退文案而不是崩: 发布这一步不该因为取说明失败就发不出去."""
    notes = _load_script()
    broken = tmp_path / "pulls.json"
    broken.write_text("not json", encoding="utf-8")
    out = tmp_path / "release-notes.md"

    assert notes.main(["--pulls", str(broken), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").strip() == notes.FALLBACK


def test_the_release_workflow_reads_the_notes_from_the_merged_pull_request() -> None:
    """``release.yml`` 要用合入提交找 PR 再调脚本 —— 两处都别写回死文案."""
    workflow = (_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    code = "\n".join(
        line for line in workflow.splitlines() if not line.strip().startswith("#")
    )

    hint = "发布说明取自 PR: 工作流要调 scripts/release_notes.py"
    assert "scripts/release_notes.py" in code, hint

    hint = "找 PR 要用合入提交(github.sha): RELEASE_TARGET 在手动档可以是任意 ref, 与 PR 无关"
    assert "commits/$GITHUB_SHA/pulls" in code, hint
    assert "commits/$RELEASE_TARGET/pulls" not in code, hint

    hint = "发布正文必须来自 --notes-file, 不能再用 heredoc 写死一段固定内容"
    assert "--notes-file release-notes.md" in code, hint
    assert "cat > release-notes.md" not in code, hint
