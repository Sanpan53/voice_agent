"""画像加权重排。

向量相似度只是语义层的，最终排序还要把用户画像算进去：偏好品牌加分、价格远超平均
客单价减分（价格敏感用户）、最近买过同款减分。
"""

from typing import Any

from app.schemas.profile import UserProfileSnapshot
from app.schemas.recommend import RecommendedItem

# ===== budget 锚点：用户给了预算上限时，价格越接近上限（60%~95% 档）的越优先 =====
# 目的是——用户说"预算 2000"时，客单心锚在 2000 附近，不是奔着 500 去的；推荐应该
# 倾向接近 budget 的中高端，而不是一来就把最低价顶上来。
BUDGET_HIGH_RATIO = 0.6
BUDGET_HIGH_BONUS = 0.25
BUDGET_MID_RATIO = 0.4
BUDGET_MID_BONUS = 0.05
#: 远低于预算，轻微扣分（可能档次不够）
BUDGET_LOW_PENALTY = -0.10

#: 品牌亲和度权重
BRAND_AFFINITY_WEIGHT = 0.2

#: 价格 / 平均客单价 超过这个倍数就算"偏贵"
PRICE_RATIO_THRESHOLD = 1.5
#: 画像没给价格敏感度时的默认值
DEFAULT_PRICE_SENSITIVITY = 0.5
#: 偏贵时按敏感度扣的分
PRICE_SENSITIVITY_PENALTY = 0.15

#: 最近已购买过同款商品的扣分
REPEAT_PURCHASE_PENALTY = 0.3


def _as_number(value: Any) -> float | None:
    """把槽位里的 budget 转成 float；非数值返回 ``None``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_float(value: Any) -> float | None:
    """把 ``Decimal | None`` 一类的画像字段转成 float。"""
    if value is None:
        return None
    return float(value)


class ProfileReranker:
    """按画像给候选打分并重排。"""

    def rerank(
        self,
        candidates: list[RecommendedItem],
        profile: UserProfileSnapshot | None,
        slots: dict[str, Any] | None,
    ) -> list[RecommendedItem]:
        """返回按综合分从高到低排序的新列表。

        Note:
            ``profile`` 为 ``None`` 时**也要走 budget 锚点打分**，不能直接 return——
            新用户没有画像，但"预算 2000"这个心锚依然有效。
        """
        scored = [
            item.with_match_score(self.compute_score(item, profile, slots))
            for item in candidates
        ]
        scored.sort(key=lambda item: item.match_score, reverse=True)
        return scored

    @staticmethod
    def compute_score(
        item: RecommendedItem,
        profile: UserProfileSnapshot | None,
        slots: dict[str, Any] | None,
    ) -> float:
        """单条商品的综合分。"""
        score = item.match_score

        budget = _as_number((slots or {}).get("budget"))
        if budget is not None and budget > 0 and item.price <= budget:
            ratio = item.price / budget
            if ratio >= BUDGET_HIGH_RATIO:
                score += BUDGET_HIGH_BONUS  # 主推档
            elif ratio >= BUDGET_MID_RATIO:
                score += BUDGET_MID_BONUS  # 中等档
            else:
                score += BUDGET_LOW_PENALTY

        if profile is not None:
            # 偏好品牌加分。brand 从 attributes JSONB 里取——数据里
            # 没写 brand 时亲和度就是 0，不加不扣。
            brand = str(item.attributes.get("brand") or "")
            score += float(profile.brand_affinity.get(brand, 0.0)) * BRAND_AFFINITY_WEIGHT

            # 价格敏感：超过用户平均客单价太多扣分。
            # 这里多一个 avg > 0 的守卫：avg=0 会算出 Infinity 从而稳定命中扣分分支；
            # avg 为 0 实际含义是"没有历史订单"，不该因此扣分。
            avg_order_amount = _as_float(profile.avg_order_amount)
            if avg_order_amount is not None and avg_order_amount > 0:
                if item.price / avg_order_amount > PRICE_RATIO_THRESHOLD:
                    sensitivity = _as_float(profile.price_sensitivity)
                    if sensitivity is None:
                        sensitivity = DEFAULT_PRICE_SENSITIVITY
                    score -= PRICE_SENSITIVITY_PENALTY * sensitivity

            # 最近已经买过同款扣分
            if item.product_id in profile.recent_purchased:
                score -= REPEAT_PURCHASE_PENALTY

        return score
