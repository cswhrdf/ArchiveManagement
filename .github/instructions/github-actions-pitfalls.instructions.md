---
applyTo: ".github/workflows/*.yml, **/.github/workflows/*.yml, **/.github/workflows/*.yaml"
description: '写或改 GitHub Actions 工作流时的常见坑与检查清单。'
---

# GitHub Actions 工作流编写规范

本规范基于本仓库 CI 真实踩过的坑整理（缓存抢锁、矩阵放大、产物静默丢失、门禁被吞、跨平台差异）。生成或修改 `.github/workflows/*.yml` 时必须遵守：

**核心前提：CI 的坑不会让任何用例变红。** 抢锁只是警告、产物少一份照样出报告、门禁被吞就是绿灯 —— 所以每条规则都要落到"要么写死在工作流里，要么写成守卫测试"。

## 1. 改前必查三件事

- **作业数量会变吗**：动了 `strategy.matrix` 就从 1 份变 N 份（`os × shard` 很容易变成 9 份）。任何"全局唯一"的副作用 —— 缓存写入、产物名、远端资源独占（部署/评论/发布）、日志分组名 —— 都要重新指定唯一的持有者。
- **谁在等谁**：用 `needs` 表达真实依赖。上传者从 A 作业搬到 B 作业时，`needs` 忘了改就会在"产物还没传完"时开始下载，报告静默少掉那部分数据（作业照样绿）。
- **失败时还能拿到什么**：报告、日志、失败现场这类"给人看的东西"要 `if: always()`；只有"发布"这一步挂在自检成功上。

## 2. 依赖缓存：一个 key 只能有一个写入者

多个作业并行保存**同一个** cache key 时，只有第一个能抢到，其余报：

```text
Failed to save: Unable to reserve cache with key ..., another job may be creating this cache.
```

cache key 由「架构 + runner 镜像 + 运行时版本 + 锁文件哈希」算出，**同平台的各个分片算出的 key 完全相同**。做法：只让一片写，其余（含其它作业）一律 `save-cache: false`，但保留 `enable-cache: true` 让它们读。

```yaml
      - uses: astral-sh/setup-uv@v7
        with:
          enable-cache: true
          save-cache: ${{ matrix.shard == 0 }}   # 每平台只有一个写入者
```

## 3. 产物：命名、下载、隐藏文件

- **产物名必须带矩阵维度**（`allure-results-${{ matrix.os }}-${{ matrix.shard }}`）：`upload-artifact` 允许同名，行为是**互相覆盖**而不是报错。
- **明确下载语义**：`merge-multiple: true` 把各产物摊平进一个目录；`false` 让每份产物落进以产物名命名的子目录（要先逐份对数时用它）。用 `pattern:` 匹配多个产物时必须显式写这两者之一。
- **但只匹配到 `一个` 产物时上述规则不成立**：`download-artifact` 会把内容**直接解到 `path` 里**，不建那层"以产物名命名的子目录"（与 `merge-multiple` 取什么值无关）。所以"按目录名认分片"的下游（本仓库的 `merge_allure_results.py`）遇到**只有一个分片的平台**就会一个目录都匹配不到。**单片平台的 `path` 要直接写成那个分片目录名**（本仓库用矩阵字段 `download_path`），多片平台才写 `.`。踩过一次（2026-10-01，run 36754229023）：macOS 单片，分片作业全绿、产物也传了（汇总作业的运行总账写着"缺失 0 个"），但合并报"以下模式没匹配到目录"，该平台的结论与覆盖率全程没进报告。守卫：`tests/unit/test_sharding.py::test_single_shard_platform_downloads_into_its_own_shard_directory`。
- **下载要容错**：某个分片在跑测试前就挂了 → 它没有产物 → 下载步骤会失败并连累后面的合并与报告。加 `continue-on-error: true`，并在合并日志里**逐份报文件数**（少一份要能一眼看出来）。- **上传不要静默**: 分片那一步自己写 `if-no-files-found: error`。写 `ignore` 的后果是产物根本不被创建, 而下游只能报一句“某平台缺片” —— 得再翻回去翻那个分片作业才知道是哪儿断的（2026-09-30 实测: macOS 分片的 `allure-results-*` / `coverage-data-*` 全程没出现过, pytest-report 里只看到“以下模式没匹配到目录”, 覆盖率汇总则把它说成“未通过”）。这一片本来就是红的, 把“为什么没有产物”提到源头说, 不丢任何结论。- **隐藏文件默认不上传**：`.coverage.shard-0`、`.allure/history.jsonl` 这类以 `.` 开头的路径要显式放行，否则上传报 `no files found` 或内容为空。

