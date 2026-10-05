---
applyTo: "src/archive_management/ui/**.py, tests/unit/test_ui_widgets.py, tests/integration/test_gui_buttons.py, tests/integration/test_gui_scrollbars.py"
description: 'CustomTkinter 按需滚动条(以及所有"改几何 → 事件 → 再改几何"的循环)引起的界面抽搐与递归崩溃: 定位手法、根因、修法与守卫。'
---

# 按需滚动条的抽搐 / 递归崩溃 / 常驻灰条: 定位与修法

本规范来自两次真实故障:

1. 打开"编辑标签"弹窗后**连点两次"添加标签"**, 界面开始抽搐, 控制台报
   `Exception in Tkinter callback: maximum recursion depth exceeded`;
2. 评审时发现页两个列表、主页列表、定时任务窗口、编辑标签弹窗右侧**永远**立着一条拖不动的
   灰条(内容其实装得下)。

第一类故障会让软件卡死或崩溃, 因此它的守卫用例是 **`@pytest.mark.blocker`**(本地 pre-commit 的
blocker+critical 子集里就会跑到); 第二类是观感缺陷, 守卫按 `minor`。

改动 `src/archive_management/ui/widgets.py` 的滚动条逻辑、或新写"按内容决定滚动条/尺寸"的代码前,
必须读完本文。

## 1. 症状长什么样

- 滚动条在边界上来回闪动, 界面明显"抽";
- 事件循环被塞满: 点击后界面失去响应, CPU 占用上去;
- 控制台刷 `Exception in Tkinter callback`(Tk 回调里的异常不会中断程序, 只打日志 —— 所以
  **测试里看不见、用户只看到界面卡**);
- 递归到 `maximum recursion depth exceeded` 后, 那个回调直接失败(界面状态停在一半)。

## 2. 定位手法: 先把"抽动"变成可数的数字

不要靠肉眼猜"卡不卡"。这几个手段是当时定出根因的关键(缺一个都会猜错):

1. **兜住 Tk 回调异常**: `app.report_callback_exception = lambda exc, val, tb: errors.append(...)`
   —— 否则异常只打到 stderr, 测试与日志里都看不到。
2. **给判定函数套一层计数器**: 把 `widgets.sync_scrollbar` 换成记账包装(生产里 `auto_scrollbar`
   通过模块全局名调用它, 所以打补丁打在模块属性上即可生效)。
   - 修复前: 一次"连点两次"实测 **132 次**判定;
   - 修复后: 同一次操作 **2 次**(正常)。
3. **同时打印"内容高度 / 视口高度 / 判定结果"的时序**, 而不是只看最后一帧:

   ```text
   content=192 viewport=150 -> show
   content=192 viewport=200 -> hide      # 视口被滚动条撑大了
   content=192 viewport=150 -> show
   ...                                    # 内容一直没变, 变的是视口
   ```

   这组读数一眼就能看出"抽动的不是内容, 是视口" —— 只看单帧读数会误判成"内容一直在临界值"。

4. **量控件的高度请求**: `_desired_height` / `_current_height`。CTk 滚动条默认请求 **200px**。

## 3. 根因(四个, 都要修)

前两个管"抽动/崩溃", 后两个管"收不起来"。

- **A. 滚动条把视口撑大了**: `CTkScrollableFrame` 里滚动条与画布**同处第 1 行**, 而 CTk 的
  `CTkScrollbar` 默认请求 200px 高。于是: 显示滚动条 → 那一行被撑到 200 → 视口变高 → 内容又
  装得下 → 收起 → 视口变矮 → 又溢出 → 再显示 …… 内容高度**从头到尾没变**。
- **B. 改几何引出的回调在同一调用栈里重入**: `grid()/grid_remove()` 会引出 `<Configure>`, 而我们
  正是挂在 `<Configure>` 上做判定。没有闸门时, 内层调用会再次改几何 → 再回调 → 递归到崩溃。
- **C. 首次判定读了 `winfo_ismapped()`, 把"未映射"当成"已经收起"**: `CTkScrollableFrame` 建容器时
  **一定**把滚动条摆进布局, 但窗口还没画出来(或所在分区/子页还藏着, 比如发现页是在"游戏库"
  分区里 `grid_remove` 建好的)时, `winfo_ismapped()` 读到的是 **0**。拿它当初值 → 判定认为
  "已收起" → 内容装得下时 `needed == shown`, 于是**什么都不做**, 而滚动条其实一直摆在布局里 ——
  这就是那条常驻灰条。
- **D. `CTkScrollbar._draw()` 结尾会强制刷新事件队列**: 它最后一句是
  `self._canvas.update_idletasks()`, 而画布每次滚动/尺寸变化都会回叫 `set()` → `_draw()`。
  那次强制刷新会把 Tk 里**排队的映射动作**执行掉 —— 刚 `grid_forget()` 掉的滚动条当场又被映射
  回来(实测: 该位置灰色像素 3706; 去掉这句后 0)。它还会跨控件生效: 另一条滚动条的重画同样能把
  这条重新映射回来。

