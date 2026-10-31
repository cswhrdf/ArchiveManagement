# 界面用例的判据与坑

本文档集中界面（Tk / CustomTkinter）用例的量法、判据与实测坑，按环境与收尾、时序与量法、尺寸与配色、可用性与交互、文案五组展开。测试如何分层、等级怎么定与写用例的通用纪律见 [testing.md](testing.md)，CI 流程、失败留证、分片与报告见 [testing-ci.md](testing-ci.md)；各小节只保留仍然有效的判据与坑，完整复盘以测试文件的 docstring 与提交历史为准。

其他文档：[测试体系](testing.md)、[CI 流程、分片与报告](testing-ci.md)、[功能说明](features.md)、[开发与发布](development.md)。

## 1. 环境与收尾纪律

Tk 环境的可靠性与用例之间的不留渣，是所有界面用例的公共前提。

### 跳过只留给已知的环境问题

GUI 用例把建窗期的 TclError 写成 `pytest.skip("tk 环境不可用: ...")` 的代价是：真正的 Tcl 故障会伪装成一堆 skip（曾让两个平台都静默跳过同一条用例，而跳过不会让任何东西变红）。规矩（`tests/tk_guard.py`）：

- 只有已知环境症状才允许跳过：解释器加载不了 Tcl/Tk 库数据（`tcl_findLibrary` / `init.tcl` / `tk.tcl` / `auto.tcl` / `Can't find a usable …`）或没有显示环境（`no display name` 等）。前一组只可能由 Tcl 自己在装库时说出。
- **白名单只列 Tcl 自己的库数据文件名**：把 `couldn't read file` 这类通用说法整个收进来，会把"应用自己要读的文件打不开"也归成环境问题。两种拼法都能被文件名命中，不必放宽。
- 前缀是"tk 环境不可用"但症状不在清单里时，conftest 的报告钩子把它改成**失败**——要么修掉要么删掉，不留下永远不跑的用例。
- `gui_support.gui_app` 同理：非已知抖动的 TclError 不重试直接抛。
- **建窗必须走 `gui_support`（`gui_app` / `build_with_retry`），不许在用例里手写 `except TclError: pytest.skip(...)`**：同进程的 Tcl 抖动由它自动重试（`TK_RETRY_ATTEMPTS` 次内成功就继续跑，耗尽才以"tk 环境不可用(重试 N 次)"跳过）；手写的旧样板一次抖动就丢一条用例（2026-10-16 Windows 报告：一条 e2e 用例 14ms 被 skip，绕过了重试，同用例在 Linux 上是过的）。守卫：`test_integration_cases_do_not_bypass_the_retry_with_a_raw_skip`。

与 Tk 无关的跳过不受影响。守卫：`tests/unit/test_gui_retry.py`（含反向守卫：应用侧读文件失败不许被当成环境问题）。

### 界面用例收尾：根要拆干净，后台线程也要放掉

两条不变式，缺一条都会让**后面的**用例遭殃：

