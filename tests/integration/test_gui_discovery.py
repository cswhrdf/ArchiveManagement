"""
发现页: 扫描与导入候选、忽略记忆、监控目录管理、空态与本地化名。拆自 test_gui_buttons.py(见 docs/test-refactor-plan.md S7)。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from gui_support import gui_app

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入
    from tkinter import TclError

    import customtkinter as ctk  # 既校验可导入, 也用于构造测试用父容器
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

import archive_management.ui.discovery_page as disc_mod
from archive_management.i18n import tr
from archive_management.services.pathcheck import normalize_path
from button_support import (
    _button_texts,
    _label_texts,
    _new_app,
    _patch_dialogs,
    _pump,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.smoke,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("按钮端到端操作"),
    pytest.mark.layer("e2e"),
]

# 后台操作等待上限: 恢复/备份会真读写文件, 覆盖率与 CI 环境下会明显变慢.


def _labels_of(widget: Any) -> list[Any]:
    """递归收集控件树里所有 CTkLabel(裁剪类断言用它定位标签)."""
    import customtkinter as ctk

    found: list[Any] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(child)
        found.extend(_labels_of(child))
    return found


def test_discovery_panel_scans_filters_and_imports_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发现窗口: 扫描、筛选、忽略/恢复与导入都作用到后端数据."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.models import CandidateFilter
    from archive_management.ui.palette import Palette

    _patch_dialogs(monkeypatch, ask_text="空洞骑士")
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        assert [item.directory_id for item in panel._dirs] == ["dir-1", "dir-2"]
        # 已导入的候选(cand-1)不进这一页: 它是游戏库里的一款游戏, 在库里有完整动作.
        assert len(panel._candidates) == 5
        assert "cand-1" not in {item.candidate_id for item in panel._candidates}
        # 未选中有效候选时"导入"不可用(候选 4 的路径已失效).
        panel._select_candidate("cand-4")
        assert str(panel._import_btn.cget("state")) == "disabled"

        panel._on_scan()
        _pump(app)
        assert tr("discovery.scanning") not in panel._summary_label.cget("text")
        assert panel._dirs[0].last_scan_label != ""

        # 筛选: 只看已忽略的候选, 再把它们恢复成待处理.
        panel._filter_box.set(CandidateFilter.IGNORED.label)
        panel._on_filter_change(CandidateFilter.IGNORED.label)
        assert set(panel._cand_rows) == {"cand-5"}
        panel._select_candidate("cand-5")
        panel._on_ignore()
        _pump(app)
        assert app.backend.list_candidates(status="ignored") == []

        # 导入一条候选: 后端新增游戏, 候选变为已导入.
        before = len(app.backend.list_games())
        panel._filter_box.set(CandidateFilter.NEW.label)
        panel._on_filter_change(CandidateFilter.NEW.label)
        panel._select_candidate("cand-2")
        panel._on_import()
        _pump(app)

        assert len(app.backend.list_games()) == before + 1
        imported = next(
            item
            for item in app.backend.list_candidates()
            if item.candidate_id == "cand-2"
        )
        assert imported.status == "imported"
        assert imported.game_id is not None
        # 导入之后它就从这一页收走了(转而在游戏库里查看).
        assert "cand-2" not in {item.candidate_id for item in panel._candidates}
        assert "cand-2" not in panel._cand_rows
    finally:
        app.destroy()


def test_discovery_clear_results_keeps_imported_and_remembers_ignores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """\"清空结果\"按钮的完整路径: 待处理/已忽略清掉、已导入保留、忽略按名字记住.

    出处(用户 2026-10-03): 重新扫描时清空当前记录重新扫, 只按名字记住设置了忽略的游戏。

    走的是界面上的真按钮: 忽略一条 → 点\"清空结果\"(确认) → 列表里只剩已导入的那条 →
    再扫描一遍, 被忽略的那条**直接回到已忽略**(不会又变成待处理), 其余重新出现。
    """
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    _patch_dialogs(monkeypatch)
    monkeypatch.setattr(disc_mod, "confirm_dialog", lambda *_a, **_k: True)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        panel._select_candidate("cand-2")
        panel._on_ignore()
        _pump(app)

        panel._on_clear_scan()
        _pump(app)
        left = {item.candidate_id for item in app.backend.list_candidates()}
        assert left == {"cand-1"}, f"清空后应该只剩已导入的那条: {left}"
        assert panel._summary_label.cget("text") != "", "清空后要给一句结果提示"

        panel._on_scan()
        _pump(app)
        ignored = {
            item.candidate_id for item in app.backend.list_candidates(status="ignored")
        }
        pending = {
            item.candidate_id for item in app.backend.list_candidates(status="new")
        }
        assert ignored == {"cand-2", "cand-5"}, f"忽略过的名字要按名字记住: {ignored}"
        assert pending == {"cand-3", "cand-4", "cand-6"}, f"其余应重新出现: {pending}"
    finally:
        app.destroy()


def test_discovery_panel_hides_already_imported_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已导入的候选不进发现页: 它们是游戏库里的游戏, 在库里有完整动作.

    演示数据里 cand-1("星际拓荒")就是"已导入"的那一条 —— 夹具先自证它存在, 再钉住
    三件事: 列表里没有它、筛选项里没有"已导入"、底栏的分母也不算它。
    """
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.models import CandidateFilter
    from archive_management.ui.palette import Palette

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        imported = {
            item.candidate_id for item in app.backend.list_candidates(status="imported")
        }
        assert imported == {"cand-1"}, "夹具失效: 演示数据里应当正好有一条已导入候选"

        # ① 列表里没有它(切到"全部"也一样), 但后端那条记录仍然留着.
        listed = {item.candidate_id for item in panel._candidates}
        assert listed & imported == set()
        assert len(listed) == len(app.backend.list_candidates()) - len(imported)
        panel._on_filter_change(CandidateFilter.ALL.label)
        assert set(panel._cand_rows) & imported == set()

        # ② 筛选枚举里也没有"已导入"这一项(它的动作只在游戏库里).
        assert [item.value for item in CandidateFilter] == ["all", "new", "ignored"]

        # ③ 底栏分母按"这一页真的会列出来的条数"算, 三种状态并列在**同一行**里.
        pending = sum(1 for item in panel._candidates if item.status == "new")
        ignored = sum(1 for item in panel._candidates if item.status == "ignored")
        assert panel._summary_label.cget("text") == tr(
            "discovery.counts_candidates",
            candidates=len(listed),
            pending=pending,
            ignored=ignored,
        )
        # 第二行留给扫描结果: 不再重复同一批数(评审时的两行计数重复).
        assert panel._detail_label.cget("text") == ""
    finally:
        app.destroy()


