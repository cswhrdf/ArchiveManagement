from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


project_root = Path(SPECPATH).parent
source_root = project_root / "src"
resource_root = source_root / "archive_management" / "resources"

datas = collect_data_files("customtkinter")
datas.append((str(resource_root), "archive_management/resources"))

# 应用图标(写进 exe 的 PE 资源, 也就是资源管理器/任务栏那个图标)。位图是
# ``resources/app.svg`` 的产物, 由 ``uv run python scripts/build_icon.py`` 生成并入库;
# 缺了就当场失败 —— 少一个图标不会让构建报错, 只会让发布包默默地顶着默认图标,
# 而“默默地少点什么”正是这里要拦的。
icon_file = resource_root / "app.ico"
if not icon_file.is_file():
    raise SystemExit(
        "缺少打包图标 resources/app.ico: 先运行 "
        "`uv run python scripts/build_icon.py` 重画(SVG 是唯一真相)"
    )

a = Analysis(
    [str(source_root / "archive_management" / "__main__.py")],
    pathex=[str(source_root)],
    binaries=[],
    datas=datas,
    # Pillow 由 PyInstaller 自带的 hook 收集(``CTkImage`` 需要 ``PIL.ImageTk``), 无需手工列举条目.
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

onedir_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ArchiveManagement",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(icon_file),
)

onedir = COLLECT(
    onedir_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ArchiveManagement",
)

onefile_exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ArchiveManagement-onefile",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(icon_file),
)