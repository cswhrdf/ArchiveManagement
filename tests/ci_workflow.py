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
# 定时那轮只有"门 + 调用"(见 nightly.yml 顶部): 不含任何作业定义的副本 —— 全量链与报告链
# 只有 ci.yml 一份, 免得两份文件各写一遍分片矩阵, 改一处忘了另一处。
NIGHTLY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nightly.yml"
# 发布工作流也要守同一批不变式(依赖分组、uv run 的隐式 sync)。
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

# runner 镜像 → 报告里的平台展示名(与 allurerc.mjs 的 matcher、runner.os 一致).
OS_PLATFORMS = {
    "ubuntu-latest": "Linux",
    "windows-latest": "Windows",
    "macos-latest": "macOS",
}

# 一行的"键: 值": 值可能是带引号的字符串(内含逗号, 如 "0,1,2")、普通标量或占位符.
_ENTRY_KEY_VALUE = re.compile(r"([A-Za-z_]\w*):\s*(\"[^\"]*\"|'[^']*'|[^,\n}]+)")
# needs 之类的行内列表(或者被编辑器摊成的多行列表).
_NEEDS = re.compile(r"needs:\s*\[([^\]]+)\]")


def workflow_text() -> str:
    """读取 CI 工作流文本."""
    return WORKFLOW.read_text(encoding="utf-8")


def jobs(text: str) -> dict[str, str]:
    """取出 ``jobs:`` 下每个作业的名字与正文(名字 → 那一节文本).

    顶层作业键的缩进是两个空格(四空格及更深的是作业内部的键), 且只从 ``jobs:``
    之后开始扫 —— 前面的 ``on:`` / ``env:`` 也是两空格缩进的键。没有 ``jobs:`` 行时
    按整段扫: 用例里手写的合成文本就属于这种, 真实工作流都有这行。
    """
    blocks = re.split(r"^jobs:\n", text, maxsplit=1, flags=re.MULTILINE)
    body = blocks[1] if len(blocks) == 2 else text
    found = re.findall(
        r"\n  ([A-Za-z_][\w-]*):\n(.*?)(?=\n  [A-Za-z_][\w-]*:|\Z)",
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


def matrix_entries(text: str, job: str) -> list[dict[str, str]]:
    """取出作业矩阵里 ``include`` 的逐条参数(``os`` / ``platform`` / ``shard`` 等)."""
    include = _include_text(job_block(text, job))
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


def needs_of(text: str, job: str) -> set[str]:
    """取出某个作业声明的 ``needs``(行内列表会被编辑器摊成多行, 空项要滤掉)."""
    matched = _NEEDS.search(job_block(text, job))
    assert matched is not None, f"{job} 没有声明 needs"
    return {name.strip() for name in matched.group(1).split(",") if name.strip()}


def job_condition(text: str, job: str) -> str:
    """取出作业级 ``if:`` 的条件文本; 作业没写 ``if:`` 时返回空串.

    作业内部的键是四空格缩进, 步骤是六空格以上(其键更深), 所以只认四空格那一行 ——
    否则 ``if: always()`` 这类条件会把步骤级的一起数进来。
    """
    matched = re.search(r"^ {4}if:\s*(.+)$", job_block(text, job), re.MULTILINE)
    return "" if matched is None else matched.group(1).strip()
