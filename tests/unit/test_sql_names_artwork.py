"""
名称缓存与美术资源: 候选名预取/语言切换重用、封面自定义与图标派生、下载重入与缓存失败降级。拆自 test_sql_backend.py(见 docs/test-refactor-plan.md S9)。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import archive_management.ui.sql_backend as sql_mod
import helpers
from archive_management.domain import (
    ArtworkRef,
    GameCandidate,
    PlatformGame,
)
from archive_management.exceptions import (
    ArtworkImageError,
)
from archive_management.i18n import set_locale
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    CandidateRepository,
    GameRepository,
)
from archive_management.services.artwork import (
    ICON_SIZE,
    ICON_VERSION,
    NOT_AN_IMAGE,
    STEAM_COVER_ASSET,
    artwork_cache_at,
    steam_icon,
)
from archive_management.services.game_names import name_cache_at
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.sql_backend import SqlArchiveService

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).
from sql_support import (
    _FakeAdapter,
    _FakeSaveSource,
    _service,
    _steam_service,
)

pytestmark = [
    pytest.mark.backend,
    pytest.mark.database,
    pytest.mark.critical,
    pytest.mark.epic("数据持久化"),
    pytest.mark.feature("真实 SQLite 后端"),
    pytest.mark.story("备份恢复与删除数据流"),
    # 真实 SQLite + 文件系统 + 调度后端, 按层定义归入 integration.
    pytest.mark.layer("integration"),
]


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


_ICON_HASH = "b2f863a4c63bc1c5667a8a7e3e9355ef260ce6d2"


class _StubNames:
    """按 AppID 返回固定译名的替身(不联网)."""

    def __init__(self, names: dict[str, str]) -> None:
        """绑定 AppID 到译名的映射."""
        self._names = names

    def fetch(
        self, app_id: str, *, language: str, timeout: float, max_bytes: int
    ) -> str | None:
        """返回预设译名(没有映射时返回 None, 走原名回落)."""
        del language, timeout, max_bytes
        return self._names.get(app_id)


def test_artwork_path_reads_only_the_cache(tmp_path: Path) -> None:
    """封面/图标接口只查缓存: 预置缓存图能拿到路径, 未配置缓存目录时返回空."""
    cache = artwork_cache_at(tmp_path / "cache")
    cover = cache.store(
        "steam", "730", "cover", STEAM_COVER_ASSET, content=_PNG, extension="png"
    )
    icon = cache.store(
        "steam", "730", "icon", ICON_VERSION, content=_PNG, extension="png"
    )
    service, game_id, _database = _steam_service(tmp_path, cache_dir=tmp_path / "cache")
    plain, plain_id, _other = _steam_service(tmp_path / "plain")

    assert service.artwork_path(game_id, "cover") == str(cover)
    assert service.artwork_path(game_id, "icon") == str(icon)
    assert plain.artwork_path(plain_id, "cover") == ""


def _picked_cover(tmp_path: Path) -> Path:
    """写一张真 PNG(扮演"用户挑的那张封面")."""
    from PIL import Image

    path = tmp_path / "picked.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 360), "green").save(path, format="PNG")
    return path


def test_a_custom_cover_wins_and_clearing_it_restores_the_builtin(
    tmp_path: Path,
) -> None:
    """用户自己指定的图优先于缓存/平台图; 恢复默认之后回到内置图.

    两套图放在不同目录(自定义图在 ``data_dir/artwork``, 平台图在 ``cache_dir``), 所以
    清理缓存不会把用户的选择带走 —— 这也是这个功能必须单独一份存储的原因。
    """
    cache = artwork_cache_at(tmp_path / "cache")
    builtin = cache.store(
        "steam", "730", "cover", STEAM_COVER_ASSET, content=_PNG, extension="png"
    )
    service, game_id, _database = _steam_service(tmp_path, cache_dir=tmp_path / "cache")
    picked = _picked_cover(tmp_path)

    assert service.artwork_path(game_id, "cover") == str(builtin)
    assert service.user_artwork_path(game_id, "cover") == ""

    stored = service.set_game_artwork(game_id, "cover", str(picked))

    assert Path(stored).is_file()
    assert service.user_artwork_path(game_id, "cover") == stored
    assert service.artwork_path(game_id, "cover") == stored, "用户指定的图要优先"

    assert service.clear_game_artwork(game_id, "cover") is True
    assert service.user_artwork_path(game_id, "cover") == ""
    assert service.artwork_path(game_id, "cover") == str(builtin), "回到内置图"
    assert service.clear_game_artwork(game_id, "cover") is False, "没图可清时返回假"


def test_a_custom_cover_works_for_a_game_without_platform_art(tmp_path: Path) -> None:
    """手动添加的游戏(平台给不出图)也能有封面 —— 这是这个功能最直接的动机."""
    service, game_id, _database = _steam_service(tmp_path)  # 无 cache_dir
    picked = _picked_cover(tmp_path)

    assert service.artwork_path(game_id, "cover") == "", "没有缓存时本来就是空"

    stored = service.set_game_artwork(game_id, "cover", str(picked))

    assert service.artwork_path(game_id, "cover") == stored


def test_a_broken_picked_file_is_refused_with_a_reason_code(tmp_path: Path) -> None:
    """用户选了个不是图片的文件: 带原因代码报错, 而且不留下半张图."""
    service, game_id, _database = _steam_service(tmp_path)
    junk = tmp_path / "not-an-image.txt"
    junk.write_text("这不是图片", encoding="utf-8")

    with pytest.raises(ArtworkImageError) as rejected:
        service.set_game_artwork(game_id, "cover", str(junk))

    assert rejected.value.code == NOT_AN_IMAGE
    assert service.user_artwork_path(game_id, "cover") == ""


def test_prefetch_names_localizes_only_the_names_it_wrote(tmp_path: Path) -> None:
    """译名只写到"名字还是程序写的"游戏上: 用户起的名字优先, 取不到就保留原名."""
    fetcher = _StubNames({"730": "无尽塔防 2", "1": "无名游戏"})
    service, game_id, database = _steam_service(tmp_path, name_fetcher=fetcher)
    service._localize_names(refresh=False)

    game = GameRepository(database).get(int(game_id))
    assert game is not None
    assert game.name == "无尽塔防 2"

    # 用户改过名(与首次录入的名称不同)的游戏不会再被译名覆盖.
    renamed = game.model_copy(update={"name": "我给它起的名字"})
    GameRepository(database).update(renamed)
    service._localize_names(refresh=False)

    stored = GameRepository(database).get(int(game_id))
    assert stored is not None
    assert stored.name == "我给它起的名字"


class _PerLanguageNames:
    """按语言返回译名的替身: 中文一份、英文一份, 并记下每一步问了什么(不联网)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def fetch(
        self, app_id: str, *, language: str, timeout: float, max_bytes: int
    ) -> str | None:
        """返回该语言下的译名, 同时记下这次询问."""
        del timeout, max_bytes
        self.calls.append((app_id, language))
        if app_id != "730":
            return None
        return {"schinese": "无尽塔防 2", "english": "Bloons TD 2"}.get(language)


