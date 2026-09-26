"""导出包格式的单元测试(打包、校验与安全解包).

覆盖三条硬性约束: **写出的包能回读校验**、**半成品不出现在用户选的路径上**、
**解包只允许在目标目录里写普通文件**(拒绝 Zip Slip、符号链接与超限的包)。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from archive_management.exceptions import OperationCancelledError, PackageError
from archive_management.services import export_format as fmt

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("导出包格式"),
    pytest.mark.story("打包与安全解包"),
    pytest.mark.layer("integration"),
]


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    """两个小文件: 一个纯文本, 一个带中文名与二进制内容(可重复调用).

    内容按**字节**写(``write_text`` 在 Windows 上会把 ``\\n`` 换成 ``\\r\\n``,
    那样断言就要跟着平台变了)。
    """
    text = tmp_path / "a.txt"
    text.write_bytes(b"alpha\n")
    binary = tmp_path / "nested" / "中文.bin"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"\x00\x01" * 10)
    return text, binary


def _members(tmp_path: Path) -> list[fmt.PackageFile]:
    """一个最小导出包的成员: 一个分支节点文件 + 一个时间线文件."""
    text, binary = _sources(tmp_path)
    return [
        fmt.PackageFile("branches/n1/loc-0/a.txt", text),
        fmt.PackageFile("timeline/n2/loc-0/nested/中文.bin", binary),
    ]


def _write(
    tmp_path: Path,
    *,
    members: list[fmt.PackageFile] | None = None,
    progress: fmt.ProgressCallback | None = None,
    cancelled: fmt.CancelProbe | None = None,
    config: dict[str, object] | None = None,
) -> fmt.PackageSummary:
    """按固定内容写一个包(测试里反复用到; ``config`` 可换成别的配置)."""
    payload = {"game": {"name": "Demo"}, "locations": []} if config is None else config
    return fmt.write_package(
        tmp_path / "Demo.archive.zip",
        game={"name": "Demo", "app_id": 730},
        config=payload,
        members=_members(tmp_path) if members is None else members,
        tool_version="9.9.9",
        progress=progress,
        cancelled=cancelled,
    )


def _rewrite_zip(path: Path, extra: tuple[str, bytes]) -> None:
    """把 zip 重写成"原内容 + 一个额外条目"(同名则替换内容)."""
    with zipfile.ZipFile(path) as source:
        items = [
            (info.filename, source.read(info.filename)) for info in source.infolist()
        ]
    items = [item for item in items if item[0] != extra[0]]
    items.append(extra)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for name, content in items:
            target.writestr(name, content)


def _write_raw_zip(path: Path, entries: dict[str, bytes]) -> None:
    """直接写一个内容自定的 zip(用于构造结构不合法或恶意的包)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def _manifest_json(**overrides: object) -> bytes:
    """一份最小合法清单(可覆盖字段)的 JSON 字节."""
    payload: dict[str, object] = {
        "format": fmt.EXPORT_FORMAT,
        "version": fmt.EXPORT_FORMAT_VERSION,
        "game": {"name": "Demo"},
        "entries": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_write_package_records_every_member_with_hashes(tmp_path: Path) -> None:
    """清单按路径排序记录大小与 sha256, 摘要给出条数与总字节."""
    text, binary = _sources(tmp_path)

    summary = _write(tmp_path)

    assert summary.path.is_file()
    assert summary.file_count == 2
    assert summary.total_bytes == text.stat().st_size + binary.stat().st_size
    with zipfile.ZipFile(summary.path) as archive:
        manifest = json.loads(archive.read(fmt.MANIFEST_NAME))
        assert manifest["format"] == fmt.EXPORT_FORMAT
        assert manifest["version"] == fmt.EXPORT_FORMAT_VERSION
        assert manifest["tool_version"] == "9.9.9"
        assert manifest["game"] == {"name": "Demo", "app_id": 730}
        assert manifest["files"] == {
            "count": 2,
            "bytes": summary.total_bytes,
        }
        assert [item["path"] for item in manifest["entries"]] == [
            "branches/n1/loc-0/a.txt",
            "timeline/n2/loc-0/nested/中文.bin",
        ]
        first = manifest["entries"][0]
        assert first["sha256"] == fmt.sha256_of_file(text)
        assert archive.read(fmt.CONFIG_NAME)


def test_written_package_reads_back_with_matching_entries(tmp_path: Path) -> None:
    """回读: 清单与配置都能解析, 逐条哈希自洽, 目录前缀能筛出节点成员."""
    destination = _write(tmp_path).path

    contents = fmt.read_package(destination, verify_hashes=True)

    assert contents.path == destination
    assert contents.game == {"name": "Demo", "app_id": 730}
    assert contents.config["game"] == {"name": "Demo"}
    assert [entry.path for entry in contents.entries] == [
        "branches/n1/loc-0/a.txt",
        "timeline/n2/loc-0/nested/中文.bin",
    ]
    assert contents.member_paths("branches/n1") == ("branches/n1/loc-0/a.txt",)
    assert contents.member_paths("timeline/n2") == (
        "timeline/n2/loc-0/nested/中文.bin",
    )


def test_write_package_replaces_the_target_atomically(tmp_path: Path) -> None:
    """目标已存在时覆盖成完整包(用户已在保存框确认), 且不留临时文件."""
    destination = tmp_path / "Demo.archive.zip"
    destination.write_text("旧内容", encoding="utf-8")

    summary = _write(tmp_path)

    assert summary.path == destination
    assert not list(tmp_path.glob(f"*{fmt._PARTIAL_SUFFIX}*"))
    assert fmt.read_package(destination, verify_hashes=True).entries


def test_write_package_cleans_up_when_cancelled(tmp_path: Path) -> None:
    """取消: 目标路径上不出现任何东西, 临时文件也被删掉."""
    destination = tmp_path / "Demo.archive.zip"

    with pytest.raises(OperationCancelledError):
        _write(tmp_path, cancelled=lambda: True)

    assert not destination.exists()
    assert not list(tmp_path.glob(f"*{fmt._PARTIAL_SUFFIX}*"))


def test_write_package_reports_progress(tmp_path: Path) -> None:
    """进度比例单调不减且落在 0..1, 并带上可读说明."""
    seen: list[tuple[float, str]] = []

    _write(tmp_path, progress=lambda ratio, text: seen.append((ratio, text)))

    ratios = [ratio for ratio, _text in seen]
    assert ratios == sorted(ratios)
    assert all(0.0 <= ratio <= 1.0 for ratio in ratios)
    assert any("a.txt" in text for _ratio, text in seen)


@pytest.mark.blocker
@pytest.mark.parametrize(
    "name",
    ["../escape.txt", "/abs.txt", "C:/drive.txt", "a\\b.txt", "a//b.txt", "x/"],
)
def test_member_paths_must_stay_inside_the_package(tmp_path: Path, name: str) -> None:
    """不安全的包内路径在建对象时就拒绝(绝对路径、``..``、反斜杠、空段)."""
    text, _binary = _sources(tmp_path)

    with pytest.raises(PackageError):
        fmt.PackageFile(name, text)


def test_write_package_rejects_duplicate_member_paths(tmp_path: Path) -> None:
    """同一路径出现两次(大小写与分隔符不敏感)会互相覆盖, 必须拒绝."""
    text, _binary = _sources(tmp_path)
    members = [
        fmt.PackageFile("branches/n1/a.txt", text),
        fmt.PackageFile("branches/n1/A.TXT", text),
    ]

    with pytest.raises(PackageError):
        _write(tmp_path, members=members)


def test_write_package_rejects_a_source_that_is_not_a_file(tmp_path: Path) -> None:
    """来源不是普通文件(目录/已消失)时给出明确的格式错误."""
    text, _binary = _sources(tmp_path)
    members = [
        fmt.PackageFile("branches/n1/a.txt", text),
        fmt.PackageFile("branches/n1/gone.txt", tmp_path / "gone.txt"),
    ]

    with pytest.raises(PackageError):
        _write(tmp_path, members=members)


@pytest.mark.blocker
def test_write_package_refuses_to_follow_a_symlink(tmp_path: Path) -> None:
    """来源是符号链接时拒绝: 否则会把链接指向的内容一起打进包里."""
    target = tmp_path / "outside.txt"
    target.write_bytes(b"secret\n")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - 平台不允许创建链接
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(PackageError):
        _write(tmp_path, members=[fmt.PackageFile("branches/n1/link.txt", link)])


def test_write_package_refuses_a_member_over_the_entry_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单个文件超过上限时不写包(上限是拒绝解压炸弹的一半, 写侧同样适用)."""
    text, _binary = _sources(tmp_path)
    monkeypatch.setattr(fmt, "MAX_ENTRY_BYTES", 1)

    with pytest.raises(PackageError):
        _write(tmp_path, members=[fmt.PackageFile("branches/n1/a.txt", text)])


def test_read_package_detects_content_that_does_not_match_the_manifest(
    tmp_path: Path,
) -> None:
    """包内内容被改过: 要求校验哈希时拒绝, 只做体检时放行."""
    destination = _write(tmp_path).path
    _rewrite_zip(destination, ("branches/n1/loc-0/a.txt", b"tampered\n"))

    with pytest.raises(PackageError):
        fmt.read_package(destination, verify_hashes=True)

    contents = fmt.read_package(destination)
    assert contents.entries[0].size == len(b"alpha\n")


@pytest.mark.blocker
def test_read_package_rejects_a_member_that_escapes_the_package_root(
    tmp_path: Path,
) -> None:
    """包内夹带 ``../escape.txt`` 时必须拒绝(清单里没有它也一样)."""
    destination = _write(tmp_path).path
    _rewrite_zip(destination, ("../escape.txt", b"boom"))

    with pytest.raises(PackageError):
        fmt.read_package(destination, verify_hashes=True)


@pytest.mark.blocker
def test_read_package_rejects_a_traversal_path_in_the_manifest(tmp_path: Path) -> None:
    """清单自己写了一条越界路径: 解析清单时就拒绝."""
    destination = tmp_path / "bad.archive.zip"
    manifest = _manifest_json(
        entries=[{"path": "../escape.txt", "size": 0, "sha256": "0" * 64}]
    )
    _write_raw_zip(destination, {fmt.MANIFEST_NAME: manifest, fmt.CONFIG_NAME: b"{}"})

    with pytest.raises(PackageError):
        fmt.read_package(destination)


@pytest.mark.blocker
def test_read_package_rejects_a_symlink_entry(tmp_path: Path) -> None:
    """符号链接条目一律拒绝: 不跟随链接才能保证只在目标目录里写普通文件."""
    destination = tmp_path / "link.archive.zip"
    info = zipfile.ZipInfo("branches/n1/loc-0/link")
    info.external_attr = 0o120777 << 16  # S_IFLNK | 0777
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr(fmt.CONFIG_NAME, b"{}")
        archive.writestr(
            fmt.MANIFEST_NAME,
            _manifest_json(
                entries=[
                    {
                        "path": "branches/n1/loc-0/link",
                        "size": 0,
                        "sha256": "0" * 64,
                    }
                ]
            ),
        )
        archive.writestr(info, b"/etc/passwd")

    with pytest.raises(PackageError):
        fmt.read_package(destination)


def test_read_package_enforces_the_entry_count_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """条目数超过上限的包直接拒绝(解压炸弹的第一道闸)."""
    destination = _write(tmp_path).path
    monkeypatch.setattr(fmt, "MAX_ENTRIES", 1)

    with pytest.raises(PackageError):
        fmt.read_package(destination)


def test_read_package_enforces_the_entry_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单文件展开超过上限的包直接拒绝."""
    destination = _write(tmp_path).path
    monkeypatch.setattr(fmt, "MAX_ENTRY_BYTES", 1)

    with pytest.raises(PackageError):
        fmt.read_package(destination)


def test_read_package_enforces_the_total_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """总展开量超过上限的包直接拒绝."""
    destination = _write(tmp_path).path
    monkeypatch.setattr(fmt, "MAX_TOTAL_BYTES", 4)

    with pytest.raises(PackageError):
        fmt.read_package(destination)


def test_read_package_enforces_the_compression_ratio_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """压缩比异常高的条目视为解压炸弹."""
    destination = tmp_path / "bomb.archive.zip"
    payload = b"0" * 100_000
    _write_raw_zip(
        destination,
        {
            fmt.CONFIG_NAME: b"{}",
            fmt.MANIFEST_NAME: _manifest_json(entries=[]),
            "branches/n1/blob.bin": payload,
        },
    )
    monkeypatch.setattr(fmt, "MAX_COMPRESSION_RATIO", 1.0)

    with pytest.raises(PackageError):
        fmt.read_package(destination)


@pytest.mark.parametrize(
    "entries",
    [
        {"config.json": b"{}"},  # 缺清单
        {"manifest.json": b"{ not json"},  # 清单不是 JSON
        {"manifest.json": b"[]", "config.json": b"{}"},  # 清单不是对象
        {"manifest.json": b"{}", "config.json": b"{}"},  # 没有格式标识
    ],
)
def test_read_package_rejects_malformed_packages(
    tmp_path: Path, entries: dict[str, bytes]
) -> None:
    """缺清单 / 不是 JSON / 不是对象 / 没有格式标识, 一律拒绝而不是猜."""
    destination = tmp_path / "broken.archive.zip"
    _write_raw_zip(destination, entries)

    with pytest.raises(PackageError):
        fmt.read_package(destination)


def test_read_package_rejects_an_unknown_format_version(tmp_path: Path) -> None:
    """格式版本不认识时拒绝(不按当前结构硬解)."""
    destination = tmp_path / "future.archive.zip"
    _write_raw_zip(
        destination,
        {
            fmt.CONFIG_NAME: b"{}",
            fmt.MANIFEST_NAME: _manifest_json(version=fmt.EXPORT_FORMAT_VERSION + 1),
        },
    )

    with pytest.raises(PackageError):
        fmt.read_package(destination)


def test_read_package_rejects_a_missing_or_broken_file(tmp_path: Path) -> None:
    """路径不存在、以及根本不是压缩包的文件."""
    missing = tmp_path / "nope.zip"
    with pytest.raises(PackageError):
        fmt.read_package(missing)

    text = tmp_path / "text.zip"
    text.write_text("我不是压缩包", encoding="utf-8")
    with pytest.raises(PackageError):
        fmt.read_package(text)


def test_read_package_rejects_an_entry_the_manifest_does_not_declare(
    tmp_path: Path,
) -> None:
    """包内多出清单没声明的条目: 结构不一致, 拒绝."""
    destination = _write(tmp_path).path
    _rewrite_zip(destination, ("branches/n1/loc-0/extra.txt", b"extra"))

    with pytest.raises(PackageError):
        fmt.read_package(destination)


def test_extract_package_writes_the_members_under_the_target(tmp_path: Path) -> None:
    """解包把内容原样写到目标目录之下, 并返回实际写出的包内路径."""
    contents = fmt.read_package(_write(tmp_path).path, verify_hashes=True)
    target = tmp_path / "staging"
    target.mkdir()

    written = fmt.extract_package(contents, target)

    assert written == (
        "branches/n1/loc-0/a.txt",
        "timeline/n2/loc-0/nested/中文.bin",
    )
    assert (target / "branches/n1/loc-0/a.txt").read_bytes() == b"alpha\n"
    assert (
        target / "timeline/n2/loc-0/nested/中文.bin"
    ).read_bytes() == b"\x00\x01" * 10


def test_extract_package_only_writes_the_selected_members(tmp_path: Path) -> None:
    """只解出选定的条目(批量导入与按游戏挑选用)."""
    contents = fmt.read_package(_write(tmp_path).path)
    target = tmp_path / "staging"
    target.mkdir()

    written = fmt.extract_package(contents, target, members=["branches/n1/loc-0/a.txt"])

    assert written == ("branches/n1/loc-0/a.txt",)
    assert (target / "branches/n1/loc-0/a.txt").is_file()
    assert not (target / "timeline").exists()


def test_extract_package_refuses_to_overwrite(tmp_path: Path) -> None:
    """目标已存在同名文件时拒绝(解包目标是新建的暂存目录, 撞名说明有问题)."""
    contents = fmt.read_package(_write(tmp_path).path)
    target = tmp_path / "staging"
    (target / "branches/n1/loc-0").mkdir(parents=True)
    (target / "branches/n1/loc-0/a.txt").write_text("已有", encoding="utf-8")

    with pytest.raises(PackageError):
        fmt.extract_package(contents, target, members=["branches/n1/loc-0/a.txt"])

    assert (target / "branches/n1/loc-0/a.txt").read_text(encoding="utf-8") == "已有"


def test_extract_package_removes_a_member_that_fails_verification(
    tmp_path: Path,
) -> None:
    """哈希不符时不留半成品: 目标文件被删掉, 而不是留下一份坏内容."""
    destination = _write(tmp_path).path
    _rewrite_zip(destination, ("branches/n1/loc-0/a.txt", b"tampered\n"))
    contents = fmt.read_package(destination)
    target = tmp_path / "staging"
    target.mkdir()

    with pytest.raises(PackageError):
        fmt.extract_package(contents, target, members=["branches/n1/loc-0/a.txt"])

    assert not (target / "branches/n1/loc-0/a.txt").exists()


def test_extract_package_honours_cancellation(tmp_path: Path) -> None:
    """取消探针为真时立刻中止."""
    contents = fmt.read_package(_write(tmp_path).path)
    target = tmp_path / "staging"
    target.mkdir()

    with pytest.raises(OperationCancelledError):
        fmt.extract_package(contents, target, cancelled=lambda: True)


def test_member_target_resolves_under_the_root(tmp_path: Path) -> None:
    """条目目标路径始终落在给定根目录之下, 越界形态直接拒绝."""
    assert fmt.member_target(tmp_path, "a/b.txt") == tmp_path / "a" / "b.txt"

    with pytest.raises(PackageError):
        fmt.member_target(tmp_path, "../a.txt")


def _written_members_of(path: Path) -> set[str]:
    """在真实 zip 上读一次"除清单与配置之外的成员名"(单独成函数, 免得嵌套 with)."""
    with zipfile.ZipFile(path) as archive:
        return fmt._written_members(archive)


def _write_zip_with_a_duplicate_member(path: Path, name: str) -> None:
    """写一个同一路径出现两次的包(清单只声明一次), 用于钉住重复路径的拒绝."""
    entries = [{"path": name, "size": 1, "sha256": "0" * 64}]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(fmt.CONFIG_NAME, b"{}")
        archive.writestr(fmt.MANIFEST_NAME, _manifest_json(entries=entries))
        archive.writestr(name, b"a")
        archive.writestr(name, b"b")


# -- 配置数组的取值 ---------------------------------------------------------


def test_config_list_returns_empty_for_a_missing_key(tmp_path: Path) -> None:
    """配置里没有这个键时返回空列表(旧包缺字段也要能读)."""
    contents = fmt.read_package(_write(tmp_path).path)

    assert contents.config_list("schedule") == []


def test_config_list_rejects_values_that_are_not_object_arrays(tmp_path: Path) -> None:
    """配置里的数组必须是对象数组: 不是数组、元素不是对象, 都直接拒绝."""
    (tmp_path / "plain").mkdir()
    (tmp_path / "items").mkdir()
    plain = fmt.read_package(
        _write(tmp_path / "plain", config={"backups": "不是数组"}).path
    )
    items = fmt.read_package(_write(tmp_path / "items", config={"backups": [1]}).path)

    with pytest.raises(PackageError, match="backups"):
        plain.config_list("backups")
    with pytest.raises(PackageError, match="backups"):
        items.config_list("backups")


# -- 写侧守卫 ---------------------------------------------------------------


def test_write_package_rejects_a_source_that_is_a_directory(tmp_path: Path) -> None:
    """来源是目录时拒绝: 包里只装普通文件, 不把一整个目录递归装进去."""
    text, _binary = _sources(tmp_path)
    members = [
        fmt.PackageFile("branches/n1/a.txt", text),
        fmt.PackageFile("branches/n1/dir", tmp_path / "nested"),
    ]

    with pytest.raises(PackageError, match="不是普通文件"):
        _write(tmp_path, members=members)


@pytest.mark.blocker
def test_write_package_refuses_a_source_that_reports_as_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """来源是符号链接时拒绝(本机无权建链接时用替身钉住这条守卫)."""
    link = tmp_path / "link.txt"
    link.write_bytes(b"secret\n")
    real = Path.is_symlink

    def looks_like_a_link(self: Path) -> bool:
        return True if self == link else real(self)

    monkeypatch.setattr(Path, "is_symlink", looks_like_a_link)

    with pytest.raises(PackageError, match="符号链接"):
        _write(tmp_path, members=[fmt.PackageFile("branches/n1/link.txt", link)])


def test_write_package_enforces_the_total_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """导出内容总大小超过上限时不写包(上限是拒绝解压炸弹的另一半)."""
    monkeypatch.setattr(fmt, "MAX_TOTAL_BYTES", 1)

    with pytest.raises(PackageError, match="总大小超过上限"):
        _write(tmp_path)


def test_write_package_enforces_the_entry_count_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """导出内容条数超过上限时不写包."""
    monkeypatch.setattr(fmt, "MAX_ENTRIES", 1)

    with pytest.raises(PackageError, match="条数超过上限"):
        _write(tmp_path)


# -- 回读校验 ---------------------------------------------------------------


def test_verify_written_rejects_a_member_the_package_does_not_have(
    tmp_path: Path,
) -> None:
    """回读时磁盘上的条目少于清单: 报错而不是把缺内容的包交出去."""
    summary = _write(tmp_path)

    with pytest.raises(PackageError, match="条目与清单不一致"):
        fmt._verify_written(summary.path, entries=summary.entries[:1], cancelled=None)


def test_written_members_requires_the_manifest_and_the_config(tmp_path: Path) -> None:
    """回读时缺清单或配置就拒绝: 少了它们这份包称不上写成功."""
    destination = tmp_path / "incomplete.zip"
    _write_raw_zip(destination, {"branches/n1/a.txt": b"alpha"})

    with pytest.raises(PackageError, match="缺少"):
        _written_members_of(destination)


def test_write_package_honours_cancellation_during_readback(tmp_path: Path) -> None:
    """回读校验阶段也要认取消: 取消后目标路径上不留文件."""
    calls = 0

    def probe() -> bool:
        nonlocal calls
        calls += 1
        return calls > 3

    with pytest.raises(OperationCancelledError):
        _write(tmp_path, cancelled=probe)

    assert not (tmp_path / "Demo.archive.zip").exists()
    assert not list(tmp_path.glob(f"*{fmt._PARTIAL_SUFFIX}*"))


def test_verify_written_rejects_a_size_mismatch(tmp_path: Path) -> None:
    """回读时条目大小与清单不符: 只信磁盘上真正写出来的字节."""
    summary = _write(tmp_path)
    first = summary.entries[0]
    wrong = fmt.PackageEntry(path=first.path, size=first.size + 1, sha256=first.sha256)

    with pytest.raises(PackageError, match="大小不一致"):
        fmt._verify_written(
            summary.path, entries=(wrong, *summary.entries[1:]), cancelled=None
        )


def test_verify_written_rejects_a_hash_mismatch(tmp_path: Path) -> None:
    """回读时条目哈希与清单不符: 压缩器或磁盘出问题都在这里暴露."""
    summary = _write(tmp_path)
    first = summary.entries[0]
    wrong = fmt.PackageEntry(path=first.path, size=first.size, sha256="0" * 64)

    with pytest.raises(PackageError, match="哈希不一致"):
        fmt._verify_written(
            summary.path, entries=(wrong, *summary.entries[1:]), cancelled=None
        )


def test_read_package_kind_reports_a_missing_file(tmp_path: Path) -> None:
    """分类读取前先看文件在不在(与 read_package 同一套可读报错)."""
    with pytest.raises(PackageError, match="不存在"):
        fmt.read_package_kind(tmp_path / "nope.zip")


@pytest.mark.parametrize(
    "entries",
    [
        "not a list",
        [1],
        [{"path": "branches/n1/a.txt", "size": -1, "sha256": "0" * 64}],
        [{"path": "branches/n1/a.txt", "size": 0, "sha256": "zz"}],
    ],
)
def test_read_package_rejects_a_broken_entry_table(
    tmp_path: Path, entries: object
) -> None:
    """条目表不是数组 / 条目不是对象 / size 非法 / sha256 非法: 一律拒绝."""
    destination = tmp_path / "broken.archive.zip"
    _write_raw_zip(
        destination,
        {
            fmt.CONFIG_NAME: b"{}",
            fmt.MANIFEST_NAME: _manifest_json(entries=entries),
        },
    )

    with pytest.raises(PackageError):
        fmt.read_package(destination)


def test_read_package_rejects_a_declared_entry_that_is_missing(tmp_path: Path) -> None:
    """清单声明了包内没有的条目: 拒绝(否则导入时会少文件)."""
    destination = tmp_path / "short.archive.zip"
    _write_raw_zip(
        destination,
        {
            fmt.CONFIG_NAME: b"{}",
            fmt.MANIFEST_NAME: _manifest_json(
                entries=[{"path": "branches/n1/a.txt", "size": 1, "sha256": "0" * 64}]
            ),
        },
    )

    with pytest.raises(PackageError, match="缺少清单声明"):
        fmt.read_package(destination)


def test_read_package_ignores_directory_entries(tmp_path: Path) -> None:
    """包里的目录项不算内容(只有文件条目才参与清单比对)."""
    destination = _write(tmp_path).path
    _rewrite_zip(destination, ("branches/n1/loc-0/empty/", b""))

    contents = fmt.read_package(destination, verify_hashes=True)

    assert [entry.path for entry in contents.entries] == [
        "branches/n1/loc-0/a.txt",
        "timeline/n2/loc-0/nested/中文.bin",
    ]


@pytest.mark.filterwarnings("ignore:Duplicate name:UserWarning")
def test_read_package_rejects_the_same_path_twice(tmp_path: Path) -> None:
    """包内同一路径出现两次会互相覆盖, 必须拒绝."""
    destination = tmp_path / "dup.archive.zip"
    _write_zip_with_a_duplicate_member(destination, "branches/n1/a.txt")

    with pytest.raises(PackageError, match="同一路径出现多次"):
        fmt.read_package(destination)


def test_extract_package_enforces_the_total_size_limit_while_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按实际写出的字节复核总量上限(不信任 zip 头里的声明)."""
    contents = fmt.read_package(_write(tmp_path).path)
    target = tmp_path / "staging"
    target.mkdir()
    monkeypatch.setattr(fmt, "MAX_TOTAL_BYTES", 1)

    with pytest.raises(PackageError, match="总大小超过上限"):
        fmt.extract_package(contents, target)


def test_extract_package_enforces_the_entry_size_limit_while_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按实际读到的字节复核单条目上限."""
    contents = fmt.read_package(_write(tmp_path).path)
    target = tmp_path / "staging"
    target.mkdir()
    monkeypatch.setattr(fmt, "MAX_ENTRY_BYTES", 1)

    with pytest.raises(PackageError, match="条目超过上限"):
        fmt.extract_package(contents, target)