def test_discovery_panel_manages_monitored_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """监控目录的新增校验: 非法路径给出后端原因, 合法路径写入列表."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    # 依次回答: 先给一个空路径(应被拒绝), 再给一个真实目录.
    _patch_dialogs(monkeypatch, ask_text_queue=["   ", str(tmp_path)])
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    panel = DiscoveryPanel(
        ctk.CTkFrame(app),
        backend=app.backend,
        palette=Palette.for_theme(app._theme),
    )
    before = len(panel._dirs)

    panel._on_add_dir()
    _pump(app)
    assert len(panel._dirs) == before
    assert "不能为空" in panel._summary_label.cget("text")

    panel._on_add_dir()
    _pump(app)
    assert len(panel._dirs) == before + 1
    assert str(tmp_path) in {item.path for item in panel._dirs}

    # 停用后不再参与扫描, 但记录仍保留.
    panel._select_dir(panel._dirs[-1].directory_id)
    panel._on_toggle_dir()
    _pump(app)
    assert panel._dir_item() is not None
    assert panel._dir_item().enabled is False  # type: ignore[union-attr]


def test_discovery_panel_dir_actions_with_nothing_to_act_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """监控目录的编辑/启停/删除在"输入为空、未确认、选中已不存在"时都不动数据."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    # ask_text 返回空串(= 用户直接确定/取消), confirm_dialog 一律返回"否"。
    _patch_dialogs(monkeypatch, ask_text="", confirm=False)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        app.backend.add_monitored_directory(str(tmp_path))
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        before = [
            item.directory_id for item in app.backend.list_monitored_directories()
        ]
        assert before, "演示数据里应该至少有一个监控目录"

        # ① 新增目录: 路径为空 → 什么都不发生。
        panel._on_add_dir()
        assert [
            item.directory_id for item in app.backend.list_monitored_directories()
        ] == before

        # ② 编辑目录: 没选中任何目录。
        panel._selected_dir = None
        panel._on_edit_dir()
        panel._on_toggle_dir()
        panel._on_remove_dir()
        # ③ 编辑目录: 选中了真实目录, 但新路径为空/取消。
        panel._select_dir(before[0])
        panel._on_edit_dir()
        # ④ 删除目录: 用户在确认框里选了"否"。
        panel._on_remove_dir()
        assert [
            item.directory_id for item in app.backend.list_monitored_directories()
        ] == before

        # ⑤ 选中的 id 已经不存在(窗口没刷新, 记录在别处被删了)。
        panel._selected_dir = "已经不存在的目录"
        panel._on_edit_dir()
        panel._on_toggle_dir()
        panel._on_remove_dir()
        _pump(app)

        assert [
            item.directory_id for item in app.backend.list_monitored_directories()
        ] == before
        assert panel._dir_item() is None
    finally:
        app.destroy()