- **不得留下 Tk 根**：`_default_root` 上挂着不能用 / 没拆干净的解释器时，后面每条用例贴图都报"图片不存在"。收尾做验证并补救，断裂按失败报。
- **不得攒下后台调度线程**：`BackgroundScheduler()` 构造即 `start()`，释放入口必须是 `destroy()` 也会走的幂等 `ArchiveApp._release_background()`。守卫：`test_gui_roots.py::test_a_real_backend_does_not_outlive_its_window`（故意绕开收尾、只走产品关窗路径）+ 收尾里的**相对基线**判据——同进程里非 GUI 用例也会建真后端，绝对判据会把别人的存量算到当前用例头上。
- **会话末一个都不许剩**：上面那条相对判据只对"自己建了窗口"的用例生效（没有窗口可归因就不判），**没有窗口的用例**因此是盲区——`test_run_gui_reports_every_config_state` 用替身窗口跑了三次 `run_gui`（替身的 `mainloop` 就地返回，而真窗口那条路是"关窗时释放后台资源"），三个后端各留一个 `APScheduler` 线程一路活到会话末，直到它们挂住别的东西（2026-10-16 的 Windows 片：那条用例卡到 60s 超时上限，之后的用例与覆盖率一起没了）。落点在 `tests/conftest.py`，两层：
  1. **逐条用例判**（`pytest_runtest_makereport` 的 teardown 阶段）：这条用例进出时多出来的线程在 2s 内没退回去 ⇒ **把这条用例的报告改成失败**，失败信息给出线程清单与两个出口（`shutdown()` / `close_apps`）。只在"看出变多"时等，干净用例一次都不等；已经红了的用例不补刀（收尾夹具自己报的现场更具体）。
  2. **会话末兜底**（`pytest_sessionfinish`）：还活着的就点名"进出时存量变多的用例"、把会话判红，并**写一条 broken 结论项**（`gui_support.write_leak_result`）。
     第 2 条必须写进报告：**会话级不变量不属于任何一条用例**，只喊日志 + 改退出码就会出现"红的作业配绿的报告"——2026-10-16 run 38777794002 就是这么演的（质量作业里 `pytest tests/performance` 退出码 1，而报告里 14 条性能用例全绿、性能结论项也写着"通过"；漏点是 `tests/performance/` 里两条只为量一次而建的真后端，现在都自己 `shutdown()` 了）。举证的量法是"该用例自己收尾"，所以真有替身/裸后端的新用例要自己 `shutdown()`。

### GUI 用例必须真的跑起来（skip 是有代价的）

"Tk 起不来"被当环境问题处理的代价：真 Tcl 故障伪装成一片 skip，GUI 用例占覆盖率很大一块（实测模拟坏 Tcl：全量 769 passed / 68 skipped，覆盖率 67.88% < 门槛，且失败信息看不出原因）。所以 CI 在 Windows 与 macOS、pytest 之前显式自检：

```shell
uv run python -c "import tkinter; root = tkinter.Tk(); root.destroy(); print('Tkinter OK')"
```

坏掉时非 0 退出直接把原因抬到表面（Linux 的显示环境由 `xvfb-run` 提供，不重复检查）。真发生过的一次：uv 托管的 standalone 解释器靠自身目录的 tcl 数据文件定位 Tcl，副本陈旧/不完整就报 `Can't find a usable init.tcl`（astral-sh/uv#7036）。**不必每次重装**（一次 30MB × 9 个作业）：先普通安装 → 自检（continue-on-error）→ 失败才 `uv python install --reinstall 3.12` + `uv sync --locked` + 再复检一次（这步没有 continue-on-error，修不好照样红）。macOS 片自 2026-10-05 换 `setup-python` 的 python.org 构建 + `UV_PYTHON_PREFERENCE=only-system`，见 [testing-ci.md](testing-ci.md) 的「macOS 原生崩溃」一节。

## 2. 时序与量法

界面度量不与首帧时序赛跑，断言等的是不变量本身而不是固定秒数。

### GUI 布局用例不要和"首帧时序"赛跑

- `winfo_width()` 首帧往往只有 1，且可能不再有尺寸变化事件补救。判定"布局停在上一次宽度"这类回归**用假控件**（只实现 `_text` 与 `winfo_width()` 的替身）验看门狗本身，真窗口那条只跑真布局。
- 断言"当前宽度下该截成什么"别写死像素/字符数：用同一个 `fit_text(full, font, label.winfo_width())` 生成期望值比**文本**，像素只卡缝隙上限。
- **重裁必须一定会发生且不靠等秒**：容器事件（滚动区 `<Configure>`）走延后 60ms 的**合并**式排队（已排过就不再排、不撤销；触发源可能连续时必须合并不是推后——Xvfb/CTk 的连续尺寸事件会把"取消重排"式 debounce 无限推后）；标签自己的 `<Configure>` 兜底（内宽变了外宽没变时容器不发事件）。契约守卫：`tests/unit/test_ui_scheduling.py`（替身控件驱动真实调度与裁剪，不建窗）。
- **用例不和延迟赛跑**：断言前先等**不变量本身**成立（显示文本 == 该宽度下的 `fit_text` 结果，见 `_wait_until_the_name_fits`，超时才失败），不泵固定时长。