```yaml
      - uses: actions/upload-artifact@v7
        with:
          name: coverage-data-${{ matrix.os }}-${{ matrix.shard }}
          path: .coverage.shard-${{ matrix.shard }}
          include-hidden-files: true
```

## 4. 退出码：门禁必须单独成步

多行 `run:` 只有**最后一条**命令的退出码决定步骤成败；管道默认吞掉前面的失败。

```yaml
      # ✅ 门禁单独成步：非零退出码才能让作业失败
      - name: Enforce the coverage threshold
        run: uv run coverage report --show-missing

      # ❌ 门禁夹在中间：失败会被后面成功的命令覆盖
      - name: Coverage
        run: |
          uv run coverage combine
          uv run coverage report --show-missing
          uv run coverage xml -o coverage.xml
```

- 管道要取原命令的退出码：bash 用 `code=${PIPESTATUS[0]}`，pwsh 用 `$LASTEXITCODE`。
- "先跑完再判定"（门禁失败仍要出报告）：该步 `continue-on-error: true` 并把退出码写进日志，末尾单独一步 `exit 1` 决定作业成败。
- 发布类步骤用 `if: always() && steps.<自检>.outcome == 'success'`：宁可作业红，也不发布残缺产物。

## 5. 跨平台 shell 与编码

- **顶层统一 UTF-8**：Windows runner 控制台是 cp1252，Python 打中文会 `UnicodeEncodeError` 直接打断步骤。

  ```yaml
  env:
    PYTHONUTF8: "1"
  ```

- **pwsh 不展开通配符**（bash 会）：`python script.py "dir-*"` 在 Windows 上会把 `*` 原样传进去。统一把通配符当字符串传，由脚本自己用 `Path.glob` 展开（注意 `Path.glob` 不接受绝对模式，要拆成"锚点 + 相对模式"）。
- **路径与命令分两步写**，不要拼"两边都能跑"的魔法命令：

  ```yaml
      - name: Collect (Linux/macOS)
        shell: bash
        run: cp coverage-data-*/.coverage.shard-* ./

      - name: Collect (Windows)
        shell: pwsh
        run: Get-ChildItem -Path coverage-data-* -Force -File | Copy-Item -Destination .
  ```

- **环境问题用重建解决，而不是打补丁**：托管解释器缺数据文件（如 uv standalone 找不到 `init.tcl`）时用 `uv python install --reinstall 3.12`，不要事后设一堆 `TCL_LIBRARY` 之类的变量。
- **GUI/显示相关用例要先自检**：把"环境起不来当 skip"的用例成片 skip，会把覆盖率门槛变成难定位的失败；在跑测试前显式确认一次（Linux 用 `xvfb-run`，Windows/macOS 建一次 Tk 窗口）。

## 6. 报告类作业

