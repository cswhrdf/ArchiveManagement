"""批量包格式的单元测试(打包、结构校验与安全解包).

批量包只装各款游戏**已经写好的单包**, 不重复游戏内容; 它复用单包的路径/大小/
哈希/符号链接检查, 并在读包时把每个内层包交给单包读取器。这里锁住的就是这两条:
外层包不含游戏内容(条目名与展开总量都能证明), 以及外层与内层任何一处对不上都会
被拒绝。

内容文件刻意用"可压缩的长文本": 只有内容的压缩空间足够大, "批量包的展开总量
接近各内层包之和、而不是各游戏内容之和"这条断言才有分辨力。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from archive_management.exceptions import OperationCancelledError, PackageError
from archive_management.services import export_format as fmt

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("导出包格式"),
    pytest.mark.story("批量打包与安全解包"),
    pytest.mark.layer("integration"),
]


def _payload(game: str) -> bytes:
    """一款游戏的内容文件(按游戏名派生, 可压缩的长文本)."""
    return f"world {game}\n".encode() * 2000


def _inner(
    tmp_path: Path,
    name: str = "Demo",
    *,
    app_id: int | None = 730,
    content: bytes | None = None,
) -> Path:
    """写一个最小的单游戏导出包(内容确定, 便于逐字节比对), 返回它的路径."""
    source = tmp_path / f"{name}.dat"
    source.write_bytes(_payload(name) if content is None else content)
    return fmt.write_package(
        tmp_path / fmt.archive_entry_name(name),
        game={"name": name, "steam_app_id": app_id, "platform": "windows"},
        config={
            "game": {"name": name},
            "locations": [],
            "backups": [{"id": "n1", "is_safety": False}],
        },
        members=[fmt.PackageFile("branches/n1/loc-0/slot.dat", source)],
        tool_version="9.9.9",
    ).path


def _built(tmp_path: Path, *names: str) -> tuple[Path, tuple[Path, ...]]:
    """把若干款游戏各写一个内层包, 再打成批量包, 返回外层包与各内层包的路径."""
    destination = tmp_path / "batch.archive.zip"
    games: list[fmt.BatchGame] = []
    inners: list[Path] = []
    for name in names:
        inner = _inner(tmp_path, name)
        inners.append(inner)
        games.append(fmt.BatchGame(fmt.archive_entry_name(name), inner))
    fmt.write_batch_package(destination, games=games, tool_version="9.9.9")
    return destination, tuple(inners)


def _edit_manifest(path: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    """改写外层包的清单(内层包一字不动), 用于构造"清单与实际对不上"的现场."""
    with zipfile.ZipFile(path) as archive:
        items = [
            (info.filename, archive.read(info.filename)) for info in archive.infolist()
        ]
    rewritten: list[tuple[str, bytes]] = []
    for name, blob in items:
        if name == fmt.MANIFEST_NAME:
            manifest: dict[str, Any] = json.loads(blob)
            edit(manifest)
            blob = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        rewritten.append((name, blob))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for name, blob in rewritten:
            target.writestr(name, blob)


def _add_member(path: Path, name: str, blob: bytes) -> None:
    """往外层包里加一条清单没声明的条目(构造"多出来"的现场)."""
    with zipfile.ZipFile(path) as archive:
        items = [
            (info.filename, archive.read(info.filename)) for info in archive.infolist()
        ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for member, content in items:
            target.writestr(member, content)
        target.writestr(name, blob)


def _raw_batch(
    path: Path,
    entries: Mapping[str, bytes],
    *,
    name: str = "Demo",
) -> None:
    """手工拼一个"外包装配完全自洽"的批量包: 清单行按条目内容算出 size/sha256.

    用它可以在外层挑不出毛病的前提下塞进一个**内容有问题**的内层包, 于是失败点
    落在内层单包自己的校验上(而不是外层的哈希或大小比对).
    """
    rows = [
        {
            "entry": entry,
            "name": name,
            "steam_app_id": 730,
            "platform": "windows",
            "nodes": 1,
            "files": 1,
            "bytes": len(blob),
            "size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        }
        for entry, blob in entries.items()
    ]
    manifest = {
        "format": fmt.EXPORT_FORMAT,
        "kind": fmt.BATCH_KIND,
        "version": fmt.EXPORT_FORMAT_VERSION,
        "tool_version": "9.9.9",
        "games": rows,
        "files": {
            "count": len(rows),
            "bytes": sum(len(blob) for blob in entries.values()),
        },
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            fmt.MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        )
        for entry, blob in entries.items():
            archive.writestr(entry, blob)


def _outer_members(path: Path) -> tuple[list[str], int, int]:
    """返回外层包的文件名(排序)、它们的展开总量与清单自身的展开大小."""
    with zipfile.ZipFile(path) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = sorted(info.filename for info in infos)
        total = sum(info.file_size for info in infos)
        manifest_size = archive.getinfo(fmt.MANIFEST_NAME).file_size
    return names, total, manifest_size


# -- 往返 -------------------------------------------------------------------


def test_batch_round_trip_keeps_two_games_in_order(tmp_path: Path) -> None:
    """两款游戏各一个内层包: 外层清单与内层单包都能完整回读, 顺序与传入一致."""
    destination, inners = _built(tmp_path, "Demo", "Other")

    with fmt.read_batch_package(destination, verify_hashes=True) as batch:
        assert batch.path == destination
        assert batch.game_count == 2
        assert [item.entry for item in batch.games] == [
            "Demo.archive.zip",
            "Other.archive.zip",
        ]
        assert [item.name for item in batch.games] == ["Demo", "Other"]
        assert batch.games[0].package.config_list("backups") == [
            {"id": "n1", "is_safety": False}
        ]
        assert [entry.path for entry in batch.games[1].package.entries] == [
            "branches/n1/loc-0/slot.dat"
        ]
        workdir = batch.workdir
        # 解出来的内层包与磁盘上的单包逐字节相同(批量格式不动内容).
        assert (workdir / "Demo.archive.zip").read_bytes() == inners[0].read_bytes()

    assert not workdir.exists()


def test_batch_manifest_records_kind_versions_and_real_hashes(tmp_path: Path) -> None:
    """外层清单有 kind/版本/时间/工具版本, 逐游戏记下真实哈希与内层包大小."""
    destination, inners = _built(tmp_path, "Demo", "Other")

    with zipfile.ZipFile(destination) as archive:
        manifest = json.loads(archive.read(fmt.MANIFEST_NAME))

    assert manifest["format"] == fmt.EXPORT_FORMAT
    assert manifest["kind"] == fmt.BATCH_KIND
    assert manifest["version"] == fmt.EXPORT_FORMAT_VERSION
    assert manifest["tool_version"] == "9.9.9"
    assert manifest["created_at"]
    sizes = [item.stat().st_size for item in inners]
    assert manifest["files"] == {"count": 2, "bytes": sum(sizes)}
    rows = manifest["games"]
    assert [row["entry"] for row in rows] == ["Demo.archive.zip", "Other.archive.zip"]
    for row, inner, size in zip(rows, inners, sizes, strict=True):
        assert row["entry"] == inner.name
        assert row["size"] == size
        assert row["sha256"] == fmt.sha256_of_file(inner)
        assert row["steam_app_id"] == 730
        assert row["platform"] == "windows"
        assert row["nodes"] == 1
        assert row["files"] == 1
    assert [row["name"] for row in rows] == ["Demo", "Other"]
    assert [row["bytes"] for row in rows] == [
        len(_payload("Demo")),
        len(_payload("Other")),
    ]


def test_the_batch_does_not_duplicate_the_game_content(tmp_path: Path) -> None:
    """外层包只装内层包: 没有 branches/ 条目, 展开总量 = 内层包之和 + 清单."""
    destination, inners = _built(tmp_path, "Demo", "Other")

    names, total, manifest_size = _outer_members(destination)

    assert names == ["Demo.archive.zip", "Other.archive.zip", fmt.MANIFEST_NAME]
    assert not [name for name in names if name.startswith(f"{fmt.BRANCHES_DIR}/")]
    assert total == sum(item.stat().st_size for item in inners) + manifest_size
    # 内容是可压缩的长文本: 外层若把内容再装一遍, 总量会接近各游戏内容之和.
    game_bytes = len(_payload("Demo")) + len(_payload("Other"))
    assert total < game_bytes / 4


def test_write_batch_package_reports_progress(tmp_path: Path) -> None:
    """进度比例单调不减且落在 0..1, 并带上正在写的条目名."""
    destination = tmp_path / "batch.archive.zip"
    inner = _inner(tmp_path, "Demo")
    seen: list[tuple[float, str]] = []

    fmt.write_batch_package(
        destination,
        games=[fmt.BatchGame("Demo.archive.zip", inner)],
        progress=lambda ratio, text: seen.append((ratio, text)),
    )

    ratios = [ratio for ratio, _text in seen]
    assert ratios == sorted(ratios)
    assert all(0.0 <= ratio <= 1.0 for ratio in ratios)
    assert any("Demo.archive.zip" in text for _ratio, text in seen)


def test_write_batch_package_replaces_the_target_atomically(tmp_path: Path) -> None:
    """目标已存在时覆盖成完整包(用户在保存框已确认), 且不留临时文件."""
    destination = tmp_path / "batch.archive.zip"
    destination.write_bytes(b"old")
    inner = _inner(tmp_path, "Demo")

    summary = fmt.write_batch_package(
        destination, games=[fmt.BatchGame("Demo.archive.zip", inner)]
    )

    assert summary.path == destination
    assert summary.game_count == 1
    assert summary.file_count == 1
    assert summary.total_bytes == inner.stat().st_size
    assert not list(tmp_path.glob(f"*{fmt._PARTIAL_SUFFIX}*"))
    with fmt.read_batch_package(destination) as batch:
        assert batch.game_count == 1


def test_write_batch_package_cleans_up_when_cancelled(tmp_path: Path) -> None:
    """取消: 目标路径上不出现任何东西, 临时文件也被删掉."""
    destination = tmp_path / "batch.archive.zip"
    inner = _inner(tmp_path, "Demo")

    with pytest.raises(OperationCancelledError):
        fmt.write_batch_package(
            destination,
            games=[fmt.BatchGame("Demo.archive.zip", inner)],
            cancelled=lambda: True,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(f"*{fmt._PARTIAL_SUFFIX}*"))


def test_read_batch_package_cleans_up_its_temp_directory(tmp_path: Path) -> None:
    """内层包在 with 块内可用, 退出后临时目录被删掉(close 可重复调用)."""
    destination, inners = _built(tmp_path, "Demo")

    with fmt.read_batch_package(destination) as batch:
        workdir = batch.workdir
        assert (workdir / "Demo.archive.zip").read_bytes() == inners[0].read_bytes()
        batch.close()
        assert not workdir.exists()
        batch.close()

    assert not workdir.exists()


# -- 写侧的拒绝 -------------------------------------------------------------


def test_write_batch_package_rejects_duplicate_entry_names(tmp_path: Path) -> None:
    """两个内层包用同一个条目名会互相覆盖(大小写不敏感), 必须拒绝."""
    inner = _inner(tmp_path, "Demo")
    games = [
        fmt.BatchGame("Demo.archive.zip", inner),
        fmt.BatchGame("demo.ARCHIVE.ZIP", inner),
    ]

    with pytest.raises(PackageError):
        fmt.write_batch_package(tmp_path / "batch.archive.zip", games=games)


def test_write_batch_package_rejects_an_empty_game_list(tmp_path: Path) -> None:
    """没有游戏的批量包没有意义, 写侧直接拒绝."""
    with pytest.raises(PackageError):
        fmt.write_batch_package(tmp_path / "batch.archive.zip", games=[])


@pytest.mark.blocker
@pytest.mark.parametrize(
    "entry",
    [
        "",
        "../escape.zip",
        "/abs.zip",
        "C:/drive.zip",
        "a\\b.zip",
        "nested/Demo.zip",
        "Demo.dat",
        "Demo",
    ],
)
def test_batch_entry_names_must_be_plain_zip_files(tmp_path: Path, entry: str) -> None:
    """条目名必须是**单段**、以 .zip 结尾的相对名(越界、子目录、别的后缀都拒绝)."""
    inner = _inner(tmp_path, "Demo")

    with pytest.raises(PackageError):
        fmt.BatchGame(entry, inner)


def test_write_batch_package_rejects_a_source_that_is_not_a_file(
    tmp_path: Path,
) -> None:
    """来源不存在时给出明确的格式错误(而不是写出一只空壳)."""
    with pytest.raises(PackageError):
        fmt.write_batch_package(
            tmp_path / "batch.archive.zip",
            games=[fmt.BatchGame("Demo.archive.zip", tmp_path / "gone.zip")],
        )


@pytest.mark.blocker
def test_write_batch_package_refuses_to_follow_a_symlink(tmp_path: Path) -> None:
    """来源是符号链接时拒绝: 否则会把链接指向的内容一起装进包里."""
    inner = _inner(tmp_path, "Demo")
    link = tmp_path / "link.zip"
    try:
        link.symlink_to(inner)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(PackageError):
        fmt.write_batch_package(
            tmp_path / "batch.archive.zip",
            games=[fmt.BatchGame("link.zip", link)],
        )


def test_write_batch_package_rejects_an_inner_package_that_is_not_a_package(
    tmp_path: Path,
) -> None:
    """内层"包"其实是垃圾字节时, 写包就拒绝(不等导入时才发现)."""
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip at all")

    with pytest.raises(PackageError):
        fmt.write_batch_package(
            tmp_path / "batch.archive.zip",
            games=[fmt.BatchGame("broken.zip", broken)],
        )


def test_write_batch_package_enforces_the_entry_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单个内层包超过上限时不写包(与单包共用同一条上限)."""
    inner = _inner(tmp_path, "Demo")
    monkeypatch.setattr(fmt, "MAX_ENTRY_BYTES", 1)

    with pytest.raises(PackageError):
        fmt.write_batch_package(
            tmp_path / "batch.archive.zip",
            games=[fmt.BatchGame("Demo.archive.zip", inner)],
        )


