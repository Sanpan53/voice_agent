"""变异测试：验证新测试真的能抓到回归。

做法：逐条给生产代码注入一个"看起来合理但错了"的改动，跑全量测试，
**必须有测试变红**才算这条测试是有效的。跑完无论成败都还原。

变异分三类：

+ **会下错单的**（M1-M4）——否定判成肯定、越界夹到最后一款、丢权限、丢归属；
+ **会丢数据的**（M5-M8）——不提交、提交晚于回流、跳过扣库存、绕过条件 UPDATE；
+ **会卡住用户的**（M9-M11）——失败后不退回 RECOMMEND、听不懂就当确认、
  下单后不离开待确认状态。

用法::

    python scripts/mutate_order_flow.py
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 单次 pytest 的超时（秒）。全量约 2.4 秒，给足余量。
TIMEOUT = 180

MUTATIONS = [
    # ---------- 会下错单的 ----------
    (
        "app/services/order_intent_rules.py",
        "    if any(phrase in text for phrase in NO_PHRASES):\n"
        "        return Confirmation.NO",
        "    if False:\n        return Confirmation.NO",
        "M1  否定不再优先（bug：\"不好\"被判成确认）",
    ),
    (
        "app/services/order_intent_rules.py",
        "    if len(text) <= MAX_BARE_YES_LENGTH and any(",
        "    if len(text) <= 999 and any(",
        "M2  去掉单字肯定词的长度闸门（\"好贵啊\"算确认）",
    ),
    (
        "app/services/order_reference_resolver.py",
        r'    rf"第\s*([一二三四五1-5])|([一二三四五1-5])\s*[{_MEASURE_WORDS}]"',
        '    r"[一二三四五1-5]"',
        "M3  退回宽松正则（\"便宜一点\"下单第一款）",
    ),
    (
        "app/services/order_reference_resolver.py",
        "        if index is None or not 0 <= index < len(last):",
        "        if index is None:",
        "M4  去掉越界检查（\"第五款\"不再算失败）",
    ),
    (
        "app/services/order_service.py",
        "        if not _in_scope(product, scope):",
        "        if False:",
        "M5  下单不做商家范围校验",
    ),
    (
        "app/services/order_service.py",
        "        if pending.user_id != user_id:",
        "        if False:",
        "M6  确认别人的预览单（横向越权）",
    ),
    # ---------- 会丢数据的 ----------
    (
        "app/infrastructure/database.py",
        "        else:\n            await session.commit()",
        "        else:\n            pass",
        "M7  请求成功不提交（最初的原始 bug）",
    ),
    (
        "app/infrastructure/database.py",
        "        except Exception:\n            await session.rollback()\n            raise",
        "        except Exception:\n            raise",
        "M8  异常不回滚（半个订单留在库里）",
    ),
    (
        "app/services/order_service.py",
        "        await self._repo.commit()",
        "        pass",
        "M9  下单不提交（用户被告知成功后库里没有）",
    ),
    (
        "app/services/order_service.py",
        "        if not decremented:\n"
        "            logger.warning("
        '"[Order] 扣库存失败（并发抢购？）'
        'productId=%s", pending.product_id)\n'
        '            raise OrderError("库存不足")',
        "        if not decremented:\n"
        "            logger.warning("
        '"[Order] 扣库存失败（并发抢购？）'
        'productId=%s", pending.product_id)',
        "M10 扣库存失败仍然继续下单（超卖）",
    ),
    (
        "app/repositories/product_repository.py",
        "            .where(Product.id == product_id, Product.stock >= quantity)",
        "            .where(Product.id == product_id)",
        "M11 扣库存去掉 stock >= qty 条件（并发超卖）",
    ),
    (
        "app/services/order_service.py",
        "            except Exception:  # noqa: BLE001 — 画像回流失败不影响下单\n"
        '                logger.error("购买行为回流失败 orderId=%s", order.id, exc_info=True)',
        "            except Exception:  # noqa: BLE001 — 画像回流失败不影响下单\n"
        '                logger.error("购买行为回流失败 orderId=%s", order.id, exc_info=True)\n'
        "                raise",
        "M12 画像回流失败把已提交的订单变成\"下单失败\"",
    ),
    # ---------- 会卡住用户的 ----------
    (
        "app/services/orchestrator_service.py",
        "        if state.phase == PHASE_ORDER_CONFIRM and self._orders is not None:",
        "        if False:",
        "M13 去掉订单分支短路（\"再想想\"被 LLM 判成闲聊）",
    ),
    (
        "app/services/orchestrator_service.py",
        '                logger.warning("[Orc] 下单失败 sessionId=%s: %s", session_id, exc)\n'
        "                state.phase = PHASE_RECOMMEND",
        '                logger.warning("[Orc] 下单失败 sessionId=%s: %s", session_id, exc)\n'
        "                state.phase = PHASE_ORDER_CONFIRM",
        "M14 下单失败后仍留在待确认状态（用户被困）",
    ),
    (
        "app/services/orchestrator_service.py",
        "        state.phase = PHASE_ORDER_CONFIRM\n"
        "        return EmotionResult(speech_text=ORDER_ASK_CONFIRM_SPEECH, display_blocks=[])",
        "        state.phase = PHASE_RECOMMEND\n"
        "        return EmotionResult(speech_text=ORDER_ASK_CONFIRM_SPEECH, display_blocks=[])",
        "M15 没听懂就退出待确认状态（预览单永远等不到确认）",
    ),
    (
        "app/services/order_intent_rules.py",
        "    # 4. 其余一律\"没听懂\"——宁可多问一句，也不替用户做主\n"
        "    return Confirmation.UNCLEAR",
        "    # 4. 其余一律\"没听懂\"——宁可多问一句，也不替用户做主\n"
        "    return Confirmation.YES",
        "M16 兜底值改成 YES（没听懂 = 替用户下单）",
    ),
    (
        "app/services/orchestrator_service.py",
        "            state.phase = PHASE_ENDED\n"
        "            tail = order.order_no[-ORDER_NO_DISPLAY_LEN:]",
        "            state.phase = PHASE_ORDER_CONFIRM\n"
        "            tail = order.order_no[-ORDER_NO_DISPLAY_LEN:]",
        "M17 下单成功后不离开待确认状态（再说一句\"好\"又是一单）",
    ),
    (
        "app/services/order_service.py",
        "        if self._pending is not None:\n"
        "            await self._pending.remove(session_id)\n\n"
        "        # 下单成功 = 一次会话的\"有效结束\"",
        "        if False:\n"
        "            await self._pending.remove(session_id)\n\n"
        "        # 下单成功 = 一次会话的\"有效结束\"",
        "M18 下单后不清理预览单（同一单能被确认两次）",
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
