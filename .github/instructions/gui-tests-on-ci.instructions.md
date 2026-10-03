---
applyTo: "tests/integration/test_gui_*.py,tests/gui_support.py,tests/unit/test_ui_*.py,tests/unit/test_dialogs.py,src/archive_management/ui/home_page.py,src/archive_management/ui/dialogs.py,src/archive_management/ui/keyboard.py"
description: "写或改 GUI 端到端用例(以及它们量到的那几处界面代码)时的实测坑: CI 上的窗口尺寸不由用例决定、字体度量会变、合成键盘事件走焦点窗口(焦点可能压根不在对话框里)、尺寸夹取要看请求高度并收敛、延后的布局任务要与控件一起撤、行/卡片会被重渲染销毁(等待之后必须重新取引用)、平台默认值(-topmost/尾随空行计高)必须显式写掉、父窗口还没落地时算出来的居中/相对位置是错的(冷启动首次打开). 遇到"本机绿、CI 红(尤其只有 Windows 或只有 Linux 红)"的布局/尺寸/文字截断/按键失败时先读这篇。"
---

# GUI 端到端用例在 CI 上: 十条实测出来的坑与写法

本仓库的 GUI 用例跑在真 Tk 上(本地 Windows、CI 的 ubuntu(xvfb) + windows runner), 下面每条都是
**实测踩过**的: 失败形态都是"本机全绿、CI 上某几台红", 而且红的那几条看起来像产品 bug, 其实是用例
的假设不成立(2026-09-30 那一轮: 7 条 GUI 端到端失败里 6 条属于这一类)。

改这批文件前先读完本文。

## 0. 先分清楚"产品行为"与"用例假设"

CI 红的第一件事是判断**这条断言在测什么**。同一个断言在两种环境下含义不同:

- 测的是**产品** —— 那就改产品(并加守卫);
- 测的是**环境** —— 那就把它写成与环境无关的形式, 或者带实测数字 `pytest.skip`。

下面几条都属于后者, 因为它们量的东西(桌面大小、字体度量、焦点归属、事件时序)在 CI 上与开发机不同。

## 1. 窗口尺寸不由用例决定(Windows/macOS 上尤其)

- **Windows/macOS runner 有窗口管理器**: `geometry("1400x900+160+120")` 会被压回桌面。GitHub 的
  Windows runner 桌面只有约 **1024x768**, 减去任务栏更矮 —— 实测 2026-09-30: 记住 1400x900 的窗口
  在那边打开成 **1200x749**(749 = 768 − 任务栏, 1200 = 主窗口的 `WINDOW_MIN_SIZE`), 于是
  `assert (1200, 749) == (1400, 900)` 红。
- **Linux CI(xvfb)没有窗口管理器**: 任何尺寸都能设, 所以"Linux 绿、Windows 红"是常见组合。反过来
  "窗口长不大"这类假设在 Linux 上也不成立 —— 别把平台的偶然当成规格。
- 正确写法(见 `tests/integration/test_gui_sizes.py` 的几何用例):
  - 期望值按**规格**从**真桌面**算出来, 而不是写死常量, 也不要调用被测的那个纯函数:

    ```python
    screen = (int(app.winfo_screenwidth()), int(app.winfo_screenheight()))
    size = (
        max(WINDOW_MIN_SIZE[0], min(saved.width, screen[0] - SCREEN_MARGIN)),
        max(WINDOW_MIN_SIZE[1], min(saved.height, screen[1] - SCREEN_MARGIN)),
    )
    ```

  - 位置只在"真桌面装得下这套几何"时才断言, 否则跳过位置断言(窗口管理器说了算):

    ```python
    if x + width <= screen_w and y + height <= screen_h - TITLE_MARGIN:
        assert actual_position == position
    ```

  - "读-改-写"(关窗时把几何写回配置)这类判据**与桌面无关**, 断言 == 窗口**实测**的几何:

    ```python
    app.geometry("1500x820+240+150")
    moved = (winfo_width, winfo_height, winfo_x, winfo_y)  # 关窗前量
    app._on_close()
    assert load_config(path).window.geometry() == moved
    ```

  - 需要"更宽就能多显示几个字"这类**相对**结论时: 先量实际宽度, 没变(或变化 < 100px)就
    `pytest.skip` 并打印实测宽度与屏幕宽度 —— Xvfb 上检查照跑, Windows/macOS 上诚实跳过。