### 列表里的文字"放得下或带省略号"：判据与六个坑

判据两条：① 每个文本控件的每行都放得下（多行按行分别量）；② **夹具文本必须长到"必须裁"且断言真的出现省略号**（演示数据名字都短，量出的"0 处截断"是假的；`test_gui_text_fit.py` 专门配长文本后端）。

六个坑：

- `wraplength` 断不开没有空格的 CJK 文本（Tk 只在空格断行）——要先断行再补省略号得自己算：`fit_text(text, font, width, max_lines=N)`（`ui/textfit.py`）。
- 裁剪宽度取**被拉伸的容器**不取标签自己（`pack(side="left")` / `grid(sticky="w")` 的标签宽度=文字宽度，按它裁会越裁越短）。共享实现 `widgets.track_fit(容器, 标签, …)`：按容器宽裁、宽变重裁、没变不动。期望值用同一个裁剪函数按实测宽算（比文本不比像素），前提用**纯拉丁**长文本。
- 量不到先看那块区域**在不在清单里**（曾漏的是面板从不在 `_areas` 清单、标签也没走 `fit_label`——判据本身没问题）。
- 会自己换行的标签按断行后的宽度判（比 `winfo_reqwidth()`，不比整段文字宽——Tk 已按 wraplength 断行，拿整段比会假红；`_needed_width()`）。
- **CTkLabel 的 wraplength 是逻辑像素、`winfo_width()` 是物理像素**（CTk 会把 wraplength 乘窗口缩放，125% 屏上写 284 内层拿到 355）——写之前过 `widgets.wrap_budget(window, width)`；inset/minimum/initial 这类设计尺寸本来就是逻辑像素不要换。
- 行/卡片会被重渲染销毁（`_render_games` 异步落地、延后 60ms 的 `_schedule_list_sync` 都是先 destroy 再重建），"等一会儿再量"的引用必须当场重新取（`_live_row` 先 `winfo_exists()`），"贴右"这类由延后任务算出的不变量写成"等到成立"。反过来，**要量"刚渲染/刚设的状态"就别在中间泵事件**（悬停判定：`_set_hover(None)` 后立刻读）。"夹具必须长到被裁"也别靠时间：after(60ms) 的重裁在 CI 上可能还没跑，量之前 `_flush_delayed(app)` 直接调一次。

## 3. 尺寸与配色的判据

窗口尺寸、间距刻度、按钮配色、对比度与卡片行样式各有数值判据。

### 窗口尺寸的判据：屏高用替身，量之前先让窗口真的布局出来

给屏高/屏宽装替身（monkeypatch `tkinter.Misc` 的 `winfo_screenheight` / `winfo_screenwidth`），在 768 / 900 / 1080 三档把所有窗口与对话框量一遍（`test_gui_sizes.py`）。判据两层别合并：**硬线**（所有窗口：窗口+标题栏不出屏幕，标题栏按 48px 自折算——`winfo_height()` 是客户区）；**舒适线**（只对模态弹窗：≤ 屏幕可用高度 80%；工作区窗口各有最小尺寸只守硬线）。

坑：

