---
description: 改完代码要拿"全量结果"(含覆盖率门禁)时的固定做法: 一律用 scripts/run_tests_local.py 并行分片跑, 不要单进程干等; 覆盖率门禁要加 --coverage; 以及拿到红时怎么区分"负载引起的偶发"与真回归。
applyTo: "**"
---

# 本地全量验证: 一律用 `scripts/run_tests_local.py`

**规则(用户 2026-10-14 明确要求)**: 改完仓库里任何东西, 要拿"全量结果"时**不要**直接 `pytest tests/unit tests/integration`, 也**不要**裸跑单进程的 `coverage run --rcfile=.coverage-platform.rc -m pytest` —— 一律走这个脚本, 把墙钟省下来:

```powershell
.venv\Scripts\python.exe scripts\run_tests_local.py --coverage   # 全量 + 覆盖率门禁(收尾 combine + report)
.venv\Scripts\python.exe scripts\run_tests_local.py              # 只要红绿时(不判覆盖率)
```

- `--coverage` 每片用 `coverage run --rcfile=.coverage-platform.rc -m pytest` 起, 各写自己的 `.coverage.shard-<片>`(同一份数据文件被两个进程写会互相冲掉), 收尾在日志目录里 `coverage combine` 成一份再 `coverage report -m`; **覆盖率报告的退出码与各片退出码一起取最大值**当脚本退出码。 rc(`.coverage-platform.rc`, 未入库的派生物)不在时脚本会先 `coverage_platform.py write` 生成。
- 实测(2026-10-14, 2850 条): 单进程 `coverage run … -m pytest` **14:48** → 双片 `--coverage` **13:05**, 结论逐数一致(passed 2850 / skipped 9, 覆盖率 100%)。**省得不多是因为这套用例被 GUI 与磁盘 I/O 卡着** (两片各约 13 分钟, 并发会让每条 GUI 用例更慢), 脚本自己记的非覆盖率场景省约三分之一(见它的 docstring)。
- **拿到红先分清偶发与回归**: 并发 + 覆盖率的追踪开销会放大 GUI 用例里本来就有的时序敏感点 (2026-10-14 实测: 双片跑时 `test_gui_dropdown.py::test_clicking_the_arrow_opens_the_dropdown` 报"点箭头没打开下拉", 单跑该模块 9 passed)。**判据是"单跑同一模块也绿 + 失败点是已知的时序敏感用例"**, 两条都满足才按偶发处理; 不满足就老实当回归查(别拿"并发跑的"当免责)。
  - 那条具体的红**已经把成因量出来了**: 同一个选择框上留着开着的浮层时, 再点箭头是把浮层收起 (`_close_on_next_click`) → `active_dropdown()` 为 None; 用例现在自己摆正起点(先关浮层 + 拨开关) 并先确认坐标量得出(宽 > 8)。**再遇到这条红就是回归**, 不是偶发 —— 该查的是浮层状态泄漏本身。
  - 第二条已知偶发(2026-10-14, 同一轮里咬到): `tests/integration/test_gui_layout.py::test_library_and_discovery_borders_are_visible`, 失败详情是某个子控件与父容器**右边贴齐 0px**(模块口径里这就是"贴边盖住 1px 描边")。核实口径同上: 单跑该模块 24 passed ×2、前一模块 + 覆盖率追踪 34 passed、复跑全量 2/2 绿。它与 md/文档类的改动无关(md 影响不了 Tk 几何), 但也**没定论**它一定是测量假象 —— 真要判就得把这条守卫的等待/判据改硬(把"贴齐 0px"与">0 越界"分开, 或要求更多轮几何一致)。
  - 第三条已知偶发(2026-10-15, 全在 `tests/integration/test_gui_layout.py`): 报 **"表头与数据行没对齐"** 的那一族断言。已观测到的用例名: `test_high_dpi_keeps_the_status_column_to_two_lines`、`test_a_long_state_column_does_not_push_the_fixed_columns`、`test_long_game_name_does_not_widen_the_list_rows`(2026-10-17 又多了 `test_the_header_follows_the_scrollbar_in_the_smallest_window` —— 它量的是"滚动条出现后表头必须跟着重排", 报的仍是同一条断言, 属同一族) —— 而且**同一轮里换着来**(一次全量跑只红其中一条, 复跑换成邻居)。**指纹是六列整体位移一个恒定的水平量**(不是某一列单独跑偏, 而是表头块与数据行块整体错开): 2026-10-14 记的是 14px(≈滚动条宽), 2026-10-15 两轮实测 **27px** 与 **16px**, 2026-10-17 双片 + 覆盖率实测 **33px**。成因是对齐任务在并发 + 覆盖率追踪下没收敛(`home_page._align_table_header` 的 `_ALIGN_MAX_ATTEMPTS`)。核实: 单跑该模块 **24 passed**、定向跑这几条(`-k "long_game_name or high_dpi or long_state_column"`)**5 passed ×2** ⇒ 按偶发处理。它已经咬了三轮, 真要根治得把"等对齐"的判据改硬(把等待上限/收敛条件写进 `_wait_for_the_columns_to_line_up`, 或把 `_ALIGN_MAX_ATTEMPTS` 提到能覆盖最坏时序), 那是单独一轮的事。
  - 第四条已知偶发(2026-10-16, **不是断言红, 是整片被杀**): 双片 + 覆盖率那一轮里, `tests/integration/test_gui_keyboard.py::test_every_screen_is_reachable_by_keyboard` 卡到 60s 上限, `tests/timeout_guard.py` 落完现场 `os._exit(1)` ⇒ **那一片的 pytest 进程当场死掉**: 它后面没跑完, 而且 `.coverage.shard-<片>` 也没写出来(覆盖率在 atexit 落盘, `os._exit` 跳过整条 atexit)。**指纹**: 该片日志(`%TEMP%\pytest-shards-*\shard-<片>.log`)的**最后几行**是 `timeout_guard.on_timeout` 的栈 + `[超时留证] ...`; `coverage.log` 里写着 `Combined 1 file`, 于是 TOTAL 只有 **94%** —— **看到"覆盖率莫名低了"先去看是不是超时把某一片杀了, 别去补用例**。主线程栈是 `window.update()` ← 用例自己的 `_settle`(即"Tk 在事件回调里没回来"), dump 里能直接读出来: `crash-dumps/tests_integration_test_gui_keyboard.py__test_every_screen_is_reachable_by_keyboard_timeout.dump`(用 `coredumpy.Coredumpy.load_data_from_path(str(path))` 后沿 `frame.f_back` 走)。核实: 单跑该模块 **10 passed ×2**(各 17s) ⇒ 按并发/负载引起的偶发处理。它在 CI 上也会**被那层重跑包装接住**(见 `docs/testing.md`: 第一遍重跑, 仍超时就把这条 `--deselect` 掉跑完其余用例, 覆盖率照旧拿到手)。**已修(2026-10-16)**: 这条用例逐控件做 聚焦/失焦 + 全量 `update()`, 实测单跑 **9.7s** / 带覆盖率追踪 **12.0s**, 但双片 + 覆盖率满载会涨到 **60s 以上** —— 它咬了两轮(两轮都把那一片杀掉)之后, 给它单独加了 `@pytest.mark.timeout(150)`(pyproject 里写明"个别慢用例可用 `@pytest.mark.timeout(N)` 覆盖"), 注释里留着这几组实测数字; 真正的卡死仍有上限兜着。
- 透传参数用 `--` 隔开; **不要再写 `--shard-count` / `--shard-index`**(片号由脚本注入, 重复给会悄悄改口径, 脚本会直接拒绝)。想刷新分片权重时才加 `--record-durations=tests/durations.json`。- **单条/局部验证照旧直接跑 pytest**(那时不需要分片): 例如 `coverage run --rcfile=.coverage-platform.rc --data-file="$env:TEMP\.coverage-x" -m pytest <文件> -q`
  - `coverage report -m --rcfile=.coverage-platform.rc --data-file="$env:TEMP\.coverage-x" --include="*<模块>.py"` (见 `CONTRIBUTING.md` 第四节与 `docs/testing.md`)。
- **CI 不用这个脚本**: 那边是机器级并行(每片一个 runner, `COVERAGE_FILE=.coverage.shard-<片>` + 汇总作业 combine), 见 `.github/workflows/ci.yml` 的分片矩阵。这个脚本只为"本地手动跑全量"服务。
- 相关守卫: `tests/unit/test_durations.py` 里锁着脚本的分片改写、耗时合并与 `--coverage` 的命令/环境/退出码; 改脚本时那些用例要一起看。