- **CLI 必须在仓库根跑**：报告工具靠 cwd 读配置（如 `allurerc.mjs`），换目录会静默退回默认值（主题、语言、环境维度全丢）。
- **生成前删掉输出目录**：生成器不会刷新已存在的输出目录，否则"生成成功"但数据是旧的。
- **发布前自检**：校验必需资源、索引引用的文件、附件是否齐全、条数与输入目录是否一致；自检不通过就不发布。
- **钉死 CLI 版本**：校验器类工具的 bug 常表现为**静默放行**（退出 0 且无输出），比崩溃更危险；升级版本后重跑一次门禁。
- **结论的"归属"要由执行位置决定**：为某个平台跑的检查别放在公共平台上执行（`mypy --platform win32` 在 Ubuntu 上跑照样退 0），否则报告里那条结论带着 Windows 的标签却产自别的机器 —— 用环境/平台一筛就说错了。拆成按平台展开的作业，标签由"该在哪跑"推出（一处常量），跑错平台要**报错**而不是静默跳过。
- **但"报告生成"不必跑在产出数据的平台上**：合并、覆盖率汇总、生成报告、自检都是纯文件操作，放哪台机器都一样（本仓库两个平台的报告都在 Ubuntu 上生成，省掉贵平台的计费，顺带不用维护第二套 shell 分支）。代价是"这份结论算哪个平台的"必须改成**参数**（`--platform` / `--expect-platforms` 读矩阵）：那台机器上 `runner.os` 恒为同一个值，拿它当平台名会把所有平台的结论都标错，而且不会有任何一步失败。
- **跨平台合并数据要记相对路径**：在 Linux 上合并 Windows 产生的覆盖率数据时，绝对路径（`D:\a\...`）在那台机器上无法还原 —— 覆盖率用 `relative_files = true` 记"相对工作目录"的名字，`combine` 会把分隔符换成本机写法并归一到真实文件（本仓库的报告作业正是这么做的，由一条真跑 combine 的用例钉住）。少了它表现为"合并成功但一个文件都对不上"（报告空掉），而且**只在 Linux 上才暴露**。
- **别用绝对计数当"没漏收"的判据**：`minTestsCount: 3000` 这类常量会随规模增长从"够严"变成"够松"（用例涨到某个量之后，缺一整个平台的产物也不再触发），而且失效时没人知道。能写成结构不变式就写：每个环境都要有结果，或者"只看真实用例"（规则集级 `filter` 滤掉脚本生成的汇总项/门禁项后，再看各环境是否都有用例）。
- **"内容"问题别拦发布，"结构"问题才拦**：自检脚本查出报告缺资源（详情目录、附件、条数不符）时不发布，因为那份报告没用；而"某个平台没数据"这类内容问题要在**发布之后**判定 —— 报告正是用来看"哪一片挂了"的地方，拦下来就拿不到证据了。做法是 `continue-on-error: true` + 末尾的结论步骤接手（同第 4 节的"先跑完再判定"）。
- **附件要有体积上限**：把覆盖率 XML、堆栈 dump、截图挂进报告会显著增大报告与 artifact，超限时降级为"只记落点"。

- **非门禁的"发布"动作要 `continue-on-error`**：一轮运行只有 `conclusion == success` 才会被下一轮当作历史基线（本仓库的趋势靠"上一轮成功运行的 artifact"往回取），所以部署站点这类"给人看的副本"动作失败时，不该把这一轮已经写好的历史行一起作废（它在 UI 上仍是失败）。

- **新增作业先看守卫的假设**：依赖分组守卫要求每个作业都有 `uv sync`，所以新增一个"不碰 Python"的作业（如发布 Pages）要把它登记进"不用 uv 的作业"清单，并由守卫反向自查"登记项真的没有 uv 命令" —— 而不是给守卫开一个能随手加名字的后门。

## 7. 分片（shard）特有

- **矩阵列表与脚本参数必须一致**（`shard: [0, 1, 2]` ⇄ `--shard-count 3`）：不一致会让一部分用例静默不跑；索引越界要让测试框架直接报错，而不是忽略。
- **覆盖率门槛只在合并后判**：单片覆盖率天生偏低，分片用 `--cov-fail-under=0 --cov-report=` 关掉，合并后再判。
- **覆盖率数据合并**：用 `COVERAGE_FILE=.coverage.shard-<片>` 让每片写独立文件再 `coverage combine`。注意 `coverage combine` 以 `COVERAGE_FILE` 为基名 glob `<基名>.*` —— 在同一个 shell 里循环设置过该变量后必须清掉，否则会误报 `No data to combine`、`coverage report` 只报最后一片的数字。

## 8. 把不变式写成守卫

守卫就是**读工作流文本的普通测试**（正则 + 计数即可，不用解析 YAML），放在 `tests/unit/` 下：

```python
_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def test_ci_writes_the_dependency_cache_from_one_shard_only() -> None:
    """同一平台的各片会算出同一个 cache key: 只让片 0 写缓存."""
    text = _WORKFLOW.read_text(encoding="utf-8")

    hint = "缓存写入者必须是分片 0: 同平台的三片共用同一个 cache key"
    assert "save-cache: ${{ matrix.shard == 0 }}" in text, hint
    assert "save-cache: true" not in text, "有作业无条件写缓存: 并行时会抢同一个 key"
    assert text.count("save-cache: ") == text.count("save-cache: false") + 1
```

值得写守卫的不变式：矩阵列表 ⇄ 脚本参数、"唯一者"身份（缓存写入者 / 产物名持有者）、`needs` 关系、关键动作真的被调用（合并/自检脚本出现在 `run:` 里）、关键字段没被摘掉（`include-hidden-files` / `continue-on-error` / `if: always()` / `PYTHONUTF8`）、门槛值只写一处。

## 9. 改完的自检顺序

