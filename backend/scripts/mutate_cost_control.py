"""变异测试：验证新测试真的能抓到回归。

做法：逐条给生产代码注入一个"看起来合理但错了"的改动，
跑全量测试，**必须有测试变红**才算这条测试是有效的。跑完无论成败都还原。

**这类改动格外需要变异测试。** 其他模块的 bug 会下错单、会丢数据、会越权——
都能从行为上肉眼看见。这里的 bug 全是"钱多花了"：缓存没命中、模板没生效、
巡检没跑，接口一律返回 200，回复内容也说得通，**唯一的变化是账单**。
没有计数断言就没有任何东西能发现它们。

变异分四类：

+ **归一化写歪**（M1-M3）——大小写不归一、退回逐字相等、用进程随机化哈希；
+ **短路写歪**（M4-M6）——两组合并（原 bug）、共享常量被污染、结果写回 Redis；
+ **模板写歪**（M7-M10）——子串匹配、大小写敏感、顺序反转、分支不走模板；
+ **省钱路径写歪**（M11-M19）——query 被缓存、缓存故障冒泡、巡检被异常打断、
  锁内 await、TTS 缓存不查、空音频被缓存、检索不用 embed_query。

用法::

    python scripts/mutate_cost_control.py
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 单次 pytest 的超时（秒）。全量约 2.7 秒；M16（锁内 await 死锁）要等
#: 测试里那个 10 秒的 ``join``，所以余量给足。
TIMEOUT = 300

MUTATIONS = [
    # ---------- 归一化写歪 ----------
    (
        "app/services/utterance_fingerprint.py",
        '    return _NOISE.sub("", utterance).lower()',
        '    return _NOISE.sub("", utterance)',
        "M1  不转小写（大小写不同的同一句话命中不了同一条缓存）",
    ),
    (
        "app/services/utterance_fingerprint.py",
        '    return _NOISE.sub("", utterance).lower()',
        '    return utterance.strip()',
        "M2  退回逐字相等（多打一个句号就多烧一次 LLM）",
    ),
    (
        "app/services/utterance_fingerprint.py",
        '    digest = hashlib.md5(normalize(utterance).encode("utf-8"))'
        ".hexdigest()  # noqa: S324",
        "    digest = format(hash(normalize(utterance)) & 0xFFFFFFFFFFFFFFFF, '016x')",
        "M3  改用内置 hash()（进程随机化，重启即缓存全失效且不报错）",
    ),
    # ---------- 短路写歪 ----------
    (
        "app/services/common_confirmer.py",
        "    if normalized in CONTINUE_WORDS:\n"
        "        return _CONTINUE_INTENT.model_copy(deep=True)",
        "    if normalized in CONTINUE_WORDS:\n"
        "        return _CONFIRM_INTENT.model_copy(deep=True)",
        "M4  「继续」类退回 ORDER_CONFIRM（原 bug：说『继续』被问『要下哪一单』）",
    ),
    (
        "app/services/common_confirmer.py",
        "    if normalized in CONFIRM_WORDS:\n        return _CONFIRM_INTENT.model_copy(deep=True)",
        "    if normalized in CONFIRM_WORDS:\n        return _CONFIRM_INTENT",
        "M5  返回共享常量（调用方一改 slots，之后每次短路都返回被污染的那份）",
    ),
    # 注：``MAX_LENGTH`` 那道长度闸门**故意不在这里变异**。
    # 判定用的是整串集合成员，而词表里最长的词恰好 3 字，所以闸门拦下来的每一句，
    # 集合判定本来也会拦掉——摘掉它，全量测试一条都不会红（实测过）。
    # 一条永远杀不死的变异只会给每次跑批添噪音。它由
    # ``test_every_confirmer_word_fits_within_the_length_gate`` 这条**不变式测试**
    # 守着：谁往词表里加了超长词，测试立刻变红，逼他直面这个水位的存在。
    (
        "app/services/intent_service.py",
        "            # **不写回 IntentCache**：本来就是常量返回，写缓存只是白占一个 key。\n"
        "            return confirmer",
        "            if self._cache is not None:\n"
        "                await self._cache.put(session_id, utterance, confirmer)\n"
        "            return confirmer",
        "M6  confirmer 结果写回 Redis（每次『嗯』都白占一个 key，5 分钟）",
    ),
    (
        "app/services/intent_service.py",
        "        confirmer = common_confirmer.match(utterance)\n"
        "        if confirmer is not None:",
        "        confirmer = None\n        if confirmer is not None:",
        "M7  confirmer 短路根本没接上（每次还是查 Redis / 调 LLM）",
    ),
    # ---------- 模板写歪 ----------
    (
        "app/services/chitchat_templates.py",
        "        if pattern.fullmatch(stripped):",
        "        if pattern.search(stripped):",
        "M8  退回子串匹配（整串匹配语义不等价）",
    ),
    (
        "app/services/chitchat_templates.py",
        "    (re.compile(pattern, re.IGNORECASE), reply) for pattern, reply in TEMPLATES",
        "    (re.compile(pattern), reply) for pattern, reply in TEMPLATES",
        "M9  大小写敏感（『你是AI吗』命不中——大小写敏感时会这样漏）",
    ),
    (
        "app/services/chitchat_templates.py",
        "    for pattern, reply in _COMPILED:",
        "    for pattern, reply in reversed(_COMPILED):",
        "M10 模板顺序反转（无序映射的迭代顺序本来就不确定）",
    ),
    (
        "app/services/orchestrator_service.py",
        "        template_reply = chitchat_templates.match(utterance)\n"
        "        if template_reply is not None:",
        "        template_reply = None\n        if template_reply is not None:",
        "M11 编排层不查模板（闲聊每次都烧一次 qwen-max）",
    ),
    # ---------- 省钱路径写歪 ----------
    (
        "app/services/embedding_cache.py",
        "        return await self._delegate.embed_query(text)",
        "        return await self.embed(text)",
        "M12 query 也走缓存（用户 query 把 Redis 灌满，命中率还是零）",
    ),
    (
        "app/services/embedding_cache.py",
        "        if isinstance(cached, list) and cached:",
        "        if cached is not None:",
        "M13 脏缓存值当成有效向量（垃圾数据一路传到 SQL 才炸）",
    ),
    (
        "app/services/embedding_cache.py",
        '            logger.warning(\n'
        '                "[Cost] Embedding 缓存读取失败，降级为直接计算 key=%s", '
        'key, exc_info=True\n'
        "            )\n"
        "            cached = None",
        "            raise",
        "M14 缓存读失败直接抛（Redis 抖一下，商品检索打成 500）",
    ),
    (
        "app/agent/agent_memory_guard.py",
        "            except asyncio.CancelledError:\n"
        "                raise\n"
        "            except Exception:  # noqa: BLE001 — 巡检失败只记日志，绝不退出循环\n"
        '                logger.error("[Cost] 记忆巡检失败", exc_info=True)',
        "            except asyncio.CancelledError:\n                raise",
        "M15 巡检异常不兜住（一次抖动让『每分钟扫一遍』变成『只扫过一遍』）",
    ),
    (
        "app/agent/agent_factory.py",
        "        with self._lock:\n            snapshot = list(self._cache.items())\n"
        "        for session_id, agent_set in snapshot:\n"
        "            await consumer(session_id, agent_set)\n"
        "        return len(snapshot)",
        "        with self._lock:\n"
        "            for session_id, agent_set in list(self._cache.items()):\n"
        "                await consumer(session_id, agent_set)\n"
        "            return len(self._cache)",
        "M16 锁内 await（持有锁跨 await，堵死整个事件循环）",
    ),
    (
        "app/voice/cached_tts_phrases.py",
        "            if not audio:",
        "            if False:",
        "M17 空音频也缓存（这一句以后永远静音，且不会回落真实 TTS）",
    ),
    (
        "app/services/orchestrator_service.py",
        "            cached = self._tts_cache.get(sentence) if self._tts_cache"
        " is not None else None\n"
        "            if cached is not None:",
        "            cached = None\n            if cached is not None:",
        "M18 _speak 不查 TTS 短语缓存（预热白做，过渡语每次照样计费）",
    ),
    (
        "app/services/product_search_service.py",
        "        vector = await self._embedding.embed_query(query)",
        "        vector = await self._embedding.embed(query)",
        "M19 商品检索退回缓存的 embed（用户 query 全进 Redis，命中率接近零）",
    ),
]


def run_tests() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return True, "（超时 = 有测试挂起了，仍算捕获）"

    lines = [
        line
        for line in result.stdout.strip().splitlines()
        if "passed" in line or "failed" in line or "error" in line
    ]
    return result.returncode != 0, lines[-1] if lines else ""


def main() -> int:
    survivors: list[str] = []

    for path, old, new, label in MUTATIONS:
        target = ROOT / path
        original = target.read_text()
        if old not in original:
            print(f"{label}: 锚点未找到，**跳过**（代码改过了？）", flush=True)
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