def test_write_batch_package_enforces_the_game_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """游戏数超过上限直接拒绝(上限用 monkeypatch 调小, 与单包上限用例同构)."""
    inner = _inner(tmp_path, "Demo")
    monkeypatch.setattr(fmt, "MAX_BATCH_GAMES", 1)
    games = [fmt.BatchGame("a.zip", inner), fmt.BatchGame("b.zip", inner)]

    with pytest.raises(PackageError):
        fmt.write_batch_package(tmp_path / "batch.archive.zip", games=games)


# -- 读侧的拒绝 -------------------------------------------------------------


def test_read_batch_package_rejects_a_missing_or_corrupt_file(tmp_path: Path) -> None:
    """路径不存在、以及根本不是压缩包的文件: 与单包同一套可读报错."""
    with pytest.raises(PackageError):
        fmt.read_batch_package(tmp_path / "nope.zip")

    text = tmp_path / "text.zip"
    text.write_bytes("我不是压缩包".encode())
    with pytest.raises(PackageError):
        fmt.read_batch_package(text)


def test_read_batch_package_rejects_a_batch_without_games(tmp_path: Path) -> None:
    """清单里的 games 是空数组: 读侧同样拒绝(不返回一个空批次)."""
    destination, _inners = _built(tmp_path, "Demo")
    _edit_manifest(destination, lambda manifest: manifest.update({"games": []}))

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination)