- 模态对话框在"被替换掉的 `wait_window`"里量时窗口还没真正布局（子控件还是初始值；`update_idletasks()` 不够，要完整 `update()`）。"内容真的能滚"用 `canvas.yview()` 可见跨度 <1 判，不拿两个 `winfo_height()` 相减（后者是被污染读数）；配套断言"正文第一行挂在滚动区里"。正文第一行别用"控件树第一个 CTkLabel"（实测是空文本装饰标签且不在滚动区）；认有文本的标签 + 祖先里有带 `_parent_canvas` 的控件（CTkScrollableFrame 的 Tk 侧直接子控件是内部容器，isinstance 找不到，按 `_parent_canvas` 特征、广度优先取最外层）。
- 构造时的屏高替身要给一个**装不下设计尺寸**的值（768），否则"构造里有没有用尺寸策略"验不出来。
- 舒适线的 chrome 高度有平台差异（同一份代码 Linux 比 Windows 高 7px，正文顶格的对话框只在 Linux CI 超线）：`_present()` 里按实测 `winfo_reqheight()` 超线收多少（`_clamp_to_comfort_line`，收到 `_DIALOG_BODY_MIN` 为止，正文区自己滚）。正文区要**递归**找（批导对话框套在卡片容器里）；守卫要把场景搬到**本机也会溢出**的屏高（`_CLAMP_SCREEN=700`），否则本机永远绿。
- **单位：屏高替身是物理像素、窗口尺寸是逻辑像素**（CTk 写 geometry 会乘窗口缩放；不换算就是 125% 屏上"窗口出屏"假红的来源，见 `widgets.window_scaling`）。同族判据要**允许"最小尺寸装不下"**（主窗口 720 下限在高 DPI 矮屏无解——逐条查策略：设计尺寸 / 屏幕−安全边距 / 最小尺寸取最大，不拿窗口比屏幕）；舒适线也有**够不着**的时候（正文已压到 `_DIALOG_BODY_MIN` 仍超线）：口径是"正文区已在下限 + 整窗不出屏"。

### 间距/圆角/字号的判据：页面只许用刻度上的值

把界面模块源码读成 ast，统计 `padx` / `pady` / `corner_radius` / `CTkFont(size=…)` 取值。刻度收在 `src/archive_management/ui/metrics.py`（间距 10 档：12 以内步长 2、以上步长 4）与 `typography.py`（字号 9 档）。守卫是**静态的**（`tests/unit/test_ui_metrics.py`，不启动界面）：① padx/pady 整数必须在间距刻度上；② corner_radius 必须是圆角档位（RADIUS_NONE / RADIUS_PILL / RADIUS_SM / RADIUS_MD / RADIUS_LG）；③ CTkFont(size=…) 必须是字号阶梯；④ **兜底**：扫到的文件数与站点数必须与登记数字相等（刻度模块改名、glob 写错、kwarg 改名都会让"0 处违规"空转）。

注意三条：**具名常量是允许的出口但要留注释**（量出来的缩进没法落刻度时写成模块级常量；出口的代价由"站点数相等"兜住——把一批数字挪进常量，站点数会掉、测试立刻红）；**改值按角色改不取最近的数**（取值理由写在 metrics.py 模块文档）；**收敛是视觉变更，必须重抓截图人工过一遍**。

### 按钮配色的判据：底色必须是调色板的 token，且危险色只有一处定义

建窗遍历控件树的 CTkButton，读 `cget("fg_color")` / `("text_color")` / `("state")`，把调色板颜色值反向映射成 token 名逐条断言（`test_gui_styles.py`）。判据：

1. 每个按钮底色必须是某个 token——读到 CTk 默认色对 `['#3B8ED0','#1F6AA5']` 就说明从没被上过色。
2. 同一**父容器**里至多一个主色按钮（按父容器分组不是按窗口：主窗口是多分区窗口）。
3. 「取消」「关闭」永远是次色。
4. **危险色（含 danger_soft）按钮集合 == 登记表**（集合相等不是包含：少了红、多了也红）。
5. **破坏性动作颜色按回调名判不看文案**：command 可读（`button.cget("command")` 返回原回调），取 `__name__`，带 delete/remove/purge/erase/trash/unlink 的必须落在 `widgets.DESTRUCTIVE_STYLES` 或禁用态——拦的是将来（新增删除按钮穿主色自动红）；第 4 条补位拦"匿名回调的确认按钮误穿危险色"（lambda 天然豁免第 5 条）。颜色→样式是反推的（遍历 BUTTON_STYLES 与 button_colors 对比），widgets 改配色这里跟着变。兜底：回调名带删除词的实测集合与 `_DESTRUCTIVE_HANDLERS` 比相等。