def test_discovery_panel_candidate_actions_need_a_valid_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选的导入/忽略/修正路径: 未选中、选中已不存在、对话框取消都不动数据."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    # import_game_dialog 返回 None(= 关闭对话框), ask_text 为空(= 不填新路径)。
    _patch_dialogs(monkeypatch, ask_text="", import_result=None)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        app.backend.scan_candidates()
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        panel.reload()
        before = app.backend.list_candidates()
        assert before, "演示数据里应该至少有候选游戏"
        # 这一页只列未导入的候选(cand-1 已导入, 只在游戏库里), 所以挑一条真在列表里的.
        listed = next(item for item in panel._candidates if item.status == "new")

        # ① 没选候选。
        panel._selected_candidate = None
        panel._on_import()
        panel._on_ignore()
        panel._on_relocate()
        # ② 选中的候选 id 已经不存在。
        panel._selected_candidate = "已经消失的候选"
        panel._on_import()
        panel._on_ignore()
        panel._on_relocate()
        # ③ 真实候选, 但导入对话框被取消、修正路径没填新路径。
        panel._select_candidate(listed.candidate_id)
        panel._on_import()
        panel._on_relocate()
        _pump(app)

        assert app.backend.list_candidates() == before
        # 取消导入、又不填新路径: 候选一个字段都没变(选中仍是那个真实候选)。
        assert panel._candidate_item() is not None
    finally:
        app.destroy()


