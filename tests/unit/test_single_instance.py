"""单实例闸门的单元测试: 同一个数据目录只允许一个界面进程.

这里量的是**真锁**: 每个用例都在 ``tmp_path`` 里真建文件、真上操作系统级锁 —— 换替身
就测不出"第二个句柄到底能不能拿", 而那正是这个模块存在的全部理由。

退出码与"打包版才生效"由命令行那头把关(``tests/unit/test_cli.py`` 的
``test_a_second_packaged_instance_*`` 一组), 这里只管锁本身。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.services import single_instance

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("基础工程"),
    pytest.mark.feature("单实例闸门"),
    pytest.mark.story("重复启动的拦截"),
    # 真实文件系统 + 真实操作系统级锁, 按"接了什么"判层.
    pytest.mark.layer("integration"),
]


def _lock(tmp_path: Path) -> single_instance.InstanceLock:
    """按默认落点在临时目录里造一把锁."""
    return single_instance.InstanceLock(tmp_path / single_instance.LOCK_NAME)


def test_the_second_holder_is_refused_until_the_first_releases(tmp_path: Path) -> None:
    """锁在手上时第二个实例拿不到; 放开之后立刻能拿(不是"一次就锁死")."""
    first = _lock(tmp_path)
    assert first.acquire() is True
    assert single_instance.InstanceLock(first.path).acquire() is False

    first.release()

    with single_instance.InstanceLock(first.path) as locked:
        assert locked is True


def test_acquire_is_idempotent_and_release_can_be_repeated(tmp_path: Path) -> None:
    """同一个对象上重复 acquire 保持"已持有"; 重复 release 不抛异常(收尾路径会被走多次)."""
    lock = _lock(tmp_path)
    assert lock.acquire() is True
    assert lock.acquire() is True, "重复 acquire 不该把自己顶掉(否则第二次会以为被占用)"

    lock.release()
    lock.release()
    assert single_instance.InstanceLock(lock.path).acquire() is True


def test_missing_directories_are_created_and_the_stamp_names_the_holder(
    tmp_path: Path,
) -> None:
    """锁文件(连同缺失的目录)自己建出来, 并写下"谁占着"供人工核对."""
    lock = single_instance.InstanceLock(tmp_path / "data" / "sub" / "instance.lock")
    assert lock.acquire() is True
    try:
        assert lock.path.is_file()
        # 站在"第二个实例"那一面读: 锁被占着时也要读得到是谁占着(否则提示只能说"未知").
        stamp = single_instance.InstanceLock(lock.path).holder()
    finally:
        lock.release()

    assert f"pid={os.getpid()}" in stamp
    assert "version=" in stamp


def test_holder_is_empty_when_the_file_does_not_exist(tmp_path: Path) -> None:
    """锁文件还没建过时读到的就是空串(提示文案里会回落成"未知")."""
    assert _lock(tmp_path / "nowhere").holder() == ""


def test_the_context_manager_hands_back_the_result_and_releases(tmp_path: Path) -> None:
    """``with`` 里拿到的是"锁到手了没有"; 退出 ``with`` 一定放开."""
    path = tmp_path / single_instance.LOCK_NAME
    with single_instance.InstanceLock(path) as locked:
        assert locked is True
        assert single_instance.InstanceLock(path).acquire() is False

    assert single_instance.InstanceLock(path).acquire() is True


def test_the_lock_file_lives_in_the_data_directory_so_portable_roots_do_not_collide(
    tmp_path: Path,
) -> None:
    """锁落在数据目录: ``--root`` 便携实例各用各的锁, 不会互相挡."""
    portable = ApplicationPaths.default(override_root=tmp_path / "one")
    other = ApplicationPaths.default(override_root=tmp_path / "two")

    assert single_instance.lock_path(portable) == (
        portable.data_dir / single_instance.LOCK_NAME
    )
    assert single_instance.lock_path(portable) != single_instance.lock_path(other)


def test_the_foreground_request_is_read_then_cleared_exactly_once(
    tmp_path: Path,
) -> None:
    """第二个实例写、已经开着的那个读: 读**不清**(先抬窗口)、抬完再清(回执).

    文件留着的话, 每一轮轮询(100ms)都会把窗口抬起来一次 —— 用户手动切到别的软件后又被
    抢回去; 而清得太早(读的时候顺手清)则第二个实例看不到"处理完了", 它会以为对方没响应。
    """
    paths = ApplicationPaths.default(override_root=tmp_path)
    path = single_instance.focus_request_path(paths)
    assert path.parent == paths.data_dir
    assert single_instance.read_focus_request(path) == "", "还没人请求时读到的就是空"

    token = single_instance.request_focus(paths)

    assert f"pid={os.getpid()}" in token
    assert path.is_file()
    assert single_instance.read_focus_request(path) == token
    assert path.is_file(), "读不该把请求弄丢(回执要等抬完窗口再发)"
    single_instance.clear_focus_request(path)
    assert not path.exists(), "清完就不该还在"
    assert single_instance.read_focus_request(path) == "", "同一个请求不该抬第二次窗口"
    single_instance.clear_focus_request(path)  # 重复清不报错


def test_the_second_instance_waits_for_the_receipt(tmp_path: Path) -> None:
    """第二个实例等"文件被清掉"这个回执, 超时就返回 False(不当错误)."""
    path = tmp_path / single_instance.FOCUS_NAME
    assert single_instance.wait_until_handled(path, timeout=0) is True, "本来就没请求"

    path.write_text("pid=1 at=now", encoding="utf-8")
    assert single_instance.wait_until_handled(path, timeout=0.05) is False, (
        "没人处理就超时"
    )

    single_instance.clear_focus_request(path)
    assert single_instance.wait_until_handled(path, timeout=0.05) is True


def test_the_holder_pid_is_read_out_of_the_stamp() -> None:
    """锁文件里那句"谁占着"要能取出 pid(交前台权限时用它)."""
    assert single_instance.holder_pid("pid=4321 version=0.0.1") == 4321
    assert single_instance.holder_pid("") is None
    assert single_instance.holder_pid("pid=abc") is None
    assert single_instance.holder_pid("version=0.0.1") is None


def test_handing_over_the_foreground_needs_a_pid_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """交前台权限那一步的两种失败都不抛: 取不到 pid 是空操作, 没有权限只返回 False.

    第二步(真的去调 ``user32``)只在 Windows 上有用, 这里把加载器换成会抛 ``OSError`` 的替身
    —— 两个平台上都能跑到那条"没权限"的分支。
    """
    import ctypes

    assert single_instance.hand_over_foreground(None) is False

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("没有权限")

    monkeypatch.setattr(ctypes, single_instance._WIN_DLL_ATTR, _boom, raising=False)
    assert single_instance._allow_windows_foreground(4321) is False


def test_a_request_that_cannot_be_written_still_lets_the_caller_exit(
    tmp_path: Path,
) -> None:
    """请求写不进去或读不出内容时都只返回空串, 不抛异常.

    这一步失败只该表现为"窗口没跳到前台", 不该把第二个实例的退出路径搞挂(它还要退 3);
    反过来, 文件里是空的时也不该当成"有人请求过"。
    """
    paths = ApplicationPaths.default(override_root=tmp_path)
    blocked = single_instance.focus_request_path(paths)
    blocked.parent.mkdir(parents=True)
    blocked.mkdir()  # 用目录占住这个名字: 写文件必然失败

    token = single_instance.request_focus(paths)

    assert f"pid={os.getpid()}" in token, "写不进去也照旧返回令牌(供日志记一笔)"
    assert single_instance.read_focus_request(blocked) == "", (
        "读不成文件的请求就是没有请求"
    )

    blocked.rmdir()
    blocked.write_text("   \n", encoding="utf-8")
    assert single_instance.read_focus_request(blocked) == "", "空文件不算请求"


def test_the_escape_hatch_reads_the_usual_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逃生门只认明确写出来的"开": 空值/写错都当没设(闸门保持关闭)."""
    monkeypatch.delenv(single_instance.ALLOW_ENV, raising=False)
    assert single_instance.multi_instance_allowed() is False

    for value in ("1", "true", "TRUE", " yes ", "on"):
        monkeypatch.setenv(single_instance.ALLOW_ENV, value)
        assert single_instance.multi_instance_allowed() is True, value

    for value in ("", "0", "no", "off", "maybe"):
        monkeypatch.setenv(single_instance.ALLOW_ENV, value)
        assert single_instance.multi_instance_allowed() is False, value