实现纪律：**危险色只有一处定义**（`widgets.button_colors(palette, style)`；`paint_button_style()` 供不登记重绘的按钮共用；单测钉"登记式与就地创建的按钮取色一致"）。模态框在 `wait_window` 替身里量、非模态弹窗（`info_dialog` 不调 `wait_window`）建完直接量——否则那个界面一条都量不到（兜底断言"登记表每个界面都量到了"）。**空库那一轮别跳过**：主窗口 kit.apply 的调用都在动作里，空库首启时顶栏/状态栏/页面容器停在默认灰、按钮默认蓝，只有空库能看出（有数据时 `_load_first_game` 顺手刷对）——判据分两条：逐界面量按钮 + 另建空库量"顶栏/页面容器/状态栏恰好三个调色板色、树里没有看得见却穿主题对色的控件"。

### 重要功能键的配色索引：按回调名登记"允许的性质"

与破坏性动作同一手法扩到"重要功能"：`test_gui_styles.py` 的 `_IMPORTANT_ACTIONS` 登记**回调名 → 允许的性质集合**，反推出的性质必须落在集合里；每个登记项至少一条可用态观察（禁用色反推不出本色）。两点：同一功能会有两个入口两个档位（登记的是集合，写窄并注明理由）；**清单必须能被兜底断言检验**（拼错/改名的回调名对着空白恒绿）。

### 对比度判据：三档阈值 + 装饰性描边的边界

"这行字看得清吗"必须能算出数值（`ui/contrast.py`：WCAG 2.1 相对亮度与对比度，纯算术不碰 Tk；阈值与逐对登记表在 `tests/unit/test_ui_contrast.py`）：

| 对比场景                           | 阈值（WCAG AA） | 覆盖的 token（两套主题同表）                        |
| ---------------------------------- | --------------- | --------------------------------------------------- |
| 正文 / 表格文字 on 背景            | ≥ 4.5:1         | text / text_soft / text_muted × bg / surface / card |
| 大字号 / 图标 / 按钮文字 on 按钮底 | ≥ 3:1           | text on primary / secondary / danger 底             |
| 交互组件描边与状态指示             | ≥ 3:1           | primary / danger 描边 on bg / surface / card        |

守卫逐对 parametrize（两套主题各量一遍），失败信息直接给两个 token 名与实测值。兜底四条：每个文字 token 都必须被判过、主要表面色都当过一次底色、登记规模钉 死、两套主题的 accent/danger/text_muted/text_disabled 不许同值。

坑：**探针量表要比守卫登记表更全**（探针也要跟着调色板走）；**别把装饰性描边塞进 3:1**（"不要求"也要登记：1.2:1 下限+理由）；**"不要求"与"要求"写在不同档位里就要拆 token**（输入框与面板曾共用 border，拆出 input_border；候选色要对全部表面色量、先量 CTk 默认 border_width 才动手；守卫：`tests/unit/test_ui_input_border.py` 的 AST 完整性守卫 + `test_gui_theme_repaint` 切主题后单挑输入控件量）；**悬停底色也是文字底色必须一起登记**（漏登记时悬停态文字对比度从来没被算过）。

### 卡片/列表行的判据：选中 > 悬停 > 常规，只有一条规则

可点可选中的卡片与列表行（详情页备份卡片、主页列表行/海报卡、发现页候选/目录行、管理窗口位置行）用同一套语言，收敛到 `widgets.card_surface_colors(palette, selected=…, hovered=…)`：选中 → accent_soft 底+描边（刻意不用实心强调色）；未选中悬停 → card_hover 底+card_border；常规 → card 底+card_border。判据两半（`tests/unit/test_ui_widgets.py` + `tests/integration/test_gui_home.py`）：纯函数四组合 × 两套主题；真驱动 `_set_hover` 看重绘，且断言**卡片与子控件都绑了 `<Enter>`**（只绑卡片，鼠标移到文字上反馈会闪掉）。