def test_read_batch_package_rejects_an_entry_name_that_escapes(tmp_path: Path) -> None:
    """外层清单里写了一条越界的条目名: 解析清单时就拒绝."""
    destination, _inners = _built(tmp_path, "Demo")
    _edit_manifest(
        destination,
        lambda manifest: manifest["games"][0].update({"entry": "../escape.zip"}),
    )

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination)


def test_read_batch_package_rejects_a_missing_entry(tmp_path: Path) -> None:
    """清单声明了一个包里没有的条目: 拒绝(不少导一款游戏)."""
    destination, _inners = _built(tmp_path, "Demo")
    _edit_manifest(
        destination,
        lambda manifest: manifest["games"][0].update({"entry": "Gone.archive.zip"}),
    )

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination)


def test_read_batch_package_rejects_duplicate_entries(tmp_path: Path) -> None:
    """清单里两行指向同一个条目: 后一行会静默丢掉一款游戏, 必须拒绝."""
    destination, _inners = _built(tmp_path, "Demo", "Other")

    def merge_rows(manifest: dict[str, Any]) -> None:
        manifest["games"][1]["entry"] = manifest["games"][0]["entry"]

    _edit_manifest(destination, merge_rows)

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination)


def test_read_batch_package_rejects_an_entry_the_manifest_does_not_declare(
    tmp_path: Path,
) -> None:
    """外层多出清单没声明的条目: 结构不一致, 拒绝."""
    destination, _inners = _built(tmp_path, "Demo")
    _add_member(destination, "extra.zip", b"extra")

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination)


