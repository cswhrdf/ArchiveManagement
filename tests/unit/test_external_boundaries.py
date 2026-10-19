"""回收站后端与系统选择框的降级行为.

两者都是"外部环境可能不可用"的边界封装: 回收站不可用时必须报错而不是回退到
永久删除, 选择框取消时必须返回 ``None`` 而不是空字符串。这里用替身注入外部
依赖, 因此不触碰真实回收站, 也不需要图形环境。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from tkinter import filedialog

import pytest

from archive_management.exceptions import StorageError
from archive_management.services import trash as trash_mod
from archive_management.ui import pickers

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("外部依赖边界"),
    pytest.mark.story("回收站与系统选择框"),
    pytest.mark.layer("unit"),
]


def _fake_send2trash(monkeypatch: pytest.MonkeyPatch, behaviour: object) -> list[str]:
    """把 ``send2trash`` 模块替换成替身, 返回记录到的路径."""
    calls: list[str] = []

    def fake(path: str) -> None:
        calls.append(path)
        if isinstance(behaviour, Exception):
            raise behaviour

    module = types.ModuleType("send2trash")
    module.send2trash = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "send2trash", module)
    return calls


def test_send_to_trash_passes_the_path_as_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """正常路径走系统回收站: 传入的必须是可以直接交给后端的字符串路径."""
    calls = _fake_send2trash(monkeypatch, behaviour=None)
    target = tmp_path / "save"

    trash_mod.send_to_trash(str(target))

    assert calls == [str(target)]


def test_send_to_trash_wraps_os_error_as_storage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """系统拒绝(权限/网络盘)时转成可展示错误, 调用方据此判定"未删除"."""
    _fake_send2trash(monkeypatch, behaviour=OSError("回收站不可用"))

    with pytest.raises(StorageError, match="移入回收站失败"):
        trash_mod.send_to_trash(str(tmp_path / "save"))


def test_missing_backend_is_reported_without_deleting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """依赖缺失时明确报错: 绝不能回退成永久删除."""
    monkeypatch.setitem(sys.modules, "send2trash", None)
    target = tmp_path / "save"
    target.mkdir()

    with pytest.raises(StorageError, match="缺少回收站支持"):
        trash_mod.send_to_trash(str(target))

    assert target.is_dir()


def test_pick_directory_returns_choice_or_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """目录选择: 选中返回路径, 取消返回 ``None``."""
    answers = {"chosen": str(tmp_path)}

    def ask_directory(**_kwargs: object) -> str:
        return answers["chosen"]

    monkeypatch.setattr(filedialog, "askdirectory", ask_directory)

    assert pickers.pick_directory(title="选择目录") == str(tmp_path)

    answers["chosen"] = ""
    assert pickers.pick_directory(title="选择目录") is None


def test_pick_file_returns_choice_or_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """文件选择: 选中返回路径, 取消返回 ``None``."""
    chosen = tmp_path / "slot1.dat"
    answers = {"chosen": str(chosen)}

    def ask_open_file(**_kwargs: object) -> str:
        return answers["chosen"]

    monkeypatch.setattr(filedialog, "askopenfilename", ask_open_file)

    assert pickers.pick_file(title="选择存档") == str(chosen)

    answers["chosen"] = ""
    assert pickers.pick_file(title="选择存档") is None


def test_pick_save_file_returns_choice_or_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """保存对话框: 选中返回路径, 取消返回 ``None``, 预填文件名要透传."""
    chosen = tmp_path / "Demo.archive.zip"
    answers: dict[str, str] = {"chosen": str(chosen)}
    seen: list[dict[str, object]] = []

    def ask_save_file(**kwargs: object) -> str:
        seen.append(kwargs)
        return answers["chosen"]

    monkeypatch.setattr(filedialog, "asksaveasfilename", ask_save_file)

    picked = pickers.pick_save_file(title="导出游戏", initialfile="Demo.archive.zip")

    assert picked == str(chosen)
    assert seen[-1] == {"title": "导出游戏", "initialfile": "Demo.archive.zip"}

    answers["chosen"] = ""
    assert (
        pickers.pick_save_file(title="导出游戏", initialfile="Demo.archive.zip") is None
    )
    # 不预填名字时也要把选项传给对话框(Tk 收到空串 = 没有默认文件名).
    pickers.pick_save_file(title="导出游戏")
    assert seen[-1] == {"title": "导出游戏", "initialfile": ""}


def test_resolve_start_dir_walks_to_the_nearest_existing_ancestor(
    tmp_path: Path,
) -> None:
    """输入的地址能走到多近就走到多近: 目录原样, 文件取所在目录, 不存在退祖先."""
    saves = tmp_path / "Saves"
    saves.mkdir()
    game = saves / "game.dat"
    game.write_text("x", encoding="utf-8")

    assert pickers.resolve_start_dir(f"  {saves} ") == str(saves)
    assert pickers.resolve_start_dir(str(game)) == str(saves)
    assert pickers.resolve_start_dir(str(saves / "NewGame")) == str(saves)
    assert pickers.resolve_start_dir(str(saves / "NewGame" / "slots")) == str(saves)


def test_resolve_start_dir_returns_none_when_unreachable() -> None:
    """空白、相对路径、不存在的盘符都解析不出: 交给系统对话框用自己的默认."""
    assert pickers.resolve_start_dir("   ") is None
    assert pickers.resolve_start_dir("definitely/not/here") is None
    assert pickers.resolve_start_dir("Q:/no/such/drive") is None


def test_resolve_start_dir_expands_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``~`` 按用户主目录展开后再判断(用户从别处粘贴来的路径常带着它)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert pickers.resolve_start_dir("~") == str(tmp_path)
    assert pickers.resolve_start_dir("~/saves-new") == str(tmp_path)


def test_pick_directory_opens_at_the_typed_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """输入框有可解析内容时选择框直接在那里打开; 解析不出时不传位置."""
    seen: list[dict[str, object]] = []

    def ask_directory(**kwargs: object) -> str:
        seen.append(kwargs)
        return str(tmp_path)

    monkeypatch.setattr(filedialog, "askdirectory", ask_directory)

    picked = pickers.pick_directory(title="选择目录", start_from=str(tmp_path))

    assert picked == str(tmp_path)
    assert seen == [{"title": "选择目录", "initialdir": str(tmp_path)}]

    pickers.pick_directory(title="选择目录", start_from="not/absolute")
    assert seen[-1] == {"title": "选择目录"}


def test_pick_file_preselects_the_typed_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """文件选择框: 输入指向文件时预选/预填文件名, 只剩目录时只开目录不猜名字."""
    target = tmp_path / "slot1.dat"
    target.write_text("x", encoding="utf-8")
    seen: list[dict[str, object]] = []

    def ask_open_file(**kwargs: object) -> str:
        seen.append(kwargs)
        return str(target)

    monkeypatch.setattr(filedialog, "askopenfilename", ask_open_file)

    pickers.pick_file(title="选择存档", start_from=str(target))
    assert seen[-1] == {
        "title": "选择存档",
        "initialdir": str(tmp_path),
        "initialfile": "slot1.dat",
    }

    pickers.pick_file(title="选择存档", start_from=str(tmp_path / "新档.dat"))
    assert seen[-1]["initialfile"] == "新档.dat"

    pickers.pick_file(title="选择存档", start_from=str(tmp_path))
    assert "initialfile" not in seen[-1]

    pickers.pick_file(title="选择存档", start_from="")
    assert seen[-1] == {"title": "选择存档"}