def test_discovery_panel_defaults_to_candidates_and_switches_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发现窗口分成两页: 默认停在"探测结果", 可切到"监控目录".

    同时锁定回归: 候选操作只剩导入/忽略/修正路径, 不再有"加入监控"按钮。
    """
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.models import DiscoveryPage
    from archive_management.ui.palette import Palette

    _patch_dialogs(monkeypatch)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )

        def current() -> DiscoveryPage:
            """读取当前页面(经函数返回避免 mypy 对属性做字面量收窄)."""
            return panel._page

        candidates = panel._page_frames[DiscoveryPage.CANDIDATES]
        monitored = panel._page_frames[DiscoveryPage.MONITORED]

        # 默认页 = 探测结果, 监控目录页未布局(grid_remove 后 grid_info 为空).
        assert current() is DiscoveryPage.CANDIDATES
        assert candidates.grid_info() != {}
        assert monitored.grid_info() == {}

        # 候选页的按钮恰好是导入/忽略/修正路径 + 清空结果四个(没有"加入监控").
        # "清空结果"是整页动作(2026-10-03): 一次清掉非已导入的候选, 已忽略的按名字记住 ——
        # 它也在这一排里, 所以这里的判据跟着加上它(数量断言仍然钉住"没有多余的按钮")。
        expected = {
            tr("discovery.cand_import"),
            tr("discovery.cand_ignore"),
            tr("discovery.cand_relocate"),
            tr("discovery.clear_scan"),
        }
        assert set(_button_texts(candidates)) & expected == expected
        assert len(_button_texts(candidates)) == len(expected)

        panel._show_page(DiscoveryPage.MONITORED)
        assert current() is DiscoveryPage.MONITORED
        assert monitored.grid_info() != {}
        assert candidates.grid_info() == {}
        assert set(_button_texts(monitored)) >= {
            tr("discovery.dir_add"),
            tr("discovery.dir_edit"),
            tr("discovery.dir_remove"),
        }
        # "重新扫描"在页签行上, 两个页面都能用.
        assert tr("discovery.scan") in _button_texts(panel.frame)

        panel._show_page(DiscoveryPage.CANDIDATES)
        assert current() is DiscoveryPage.CANDIDATES
        assert candidates.grid_info() != {}
        assert monitored.grid_info() == {}
    finally:
        app.destroy()


def test_discovery_default_filter_is_pending_with_filter_aware_empty_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """默认筛选为"待处理"; 筛选没有匹配项时提示与该筛选相关而不是"暂无探测结果"."""
    from archive_management.domain import GameCandidate
    from archive_management.infrastructure.database import Database
    from archive_management.services.platform_scan import LocalGameScanner
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.models import CandidateFilter
    from archive_management.ui.palette import Palette
    from archive_management.ui.sql_backend import SqlArchiveService

    db = Database(tmp_path / "filter.db")
    db.migrate()
    games = tmp_path / "Games"
    (games / "Alpha").mkdir(parents=True)

    # 用替身探测: 无论本机装了多少平台游戏, 这次扫描都只有一条候选.
    def fake_scan(
        self: object, *, monitored: Sequence[str] = ()
    ) -> list[GameCandidate]:
        del self, monitored
        return [
            GameCandidate(
                name="Alpha",
                install_dir=str(games / "Alpha"),
                source="monitored",
                confidence="medium",
                reason_code="monitored_child",
                health="ok",
            )
        ]

    monkeypatch.setattr(LocalGameScanner, "scan", fake_scan)
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    service.add_monitored_directory(str(games))
    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app), backend=service, palette=Palette.for_theme(app._theme)
        )
        assert panel._filter is CandidateFilter.NEW
        assert panel._filter_box.get() == CandidateFilter.NEW.label
        assert panel._cand_rows == {}  # 还没扫描过

        panel._on_scan()
        _pump(app)
        assert [item.status for item in panel._candidates] == ["new"]
        assert set(panel._cand_rows) == {panel._candidates[0].candidate_id}

        # 忽略唯一一条待处理项: 待处理筛选下列表变空, 但提示说得清是筛选造成的.
        target = panel._candidates[0]
        panel._select_candidate(target.candidate_id)
        panel._on_ignore()
        _pump(app)
        assert panel._cand_rows == {}
        texts = _label_texts(panel._cand_box)
        assert tr("discovery.empty_filtered", filter=CandidateFilter.NEW.label) in texts
        assert any("已忽略 1" in text for text in texts)
        assert tr("discovery.candidates_empty") not in texts

        # 切到"已忽略"就能看到刚忽略的那条(筛选本身工作正常), 底栏同一行里数得到它.
        panel._on_filter_change(CandidateFilter.IGNORED.label)
        _pump(app)
        assert set(panel._cand_rows) == {target.candidate_id}
        assert "已忽略 1" in str(panel._summary_label.cget("text"))
    finally:
        app.destroy()


def test_discovery_rows_clip_long_paths_and_ignore_stale_refits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """候选行的长安装路径按"中间省略"裁进卡片; 行重建后旧标签的回调不再写已销毁控件.

    回归(评审时发现): 长路径既不换行也不省略, 会直接顶到卡片右边缘。
    两条断言分工(2026-09-27 重写, 原版在两个平台上都"不会执行"):

    * 比对**同一个裁剪函数**在同一宽度下的输出 —— 比文本不比像素, 与字体库无关,
      钉的是"布局停在上一次窄宽度"这类回归;
    * "放不下就必须裁"由夹具里的**拉丁字符**保证: 缺中日韩字体的环境(裸 Linux
      runner)量汉字近乎零宽, 但拉丁字符在任何字体下都能量出来, 所以这条重想
      在哪个平台都真的会执行。
    """
    from archive_management.domain import GameCandidate
    from archive_management.infrastructure.database import Database
    from archive_management.services.platform_scan import LocalGameScanner
    from archive_management.ui.discovery_page import _CARD_TEXT_WIDTH, DiscoveryPanel
    from archive_management.ui.palette import Palette
    from archive_management.ui.sql_backend import SqlArchiveService

    db = Database(tmp_path / "clip.db")
    db.migrate()
    games = tmp_path / "Games"
    # 路径刻意长到"任何字体都放不下": 拉丁字符每个都能被量出宽度(中日韩字体缺失也
    # 一样), 所以下面"必须裁"那一条在每个平台都真的会被执行到。
    deep = (
        games
        / "Alpha"
        / "one-very-long-directory-name-level"
        / "another-long-directory-name-level"
        / "third-long-directory-name-level"
        / "一个很深的目录层级"
        / "再来一层目录"
        / "存档目录"
    )
    deep.mkdir(parents=True)

    def fake_scan(
        self: object, *, monitored: Sequence[str] = ()
    ) -> list[GameCandidate]:
        del self, monitored
        return [
            GameCandidate(
                name="路径很长的候选游戏",
                install_dir=str(deep),
                source="monitored",
                confidence="medium",
                reason_code="monitored_child",
                health="ok",
            )
        ]

    monkeypatch.setattr(LocalGameScanner, "scan", fake_scan)
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    service.add_monitored_directory(str(games))
    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        # 面板必须被**框定宽度**: 没被摆放的容器里每个控件都按内容自适应宽度, 标签宽度
        # 就等于文本宽度 —— 那样 `width` 与被测文本同源, "放不下就该裁"永远不会成立
        # (2026-09-27 实测: 标签宽 1407 == 整条路径的度量值, 于是那两条断言从没跑过).
        host = ctk.CTkFrame(
            app, fg_color="transparent", width=_CARD_TEXT_WIDTH + 40, height=420
        )
        # CTk 的 place 不接受 width/height(尺寸给构造函数), 且固定尺寸不许被内容撑大.
        host.grid_propagate(False)
        host.place(x=0, y=0)
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)
        panel = DiscoveryPanel(
            host, backend=service, palette=Palette.for_theme(app._theme)
        )
        panel.frame.grid(row=0, column=0, sticky="nsew")
        panel._on_scan()
        _pump(app)
        item = panel._candidates[0]
        row = panel._cand_rows[item.candidate_id]
        _pump(app)

        # 这条候选行里只有安装路径是长文本(没有存档路径那样只剩结论一行), 所以
        # "带省略号的标签"就是它 —— 裁剪本身由 ui.widgets.track_fit 按容器宽度做.
        clipped = [label for label in _labels_of(row) if "…" in str(label.cget("text"))]
        assert len(clipped) == 1, "这条候选行里应该只有一个被裁的标签(安装路径)"
        label = clipped[0]

        shown = str(label.cget("text"))
        width = int(label.winfo_width())
        assert width > 1, "夹具没给标签宽度约束, 下面的断言会退化成循环论证"
        font = panel._path_font
        # 期望值用同一个裁剪函数算(比文本, 不比像素): 钉的是"布局停在上一次窄宽度".
        assert shown == panel._clip_path(item.install_dir, width), (
            "标签里显示的就是裁剪后的文本"
        )
        assert font.measure(shown) <= width, f"路径溢出卡片: {shown!r}"
        # 非循环的"必须被裁": 只量路径里的**拉丁字符** —— 缺中日韩字体的环境照样能量出
        # 它们的宽度, 所以这条前提在每个平台都成立(2026-09-27 假红就是因为夹具里全是汉字).
        ascii_only = "".join(ch for ch in item.install_dir if ord(ch) < 0x2E80)
        assert font.measure(ascii_only) > width, f"夹具路径不够长: {ascii_only!r}"
        assert shown != item.install_dir, "长安装路径必须被裁"
        assert "…" in shown, f"路径要中间省略: {shown!r}"

        # 没有存档路径时只剩结论那一行(调用方据此决定不登记重裁).
        assert panel._save_lines(item) == item.save_label

        # 忽略这条候选后整个列表重建: 旧行(连同它上面的 <Configure> 绑定)被销毁,
        # 不可能再往已销毁的控件上写文本 —— 旧实现是靠"登记表里取不到目标就返回"
        # 兜住同一个问题的, 现在结构上就不会发生。
        panel._select_candidate(item.candidate_id)
        panel._on_ignore()
        _pump(app)
        assert item.candidate_id not in panel._cand_rows
        assert not row.winfo_exists(), "旧行必须随重建销毁"
    finally:
        app.destroy()


def test_discovery_import_confirms_paths_and_probes_artwork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """导入时确认存档路径并触发封面探测: 行内已带推断结果, 状态栏给出提示."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    notices: list[str] = []
    _patch_dialogs(monkeypatch, import_result=("星露谷", ("C:\\Saves", "D:\\Other")))
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    panel = DiscoveryPanel(
        ctk.CTkFrame(app),
        backend=app.backend,
        palette=Palette.for_theme(app._theme),
        on_notice=notices.append,
    )

    # 探测结果行里直接带着推断出来的存档路径与"平台不支持"的说明.
    steam_row = next(
        item for item in panel._candidates if item.candidate_id == "cand-6"
    )
    assert steam_row.save_supported is True
    assert next(path.path for path in steam_row.save_paths).endswith("Saves")
    assert panel._save_lines(steam_row).count("\n") == len(steam_row.save_paths)
    monitored_row = next(
        item for item in panel._candidates if item.candidate_id == "cand-2"
    )
    assert monitored_row.save_supported is False
    assert monitored_row.save_label == tr(
        "discovery.save_unsupported", platform=monitored_row.source_label
    )

    panel._select_candidate("cand-6")
    panel._on_import()
    _pump(app)

    imported = next(game for game in app.backend.list_games() if game.name == "星露谷")
    assert imported.saved_paths == 2
    # 路径比对走 normalize_path: 后端存的是**规范化后的绝对路径**, 而 "C:\Saves" 这类
    # Windows 风格写法的规范化结果与平台有关(POSIX 上会落到当前工作目录之下)。
    assert [item.path for item in app.backend.list_locations(imported.game_id)] == [
        normalize_path("C:\\Saves"),
        normalize_path("D:\\Other"),
    ]
    # 页面提示写清登记了几条存档位置.
    assert panel._summary_label.cget("text") == tr(
        "result.game_added_with_paths", name="星露谷", count=2
    )
    # 导入成功后才开始探测封面, 并把这件事写到左下角状态栏(页面不弹窗).
    assert notices == [tr("discovery.artwork_started", name="星露谷")]