def test_read_batch_package_rejects_a_size_mismatch(tmp_path: Path) -> None:
    """清单声明的内层包大小与实际不符: 不要求校验哈希时也要拒绝."""
    destination, _inners = _built(tmp_path, "Demo")

    def inflate(manifest: dict[str, Any]) -> None:
        manifest["games"][0]["size"] = manifest["games"][0]["size"] + 1

    _edit_manifest(destination, inflate)

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination)


def test_verify_hashes_catches_a_tampered_inner_entry(tmp_path: Path) -> None:
    """清单里的内层包哈希被改过: 要求校验哈希时拒绝, 只做体检时放行."""
    destination, _inners = _built(tmp_path, "Demo")
    _edit_manifest(
        destination,
        lambda manifest: manifest["games"][0].update({"sha256": "0" * 64}),
    )

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination, verify_hashes=True)

    with fmt.read_batch_package(destination) as batch:
        assert batch.game_count == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", "Other"), ("steam_app_id", 999), ("platform", "linux")],
)
def test_read_batch_package_rejects_a_row_that_disagrees_with_the_inner_package(
    tmp_path: Path, field: str, value: object
) -> None:
    """外层清单那一行与内层包自己的游戏标识对不上: 拒绝(否则会把包导错)."""
    destination, _inners = _built(tmp_path, "Demo")
    _edit_manifest(
        destination,
        lambda manifest: manifest["games"][0].update({field: value}),
    )

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination)