## 4. 修法(七件套, 缺一不可)

1. **压住高度请求**(治 A, 最关键): `_pin_scrollbar_height(scrollbar)` 把
   `_desired_height` 与 `_set_dimensions(height=1)` 都设成 1px —— 行高只由画布决定, 滚动条靠
   `sticky="nsew"` 被拉伸到行高, 外观不变。**"显示"与"构造"两条路径都要压**(`_show_scrollbar`
   与 `auto_scrollbar`)。
2. **重入闸门**(治 B): 判定函数开头 `if getattr(frame, _SYNCING_FLAG, False): return False`,
   整个函数体包在 `try/finally` 里复位标记。
3. **判定记忆用自己记的状态, 且首次判定按"已显示"起算**(治 C): `grid()` 之后控件要等一轮几何
   才真正 mapped, 这中间读到"未显示"就会重复 `grid()`, 于是自激; 而窗口还没画出来时读到 0 又会
   得出"已收起"。因此 `_SHOWN_FLAG` 的初值是 **`True`**(CTk 建容器时一定摆上了), 不读 Tk。
4. **决策不变就不动几何**: 判定结果与上次相同时直接 `return`, 不重放 `grid()/grid_forget()`
   —— 改几何才是引出下一轮事件的源头。
5. **几何事件合并到一次 idle**: `<Configure>` 回调里只 `after_cancel` + `after_idle`, 不直接判定;
   `frame` 与内层 `canvas` 都要绑(`add="+"` 不覆盖 CTk 自己的回调)。
6. **收起用 `grid_forget()` 而不是 `grid_remove()`**: 两者都能把控件从布局里摘掉, 但
   `CTkBaseClass` 会把最后一次几何调用记在 `_last_geometry_manager_call` 里, 缩放/外观变化时
   重放一遍(见 `CTkBaseClass._set_scaling`)—— `grid_remove()` 是 Tk 自己的方法, **不清**那条
   记录, 已经收起的滚动条会被重新摆回去; `grid_forget()` 会把记录清成 `None`。
7. **给 `CTkScrollbar._draw` 去掉那句强制刷新**(治 D): 见
   `widgets.apply_scrollbar_visibility_fix()` —— 只在 `_draw()` 调用期间把内层画布的
   `update_idletasks` 换成空操作, 调用结束就还原。代价只是"这次调用内不再立刻重画", 画布本来就会
   在下一个 idle 重画, 悬停配色等观感差别肉眼不可见。

## 5. 判定口径: "装得下就收起"优先于"保持现状"

`scrollbar_needed(content, viewport, slack)` 的两个方向**必须以"溢出"为口径**:

- 已显示: 默认容差(2px) —— 装得下就收起(不能再要求"比视口低 8px": 按内容定高的窗口里内容高度
  恰好等于视口, 那个条件永远不成立, 滚动条一旦显示就再也收不起来);
- 已收起: 容差 8px —— 要明显溢出才立起一条, 免得一两个像素的抖动把滚动条来回抽。

这样不会来回翻, 依据是**单调性**: 滚动条出现会让画布变窄、内容只可能更高; 收起会让画布变宽、
内容只可能更矮。于是"该显示"在两种状态下都成立(反之亦然)。

## 6. 这些"看起来能修"的路子都不行(都实测过)

- `grid()` **不带参数在 Tk 里只是查询**, 不会把 `grid_remove` 掉的控件放回来 —— 重新显示必须给出
  完整的摆放参数(`row=1, column=1, sticky="nsew", padx=(0, border + 1), pady=spacing`)。
- **"`grid_remove()` 在发现页上完全无效"是当时的误判**(2026-09-27 查清): 那两条滚动条确实被摘出了
  布局(`winfo_manager() == ""`), 只是被根因 C(状态记错)与 D(重画时强制刷队列)又弄回了画面。
  当时判错的关键是**拿 `winfo_ismapped()` 当"还在不在画面上"的证据** —— 它在未映射、刚被
  `grid_forget`、以及"被重新映射回来"三种情况下都会给出误导性的读数。要判定"到底画没画", 唯一
  可信的是**像素**(`crash_capture.grab_png` 拍下来数颜色), 或者直接查 `winfo_manager()`。
- **不要用 `winfo_ismapped()` 当"当前状态"**: 未映射的窗口恒为 0, 用它做判定会得到"假绿"的用例
  (第一版守卫就是这样写的, 在真窗口上永远通过)。
- **别在 `<Configure>` 里同步改几何**: 递归崩溃就是这么来的; 合并到 idle 一次。
- **别指望在 `<Map>` 事件里"再藏一次"**: 实测滚动条被映射回来的那一刻再调 `grid_remove()` 不生效
  (那个映射动作排在同一轮 idle 里), 得从根上治(去掉 `_draw` 里的强制刷新)。

