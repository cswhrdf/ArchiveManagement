"""外部 IO 的失败路径: 控制台三条路径 + 图片/译名缓存写不进 + 商城接口失败.

这几处的共同约定是**只在边界上降级**: 缓存写不进去、接口取不到、数据库打不开, 都要变成
一句可读的原因(或一个明确的空值), 绝不把裸 ``OSError``/网络异常抛给上层。

命令行这三条走真实的 ``app.run``: 判据是退出码与打印出来的那句话。
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from PIL import Image

import archive_management.app as app_module
from archive_management.domain import PlatformGame
from archive_management.services.artwork import ArtworkCache, _cache_local
from archive_management.services.game_names import HttpNameFetcher, NameCache
from archive_management.services.platform_adapters import SteamAdapter
from archive_management.services.platform_scan import default_roots

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率(外部 IO 失败)"),
    pytest.mark.story("接口与缓存失败只在边界上降级"),
    pytest.mark.layer("unit"),
]

_APP_ID = "937310"
_PAYLOAD: dict[str, Any] = {
    _APP_ID: {
        "success": True,
        "data": {"type": "game", "name": "无尽塔防 2", "steam_appid": 937310},
    }
}


def _run_cli(argv: list[str]) -> tuple[int, str]:
    """跑一次命令行入口并收集标准输出(与 ``test_cli.py`` 同一做法)."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = app_module.run(argv)
    return code, buffer.getvalue()


def _png_bytes() -> bytes:
    """一张最小可用的 PNG(缓存前要真的能嗅探出图片类型)."""
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------- 命令行


def test_an_unknown_command_is_rejected_by_the_parser(tmp_path: Path) -> None:
    """未知子命令由 argparse 拒掉(退出码 2), 不能静默当成 init 跑."""
    with pytest.raises(SystemExit) as info:
        app_module.run(["nope", "--root", str(tmp_path)])

    assert info.value.code == 2


