from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files


project_root = Path(SPECPATH).parent
source_root = project_root / "src"
resource_root = source_root / "archive_management" / "resources"

datas = collect_data_files("customtkinter")
datas.append((str(resource_root), "archive_management/resources"))

# 平台开关: 同一份 spec 服务 Windows 与 Linux(2026-11-01 起), 差异只落在这两处 ——
# 图标资源与隐藏导入。产物名两边一致(dist/ArchiveManagement.exe / dist/ArchiveManagement),
# 所以冒烟脚本与工作流只需要按平台拼后缀。
is_windows = sys.platform == "win32"

# 应用图标(写进 exe 的 PE 资源, 也就是资源管理器/任务栏那个图标)。位图是
# ``resources/app.svg`` 的产物, 由 ``uv run python scripts/build_icon.py`` 生成并入库;
# 缺了就当场失败 —— 少一个图标不会让构建报错, 只会让发布包默默地顶着默认图标,
# 而“默默地少点什么”正是这里要拦的。**只拦 Windows**: PE 资源是 Windows 独有的概念,
# Linux 上 PyInstaller 的 ``icon=`` 是空操作, 产物的窗口/任务栏图标由运行时的
# ``iconphoto`` + ``app-256.png`` 负责(见 ui/window_icon.py), 与这里的 .ico 无关。
icon_file = resource_root / "app.ico"
if is_windows and not icon_file.is_file():
    raise SystemExit(
        "缺少打包图标 resources/app.ico: 先运行 "
        "`uv run python scripts/build_icon.py` 重画(SVG 是唯一真相)"
    )

# fcntl(POSIX 的文件锁)在 services/single_instance.py 里经 ``importlib.import_module``
# 动态导入。PyInstaller 的静态分析对字面量 import_module 大多能认, Windows 上 msvcrt
# 就是这么被打进去的(单实例冒烟一直绿); 但 fcntl 这条只有 Linux 构建才走得到, 而本仓库
# 没有 Linux 上的本地构建可以当场验证 —— 显式列出来, 不赌它。
hidden_imports: list[str] = [] if is_windows else ["fcntl"]

a = Analysis(
    [str(source_root / "archive_management" / "__main__.py")],
    pathex=[str(source_root)],
    binaries=[],
    datas=datas,
    # Pillow 由 PyInstaller 自带的 hook 收集(``CTkImage`` 需要 ``PIL.ImageTk``), 无需手工列举条目.
    # ``fcntl`` 的来路见上面的 hidden_imports。
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

# UPX: 定案**不压**(2026-10-24, J-6 实测)。`upx=True` 只在构建机装了 UPX 时才生效, 没装就被
# 静默跳过 —— PyInstaller 只在 configure 阶段说一句 "UPX is available ...", 缺了它一个字都不提,
# 所以那两处 `upx=True` 一直是个假开关(看上去压了, 实际一次都没压)。实测数字(Windows x64 /
# Python 3.12 / UPX 5.2.1, 各跑 3 次取中位):
#   onefile 40.2 MB -> 31.5 MB, 启动 3.61 s -> 3.63 s;
#   onedir  86.5 MB -> 37.6 MB(打包成 zip 41.1 -> 31.5), 但 GUI 启动到关窗 1.70 s -> 2.07 s。
# 省的体积换不来两个代价: 构建链要多一个外部二进制(还要在 CI 里装 + 断言真压了), 而 UPX 壳是
# 杀软启发式与下载拦截的高频形态, 我们又签不了名。要翻案就先在这里改成 True, 同时给
# release-build.yml 加装 UPX 的步骤并断言构建日志里出现 UPX 行 —— 判据不许建在"代码里写了 True"上。
#
# 产物形态: **单文件 exe**(2026-10-31 起。2026-10-28 曾改成只产 onedir, 那次是为了省掉一份
# 产物的冒烟与发布开销; 现在发布页要的是"下载一个 ArchiveManagement.exe, 双击就能用",
# 于是回到 onefile)。代价是每次启动会把整份包解到 %TEMP%\_MEIxxxx: 冷启动比 onedir 慢约
# 1.8 s(同机实测 onefile 2.0 s / onedir 0.5 s), 被杀或崩溃会留下约 75 MB 残渣。
# 冒烟侧的对策照旧: 子进程的 TEMP/TMP 一律指进本次工作目录, 收尾连工作目录一起删
# (scripts/smoke_packaged.py 的 `_temp_env`), 所以 CI 不会往 runner 的 %TEMP% 里攒东西。
#
# onefile 的 EXE 要**把 binaries 与 datas 一起收进来**(不带 `exclude_binaries`), 单文件因此
# 自带解释器与资源; 发布产物名就是这里的 `name=`: Windows 上 dist/ArchiveManagement.exe,
# Linux 上 dist/ArchiveManagement(PyInstaller 不加后缀)。
#
# Linux 侧的兼容性口径(2026-11-01 起 CI 也编 Linux):
#   * **glibc 地板由构建机决定** —— 解释器(uv 托管的 python-build-standalone)与 PyInstaller
#     引导程序都是 manylinux 出品(地板 2.17), 真正抬地板的是构建期从系统解析进包的库;
#     所以 CI 把 Linux 构建钉在 ubuntu-24.04(glibc 2.39): 发行版 ``_tkinter`` 的 ABI 还必须
#     与冻结解释器同小版本(24.04 的系统 Python 正是 3.12, 换镜像=换配方), 一份产物覆盖
#     Ubuntu 24.04+ / Debian 13+ / Fedora 40+ / Arch / 现役 openSUSE。Alpine(musl)需要
#     另一整套构建; 更老的发行版要另建老基线构建机再测, 都不在本期范围。
#   * **冻进包的 Tk 必须是发行版那份**(走 Xft/fontconfig 的 libtk8.6): uv 托管 CPython
#     自带的 Tcl/Tk 在 X11 只走核心位图字体、画不出汉字, 完整配方(发行版 ``_tkinter`` 拷上
#     PYTHONPATH 最前 + ``TCL_LIBRARY``/``TK_LIBRARY`` 指向 /usr/share/tcltk)在
#     release-build.yml 的 ``build-linux`` 作业里, 守卫是 test_ci_workflow 的
#     ``test_the_linux_package_bundles_a_tk_that_can_render_cjk``。
#   * **X11 客户端库与 fontconfig 不进包**(PyInstaller 视它们为系统库): 运行机器只要有桌面
#     环境就有它们; Tk 在 Wayland 会话下走 XWayland, 同样能起。
#   * 产物不装进系统、无桌面集成(.desktop/.deb/.rpm/AppImage 都是后续的分发格式增量, 不是
#     "每个发行版一个包")。
EXE(
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
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon=str(icon_file) if is_windows else None,
)
