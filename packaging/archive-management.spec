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

# UPX: 定案**不压**(2026-10-24, J-6 实测)。`upx=True` 只在构建机装了 UPX 时才生效, 没装就被
# 静默跳过 —— PyInstaller 只在 configure 阶段说一句 "UPX is available ...", 缺了它一个字都不提,
# 所以那三处 `upx=True` 一直是个假开关(看上去压了, 实际一次都没压)。实测数字(Windows x64 /
# Python 3.12 / UPX 5.2.1, 各跑 3 次取中位):
#   onedir  86.5 MB -> 37.6 MB(打包成 zip 41.1 -> 31.5), 但 GUI 启动到关窗 1.70 s -> 2.07 s;
#   onefile 40.2 MB -> 31.5 MB, 启动 3.61 s -> 3.63 s(解包开销本来就把这点差别盖住了)。
# 省的体积换不来两个代价: 构建链要多一个外部二进制(还要在 CI 里装 + 断言真压了), 而 UPX 壳是
# 杀软启发式与下载拦截的高频形态, 我们又签不了名。要翻案就先在这里改成 True, 同时给
# release-build.yml 加装 UPX 的步骤并断言构建日志里出现 UPX 行 —— 判据不许建在"代码里写了 True"上。
onedir_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ArchiveManagement",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(icon_file),
)

onedir = COLLECT(
    onedir_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ArchiveManagement",
)

onefile_exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ArchiveManagement",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(icon_file),
)