- **不要** `monkeypatch.setattr(tkinter.Misc, "winfo_screenwidth", ...)` 之后去断言**真实窗口**的
  几何: 替身只骗得了应用自己的算术, 骗不了窗口管理器, 那种断言测的是窗口管理器。

## 2. "太长了"不能靠字符数, 要靠字体量出来的宽度

缺中日韩字体的机器(Linux 镜像、裸 runner)会把汉字量成**近乎零宽**: 一串 66 个汉字在那里"放得下",
于是"夹具的长文本必须长到被裁"这个**前提**恒不成立 —— 判据退化成空断言, 而且只在 CI 上红
(实测 2026-09-30: `test_gui_text_fit` 与 `test_gui_branch_graph` 三条都是这样)。

- 长文本夹具**一律带一段长拉丁串**(拉丁字形任何字体都量得出宽度):

  ```python
  _LONG_LATIN = "-VeryLongBackupTitleSample0123456789" * 4
  _LONG_TITLE = "超长的备份标题示例" * 3 + _LONG_LATIN
  ```

- 像素容差也别拿汉字当刻度: `font.measure("测")` 在缺字体的机器上是 0, 容忍度会退化成 0 →
  `max(font.measure("测"), font.measure("W"), 8)`。
- 更稳的写法是**比文本不比像素**: `assert shown == fit_text(full, font, width)`(与字体无关, 且真能
  抓到"布局停在上一次宽度"的回归)。
- 前提要写成**强制断言**(`assert font.measure(ascii_only) > width`), 不要写成 `if ...: assert ...`
  —— `if` 那种在前提不成立时**一条断言都不跑**, 用例永远绿。\* **裁剪预算的单位要一致**: `winfo_width()` 是物理像素, 而内衬/`padx`/`wraplength` 都是设计
  (逻辑)像素 —— 从物理宽度里直接减一个设计值, 125% 的屏上都会多留出“内衬乘缩放”那一段
  (实测 2026-10-03: 内衬 24 实际占 30, 于是标签里显示的那段比“按标签宽度裁”多一个字符,
  同一条用例在 CI(100%)上绿、本机(125%)红)。统一约定见 `widgets.scaled_px` /
  `wrap_budget`: 设计值过 `scaled_px`, 量出来的值直接用。

## 3. 合成键盘事件送给"焦点窗口", 不是随便哪个控件

`widget.event_generate("<Return>")` 在 Tk 里会被转到**焦点窗口**上: 焦点不在这个对话框里时事件就
白丢了。实测 2026-09-30: 送给主按钮的内层画布"什么都没发生", 送给焦点窗口才真的走到产品的绑定
(窗口级 `<Return>` → `primary_button` → `invoke`)。

- 先让窗口真的**映射**出来(`deiconify` + 若干轮 `update()`, 未映射的窗口收不到按键), 再量当下的
  焦点并把事件送给它:

  ```python
  def _key_target(app, window):
      target = app.focus_get()
      if target is None or not str(target).startswith(str(window)):
          return window
      return target
  ```

  不要自己 `focus_set` 一个控件然后假定事件会到它身上 —— 用例该走的是"用户打开对话框后直接按键"
  那条路。

- `Esc = 取消`这类判据**只看返回值会退化成空断言**: "键没接上"与"接上了并取消"都是 `False`。再断言
  一次**窗口真的被销毁**(`assert not window.winfo_exists()`)。
- **但要先确认那个焦点到底在不在这个对话框里**: 一次按键都没有反应时, 很可能是对话框**根本没拿到
  焦点**(无窗口管理器的 Xvfb 上没人会把输入焦点交给新窗口), 而不是接线断了 —— 实测 2026-09-30 的
  Linux CI: `(False, False)` 就是这个; 而同一份代码在本机(带窗口管理器)绿。所以驱动按键之前先量
  一次焦点, **不在对话框里就把焦点交给它**(用户眼里它就在最前面):

  ```python
  if not _focus_inside(app, window):
      window.focus_force()  # 无窗口管理器的 Xvfb 上只有它能真的把输入焦点交给这个窗口
      app.update()  # 本机上 CTk 那次重新定焦的 after_idle 要靠它才会跑(实测光 force 不回)
  target = app.focus_get() if _focus_inside(app, window) else window
  ```