def test_switching_language_follows_the_locale_and_reuses_the_cache(
    tmp_path: Path,
) -> None:
    """切换语言: 名字跟着换, 而该语言已取过的译名直接复用缓存(不再联网).

    缓存按 ``<AppID>:<语言>`` 分条, 所以"换语言"本来就不需要忽略缓存: 新语言没记录
    才联网。旧行为是 ``refresh=True`` 忽略缓存重取, 来回切一次就要多问一趟商店。
    """
    fetcher = _PerLanguageNames()
    service, game_id, database = _steam_service(
        tmp_path, cache_dir=tmp_path / "cache", name_fetcher=fetcher
    )
    games = GameRepository(database)

    set_locale("zh-CN")
    service._localize_names(refresh=False)
    first = games.get(int(game_id))
    assert first is not None
    assert first.name == "无尽塔防 2"
    assert first.localized_name == "无尽塔防 2"

    set_locale("en")
    service._localize_names(refresh=False)
    second = games.get(int(game_id))
    assert second is not None
    assert second.name == "Bloons TD 2", "切换语言后名字要跟着新语言走"
    assert second.original_name == "Demo", "录入时的原名始终保留"

    # 切回中文: 命中缓存, 一次都不该再问商店.
    set_locale("zh-CN")
    service._localize_names(refresh=False)
    third = games.get(int(game_id))
    assert third is not None
    assert third.name == "无尽塔防 2"
    assert [language for _app_id, language in fetcher.calls] == [
        "schinese",
        "english",
    ], "缓存命中还联网就说明切换语言把缓存忽略了"