def test_init_reports_a_config_that_had_to_be_reset(tmp_path: Path) -> None:
    """配置内容非法被还原时要明说, 并指出原文件留在哪 —— 不能悄悄当没发生过."""
    config_path = tmp_path / "config" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{ 这不是 json", encoding="utf-8")

    code, output = _run_cli(["init", "--root", str(tmp_path)])

    assert code == 0
    assert "配置还原" in output


def test_doctor_reports_a_database_it_cannot_open(tmp_path: Path) -> None:
    """数据库文件的位置被目录占着: doctor 打印"不可用"并给原因, 仍然正常退出."""
    blocked = tmp_path / "data" / "archive-management.db"
    blocked.mkdir(parents=True)

    code, output = _run_cli(["doctor", "--root", str(tmp_path)])

    assert code == 0
    assert "数据库 : 不可用" in output


# --------------------------------------------------------------- 图片缓存


class _RefusingCache:
    """写入总失败的缓存替身(磁盘满/没权限)."""

    def store(self, *args: object, **kwargs: object) -> Path:
        """永远拒绝写入."""
        raise OSError("磁盘满")


def test_a_local_image_that_cannot_be_cached_falls_back_to_the_local_file(
    tmp_path: Path,
) -> None:
    """平台本地图片写不进缓存: 直接用本地文件(界面照样有图, 只是没进缓存)."""
    local = tmp_path / "cover.png"
    local.write_bytes(_png_bytes())
    game = PlatformGame(name="Demo", platform="steam", game_id="730")

    result = _cache_local(
        game,
        "cover",
        local,
        cast("ArtworkCache", _RefusingCache()),
        version="1",
        max_bytes=1024,
    )

    assert result.path == local
    assert result.source == "local"
    assert result.reason == "磁盘满"


# --------------------------------------------------------------- 商城接口与译名缓存


class _StubResponse:
    """最小可用的商城响应替身(可让它"不是 JSON")."""

    def __init__(
        self, content: bytes, *, payload: object = None, json_error: bool = False
    ) -> None:
        """记住响应体与 ``json()`` 的行为."""
        self.content = content
        self._payload = payload
        self._json_error = json_error

    def raise_for_status(self) -> None:
        """替身永远成功."""
        return

    def json(self) -> object:
        """返回预设对象, 或者按预设抛 ``ValueError``(不是 JSON)."""
        if self._json_error:
            raise ValueError("不是 JSON")
        return self._payload


class _StubClient:
    """只实现 ``get`` 的 httpx 客户端替身(可按需抛网络错)."""

    def __init__(
        self, *, response: object = None, error: Exception | None = None
    ) -> None:
        """二者取一: 返回一个响应, 或者抛一个异常."""
        self._response = response
        self._error = error

    def get(self, url: str, **kwargs: object) -> object:
        """记录被调用, 按预设返回或抛错."""
        del url, kwargs
        if self._error is not None:
            raise self._error
        return self._response


def test_an_oversized_store_response_is_ignored() -> None:
    """商城响应超过体积上限: 当"没取到"处理, 不解析也不缓存."""
    fetcher = HttpNameFetcher(
        client=cast(
            "httpx.Client",
            _StubClient(response=_StubResponse(b"x" * 50, payload=_PAYLOAD)),
        )
    )

    assert (
        fetcher.fetch(_APP_ID, language="schinese", timeout=1.0, max_bytes=10) is None
    )


def test_a_failing_store_request_is_ignored() -> None:
    """商城请求抛网络错(超时/断网): 返回 None, 不把异常抛给调用方."""
    fetcher = HttpNameFetcher(
        client=cast("httpx.Client", _StubClient(error=httpx.HTTPError("网络断了")))
    )

    assert (
        fetcher.fetch(_APP_ID, language="schinese", timeout=1.0, max_bytes=1000) is None
    )


def test_a_store_response_that_is_not_json_is_ignored() -> None:
    """响应不是 JSON: 与请求失败同一条退路(返回 None)."""
    fetcher = HttpNameFetcher(
        client=cast(
            "httpx.Client",
            _StubClient(response=_StubResponse(b"not json", json_error=True)),
        )
    )

    assert (
        fetcher.fetch(_APP_ID, language="schinese", timeout=1.0, max_bytes=1000) is None
    )


def test_a_name_cache_that_cannot_be_written_is_silently_skipped(
    tmp_path: Path,
) -> None:
    """译名缓存写不进去(路径被文件占着)只记日志: 译名本身照常返回, 不因缓存失败而失败."""
    blocked = tmp_path / "cache"
    blocked.write_text("占位", encoding="utf-8")
    cache = NameCache(blocked / "names.json")

    cache.put(_APP_ID, "schinese", "无尽塔防 2")  # 不抛

    assert not (blocked / "names.json").exists()


# --------------------------------------------------------------- 适配器


def test_a_steam_game_without_a_local_icon_hash_only_gets_the_cover() -> None:
    """本机没有该游戏的图标哈希时只给封面引用: 后台裁封面兜底, 不去猜图标地址."""
    adapter = SteamAdapter(default_roots(platform="windows", env={}), icons={})
    game = PlatformGame(name="Demo", platform="steam", game_id="730")

    refs = adapter.artwork_refs(game)

    assert len(refs) == 1, "只该有封面这一条引用"


def test_a_non_steam_game_has_no_artwork_refs() -> None:
    """非 Steam 的游戏没有 CDN 引用: 界面走别的兜底, 一次都不去问 CDN.

    只有这一半能造出来: ``PlatformGame.game_id`` 由模型保证非空(实测: 传空串会被 pydantic
    拒掉), 所以源码里 ``or not game.game_id`` 那半是防守型的, 已按 ``no branch`` 标掉。
    这一支在碰到 ``self`` 之前就返回了, 所以不需要真的构造适配器。
    """
    adapter = object.__new__(SteamAdapter)

    refs = SteamAdapter.artwork_refs(
        adapter, PlatformGame(name="Demo", platform="epic", game_id="1")
    )

    assert refs == ()