- 这一条**可以在本机复现**(不用等 CI): 在生成事件之前 `app.focus_force()`(把焦点抢给主窗口)
  - `update_idletasks()`(**不要**用 `update()` —— 它会把 CTk 排好的重新定焦回调一起跑掉),
    就能在 Windows 上得到同样的 `(False, False)`。把它写成一条**反例用例**
    (`test_escape_and_return_reach_the_dialog_even_when_the_focus_is_elsewhere`), 这条判据就不会再
    演一遍"只有 Linux 红"。

## 4. 延后的布局任务: 等不变量, 别睡固定时长

表头对齐、名称重裁这类是 `after(60ms)` 的任务, 而且常要跑**两轮**才收敛。用例里流逝的墙钟时间很短,
CI 上可能还没轮到 —— 实测 2026-09-30: Windows runner 上"表头与数据行整体差同一个 16px"(正好一个
滚动条宽度)。

- 用"等到不变量成立(有上限)"代替"先睡一会儿再断言", 超时后用实测值报错(见
  `_wait_for_the_columns_to_line_up` / `_wait_until_the_name_fits`)。
- 滚动条出现/消失会改变**滚动区内部**的可用宽度, 而外层的 `<Configure>` 不会来 —— 所以产品侧要在
  "滚动条判定之后"和"行内标签宽度变化时"主动补一次同步, 否则表头会永久错开。
- **别用 flush(手动调一次延后方法)去让断言通过**: 那会把"延后任务到底会不会自己跑"这条性质一起
  洗掉。要 flush 就得同时保留"只泵事件也要收敛"的用例。

## 5. 顺带: `after` 任务要跟着控件一起撤(判断"自己"的那一层要看 CTk 转发)

`CTkFrame.bind("<Destroy>", handler)` 把绑定**转发到内层 canvas**, 所以销毁事件里的 `event.widget`
是那块**画布**而不是 `self.frame`(那个对象不是 Tk 控件)。只认 `self.frame` 的话清理是**死代码** ——
实测 2026-09-30: `cancel_list_sync` 被调用 **0 次**, 挂起的 `after` 任务在控件消失后触发, 往 stderr
留 `invalid command name "..._sync_list_layout"`(报告里会挂到**下一个**用例的 stderr 附件上, 掩盖
真问题)。

- 判据把内层画布一起认(构造时记下 `self._frame_host = getattr(self.frame, "_canvas", None)`), 同时
  **不能无条件取消**: 子控件的 Destroy 会冒泡上来, 那不是"宿主没了"。
- 守卫要驱动**真接线**(把事件喂给 `_on_frame_destroyed`), 只调一次 `cancel_list_sync()` 等于没测
  接线 —— 上面那条死代码就是这么躲过守卫的。
- 同类噪声 `invalid command name "...check_dpi_scaling"` 来自 CustomTkinter 自己, 不是我们的。

## 6. 行/卡片会被重渲染销毁: "等一会儿再量"的引用必须当场重新取

主页有两条整页重建路径: `HomePage.refresh_artwork` → `_render_games`(由**异步数据落地**触发: 封面/译名
补好)与延后 60ms 的 `_schedule_list_sync`。两者都先 `destroy` 旧行再重建 `_rows`/`_row_parts`), 于是
**等待之前抓到的控件引用已经失效** —— 拿它调 `winfo_*` 报的是:

```text
_tkinter.TclError: bad window path name ".!ctkframe2.!...!ctkscrollableframe.!ctkframe4"
```

实测 2026-10-01(Windows 分片 0, `test_long_game_name_does_not_widen_the_list_rows`): 用例先
`rows = list(page._rows.values())` 抓一份快照, 再去"等各列对齐"(最多 3s, 一直在泵事件), 期间那次
重渲染落地 → 后面 `row.winfo_rootx()` 直接抛上面那句。**那句报错与"布局对不对"毫无关系**, 看上去
像控件树坏了, 只会把排查带偏。

- 凡是"等一会儿再量"的地方, 都在**量的那一刻**从 `page._rows` / `page._row_parts` 重新取
  (见 `_live_row`; `_wait_until_the_name_fits` 本来就在循环里每轮重取)。
- 量之前先 `winfo_exists()` 判一次, 把"拿到失效引用"变成一句能照着做的断言("请在等待之后重新取行"),
  别让它以 TclError 的形式冒出来。