两个坑：**CustomTkinter 重写了 bind，查询式 `widget.bind("<Enter>")` 永远返回 None**，真绑定在内部 canvas 上——断言接线要读 `widget._canvas.bind("<Enter>")`。**悬停不能顺手写成全量重绘**：只重绘受影响的两张并把 `_hover` 在列表重建时清掉（否则指向已销毁卡片，`bad window path name`）。环境差异两条：`_set_hover(id)` 后**立刻读**（延后重排会把 `_hover` 复位；CTk 的 configure 当场生效不用 pump）；量"其余卡片常规底色"前显式 `_set_hover(None)`（CI 上鼠标可能正好压在卡片上）。

## 4. 可用性与交互的判据

禁用态、键盘可达、焦点反馈与分支图交互的判据。

### 可用性的判据：禁用必须看起来禁用，忙碌期间不许有"点了没反应"

建窗驱动到目标状态，遍历 CTkButton / CTkRadioButton / CTkCheckBox 读 state 与各颜色 cget（`test_gui_states.py`）。四条：

1. **禁用态必须压暗**（底/描边/字色都换禁用色；只 `configure(state="disabled")` 不重绘的按钮禁用后仍然亮着）。落点：`widgets.paint_button_state(button, palette, style)` + `UiKit.repaint_button()`；**主题重绘也走这条**，否则切主题把禁用按钮画回亮的。
2. 单选/复选框禁用字色必须来自调色板（不给就落到 CTk 主题的另一个灰阶）。
3. 不许有"死了的控件"：可点按钮必须有回调；单选/复选必须绑 variable。
4. **忙碌期间"会启动长操作"的按钮必须禁用**，按**回调名**判不看文案（新增走 `_submit` 的入口忘了置灰自动红）。

兜底：清单里登记的每个长操作回调都必须真的在测量里出现过（防清单对着空白恒绿）。

### 键盘可用性的判据：Tab 走得通、焦点看得见、Esc/回车接得上

CTk 的实测缺口（Tk 8.6 + CTk 6.0）：按钮/单选/复选/开关画在 Canvas 上且内层 takefocus 是空串、聚焦时 border_color 不变、Canvas 无空格/回车绑定。补法在 `ui/keyboard.py`（类级补丁，随 widgets 导入安装）；守卫 `tests/integration/test_gui_keyboard.py` 逐界面量：

1. **Tab 链**：可交互控件 takefocus 让 Tk 走得到（按钮类显式 1），禁用态摘出去（0）。
2. **Tab 顺序 = 视觉顺序**：`tk_focusNext` 按堆叠顺序（≈创建顺序）走，与 grid row/column 无关——量法是比"创建顺序"与"按 (row, column) 排序"。
3. **焦点可见**：聚焦换专用环形色（3px）；主色/危险色实底换对应软底（`widgets.FOCUS_STYLE`，颜色仍在 button_colors 一处定义）；失焦时描边/底/字/悬停全部原样还原（存下来再还原）。"分得开"是数值判据：环对聚焦后底色 ≥ 3:1（`keyboard.FOCUS_RING_MINIMUM`，无头守卫 `tests/unit/test_ui_keyboard.py`）。

坑（挑关键的）：**合成 `<FocusIn>` 不触发绑定**（要量焦点环用真焦点：focus_force 拿、挪走拿 FocusOut；旧实现恒用强调色 + 断言只比"≠ accent"就是假绿）；**失焦还原要先失焦取基准**（对话框打开即定焦，不先放下会得到颠倒基准）；**焦点黑洞控件必须真的被映射且在可见区内**；**不要对窗口本身 focus_force()**（顶掉子控件焦点）；**不要用 tk_focusNext 当量具**（同进程反复建/销窗口时偶发加载不了 Tcl/Tk 库数据）；**类级补丁要对替身控件宽容**（能力探测 + suppress，遇没有该能力的对象安静跳过，否则弄红别人的单测）；**CTkRadioButton 没有可读 border_width、CTkOptionMenu 两样都没有**（`keyboard._border_options` 能读几样读几样；塞同一个 suppress 被中断会 KeyError，描边永远停在焦点环色上）。