def test_discovery_rows_show_the_localized_name() -> None:
    """探测结果行直接显示当前语言的译名, 并把探测到的原名放在括号里."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    panel = DiscoveryPanel(
        ctk.CTkFrame(app), backend=app.backend, palette=Palette.for_theme(app._theme)
    )
    row = next(item for item in panel._candidates if item.candidate_id == "cand-6")

    assert row.localized_name == "星露谷物语"
    assert panel._candidate_title(row) == "星露谷物语  (Stardew Valley)"


def test_discovery_panel_edits_and_removes_a_monitored_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """监控目录: 只答路径不答备注就整条不动; 两个都答完才写回; 删除要确认."""
    import archive_management.ui.discovery_page as disc_page_mod
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    _patch_dialogs(monkeypatch, confirm=True)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        target = panel._dirs[0].directory_id
        panel._select_dir(target)
        before = app.backend.list_monitored_directories()
        moved = str(tmp_path / "moved")

        # ① 备注那一步被取消: 路径虽然填了也不写回。
        answers = iter([moved, None])
        monkeypatch.setattr(disc_page_mod, "ask_text", lambda *_a, **_k: next(answers))
        panel._on_edit_dir()
        assert app.backend.list_monitored_directories() == before

        # ② 两个输入框都答完: 路径与备注一起写回。
        answers = iter([moved, "新备注"])
        panel._on_edit_dir()
        updated = next(
            item
            for item in app.backend.list_monitored_directories()
            if item.directory_id == target
        )
        assert (updated.path, updated.note) == (moved, "新备注")

        # ③ 删除: 确认后记录消失。
        panel._on_remove_dir()
        assert target not in {
            item.directory_id for item in app.backend.list_monitored_directories()
        }
    finally:
        app.destroy()


def test_discovery_panel_candidate_answers_and_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """候选: 取消导入不动数据 / 填了新路径就修正 / 不认识的筛选与状态都安静跳过."""
    from dataclasses import replace

    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.models import CandidateFilter, SavePathSuggestion
    from archive_management.ui.palette import Palette

    relocated = str(tmp_path / "relocated")
    _patch_dialogs(monkeypatch, ask_text=relocated, import_result=None)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        # ① 下拉框给了不认识的值: 筛选保持原样, 列表照常重绘。
        panel._on_filter_change("不认识的筛选")
        assert panel._filter is CandidateFilter.NEW

        candidate = next(item for item in panel._candidates if item.importable)
        panel._select_candidate(candidate.candidate_id)
        before = app.backend.list_candidates()

        # ② 导入对话框被取消: 候选一个字段都不变。
        panel._on_import()
        assert app.backend.list_candidates() == before

        # ③ 修正路径: 填了新路径就写回。
        panel._on_relocate()
        moved = next(
            item
            for item in app.backend.list_candidates()
            if item.candidate_id == candidate.candidate_id
        )
        assert moved.install_dir == relocated

        # ④ 后端给出三种之外的状态时, 计数只统计认识的那些。
        weird = replace(candidate, status="unknown")
        known = panel._candidates
        panel._candidates = [weird, *known]
        counts = panel._status_counts()
        panel._candidates = known
        assert sum(counts.values()) == len(known), "不认识的状态不该被算进任何一档"

        # ⑤ 有存档路径且都不危险: 路径区用成功色。
        suggestion = SavePathSuggestion(path=relocated, path_kind="directory")
        with_paths = replace(candidate, save_paths=(suggestion,))
        assert panel._save_color(with_paths) == panel._palette.success
    finally:
        app.destroy()