1. **YAML 解析一次**并打印作业、矩阵、步骤名（确认结构没写歪）；
2. 跑与工作流相关的**守卫用例**；
3. 把"CI 只能在下次 push 验证"明确写进回答，并给出**回退点**（通常是还原某个 step 或某个字段这一处）。
4. 排错时先用日志关键词定位：`Failed to save`、`Unable to reserve`、`no files found`、`UnicodeEncodeError`、`skipped`、`exit code`；绿作业也要看 `warn`/`notice` —— 那往往就是静默退化的源头。

## 10. 省额度：作业数本身就是成本

额度 = 作业实例数 × (固定开销 + 实际工作)，而固定开销（checkout + 工具安装 + 依赖同步）在每个实例上都要重付一遍。所以先数作业实例，再谈别的：

- **`concurrency` + `cancel-in-progress: true`**：连续 push 时旧的一轮没人看，却和新的一轮一样贵。按 ref 分组，PR 与各分支各管各的。
- **`paths-ignore` 只能加在 `push` 上**：`pull_request` 被路径过滤跳过时，分支保护里的必需检查会永远停在 `pending`，PR 反而合不了。要让 PR 也省，用"轻量作业 + 条件跳过重量级作业"，而不是让整个工作流不触发。
- **与平台无关的检查合到一个作业里**（ruff/格式/mypy/静态分析都在 Ubuntu 上跑一遍就够），**分两个作业只是多付一套固定开销**。
- **平台专属的检查塞进已有的对应平台作业**（例如 `mypy --platform win32` 放进 Windows 那条 pytest 分片），别为一条命令单开作业 —— 单开等于再付一次整套 setup，而其中可能有最贵的 macOS runner。
- **能"失败才做"的就别无条件做**：例如解释器重装（`uv python install --reinstall`）改成"自检失败才重装"，但**必须**保留原来的守卫语义：自检失败要先由后续步骤复检一次并拦住提交，否则等于把一道门禁悄悄变成警告。
- **矩阵的固定开销决定了片数**：某平台总时长 ≈ 片数 × setup + 串行测试时间 T，减片省额度、加片省墙钟。贵的平台（macOS 按倍率计费）少开几片。片数按平台分开时矩阵只能写成 `include` 逐条列（`os × shard` 两个轴表达不了"各平台片数不同"），片号必须从 0 连续编到"片数-1"，且要与脚本参数、清单自报的片号三处一致。
- **只做文件操作的作业固定在便宜平台上跑**：报告生成、合并、汇总都不需要产出数据的那个平台，搬到 Ubuntu 就能把 Windows ×2 / macOS ×10 的计价去掉；但注意“归属”必须改成参数（见第 6 节）。
- **别用绝对计数或"每次都装全量依赖"图省事**：依赖分组（按作业只装需要的那组）与减少作业数是同一类收益，都省在每个实例上。做这件事有两个必踩的坑：
  - **`uv run` 会自作主张 sync**：它默认按**默认组**同步环境，于是每个作业里第一句 `uv run ...` 就把刚省掉的依赖又装回来（实测：只装了测试组的临时环境里跑一次 `uv run pytest`，uv 装回 51 个包，ruff/mypy/bandit/pip-audit/pyinstaller 全回来了）。要么给每句 `uv run` 加 `--no-sync`，要么在工作流顶层设 `UV_NO_SYNC=1`（推荐，一处生效）。少写一组的失败是响亮的（`Failed to spawn: ruff`，退出码 2），但"分组白做"是**静默**的 —— 所以必须写守卫。
  - **同一作业里的每次 `uv sync` 都要带同一组参数**：形如"自检失败才重装解释器"的修复步骤里也会再 sync 一次，它只列一半的组就会把刚装好的组删掉（`uv sync` 是"同步到声明的集合"，不是叠加）。
- **缓存热时省不了几秒，别把它当大头**：装 50 个包在缓存热的环境里只要约 0.5s，所以"按作业装依赖"的收益主要在**冷缓存**那一轮（锁文件一变，uv 缓存重建）与磁盘占用；如果目标是砍分钟数，先看"少跑多少"那一类改动（跑得更少、或把最贵的平台降频），而不是"装得更少"。
- 想加新的缓存（npm、解释器目录……）时，先确认三件事：这个 action 在仓库里有没有版本先例、key 里有没有"唯一写入者"身份（OS + 作业名）、以及**缓存坏掉时的兜底**（例如解释器目录正是 Tcl 损坏的来源，缓存它可能把偶发失败带回来）。收益只有一两分钟、却要引入一个没先例的 action 时，宁可不做。
