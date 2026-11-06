"""把 ``@pytest.mark.profile`` 的耗时追踪从用例上摘下来, 挂成一条独立结论项.

出处(用户 2026-11-07): viztracer 的 trace 原本挂在打了标记的那条用例上 —— 要先知道
"是哪条用例"、再展开附件区才找得到, 而它的用途是"这一轮哪几个函数最费时", 与那条用例
自己的断言没有关系。这里在**汇总作业**里把它搬成一条按平台归环境的结论
``archive-management.profile``:

- 输入: 汇总作业摊平后的 ``allure-results``(分片已经把附件带上来了);
- 找到带 ``耗时追踪`` 前缀附件的结果, 把附件**移**成新结论的附件(改名成新结果的 id,
  不留孤儿文件), Top self time 摘要内联进描述, trace 本体作为下载附件;
- 源用例上的这两条附件引用删掉 —— 同一条 trace 在报告里只出现一次, 就挂在结论项上。

**为什么在汇总作业里做, 而不是每个平台各做一份**(与 ``create_allure_coverage.py`` 的
分工不同): 汇总作业拿到的是**全部平台**摊平后的结果, 一次运行就能按 ``env`` 标签把每个
平台的 trace 各归一条结论; 而且它是最终报告的输入, 只有在这里"从用例上摘掉"才真的
让最终报告里不重复。各平台的中间报告(pytest-report 的产物)保持原样, 那里挂在用例上
反而方便单平台排查。

没有 trace 的平台(按设计只有 Linux 追踪, 见 ``tests/profile_trace.py``)什么都不写, 只打
一行 ``::notice::``: 这一族在清单里是**按需产出**(``allure_catalog.GATE_ON_DEMAND``),
缺了不算缺失 —— 它是诊断产物, 不是门禁。
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

RESULTS_DIRECTORY = Path("allure-results")
RESULT_SUFFIX = "-result.json"
#: 结论项的身份(结果的 ``fullName``): 清单(``scripts/allure_catalog.py``)按它核"谁在产出"。
IDENTITY = "archive-management.profile"
TITLE = "耗时追踪"
#: 同时当 ``layer`` / ``testCategory`` 标签与 historyId 的一部分(与汇总脚本同一口径)。
CATEGORY = "profile"
#: 用例上那两条附件的名字前缀(见 ``tests/profile_trace.py``): 按名字认, 不按后缀 ——
#: trace 是 ``.json``、摘要是 ``.txt``, 而同一条用例还挂着 captured log 之类的附件。
TRACE_PREFIX = "耗时追踪 trace"
SUMMARY_PREFIX = "耗时追踪摘要"
#: 汇总项的严重等级: 它不验证行为, 只承载原始产物; 不打等级会让报告多一个 no_severity 桶。
SEVERITY = "trivial"
#: 认不出平台时的兜底(与 Allure 自己给的环境名一致, 不会凭空多出一个平台)。
FALLBACK_PLATFORM = "default"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行参数."""
    parser = argparse.ArgumentParser(
        description="把耗时追踪附件从用例上摘成一条独立结论项"
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=RESULTS_DIRECTORY,
        help="Allure 结果目录(默认 allure-results)",
    )
    return parser.parse_args(argv)


