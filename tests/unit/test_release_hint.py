"""PR 阶段的发版提示: 当前号 / 建议号 / 不改会怎样(2026-11-01 起定版前移到 PR).

定版不再由"合入之后改 main"完成(分支保护禁止直接推送), 而是**人在 PR 里改
``packaging.py``**。这条提示是那个流程唯一的人机接口, 所以两件事都要钉住:

① 正文说清三件事(当前号、建议号、不改会怎样) —— 只说"记得改版本号"等于没说;
② 正文带固定 marker, 工作流才能原地更新而不是每次 push 新加一条。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("发布流水线"),
    pytest.mark.story("发版提示"),
    pytest.mark.layer("unit"),
]

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "release_hint.py"


def _load_script() -> ModuleType:
    """按路径加载 ``scripts/release_hint.py``(scripts 不是包, 与别的报告脚本同一手法)."""
    spec = importlib.util.spec_from_file_location("release_hint", _SCRIPT)
    assert spec is not None, f"加载不到 {_SCRIPT}"
    assert spec.loader is not None, f"{_SCRIPT} 没有 loader"
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_hint"] = module
    spec.loader.exec_module(module)
    return module


def test_the_hint_names_both_versions_and_the_price_of_doing_nothing() -> None:
    """只写"记得改版本号"等于没说: 要给出当前号、建议号, 以及不改的后果."""
    body = _load_script().render("0.0.1", "0.0.2")

    assert "0.0.1" in body, "要说明现在是哪个号"
    assert "0.0.2" in body, "要给出建议改成哪个号"
    assert "src/archive_management/packaging.py" in body, "要指名道姓说改哪里"
    assert "不会新建 Release" in body, "要说清不改的后果: 只构建、不发版"


def test_the_hint_carries_the_marker_used_for_deduplication() -> None:
    """没有 marker 就没法原地更新: 一个 PR 会攒出七八条同样的提示."""
    hint = _load_script()
    body = hint.render("1.2.3", "1.2.4")

    assert body.startswith(hint.MARKER), "marker 要在最前面, 方便按 contains 找到它"
    assert hint.MARKER not in ("",), "marker 不能是空串(contains 空串恒真)"
    assert body.count(hint.MARKER) == 1, "同一份正文里 marker 只该出现一次"


def test_main_writes_the_comment_body(tmp_path: Path) -> None:
    """入口把正文写进 ``--out``(交给 ``gh api --input`` 发出去), 末尾留一个换行."""
    hint = _load_script()
    out = tmp_path / "hint.md"

    assert hint.main(["--current", "0.0.1", "--next", "0.0.2", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text == hint.render("0.0.1", "0.0.2"), "写出的是 render 的结果"
    assert text.endswith("\n"), "markdown 文件留一个换行"
