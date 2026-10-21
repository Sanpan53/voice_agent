"""变异测试：验证文档断言真的钉在代码上。

这项工作的一件产物是**项目文档**，而文档最大的风险不是写错，是**漂移**——
代码改了、文档没改，讲得越熟越危险。所以除了文档还产出了一个
``tests/test_doc_claims.py``，把文档里所有能从代码上机械核对的
说法变成断言。这个脚本用来证明**那些断言是有牙齿的**：改坏代码，它们必须变红。

跑法：逐条注入一个"看起来合理但和文档不符"的改动，
跑测试，**必须有测试变红**才算这条断言有效。跑完无论成败都还原。

用法::

    python scripts/mutate_doc_claims.py
"""

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 单次 pytest 的超时（秒）。这个文件只跑断言集，约 1 秒。
TIMEOUT = 180

#: 只跑断言文件本身——这里的断言就该由它自己守。
TEST_TARGET = "tests/test_doc_claims.py"

MUTATIONS = [
    # ---------- 检索与隔离 ----------
    (
        "app/services/profile_reranker.py",
        "score -= REPEAT_PURCHASE_PENALTY",
        "score += REPEAT_PURCHASE_PENALTY",
        "M1  重排器符号翻转（文档说「买过类似商品加分」，代码其实是买过同款减分）",
    ),
    (
        "app/services/scope_filter.py",
        "    if scope is None or scope.is_platform_wide():\n        return {}",
        "    if scope is None:\n        return {}",
        "M2  空列表不再等于「全平台」（把「无权限」和「不过滤」混成一个意思）",
    ),
    # ---------- 语音切分 ----------
    (
        "app/voice/sentence_aggregator.py",
        'SENTENCE_SPLIT = re.compile(r"(?<=[。！？])")',
        'SENTENCE_SPLIT = re.compile(r"(?<=[。！？.])")',
        "M3  把 ASCII 句点也当成切分点（文档里那个「3.5 被误切」的坑，被人好心补上了）",
    ),
    # ---------- 记忆水位 ----------
    (
        "app/agent/agent_memory_policy.py",
        "EMOTION_MEMORY_LIMIT = 40",
        "EMOTION_MEMORY_LIMIT = 8",
        "M4  情感水位降到和推荐一样（「情绪需要更长上下文」的论点就不成立了）",
    ),
    # ---------- 缓存与成本 ----------
    (
        "app/services/intent_cache.py",
        "TTL_SECONDS = 300",
        "TTL_SECONDS = 1800",
        "M5  意图缓存 TTL 从 5 分钟改成 30 分钟（文档说的「5 分钟内复用」就不对了）",
    ),
    # ---------- 合规 ----------
    (
        "app/compliance/compliance_checker.py",
        '"最好": "比较合适",',
        '"最好": "最好",',
        "M6  软化映射失效（绝对化用语原样念出去，「合规零事故」就是假的）",
    ),
]

#: 注：``M3`` 是一条**反向**变异——它把代码改成"更严格"，但仍然和文档
#: 描述的事实不符。这正是这里要防的那类漂移：有人照着旧文档好心补功能，
#: 补完文档就成了假话。所以"代码变好"和"文档还对"是两件事，都要有东西守着。


def run_tests() -> tuple[bool, str]:
    """跑断言集，返回 (是否被捕获, 一行摘要)。"""
    try:
        result = subprocess.run(
            ["pytest", TEST_TARGET, "-q", "--no-header", "-x"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return True, "超时（视为捕获——测试挂住了也是失效）"

    caught = result.returncode != 0
    tail = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    return caught, tail[-1] if tail else "(无输出)"


def main() -> int:
    survivors: list[str] = []

    for rel_path, old, new, label in MUTATIONS:
        target = ROOT / rel_path
        original = target.read_text()
        if old not in original:
            print(f"{label}: 跳过（锚点未命中，代码可能已改）", flush=True)
            survivors.append(label)
            continue

        target.write_text(original.replace(old, new, 1))
        try:
            caught, summary = run_tests()
        finally:
            # 先落盘再打印——万一被 Ctrl-C 打断，文件已经是好的
            target.write_text(original)

        verdict = "被捕获 ✅" if caught else "**未被捕获 ❌**"
        print(f"{label}: {verdict}  {summary}", flush=True)
        if not caught:
            survivors.append(label)

    print(flush=True)
    if survivors:
        print(f"有 {len(survivors)} 条变异存活：", flush=True)
        for label in survivors:
            print(f"  - {label}", flush=True)
        return 1

    print(f"全部 {len(MUTATIONS)} 条变异都被测试捕获 ✅", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
