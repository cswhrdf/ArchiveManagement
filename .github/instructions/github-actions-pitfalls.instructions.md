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
- **下载要容错**：某个分片在跑测试前就挂了 → 它没有产物 → 下载步骤会失败并连累后面的合并与报告。加 `continue-on-error: true`，并在合并日志里**逐份报文件数**（少一份要能一眼看出来）。
- **隐藏文件默认不上传**：`.coverage.shard-0`、`.allure/history.jsonl` 这类以 `.` 开头的路径要显式放行，否则上传报 `no files found` 或内容为空。

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
- **别用绝对计数当"没漏收"的判据**：`minTestsCount: 3000` 这类常量会随规模增长从"够严"变成"够松"（用例涨到某个量之后，缺一整个平台的产物也不再触发），而且失效时没人知道。能写成结构不变式就写：每个环境都要有结果，或者"只看真实用例"（规则集级 `filter` 滤掉脚本生成的汇总项/门禁项后，再看各环境是否都有用例）。
- **"内容"问题别拦发布，"结构"问题才拦**：自检脚本查出报告缺资源（详情目录、附件、条数不符）时不发布，因为那份报告没用；而"某个平台没数据"这类内容问题要在**发布之后**判定 —— 报告正是用来看"哪一片挂了"的地方，拦下来就拿不到证据了。做法是 `continue-on-error: true` + 末尾的结论步骤接手（同第 4 节的"先跑完再判定"）。
- **附件要有体积上限**：把覆盖率 XML、堆栈 dump、截图挂进报告会显著增大报告与 artifact，超限时降级为"只记落点"。

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
