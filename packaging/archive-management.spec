from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


project_root = Path(SPECPATH).parent
source_root = project_root / "src"
resource_root = source_root / "archive_management" / "resources"

datas = collect_data_files("customtkinter")
datas.append((str(resource_root), "archive_management/resources"))

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
)