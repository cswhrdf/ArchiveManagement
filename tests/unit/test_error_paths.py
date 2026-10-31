"""错误路径与防御分支的用例: 让"只在边界/异常上才会执行"的代码也进统计.

为什么单独一个文件: 这些用例不是新功能, 而是把既有行为里**只在边界上**才会走到的那几行也钉
住 —— 覆盖率报告里它们以前是缺口, 而缺口与"没人看过的代码"长得一模一样。判据一律是"这条边界
的输出是什么", 不是"跑过就算"。

写新用例之前先想清楚该用哪一种工具(与仓库既有口径一致):

- 能构造出边界的 ⇒ 补测试(本文件);
- 只可能在自己的平台上执行的 ⇒ ``# platform: ...`` 标记(见 ``scripts/coverage_platform.py``);
- 真的永远不会执行的 ⇒ ``# pragma: no cover - 原因``。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from archive_management.exceptions import ArtworkImageError
from archive_management.services import artwork, game_names, hotkeys, pathcheck

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率(错误路径)"),
    pytest.mark.story("边界输入要有明确的拒绝理由"),
    pytest.mark.layer("unit"),
]


def test_an_unparsable_accelerator_is_rejected_as_unknown() -> None:
    """解析不出来的文本给"未知令牌", 而不是当成合法组合放过去."""
    assert hotkeys.combo_error(None) == hotkeys.REASON_UNKNOWN


def test_an_empty_accelerator_is_rejected_as_empty() -> None:
    """空文本是"空组合", 与"未知令牌"要分开报(界面上的下一步动作不一样)."""
    combo = hotkeys.parse_accelerator("")
    assert combo is not None
    assert hotkeys.combo_error(combo) == hotkeys.REASON_EMPTY


def test_a_bare_letter_is_rejected_for_having_no_modifier() -> None:
    """单个字母键不算组合键: 它会把打字吞掉, 必须拒绝."""
    assert (
        hotkeys.combo_error(hotkeys.parse_accelerator("a"))
        == hotkeys.REASON_NO_MODIFIER
    )


def test_a_modifier_only_combo_is_rejected_for_having_no_letter() -> None:
    """只有修饰键的组合(``<ctrl>+<shift>``)要给出"没有字母键"这条理由."""
    combo = hotkeys.parse_accelerator("<ctrl>+<shift>")
    assert combo is not None
    assert combo.key is None
    assert hotkeys.combo_error(combo) == hotkeys.REASON_NO_LETTER


def test_the_pynput_form_of_a_modifier_only_combo_has_no_empty_key_part() -> None:
    """转成 pynput 写法时, 没有主键的组合不该多出一个空段(尾部多余的分隔符)."""
    converted = hotkeys.to_pynput_accelerator("<ctrl>+<shift>")
    assert converted.count("+") == 1, f"只有两个修饰键: {converted!r}"
    assert not converted.startswith("+")
    assert not converted.endswith("+")


def test_the_name_cache_forget_is_a_no_op_for_an_unknown_app(tmp_path: Path) -> None:
    """清理译名缓存时, 库里没有的 AppID 不该顺手写一次文件."""
    target = tmp_path / "names.json"
    cache = game_names.NameCache(target)
    cache.forget("999")
    assert not target.exists()


def test_the_name_cache_forget_removes_every_locale_of_one_app(tmp_path: Path) -> None:
    """清理要按 AppID 清掉全部语言的条目, 且只清它自己的."""
    cache = game_names.NameCache(tmp_path / "names.json")
    cache.put("42", "zh", "传送门")
    cache.put("42", "en", "Portal")
    cache.put("43", "zh", "半衰期")

    cache.forget("42")

    assert cache.get("42", "zh") is None
    assert cache.get("42", "en") is None
    assert cache.get("43", "zh") == "半衰期"


def test_a_summary_counts_subdirectories_and_files(tmp_path: Path) -> None:
    """汇总要分开报"文件/目录"的数量与总字节数(目录本身不算字节)."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.bin").write_bytes(b"1234")
    (tmp_path / "b.bin").write_bytes(b"12")

    summary = pathcheck.summarize_path(str(tmp_path))

    assert (summary.files, summary.directories, summary.symlinks) == (2, 1, 0)
    assert summary.total_size == 6


def test_a_summary_of_a_missing_path_is_empty(tmp_path: Path) -> None:
    """不存在的路径给空汇总(不是异常)."""
    summary = pathcheck.summarize_path(str(tmp_path / "nope"))
    assert summary.files == 0
    assert summary.directories == 0
    assert summary.symlinks == 0
    assert summary.total_size == 0


def test_a_summary_of_a_symlink_counts_only_the_link_itself(tmp_path: Path) -> None:
    """路径本身是符号链接时只记一条链接, 不跟进去数(避免越界与链接环)."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "inside.bin").write_bytes(b"x")
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - 平台相关
        pytest.skip(f"当前环境不允许创建符号链接: {exc}")

    summary = pathcheck.summarize_path(str(link))

    assert (summary.files, summary.directories, summary.symlinks) == (0, 0, 1)


def _png_bytes(size: tuple[int, int] = (4, 4)) -> bytes:
    """一张最小可用的 PNG(校验路径要真的能解码)."""
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_the_user_artwork_store_exposes_its_root(tmp_path: Path) -> None:
    """存储的根目录要能读出来(界面靠它定位"清理缓存"的范围)."""
    store = artwork.UserArtworkStore(tmp_path / "artwork")
    assert store.root == tmp_path / "artwork"


def test_an_image_that_cannot_be_written_reports_a_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘失败要给"写入失败"而不是冒一个裸 ``OSError``(界面要按原因代码提示)."""
    store = artwork.UserArtworkStore(tmp_path / "artwork")
    source = tmp_path / "cover.png"
    source.write_bytes(_png_bytes())

    def refuse(self: Path, data: bytes) -> int:
        raise OSError("磁盘满")

    monkeypatch.setattr(Path, "write_bytes", refuse)

    with pytest.raises(ArtworkImageError):
        store.save("outer-wilds", "cover", source)


def test_a_file_that_is_not_an_image_is_rejected_for_an_icon(tmp_path: Path) -> None:
    """图标位给一个不是图片的文件时要报"不是图片", 不能画出黑图."""
    source = tmp_path / "not-an-image.png"
    source.write_bytes(b"definitely not a png")

    with pytest.raises(ArtworkImageError):
        artwork.normalized_image(source, "icon")


def test_an_oversized_file_is_rejected_before_any_decoding(tmp_path: Path) -> None:
    """超过体积上限的文件在解码之前就该拒(稀疏文件只为撑体积, 不真写进去)."""
    source = tmp_path / "huge.png"
    with source.open("wb") as handle:
        handle.truncate(artwork.MAX_USER_IMAGE_BYTES + 1)

    with pytest.raises(ArtworkImageError):
        artwork.normalized_image(source, "cover")


def test_clearing_an_image_removes_the_now_empty_directory(tmp_path: Path) -> None:
    """清掉最后一张图之后不该留下空目录."""
    store = artwork.UserArtworkStore(tmp_path / "artwork")
    source = tmp_path / "cover.png"
    source.write_bytes(_png_bytes())
    saved = store.save("outer-wilds", "cover", source)
    assert saved.is_file()

    removed = store.clear("outer-wilds")

    assert removed == [saved]
    assert not saved.parent.exists()