### 焦点态不许改变"这颗按钮是什么按钮"

聚焦换色上线后 CI 一次 8 条红，根因一个：**换色把"聚焦态"变成了识别按钮性质的依据**。CI 与本地时序相反（CI 上窗口一建出来定焦就生效，本地常被抢焦）。要点：

- **产品侧真缺陷**：靠"实测底色 == accent/danger"找主按钮的实现，聚焦换软底后找不到 → 回车确认失效。修法：make_reachable 把没聚焦时的原样存到 `widget._focus_saved`，`keyboard.resting_fill(widget)` 是唯一入口。
- **量配色的用例量之前必须先失焦**（`_defocus(window)`），否则量到聚焦态：危险色记成软危险、主色"少了一颗"。
- **量之前等控件树长稳**（`_stabilize` 比形状最多 8 轮——延后重排会在量到一半时整批换控件，对已摘下控件的 focus_force 有"最后焦点"却无 FocusIn）。
- **一次量不到不算数，也不放过真缺陷**：`_focus_and_read` 试三次；三次画不出分两种——对照控件（焦点黑洞"煤鸟"，自己也接焦点环）画得上说明路通、是**真缺陷**报红；煤鸟也画不出才是环境（记 `_UNMEASURED` 附在失败信息里）。
- **把 CI 时序搬回本地的办法**：临时插件包住 `dialogs._present` 再跑 3 轮 `update()`，本地逐条搬回假红、修完确认全绿、删掉插件。

### 分支图的判据：item 账目 + 几何 + 漫游

- **item 账目**：画布 item 数 == 框 + 连线 + 文字（每框两条）+ 折叠标记（每个有孩子的框一个），与 `canvas.find_all()` 长度一致（渲染基准主判据：400 节点链 = 1998，折叠根后 = 4，`tests/performance/test_backup_scale.py`）。
- **几何**：最深一层与最右一列不越 scrollregion；父框中心 = 首末孩子中心中点（纯函数穷举，`tests/unit/test_ui_tree_layout.py`）。
- 交互三条（`tests/integration/test_gui_branch_graph.py`）：**箭头按需显示且"露着哪几条"要量控件本身**（读 `winfo_manager()`，不是 xview()/yview() 的跨度——后者只答"那边还有没有内容"；与按需滚动条同一条纪律）；**拖到框上不能变成选中**（按下先命中测试，松开位移 < 4px 才算点击）；**坐标要换算**（事件给控件坐标、布局给画面坐标，过 canvasx/canvasy）。

实测补过的缺口：箭头在布局变化后不重判（`<Configure>` 触发 `_refresh_arrows`，只在状态真变了时动控件）；按键绑定把 Event 追给回调（签名收下 `_event`，守卫走真实绑定 `event_generate("<Right>")`）；Shift+d 的 keysym 是 `D`（WASD 大小写各绑一条）；箭头按钮要在画布**之后**创建（Tk 堆叠顺序=创建顺序）。一个画布里多个悬停目标时提示由视图自己管（`widgets.HoverTip`：调用方给文案+屏幕坐标，set_hovered 挂/收、set_items 直接收）；画布焦点环用自带 highlightthickness/highlightcolor（不在 keyboard.PATCHED_TYPES 里；环对底色 ≥ 3:1）；**收起的控件拿不到焦点**（place_forget 后 focus_set 无效——"箭头真收起来了"可用"焦点给不到它"再钉一次；反过来合成按键只送给有焦点的控件，event_generate 前先 focus_set）。

### 折叠/展开的判据：空折叠 == 旧行为、藏起来必须留痕