def test_read_batch_package_rejects_an_inner_package_of_the_wrong_game(
    tmp_path: Path,
) -> None:
    """外层那一行说是 Demo, 里面装的却是 Other 的单包: 拒绝(外层自洽也不行)."""
    other = _inner(tmp_path, "Other")
    destination = tmp_path / "batch.archive.zip"
    _raw_batch(destination, {"Other.archive.zip": other.read_bytes()}, name="Demo")

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination, verify_hashes=True)


def test_read_batch_package_rejects_an_inner_package_that_fails_its_own_checks(
    tmp_path: Path,
) -> None:
    """内层包不是合法单包时, 报错里带上条目名(坏包也能说清是哪一款)."""
    destination = tmp_path / "batch.archive.zip"
    _raw_batch(destination, {"Demo.archive.zip": b"not a zip at all"})

    with pytest.raises(PackageError, match=r"Demo\.archive\.zip"):
        fmt.read_batch_package(destination, verify_hashes=True)


def test_read_batch_package_enforces_the_entry_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内层包展开超过单条目上限的批量包直接拒绝."""
    destination, _inners = _built(tmp_path, "Demo")
    monkeypatch.setattr(fmt, "MAX_ENTRY_BYTES", 1)

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination)


def test_read_batch_package_enforces_the_game_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清单里的游戏数超过上限直接拒绝(先看声明, 不解任何内层包)."""
    destination, _inners = _built(tmp_path, "Demo", "Other")
    monkeypatch.setattr(fmt, "MAX_BATCH_GAMES", 1)

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination)


def test_the_two_package_kinds_reject_each_other(tmp_path: Path) -> None:
    """单包与批量包互喂时给出"包类型不符"的可读报错, 而不是缺配置/缺条目."""
    inner = _inner(tmp_path, "Demo")
    destination, _inners = _built(tmp_path, "Demo")

    with pytest.raises(PackageError, match="批量导出包"):
        fmt.read_batch_package(inner)

    with pytest.raises(PackageError, match="单游戏导出包"):
        fmt.read_package(destination)


# -- 上限与来源形态 ---------------------------------------------------------


