"""CI 工作流的文本解析助手(给"读工作流文本的守卫用例"共用).

为什么是读文本而不是解析 YAML: 仓库的依赖清单里没有 YAML 解析器, 而守卫要断言的东西
(哪几处写了同一个平台列表、哪些参数必须一起出现)用正则 + 计数就够。代价是得容忍编辑器的
排版风格 —— ``- { os: windows-latest, shard: 0 }`` 与多行写法表达的是同一件事,
``needs: [a, b]`` 也可能被摊成多行。这里的解析把两种写法都认下来, 免得每个守卫各写一份。

只放"两个以上测试模块都要用"的东西; 断言留在各自的守卫用例里。
"""

from __future__ import annotations

import re
from pathlib import Path

# 仓库根目录: 本文件在 tests/ 下, 差一级.
REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
# 只读的构建工作流: ci.yml 的打包门禁与 release.yml 的发布档都调它(命令都在这一个文件里, 见
# `UV_STEP_WORKFLOWS`)。
RELEASE_BUILD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-build.yml"
# dev 的手动快照档: 四段版本号 + Pre-release 标签, 构建同样交给上面那份只读工作流。
DEV_RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "dev-release.yml"
# 守"依赖分组 / `uv run` 的隐式 sync / 缓存输入"这批不变式的工作流: 构建那一份是上面两个的**共同
# 下游**(门禁与发布各调它一次), 所以它一个不少地参与。
UV_STEP_WORKFLOWS = (
    WORKFLOW,
    RELEASE_WORKFLOW,
    RELEASE_BUILD_WORKFLOW,
    DEV_RELEASE_WORKFLOW,
)

# runner 镜像 → 报告里的平台展示名(与 allurerc.mjs 的 matcher、runner.os 一致).
OS_PLATFORMS = {
    "ubuntu-latest": "Linux",
    "windows-latest": "Windows",
    "macos-latest": "macOS",
}

# 一行的"键: 值": 值可能是带引号的字符串(内含逗号, 如 "0,1,2")、普通标量或占位符.
_ENTRY_KEY_VALUE = re.compile(r"([A-Za-z_]\w*):\s*(\"[^\"]*\"|'[^']*'|[^,\n}]+)")
# 矩阵的"轴": `os: [a, b]` / `shard: [0, 1]` —— 值必须写在一行里(仓库里两种写法都是这么写的).
_AXIS = re.compile(r"^\s*([A-Za-z_]\w*):\s*\[([^\]]*)\]\s*$", re.MULTILINE)
# needs 之类的行内列表(或者被编辑器摊成的多行列表).
_NEEDS = re.compile(r"needs:\s*\[([^\]]+)\]")


def workflow_text() -> str:
    """读取 CI 工作流文本."""
    return WORKFLOW.read_text(encoding="utf-8")


def jobs(text: str) -> dict[str, str]:
    """取出 ``jobs:`` 下每个作业的名字与正文(名字 → 那一节文本).

    作业键的缩进由文件自己决定 —— 仓库里两种排版并存(``ci.yml`` / ``release.yml`` 两个
    空格, ``wiki.yml`` 四个), 所以取 ``jobs:`` 之后**最浅的那一档**键
    作为作业层: 更深的都是作业内部的键。前面还有 ``on:`` / ``env:`` 这类顶层键, 所以只从
    ``jobs:`` 之后开始扫。没有 ``jobs:`` 行时按整段扫: 用例里手写的合成文本就属于这种,
    真实工作流都有这行。
    """
    blocks = re.split(r"^jobs:\n", text, maxsplit=1, flags=re.MULTILINE)
    body = blocks[1] if len(blocks) == 2 else text
    indents = [len(value) for value in re.findall(r"^( +)[A-Za-z_][\w-]*:", body, re.M)]
    assert indents, "工作流里一个作业都找不到"
    indent = min(indents)
    found = re.findall(
        rf"\n {{{indent}}}([A-Za-z_][\w-]*):\n(.*?)"
        rf"(?=\n {{{indent}}}[A-Za-z_][\w-]*:|\Z)",
        # 前面补一个换行: 第一个作业前面没有换行, 不补的话它会被整条漏掉。
        "\n" + body,
        re.DOTALL,
    )
    assert found, "工作流里一个作业都找不到"
    return dict(found)


def job_block(text: str, job: str) -> str:
    """取出 ``job:`` 那一节(到下一个顶层作业或文件末尾为止)."""
    found = jobs(text)
    assert job in found, f"工作流里找不到 {job} job"
    return found[job]


def _include_text(block: str) -> str:
    """取出 ``include:`` 下面的那段(缩进更深的行), 不含后面的 ``steps:``.

    按缩进切而不是正则贪婪匹配: 矩阵后面的 ``steps:`` 里也全是 ``- name: ...``,
    一起抓进来会把步骤当成矩阵条目。
    """
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "include:":
            continue
        indent = len(line) - len(line.lstrip())
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        return "\n".join(body)
    raise AssertionError("矩阵里找不到 include:")