- **延后任务算出来的**不变量(如"最后一列贴住右边界")同样要"等到成立", 不能量一次 —— 重建会把它
  重置回兜底值(`_wait_for_the_fixed_block_to_hug_the_right`)。
- 反过来也对: **要量"稳定态/刚设的状态"就别在中间泵事件**。悬停判定就是靠这一点(`_set_hover(None)`
  之后立刻读, 中间多一次 `_pump` 就可能被延后重排复位)。
- 这一条**在本机可以确定性复现**(不必等 CI): 取一行 → 调 `page._render_games()` → 拿旧引用调
  `winfo_rootx()`, 报的就是那句 TclError。守卫:
  `tests/integration/test_gui_layout.py::test_a_row_reference_goes_stale_after_a_rerender`。

## 7. 尺寸/夹取类改动: 一次夹取不够, 还要一起看请求高度

"这个窗口/对话框比屏幕高"这类断言在**尺寸恰好压线**的时候最容易只红一个平台。实测 2026-09-30:
700 高的屏上 Linux 的批量导入弹窗最终 **561 > 560**(限 560), 而 Windows 恰好 560 —— 同一个屏高、
同一份代码。两条教训:

- **不能只看 `winfo_height()`**: 无窗口管理器的 X11 上实测值会晚一拍(它在等 ConfigureNotify),
  于是夹取那一刻量到 560、布局定下来却是 561。`winfo_reqheight()` 是本地立刻算出来的, 没有这个
  滞后 —— 映射之后两个一起看, **取大者**:

  ```python
  height = max(int(window.winfo_reqheight()), int(window.winfo_height()), measured_at_map)
  ```

- **收一次不够就继续收**: 布局不保证 1:1 跟着可伸缩的那个控件走。按上面那步收完之后**重新量**,
  还越线就再收, 设个轮数上限(本仓库是 4 轮)与下限(`_DIALOG_BODY_MIN`), 矮屏上宁可让正文滚也
  不能把内容切掉。
- 这类改动配**替身用例**最快(把 `CTkScrollableFrame` 换成只有 `cget/configure` 的假对象), 关键
  是把"请求高度还是实测高度大"这一维做成构造参数: 一个假设固定请求高度, 一个按"正文之外 + 正文
  区"算(真窗口就是这样随正文伸缩的) —— 后者才能验"收完就收敛"。
- 失败信息里把**请求高度**也打印出来(`请求高度 561; 被切掉的子控件: 无`): 下次再红能一眼分开
  "量法滞后"与"布局真的收不下去"。

## 8. 平台默认值也是"环境": 三处必须显式写掉的东西

有类红与"CI 环境"无关, 而是**平台给同一个写法的默认值不同**。它们在本机(Windows)恒绿, 在
macOS/Linux 恒红, 所以最容易当成产品 bug 去查。

- **macOS 的无边框窗口 `-topmost` 默认读出来是 1**(Windows 上是 0)。所以"浮层没有 topmost ⇒
  读 `wm_attributes('-topmost')` 应当为假"这条判据只在 macOS 上红(2026-10-03 实测)。要守这条
  性质就**显式写一次** `attributes("-topmost", False)` —— 否则"默认值"就是平台契约的一部分,
  只在 Windows 上量到的结论不算跨平台结论。
- **X11 不算尾随空行的高度**: `text=f"{name}\n"` 想让标签"占两行"在 Linux 上没有效果(那台机器
  上 CTkLabel 退回默认高度 28, 而两行名是 39 —— 2026-10-03 的 Linux CI 报 `元信息那一行差 11px`,
  同代码的 Windows/macOS 是绿的)。要让控件占**固定行数**的高度, 用字体度量算出来显式配:

  ```python
  line = measured_font(font, widget).metrics("linespace")  # 物理像素(行高也要过缩放)
  widget.configure(height=ceil(line * lines / window_scaling(widget)))
  ```

  实测 CTkLabel 配的高度**小于**文本自然高度时会自己长高(而不是裁字), 所以不会切到内容。
  行高与文字宽度一样: 拿未缩放的字号量不准(125% 上量 15, 渲染的是 19)。

- **想验"写进去 == 读回来"要先把窗口管理器排除掉**: 这类**单位换算**判据(写逻辑值、读逻辑值)
  在桌面装不下请求尺寸时量到的是 WM 的决定, 而缩放为 1 的 CI 上连"没除回逻辑值"这种错都
  看不出来。写法是先 `app.minsize(1, 1)` 放开下限(这条判据与最小尺寸策略无关), 再用"真桌面
  装得下"的尺寸去请求(见 `test_gui_sizes.py` 的 `_geometry_inside_the_desktop`)。
