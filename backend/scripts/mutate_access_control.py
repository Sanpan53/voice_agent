"""变异测试：验证新测试真的能抓到回归。

做法：逐条给生产代码注入一个"看起来合理但错了"的改动，跑全量测试，
**必须有测试变红**才算这条测试是有效的。跑完无论成败都还原。

用法::

    python scripts/mutate_access_control.py

每条用 ``subprocess`` 起独立的 pytest 进程并带 ``timeout``——变异可能让某条
测试从"失败"变成"挂起"，没有超时的话整个脚本会卡死在那儿。
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 单次 pytest 的超时（秒）。正常全量约 2 秒，给足余量。
TIMEOUT = 120

MUTATIONS = [
    (
        "app/schemas/session.py",
        "        return not self.allowed_merchant_ids",
        "        return self.allowed_merchant_ids is None",
        "M1  空列表不再等于全平台",
    ),
    (
        "app/services/session_scope_service.py",
        "            return [product.merchant_id]",
        "            return None",
        "M2  商详页入口丢失商家锁定（静默放宽）",
    ),
    (
        "app/security/current_user.py",
        "        return decode_token(token)\n    if tokens_available() and token:",
        "        return decode_token(token) or fallback_user_id\n"
        "    if tokens_available() and token:",
        "M3  认证开启时回退到调用方传的 userId（越权）",
    ),
    (
        "app/services/recommend_orchestrator.py",
        "            query, relaxed, CANDIDATE_TOP_N, scope\n        )",
        "            query, relaxed, CANDIDATE_TOP_N, None\n        )",
        "M4  兜底重试时放宽了商家范围",
    ),
    (
        "app/api/orders.py",
        "        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, "
        "detail=NOT_FOUND_DETAIL)",
        "        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, "
        "detail=NOT_FOUND_DETAIL)",
        "M5  越权返回 403 而非 404（泄露存在性）",
    ),
    (
        "app/security/tokens.py",
        '    for raw in header_value.split(","):\n'
        "        candidate = raw.strip()\n"
        "        if candidate.startswith(SUBPROTOCOL_PREFIX):",
        "    for raw in [header_value]:\n"
        "        candidate = raw.strip()\n"
        "        if candidate.startswith(SUBPROTOCOL_PREFIX):",
        "M6  子协议按整头匹配（多子协议时漏 token）",
    ),
    (
        "app/services/recommend_candidates_service.py",
        "        visible = self._drop_out_of_scope(products, scope)",
        "        visible = products",
        "M7  取消兜底过滤",
    ),
    (
        "app/services/parallel_recommend_service.py",
        "        scope = await resolve_scope(self._scope_cache, session_id, user_id)",
        "        scope = None",
        "M8  并行版丢掉了 scope",
    ),
    (
        "app/services/session_service.py",
        "            merchant_id=merchant_id,",
        "            merchant_id=None,",
        "M9  会话主表不写 merchant_id（上下文丢失）",
    ),
    (
        "app/security/current_user.py",
        "            detail=UNAUTHORIZED_DETAIL,",  # noqa: E501 — 长字符串是要匹配的原文
        '            detail=f"未登录或已过期: {authorization}",',
        "M10 401 文案泄露调用方传的凭证",
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