def test_a_user_rename_survives_language_switches(tmp_path: Path) -> None:
    """用户改过的名字不会被译名覆盖: 切换语言、来回切都不动它."""
    fetcher = _PerLanguageNames()
    service, game_id, database = _steam_service(
        tmp_path, cache_dir=tmp_path / "cache", name_fetcher=fetcher
    )

    set_locale("zh-CN")
    service._localize_names(refresh=False)
    service.update_game(game_id, "我给它起的名字")

    for locale in ("en", "zh-CN"):
        set_locale(locale)
        service._localize_names(refresh=False)
        stored = GameRepository(database).get(int(game_id))
        assert stored is not None
        assert stored.name == "我给它起的名字"
        assert stored.localized_name == ""


def test_prefetch_names_keeps_the_detected_name_without_a_translation(
    tmp_path: Path,
) -> None:
    """该语言没有译文(或取不到)时保留探测到的原名, 不猜也不翻."""
    service, game_id, database = _steam_service(
        tmp_path, name_fetcher=_StubNames({"999": "别的游戏"})
    )

    service._localize_names(refresh=False)

    game = GameRepository(database).get(int(game_id))
    assert game is not None
    assert game.name == "Demo"


def test_prefetch_names_fills_candidate_names_without_touching_games(
    tmp_path: Path,
) -> None:
    """探测结果还不是游戏: 译名只写进缓存, 不会凭空多出一条游戏记录."""
    cache_dir = tmp_path / "cache"
    service, _game_id, database = _steam_service(
        tmp_path,
        cache_dir=cache_dir,
        name_fetcher=_StubNames({"1145360": "哈迪斯"}),
    )
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(
            name="Hades",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )

    service._localize_names(refresh=False)

    assert name_cache_at(cache_dir).get("1145360", "zh-CN") == "哈迪斯"
    assert [game.name for game in service.list_games()] == ["Demo"]


def test_delete_game_removes_its_artwork_cache(tmp_path: Path) -> None:
    """删游戏时把它的缓存图与**用户指定的图**一起删掉(都是这款游戏的派生数据)."""
    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cache.store(
        "steam", "730", "cover", STEAM_COVER_ASSET, content=_PNG, extension="png"
    )
    cache.store("steam", "730", "icon", ICON_VERSION, content=_PNG, extension="png")
    service, game_id, _database = _steam_service(tmp_path, cache_dir=cache_dir)
    custom = Path(
        service.set_game_artwork(game_id, "icon", str(_picked_cover(tmp_path)))
    )

    service.delete_game(game_id, str(tmp_path / "exports" / "steam.zip"))

    assert cache.lookup_any("steam", "730", "cover") is None
    assert cache.lookup_any("steam", "730", "icon") is None
    assert not custom.exists(), "游戏没了, 用户指定的那张图也不该留着"


def test_icon_is_derived_from_the_cover(tmp_path: Path) -> None:
    """封面兜底: 平台给不出官方图标时, 从封面裁出方形图标."""
    from PIL import Image

    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cover = tmp_path / "cover.png"
    Image.new("RGB", (300, 450), (10, 20, 30)).save(cover)
    cache.store(
        "steam",
        "730",
        "cover",
        STEAM_COVER_ASSET,
        content=cover.read_bytes(),
        extension="png",
    )
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None

    # 封面已有(不计入), 这一份是刚裁出来的图标.
    assert service._fetch_artwork(cache, referenced) == 1

    icon = service.artwork_path("1", "icon")
    assert icon != ""
    with Image.open(icon) as made:
        assert made.size == (ICON_SIZE, ICON_SIZE)