- 顺带(非 GUI, 但同一类): **macOS 的 `/dev/fd/<fd>` 不是符号链接**(`realpath` 原样返回), 而它
  `isdir()` 又是真的 —— 任何把 `/dev/fd/<fd>` 当绝对路径的记账/校验都会在 macOS 上把**合法**操作
  判成越界(2026-09-30、2026-10-03 两轮安全用例红在这里)。从 fd 反查目录要先问内核
  (`fcntl.F_GETPATH`, 缓冲区必须是**可变**的, 只读缓冲区拿不回结果且失败是静默的), 再退到
  `/proc/self/fd/<fd>` 模板, 最后 `fchdir` + `getcwd`; 并且**拒收"指回 fd"的候选** —— 全问不到
  时宁可返回"认不出来"(让用例响亮地报越界), 也不能记一个看着像绝对路径的东西。

## 9. 冷启动: 父窗口"还没落地"时算出来的居中/相对位置是错的

弹窗的居中用 `parent.winfo_width()` 算, 而**主窗口建好但还没映射**时它给的是布局前的占位值
—— 实测 2026-10-03: 那一刻主窗口 `mapped=False, winfo_width()=200`(恢复成真正的 1500x960 要等
映射), 于是"启动后第一次打开游戏设置"的弹窗偏出中心 **-539px**(第二次开是 0.0px)。弹窗**自己**
的尺寸量到的是真值, 所以单看弹窗什么都看不出来。

- 用"父窗口落地的判据"(已映射 + 几何不再变)当收工条件, 不要按固定时长等(见第 4 条); 而且收工前
  要**多复查几轮** —— 窗口管理器摆位置比映射晚一拍(实测映射瞬间读到 `rootx=0`, 下一个循环才是
  真位置; 只按"这一轮没变"收工会留下 -407px 的位差)。
- **别拿父窗口的 `<Configure>` 当信号**: Tk 会把**所有子控件**的 Configure 一起送进挂在顶层窗口
  上的绑定(实测开一次窗收到 **595** 条, 各种子控件尺寸都有), 其中任何一条"尺寸没变"都会让监听
  提前解绑 —— 真正需要它的那条事件到来时已经没人听了(第一版就这么没修好)。
- 用例侧: 把这一幕写成"建完主窗口**不进事件循环**就开弹窗"(先断言 `not app.winfo_ismapped()`
  —— 那就是这一幕的定义), 再等到父窗口几何稳定(与实现同一个判据)后断言偏差(容差给窗口管理器
  留十几像素)。固定睡眠会在慢机器/整组跑时量到中间态, 于是这条用例先红后绿(实测 -407px)。
  见 `tests/integration/test_gui_sizes.py::test_the_first_dialog_of_a_cold_start_is_centered`。

## 10. 收工前的自检清单

1. 这条断言的期望值是从**真环境**量出来的, 还是写死的常量?
2. 它有没有前提? 前提是**强制断言**且用**拉丁字符**表达吗?
3. 需要"更宽/更长/更大"时, 先量了实际值吗? 量不出来时是 skip 还是假绿?
4. 按键事件送给的是焦点窗口吗? 失败那一侧能不能"因为什么都没发生"而侥幸通过?
5. 延后任务: 等的是不变量还是墙钟时间? 有没有一条"只泵事件也要收敛"的用例?
6. 新加的 `after`/`bind` 在控件销毁时撤得掉吗? 守卫驱动的是真接线吗?
7. 尺寸/夹取: 量的是 `winfo_height()` 还是"请求与实测取大者"? 收一轮不够时还会继续收敛吗?
8. 这条断言在**焦点/尺寸/平台默认值**这几种"环境给的"条件下, 有没有一条能在本机复现的反例用例?
9. 每处测量用的控件引用都是**当下**取的吗? 中间泵过事件的话, 它可能已经被重渲染销毁了。
10. 用到平台**默认值**了吗(`-topmost`、尾随空行计高、窗口能设多大)? 要不要显式写掉它?
11. 位置/居中是在父窗口**已经落地**之后算的吗? 那一刻它可能还是布局前的占位尺寸。