def test_write_batch_package_enforces_the_total_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内层包合计超过总量上限时不写包(批量包不重复内容, 但也要防体积失控)."""
    inner = _inner(tmp_path, "Demo")
    monkeypatch.setattr(fmt, "MAX_TOTAL_BYTES", 1)

    with pytest.raises(PackageError, match="总大小超过上限"):
        fmt.write_batch_package(
            tmp_path / "batch.archive.zip",
            games=[fmt.BatchGame("Demo.archive.zip", inner)],
        )


def test_write_batch_package_rejects_a_source_that_is_a_directory(
    tmp_path: Path,
) -> None:
    """来源是目录时拒绝: 批量包只搬"内层包"这一个普通文件."""
    with pytest.raises(PackageError, match="不是普通文件"):
        fmt.write_batch_package(
            tmp_path / "batch.archive.zip",
            games=[fmt.BatchGame("Demo.archive.zip", tmp_path)],
        )


@pytest.mark.blocker
def test_write_batch_package_refuses_a_source_that_reports_as_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """来源是符号链接时拒绝(本机无权建链接时用替身钉住这条守卫)."""
    inner = _inner(tmp_path, "Demo")
    link = tmp_path / "link.zip"
    link.write_bytes(inner.read_bytes())
    real = Path.is_symlink

    def looks_like_a_link(self: Path) -> bool:
        return True if self == link else real(self)

    monkeypatch.setattr(Path, "is_symlink", looks_like_a_link)

    with pytest.raises(PackageError, match="符号链接"):
        fmt.write_batch_package(
            tmp_path / "batch.archive.zip",
            games=[fmt.BatchGame("link.zip", link)],
        )


def test_write_batch_package_rejects_an_inner_package_without_a_game_name(
    tmp_path: Path,
) -> None:
    """内层包里没有游戏名称时拒绝: 外层清单那一行没有任何东西可记."""
    source = tmp_path / "slot.dat"
    source.write_bytes(b"alpha\n")
    anonymous = fmt.write_package(
        tmp_path / "anonymous.archive.zip",
        game={},
        config={"game": {}},
        members=[fmt.PackageFile("branches/n1/loc-0/slot.dat", source)],
    ).path

    with pytest.raises(PackageError, match="没有游戏名称"):
        fmt.write_batch_package(
            tmp_path / "batch.archive.zip",
            games=[fmt.BatchGame("Anonymous.archive.zip", anonymous)],
        )


def _written_batch_members_of(path: Path) -> set[str]:
    """在真实 zip 上读一次外层成员名(单独成函数, 免得嵌套 with)."""
    with zipfile.ZipFile(path) as archive:
        return fmt._written_batch_members(archive)


def test_written_batch_members_requires_the_manifest(tmp_path: Path) -> None:
    """外层回读缺清单时直接拒绝: 少了它这份包称不上写成功."""
    destination = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("Demo.archive.zip", b"bytes")

    with pytest.raises(PackageError, match=fmt.MANIFEST_NAME):
        _written_batch_members_of(destination)


def test_verify_written_batch_rejects_a_package_with_fewer_entries(
    tmp_path: Path,
) -> None:
    """外层回读时磁盘上的条目少于清单: 报错而不是把缺游戏的包交出去."""
    destination, _inners = _built(tmp_path, "Demo")

    with pytest.raises(PackageError, match="条目与清单不一致"):
        fmt._verify_written_batch(destination, plans=(), cancelled=None)


@pytest.mark.parametrize(
    "edit",
    [
        lambda manifest: manifest.update({"games": ["boom"]}),
        lambda manifest: manifest["games"][0].update({"name": ""}),
        lambda manifest: manifest["games"][0].update({"size": "x"}),
        lambda manifest: manifest["games"][0].update({"sha256": "zz"}),
    ],
)
def test_read_batch_package_rejects_a_broken_game_row(
    tmp_path: Path, edit: Callable[[dict[str, Any]], None]
) -> None:
    """游戏行不是对象 / 名称为空 / size 非法 / sha256 非法: 读包时一律拒绝."""
    destination, _inners = _built(tmp_path, "Demo")
    _edit_manifest(destination, edit)

    with pytest.raises(PackageError):
        fmt.read_batch_package(destination)