def _matrix_axes(block: str) -> dict[str, list[str]]:
    """取出矩阵的**轴**(``os: [a, b]`` 这类行内列表); 只列 include 的写法返回空表.

    轴只在"各平台片数一致"时才写得出来, 但那时它能省掉一堆重复条目(见
    :func:`matrix_entries`)。轴声明只可能出现在 ``matrix:`` 与 ``include:`` 之间。
    """
    lines = block.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == "matrix:"), None
    )
    if start is None:  # pragma: no cover - 真实工作流的每个矩阵都这么写
        return {}
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() == "include:":
            break
        body.append(line)
    return {
        name: [item.strip().strip("\"'") for item in raw.split(",") if item.strip()]
        for name, raw in _AXIS.findall("\n".join(body))
    }


def _combinations(axes: dict[str, list[str]]) -> list[dict[str, str]]:
    """轴的笛卡尔积(每条组合带上轴上的键值)."""
    combos: list[dict[str, str]] = [{}]
    for name, values in axes.items():
        combos = [{**combo, name: value} for combo in combos for value in values]
    return combos


def _apply_include(
    combos: list[dict[str, str]], entries: list[dict[str, str]]
) -> list[dict[str, str]]:
    """按 GitHub 自己的 include 语义把条目并进组合.

    官方语义: 条目的每个键值都要与某个组合**相容**(组合里有这个键时值必须相同) —— 相容就
    合并进那个组合, 一个都配不上时该条目自己成为一条。所以"给某个平台的属性"(展示名、
    片数)只写一份就够: 另一条轴上该平台的每一片都会拿到它。
    """
    merged = [dict(combo) for combo in combos]
    extra: list[dict[str, str]] = []
    for entry in entries:
        hits = [
            combo
            for combo in merged
            if all(combo.get(key, value) == value for key, value in entry.items())
        ]
        if not hits:
            extra.append(dict(entry))
            continue
        for combo in hits:
            combo.update(entry)
    return merged + extra


def _include_entries(block: str, job: str) -> list[dict[str, str]]:
    """解析 ``include:`` 的逐条参数(行内写法与多行写法都认)."""
    include = _include_text(block)
    entries: list[dict[str, str]] = []
    # 每条以 ``- `` 开头(行内写法与多行写法都满足), 注释行先去掉再抠键值。
    # 前面补一个换行, 免得第一条(它前面没有换行)被当成"头一段"丢掉。
    for chunk in re.split(r"\n\s*-\s+", "\n" + include.strip())[1:]:
        body = "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("#")
        )
        entry = {
            key: value.strip().strip("\"'")
            for key, value in _ENTRY_KEY_VALUE.findall(body)
        }
        assert entry, f"{job} 的矩阵条目解析不出参数: {chunk!r}"
        entries.append(entry)
    assert entries, f"{job} 的 include 矩阵是空的"
    return entries


def matrix_entries(text: str, job: str) -> list[dict[str, str]]:
    """取出作业矩阵**展开后**的逐条参数(``os`` / ``platform`` / ``shard`` / ``shards`` 等).

    两种写法都认:

    * **轴 + include 补属性**(各平台片数一致时更短): 两个轴(``os: [...]`` 与
      ``shard: [0, 1, 2]``)加三条"该平台的展示名与片数", 展开后仍是"每个平台每一片一条";
    * **只列 include**(各平台片数不同时必需): 原样返回那几条。

    两种写法在 GitHub 上是同一份矩阵, 守卫要的也一直是同一件事 —— "每个平台有哪些片、
    片数是多少", 所以它不必关心矩阵是怎么写的。
    """
    block = job_block(text, job)
    entries = _include_entries(block, job)
    axes = _matrix_axes(block)
    if not axes:
        return entries
    return _apply_include(_combinations(axes), entries)


def needs_of(text: str, job: str) -> set[str]:
    """取出某个作业声明的 ``needs``(行内列表会被编辑器摊成多行, 空项要滤掉)."""
    matched = _NEEDS.search(job_block(text, job))
    assert matched is not None, f"{job} 没有声明 needs"
    return {name.strip() for name in matched.group(1).split(",") if name.strip()}


def job_condition(text: str, job: str) -> str:
    """取出作业级 ``if:`` 的条件文本; 作业没写 ``if:`` 时返回空串.

    作业内部的键缩进由文件自己决定(见 :func:`jobs`), 而作业块的**最浅那一档**就是作业级
    的键(块从作业键之后开始), 步骤级更深 —— 所以只认最浅那一档的 ``if:``, 否则
    ``if: always()`` 这类条件会把步骤级的一起数进来。
    """
    block = job_block(text, job)
    indents = [len(value) for value in re.findall(r"^( +)\S", block, re.M)]
    assert indents, f"{job} 里一行缩进都没有, 取不到作业级 if"
    matched = re.search(rf"^ {{{min(indents)}}}if:\s*(.+)$", block, re.MULTILINE)
    return "" if matched is None else matched.group(1).strip()