折叠拆"纯函数 → 绘制 → 交互"三层（`tests/unit/test_ui_tree_layout.py`、`tests/integration/test_gui_branch_graph.py`）：

- **空折叠必须逐字段等于旧结果**（最重要）：不传 / 空集合 / 不存在的 id / 叶子——四种输入铺出的图一样（既有几何判据与视觉基线都建在现在这套铺法上）。
- **折叠 = 子树不参与布局**：宽度只算一个框、不产出出边、高度只看剩几层；后代数含间接后代；藏在另一个折叠节点里的折叠不算数。
- **藏起来必须留痕**：说明行写 N 个后代，藏着选中项或当前节点各自标出（含选中/含当前）并变强调色；判据把"看得见的说明行 + 被裁时的悬停全文"合起来看。
- 三条命中各干一件事：标记折叠/展开、框选中、空白平移；按标记上既不平移也不选中。
- **折叠不改选中**（反向守卫）。
- **折叠后视口对齐到刚点的框**（scrollregion 变小 Tk 会把偏移夹回）。
- **折叠集合不持久化**：set_items 时与现有节点取交集；切视图不清。

两个坑：测试 helper 的坐标换算方向别写反（没滚动时恰好相等，一滚动就点空）；折叠痕迹判定先问"这个框是不是折叠着"（否则每个有后代的框都被当成藏着东西）。

## 5. 文案的判据

文案要完整到达用户：占位符已替换、截断可回看、空状态有去处、反馈给下一步。

### 文案完整到达用户的判据（未替换占位符 / 截断可回看 / 空状态）

- **静态（`tests/unit/test_i18n_copy.py`）**：① 遍历 `src/**` 的 `tr(...)` 调用比对字面键占位符与实参名（少传一个界面直接显示 `{name}`；键是变量或 \*\*kwargs 的按"看不出来"跳过——宁可漏报不猜）；② 中文文案里「」引用的面板/按钮名必须在文案表真实存在；③ 中英两份占位符名一致。
- **运行时（`tests/integration/test_gui_copy_quality.py`）**：驱动界面状态收集每个文本控件实际显示的字，断言无 `{}` `}` 残留、被省略号截掉的必须挂悬停提示、自己折行的末行不许只剩标点、概要卡统计值不许退化成一个符号。

两个坑：**"引用名存在吗"不能拿全表做子串比对**（名字本来就写在自己那条文案里，要比"自己以外的"文案）；**夹具必须真的造出必须截断的文本且断言造出来了**（夹具里有 >= 3 处省略号的前提断言）。顺带记一个真 bug 形状：进过"按可用性重绘"登记表的控件被渲染重建销毁后，下一轮重绘会碰不存在的控件（销毁时摘登记 + 重绘时跳过已销毁项）。

### 反馈文案的判据：失败给下一步、结果给去处与计数

机制（忙碌态/取消按钮/FeedbackKind）与文案是两回事，判据落在文案（`tests/unit/test_ui_feedback_copy.py`，纯 i18n 数据）：

- **error.\* 每条都要给"下一步"**（两套语言都命中信号词：请/重试/刷新/检查/换一个/try again/…）——拦"只把 OS reason 原样弹出"。
- **登记的 result.\* 必须带占位符**（{file}/{path} 去哪儿了、{count}/{files}/{size}/{skipped} 几条）。
- **兜底**：没进登记表的 result._ 必须在豁免清单写明理由；两套语言键一一对应；信号词表本身要真的命中全部 error._。

### 计数与时间的判据：一行一页、不带裸数字

每个页面底栏只占一行，计数写成"名词 + N + 量词"、并列用 · 分隔，不许裸数字。守卫：`test_gui_discovery.py` 断言"探测结果页第二行必须为空"并与 `tr("discovery.counts_candidates", …)` 逐字对齐。时间统一 `models.format_stamp`（2026/09/24 20:11）、空值统一 —，由 `test_ui_models.py` 与 `test_gui_lifecycle.py` 兜着。