def test_official_icon_wins_over_the_cover_crop(tmp_path: Path) -> None:
    """有官方图标时用它: 缓存里的像素来自 ico, 而不是封面(两者颜色不同)."""
    from PIL import Image

    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cover = tmp_path / "cover.png"
    Image.new("RGB", (300, 450), (10, 20, 30)).save(cover)
    cache.store(
        "steam",
        "730",
        "cover",
        STEAM_COVER_ASSET,
        content=cover.read_bytes(),
        extension="png",
    )
    official = helpers.write_steam_icon(tmp_path, _ICON_HASH)
    service, _game_id, database = _steam_service(
        tmp_path,
        cache_dir=cache_dir,
        icon=steam_icon("730", _ICON_HASH, local_path=official),
    )
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None

    assert service._fetch_artwork(cache, referenced) == 1

    stored = cache.lookup_any("steam", "730", "icon")
    assert stored is not None
    # 文件名说明这份图是怎么来的: 官方图标用哈希, 且已经归一化成方形 PNG.
    assert stored.name == f"icon-{_ICON_HASH}.png"
    with Image.open(stored) as made:
        assert made.size == (ICON_SIZE, ICON_SIZE)
        # 官方图标带透明通道: 颜色来自 ico(红)而不是封面(深蓝), 且透明度保留.
        assert made.getpixel((ICON_SIZE // 2, ICON_SIZE // 2)) == (200, 30, 30, 255)
    # 界面拿到的是缓存里那份成品(而不是平台目录里的原始 .ico).
    assert service.artwork_path("1", "icon") == str(stored)


def test_a_cover_crop_icon_is_replaced_by_the_official_one(tmp_path: Path) -> None:
    """已有裁出来的封面图标时, 拿到官方图标要覆盖它(否则升级后仍是旧图)."""
    from PIL import Image

    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    crop = tmp_path / "crop.png"
    Image.new("RGB", (ICON_SIZE, ICON_SIZE), (10, 20, 30)).save(crop)
    cache.store(
        "steam", "730", "icon", ICON_VERSION, content=crop.read_bytes(), extension="png"
    )
    # 封面也预置好: 这样这一轮要补的只有图标(返回值只数图标那 1 份).
    cache.store(
        "steam",
        "730",
        "cover",
        STEAM_COVER_ASSET,
        content=crop.read_bytes(),
        extension="png",
    )
    official = helpers.write_steam_icon(tmp_path, _ICON_HASH)
    service, _game_id, database = _steam_service(
        tmp_path,
        cache_dir=cache_dir,
        icon=steam_icon("730", _ICON_HASH, local_path=official),
    )
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None

    assert service._fetch_artwork(cache, referenced) == 1

    # 只剩官方图标那一版: 旧的封面裁剪已按“同类型过期文件”清掉.
    assert cache.lookup("steam", "730", "icon", ICON_VERSION) is None
    stored = cache.lookup("steam", "730", "icon", _ICON_HASH)
    assert stored is not None
    with Image.open(stored) as made:
        assert made.getpixel((4, 4)) == (200, 30, 30, 255)


def _cached_service(tmp_path: Path, *, cache_dir: Path | None) -> SqlArchiveService:
    """构造带/不带缓存目录的后端: 译名与封面这两件事都挂在缓存目录上."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    return SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
        cache_dir=cache_dir,
    )


def test_prefetch_does_nothing_without_a_cache_dir(tmp_path: Path) -> None:
    """没有缓存目录时译名与封面探测都直接返回(测试与未配置应用路径的场景)."""
    service = _cached_service(tmp_path, cache_dir=None)
    summary = service.add_game("手填游戏")

    service.prefetch_names()
    service.prefetch_artwork()

    assert service.artwork_path(summary.game_id, "cover") == ""
    assert service.list_games()[0].name == "手填游戏"


def test_prefetch_names_skips_games_without_an_app_id(tmp_path: Path) -> None:
    """有缓存目录, 但游戏没有 AppID: 跳过而不是去联网查名(离线也不会卡住)."""
    service = _cached_service(tmp_path, cache_dir=tmp_path / "cache")
    service.add_game("手填游戏")

    service.prefetch_names()
    service.prefetch_names(refresh=True)

    assert service.list_games()[0].name == "手填游戏"


def test_artwork_prefetch_is_not_re_entrant(tmp_path: Path) -> None:
    """上一轮还在跑时再点一次不该又起一条线程(重复下载同一批图)."""
    service = _cached_service(tmp_path, cache_dir=tmp_path / "cache")
    service._artwork_running = True  # 模拟"上一轮下载还没结束"

    service.prefetch_artwork()

    assert service._artwork_running is True, "重入的调用不该把运行标志清掉"


def test_artwork_download_marks_attempts_and_clears_the_flag(tmp_path: Path) -> None:
    """下载收尾必须清掉运行标志, 且已经试过的游戏不再重复试."""
    service = _cached_service(tmp_path, cache_dir=tmp_path / "cache")
    summary = service.add_game("手填游戏")
    service._artwork_attempts.add(summary.game_id)
    service._artwork_running = True

    service._download_artwork()
    service._download_artwork()

    assert service._artwork_running is False
    assert summary.game_id in service._artwork_attempts


class _NoRefsAdapter(_FakeAdapter):
    """支持取图但一份引用都给不出的适配器(平台没有可用图片资源)."""

    def artwork_refs(self, game: PlatformGame) -> tuple[ArtworkRef, ...]:
        """不给任何引用."""
        del game
        return ()


def test_save_candidate_source_prefers_the_injected_one(tmp_path: Path) -> None:
    """注入了候选来源就用它, 不去读本机 Steam 目录(离线与测试的前提)."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    injected = _FakeSaveSource()
    service = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
        save_source=injected,
    )

    assert service._save_candidate_source() is injected


def test_candidate_names_skip_unresolvable_and_already_cached_rows(
    tmp_path: Path,
) -> None:
    """译名只补"能解析出 AppID 且缓存里还没有"的候选: 两类跳过各走一次."""
    cache_dir = tmp_path / "cache"
    service, _game_id, database = _steam_service(
        tmp_path, cache_dir=cache_dir, name_fetcher=_StubNames({})
    )
    candidates = CandidateRepository(database)
    for index, (name, detail) in enumerate(
        [("没有 AppID", ""), ("已有译名", "appmanifest_730.acf")]
    ):
        install = tmp_path / f"cand{index}"
        install.mkdir()
        candidates.upsert(
            GameCandidate(
                name=name, install_dir=str(install), source="steam", detail=detail
            )
        )
    third = tmp_path / "cand2"
    third.mkdir()
    candidates.upsert(
        GameCandidate(
            name="取不到译名",
            install_dir=str(third),
            source="steam",
            detail="appmanifest_1145360.acf",
        )
    )
    name_cache_at(cache_dir).put("730", "zh-CN", "已有译名")

    service._localize_names(refresh=False)

    # 能解析、没缓存的那条去问了一次(替身给不出结果, 于是缓存里什么都没留下).
    assert name_cache_at(cache_dir).get("730", "zh-CN") == "已有译名"
    assert name_cache_at(cache_dir).get("1145360", "zh-CN") is None


def test_cached_name_of_a_candidate_without_an_app_id_is_empty(tmp_path: Path) -> None:
    """探测结果没有 AppID 时译名一律为空(不猜也不联网)."""
    cache_dir = tmp_path / "cache"
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    install = tmp_path / "monitored"
    install.mkdir()
    CandidateRepository(database).upsert(
        GameCandidate(name="手填", install_dir=str(install), source="monitored")
    )

    item = next(row for row in service.list_candidates() if row.name == "手填")

    assert item.localized_name == ""
    assert item.display_name == "手填"


def test_artwork_download_walks_a_game_that_can_be_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有引用可用的游戏要走完下载这一轮: 取到就计数并刷新数据版本."""

    import archive_management.ui.sql_backend as sql_mod

    cache_dir = tmp_path / "cache"
    service, game_id, _database = _steam_service(tmp_path, cache_dir=cache_dir)
    monkeypatch.setattr(
        sql_mod,
        "resolve_artwork",
        lambda *a, **k: SimpleNamespace(found=True, path=None),
    )
    service._artwork_running = True
    before = service.list_games()[0].name  # 触发一次读取, 拿到当前状态

    service._download_artwork()

    assert before == "Demo"
    assert game_id in service._artwork_attempts
    assert service._artwork_running is False


def test_store_icon_skips_an_icon_that_is_already_cached(tmp_path: Path) -> None:
    """同一来源版本的图标已在缓存里就不再重算(重算等于白解码一次)."""
    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cache.store("steam", "730", "icon", ICON_VERSION, content=_PNG, extension="png")
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None

    assert service._store_icon(cache, referenced) is None


def test_store_icon_gives_up_when_conversion_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原图解不开时不上缓存、也不抛错(界面回落名称首字占位)."""
    import archive_management.ui.sql_backend as sql_mod

    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cache.store(
        "steam", "730", "cover", STEAM_COVER_ASSET, content=_PNG, extension="png"
    )
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None
    monkeypatch.setattr(sql_mod, "square_icon", lambda *_a, **_k: None)

    assert service._store_icon(cache, referenced) is None
    assert service._fetch_artwork(cache, referenced) == 0


def test_artwork_game_needs_the_adapter_to_offer_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """适配器支持取图但给不出引用时不去下载(界面直接占位)."""
    service, _game_id, database = _steam_service(tmp_path, cache_dir=tmp_path / "cache")
    game = GameRepository(database).get(1)
    assert game is not None
    monkeypatch.setattr(
        service, "_adapter_for", lambda platform: _NoRefsAdapter(_FakeSaveSource())
    )

    assert service._artwork_game(game) is None


def test_save_candidate_source_falls_back_to_the_local_steam_cloud(
    tmp_path: Path,
) -> None:
    """没有注入来源时读本机公开的 Steam 云同步清单(只读本机文件, 不联网)."""
    from archive_management.services.steam_cloud import SteamCloudSource

    service = _service(tmp_path)

    assert isinstance(service._save_candidate_source(), SteamCloudSource)


def test_prefetch_artwork_starts_one_background_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有在跑的一轮时: 先占住运行标志, 再起一条后台线程(界面不阻塞)."""
    service = _cached_service(tmp_path, cache_dir=tmp_path / "cache")
    service.add_game("手填游戏")
    ran: list[str] = []
    monkeypatch.setattr(service, "_download_artwork", lambda: ran.append("ran"))

    assert service._artwork_running is False
    service.prefetch_artwork()

    assert service._artwork_running is True, "起线程前必须先占住运行标志"


def test_user_artwork_path_is_empty_when_the_game_is_unknown(tmp_path: Path) -> None:
    """游戏不存在(刚被删)/参数不对时给空串: 缺一张自定义封面不该打断渲染."""
    service = _service(tmp_path)

    assert service.user_artwork_path("999", "cover") == ""
    assert service.user_artwork_path("不是 id", "cover") == ""


def test_a_failed_icon_cache_write_just_skips_that_icon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写图标缓存失败(磁盘满/只读)时只跳过这张图, 交给界面回落首字位图."""
    service = _cached_service(tmp_path, cache_dir=tmp_path / "cache")
    cache = service._artwork_cache()
    assert cache is not None
    source = tmp_path / "icon.ico"
    source.write_bytes(_PNG)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("磁盘满了")

    monkeypatch.setattr(service, "_icon_source", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(sql_mod, "square_icon", lambda *_args, **_kwargs: _PNG)
    monkeypatch.setattr(cache, "store", refuse)

    referenced = PlatformGame(name="Demo", platform="steam", game_id="730")

    assert service._store_icon(cache, referenced) is None