## 7. 守卫清单(改了滚动条逻辑必须全绿)

| 用例                                                                                                                                       | 拦的是什么                                                                      |
| ------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- | --- | -------------------------------------------------------------------------------- | -------------------------------- |
| `tests/unit/test_ui_widgets.py::test_auto_scrollbar_pins_the_scrollbar_height_to_one_pixel`                                                | 根因 A: 高度请求必须是 1px(`blocker`)                                           |
| `tests/unit/test_ui_widgets.py::test_sync_scrollbar_ignores_reentrant_notifications`                                                       | 根因 B: 重入必须直接返回(`blocker`)                                             |
| `tests/unit/test_ui_widgets.py::test_sync_scrollbar_converges_instead_of_flipping`                                                         | 两轮判定不许翻来翻去(`blocker`)                                                 |
| `test_sync_scrollbar_hides_whenever_the_content_fits` / `_keeps_a_shown_bar_for_a_small_overflow` / `_does_not_pop_up_for_a_tiny_overflow` | 判定口径与滞回方向                                                              |
| `test_show_scrollbar_restores_the_grid_position`                                                                                           | 重新显示要给全摆放参数 **且** 压高度                                            |
| `tests/integration/test_gui_buttons.py::test_tags_dialog_does_not_twitch_or_crash_while_adding_rows`                                       | 真窗口: 连点两次"添加标签"后无回调异常、判定次数有上限、高度请求 1px(`blocker`) |     | `tests/unit/test_ui_widgets.py::test_the_first_verdict_assumes_the_bar_is_shown` | 根因 C: 首次判定必须真的执行收起 |
| `tests/unit/test_ui_widgets.py::test_hiding_uses_grid_forget_so_ctk_cannot_put_it_back`                                                    | 修法 6: 收起要走 `grid_forget`                                                  |
| `tests/integration/test_gui_scrollbars.py::test_the_discovery_panel_has_no_stray_scrollbar_with_an_empty_library`                          | 根因 C 的真窗口版: 空库时发现页不该有滚动条                                     |
| `tests/integration/test_gui_scrollbars.py::test_a_hidden_scrollbar_survives_the_canvas_reporting_its_position`                             | 根因 D: 画布回叫后仍必须保持收起                                                |
| `tests/integration/test_gui_scrollbars.py::test_a_scrollbar_appears_only_when_the_content_overflows`                                       | 真窗口上两个方向都成立(装得下收起 / 溢出出现)                                   |

**每个守卫都要证明"能咬住"**(注意别在断言里先跑事件循环 —— 破坏后可能需要等超时):

- 去掉 `auto_scrollbar` 里的 `_pin_scrollbar_height` → 前两条单元用例红;
- 再去掉 `_show_scrollbar` 里的那次 → 真窗口用例红(`assert 200 == 1`);
- 把重入闸门换成 `if False:` → `test_sync_scrollbar_ignores_reentrant_notifications` 报
  `assert [True] == [False]`;
- 把首次判定的初值改回 `bool(scrollbar.winfo_ismapped())` → `test_the_first_verdict_assumes_the_bar_is_shown`
  与 `test_the_discovery_panel_has_no_stray_scrollbar_with_an_empty_library` 红;
- 注释掉 `apply_scrollbar_visibility_fix()` → `test_a_hidden_scrollbar_survives_the_canvas_reporting_its_position` 红;
- 真窗口用例的断言顺序刻意是"先查高度(确定性), 再跑有限事件循环" —— 反过来的话, 破坏后用例会先
  掉进那个可能卡住的事件循环里, 靠 60s 超时才算红。

## 8. 写新代码时的检查清单

- 新增任何滚动区 → 立刻 `auto_scrollbar(frame)`(主页列表/游戏启停/定时窗口/设置窗口/各弹窗
  都这么用);
- 新加一块"按内容长高"的内容(卡片、说明、多行标签)→ 检查滚动条判定是否还成立;
- 判定函数里**只读**两个量(内容高度、视口高度), 任何"改几何"的动作都要走 4/5/6 三条规矩;
- **不要拿 `winfo_ismapped()` 当状态**: 判定的初值按"已显示", 要检查"画没画"就看像素或
  `winfo_manager()`(见第 6 节);
- **别让任何重画路径去强制刷事件队列**: `update_idletasks()` 会把排队的映射动作执行掉, 桌面上的
  控件会"自己回来"(根因 D); 需要立刻重画时优先交给 idle;
- 任何"改几何 → 触发事件 → 再改几何"的循环都按这套修: 先看**是谁把尺寸改回去了**, 再补闸门;
- 测试要盯**契约**(判定次数、高度请求、`winfo_manager()`、无回调异常), 不要盯肉眼观感; 时序不稳的
  话用替身控件把契约写死(见 `_CoupledFrame`), 或者走真窗口(见 `test_gui_scrollbars.py`)。
