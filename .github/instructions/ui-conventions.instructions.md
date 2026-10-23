---
description: "改 CustomTkinter 界面时的判据汇总: 文案走 i18n、颜色只取调色板、间距圆角字号在刻度上、按钮文字写动作、Tab 顺序=创建顺序、主题重绘登记与销毁、后台线程不碰 Tk、长文本裁剪, 以及改完必须跑的那些界面守卫。"
applyTo: src/archive_management/ui/**.py
---

# 界面改动: 每条判据都有用例盯着

这些不是审美偏好，每一条都有守卫；越界时**改代码而不是改判据**。详细判据在 [docs/testing.md](../../docs/testing.md)，交互取舍在 [docs/ui-notes.md](../../docs/ui-notes.md)，功能行为在 [docs/features.md](../../docs/features.md)。

## 1. 文案

- 一律 `tr("key", **kw)`，两份 `resources/i18n/{zh-CN,en}.json` **必须同键**；中文用全角标点、末行不许只剩标点。
- `.py` 里**不许出现全角标点**（RUF001–003 会拦）：中文标点只属于 i18n 资源，代码注释用半角。
- 长文案别直接 `label.configure(text=...)`：走第 5 条的裁剪工具，否则会被静默硬裁且没有悬停提示。

## 2. 颜色与刻度

- 颜色只能取 `ui/palette.py` 的 token：面板/卡片外框用 `border`，**输入控件边界用 `input_border`**（两者不可互换，`test_ui_input_border.py` 会读 AST 扫全部输入控件）。
- 禁用态由 `paint_button_state` 统一压暗，别在回调里手写颜色；危险动作只用 `danger` / `danger_soft` 两档样式。
- 间距/圆角/字号只能用 `ui/metrics.py` 与 `ui/typography.py` 刻度上的值。新增/删除站点要同步 `test_ui_metrics.py` 里的 `_EXPECTED_SPACE_SITES` / `_EXPECTED_RADIUS_SITES` / `_EXPECTED_FONT_SITES`，并在注释里写"这一轮为什么多了或少了几个站点"。
- 对比度三档（正文 4.5:1 / 非文字与输入边界 3:1 / 禁用 2.5:1）由 `ui/contrast.py` 与登记表量，改配色要跑 `tests/unit/test_ui_contrast.py`。

## 3. 控件语义与键盘

- **按钮文字写"点击后会发生什么"**（`停用游戏` / `暂停` / `继续`），当前状态由旁边的**状态词**说；同一句话不许既表状态又表动作。
- **Tab 顺序 = 控件创建顺序**：页头与动作行新增控件时按 grid 顺序创建，否则键盘顺序会错（`test_gui_keyboard.py` 量得到）。
- 绑定一律 `add="+"`：顶掉控件自己的处理会踩到 `CTkEntry` 占位逻辑那类坑（见 [gui-tests-on-ci](gui-tests-on-ci.instructions.md) 第 12 条）。

## 4. 主题重绘与销毁

- `UiKit` 建的控件靠 `kit.register(...)` 登记"换主题怎么重绘"；`kit.apply(palette)` 之前**先把要销毁的控件退订**（`register` 返回退订函数），否则会 configure 已销毁的控件。
- **构造函数里逐控件上色的窗口**（设置/定时/管理）没有登记表：新增控件必须同时加进"构建"与 `restyle()` / `apply_palette()` 两处 —— 只加一处就是"切主题后它还是旧配色"。
- 清空 `CTkLabel` 的图片用 `image=""`（**不是 `None`**：CTk 不会把它转给 Tk，旧图名会留着，之后任何 configure 都报 image 不存在）；`CTkImage` 必须留引用（存进 `self._xxx_images`）。

## 5. 长文本

- `wraplength` 只压请求宽度、`fit_text` 才补省略号，**两个一起用**；要跟随窗口宽度变化就用 `track_fit` / `track_wraplength`（它们会自动挂悬停提示，宽度没变就不重写文本，避免"改文本 → Configure → 再裁"互追）。
- 判据写成"显示文本 == 该宽度下 `fit_text` 的结果"，不要比像素差（与字体库无关，CI 上更稳）。

## 6. 线程

- **后台线程里绝不碰 Tk**：耗时动作走 `_submit(work, ok)`，结果经队列由 `_poll_messages` 搬回主线程；取消、收尾、`_set_busy(False)` 都回主线程做。
- 反过来也成立：要量"刚设的状态"就别在中间泵事件（见 [gui-tests-on-ci](gui-tests-on-ci.instructions.md) 的同名条目）。

## 7. 改完必跑

```shell
uv run pytest tests/unit/test_ui_metrics.py tests/unit/test_ui_contrast.py tests/unit/test_ui_input_border.py -q
uv run pytest tests/integration/test_gui_styles.py tests/integration/test_gui_layout.py -q
uv run pytest tests/integration/test_gui_states.py tests/integration/test_gui_copy_quality.py -q
```

动过滚动区还要读 [ctk-scrollbar-twitch](ctk-scrollbar-twitch.instructions.md)；界面改动在 PR 里附前后对比截图。