def load_result(path: Path) -> dict[str, Any] | None:
    """读一条结果; 损坏或类型不对时给 None(不让一条坏文件挡掉其余结论)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def labels_of(payload: dict[str, Any]) -> dict[str, str]:
    """把结果上的标签摊平成 ``{名字: 取值}``."""
    labels = payload.get("labels")
    if not isinstance(labels, list):
        return {}
    return {
        str(label.get("name")): str(label.get("value", ""))
        for label in labels
        if isinstance(label, dict)
    }


def attachment_refs(payload: dict[str, Any]) -> list[dict[str, str]]:
    """取出结果里的附件引用(只认 allure-pytest 写的 ``{name, source, type}`` 形状)."""
    attachments = payload.get("attachments")
    if not isinstance(attachments, list):
        return []
    return [
        {
            "name": str(item.get("name") or ""),
            "source": str(item.get("source") or ""),
            "type": str(item.get("type") or "text/plain"),
        }
        for item in attachments
        if isinstance(item, dict) and item.get("source")
    ]


def is_profile_attachment(ref: dict[str, str]) -> bool:
    """这条附件是不是耗时追踪的那两条(原名以 ``耗时追踪`` 开头)."""
    return ref["name"].startswith((TRACE_PREFIX, SUMMARY_PREFIX))


def sources_by_platform(
    results_dir: Path,
) -> dict[str, list[tuple[Path, dict[str, Any], list[dict[str, str]]]]]:
    """扫结果目录, 按平台把"带追踪附件的用例结果"分组.

    一组 = 同一 ``env`` 标签下的源结果与它们各自的那几条附件引用。已经是我们自己写出的
    结论项要跳过, 否则第二次跑会把附件再搬一次。
    """
    grouped: dict[str, list[tuple[Path, dict[str, Any], list[dict[str, str]]]]] = {}
    for path in sorted(results_dir.rglob(f"*{RESULT_SUFFIX}")):
        payload = load_result(path)
        if payload is None or payload.get("fullName") == IDENTITY:
            continue
        refs = [ref for ref in attachment_refs(payload) if is_profile_attachment(ref)]
        if not refs:
            continue
        labels = labels_of(payload)
        platform = labels.get("env") or labels.get("os") or FALLBACK_PLATFORM
        grouped.setdefault(platform, []).append((path, payload, refs))
    return grouped


def move_attachment(
    results_dir: Path, ref: dict[str, str], result_id: str
) -> dict[str, str] | None:
    """把一条附件搬到新结果的命名空间, 返回新的附件引用(原文件不在时给 None).

    用 ``rename`` 而不是复制: 源引用马上就要删掉, 复制只会让报告里多出 1 MB 没人引用的
    文件(最终报告是把整个结果目录打进去的)。后缀保留: Allure 靠它决定预览方式。
    """
    source = results_dir / ref["source"]
    if not source.is_file():
        return None
    suffix = source.suffix or ".txt"
    target = results_dir / f"{result_id}-attachment{suffix}"
    source.replace(target)
    return {"name": ref["name"], "source": target.name, "type": ref["type"]}


def summary_texts(results_dir: Path, refs: Iterable[dict[str, str]]) -> list[str]:
    """读摘要附件的内容(内联进描述, 报告里不用下载就能读 Top self time)."""
    texts: list[str] = []
    for ref in refs:
        if not ref["name"].startswith(SUMMARY_PREFIX):
            continue
        try:
            texts.append((results_dir / ref["source"]).read_text(encoding="utf-8"))
        except OSError:
            continue
    return texts


def build_description(summaries: list[str]) -> str:
    """结论项的描述: 摘要正文 + 这条结论从哪来(为什么它不在用例上)."""
    parts = [
        "- 由打了 `@pytest.mark.profile` 的那条用例产生, 汇总作业把它从用例上摘到这里"
        "(见 `scripts/create_allure_profile.py`) —— 报告里只出现一次, 与用例的断言无关。",
        "- 原始 trace 是下面的附件: 本机 `vizviewer <文件>` 打开, Perfetto / Chrome 也能直接开。",
        "- 只在一个平台开追踪(默认 Linux), 所以其余平台没有这一项。",
    ]
    body = "## 这条结论从哪来\n\n" + "\n".join(parts) + "\n"
    if not summaries:
        return body
    return (
        "## 本次追踪\n\n```text\n" + "\n\n".join(summaries).strip() + "\n```\n\n" + body
    )


def write_result(
    results_dir: Path,
    *,
    result_id: str,
    platform: str,
    os_label: str,
    description: str,
    attachments: list[dict[str, str]],
) -> None:
    """写入一条结论项(字段与 ``create_allure_summary.write_result`` 同一形状)."""
    timestamp = time.time_ns() // 1_000_000
    result: dict[str, Any] = {
        "uuid": result_id,
        "historyId": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"archive-management-{CATEGORY}")
        ),
        "fullName": IDENTITY,
        "name": TITLE,
        "status": "passed",
        "stage": "finished",
        "start": timestamp,
        "stop": timestamp,
        "labels": [
            {"name": "suite", "value": "Test report"},
            {"name": "os", "value": os_label},
            # 与环境维度对齐: allurerc.mjs 用 env 标签把结果归到各平台的环境, 缺了它这条
            # 结论只会出现在 default 环境里(按环境筛选时就看不到)。
            {"name": "env", "value": platform},
            {"name": "feature", "value": TITLE},
            {"name": "epic", "value": "工程与发布"},
            {"name": "story", "value": TITLE},
            {"name": "layer", "value": CATEGORY},
            {"name": "testCategory", "value": CATEGORY},
            {"name": "severity", "value": SEVERITY},
        ],
        "parameters": [{"name": "平台", "value": platform}],
        "description": description,
        "attachments": attachments,
    }
    (results_dir / f"{result_id}{RESULT_SUFFIX}").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def detach(path: Path, payload: dict[str, Any], removed: set[str]) -> None:
    """把已经搬走的附件引用从源用例上删掉并写回(其余附件原样保留).

    ``removed`` 是**搬之前**的附件文件名(引用里记的就是它们), 新名字与源结果无关。
    """
    keep = [
        ref
        for ref in payload.get("attachments") or []
        if isinstance(ref, dict) and ref.get("source") not in removed
    ]
    payload["attachments"] = keep
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """每个平台各写一条 ``archive-management.profile`` 结论(没有 trace 的平台不写)."""
    results_dir = parse_args(argv).results
    if not results_dir.is_dir():
        print(f"::notice::没有结果目录 {results_dir}, 跳过耗时追踪结论")
        return 0
    grouped = sources_by_platform(results_dir)
    if not grouped:
        print("::notice::这批结果里没有耗时追踪附件(只有标记了 profile 的平台会追踪)")
        return 0
    for platform, sources in sorted(grouped.items()):
        result_id = str(uuid.uuid4())
        refs = [ref for _, _, group in sources for ref in group]
        # 摘要要在搬家**之前**读: 搬完以后 refs 里的 source 已经不在原处了。
        summaries = summary_texts(results_dir, refs)
        moved = [
            item
            for item in (move_attachment(results_dir, ref, result_id) for ref in refs)
            if item is not None
        ]
        if not moved:
            print(f"::warning::{platform}: 追踪附件都找不到了, 跳过这条结论")
            continue
        missing = [
            ref["name"] for ref in refs if ref["name"] not in {i["name"] for i in moved}
        ]
        if missing:
            # 文件丢了但引用还在(比如产物传一半): 剩下的昭挂上去, 并把丢的哪一个喷出来 ——
            # 静默少一个附件比这难查得多。
            print(f"::warning::{platform}: 这些附件文件不存在, 已跳过: {missing}")
        labels = labels_of(sources[0][1])
        write_result(
            results_dir,
            result_id=result_id,
            platform=platform,
            os_label=labels.get("os") or platform,
            description=build_description(summaries),
            attachments=moved,
        )
        names = {item["name"] for item in moved}
        gone = {ref["source"] for ref in refs if ref["name"] in names}
        for path, payload, _ in sources:
            detach(path, payload, gone)
        print(
            f"[profile] {platform}: {len(moved)} 个附件已挂到 {IDENTITY}"
            f"(来源 {len(sources)} 条用例结果)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
