"""把商品结构化字段拼成可检索文本。"""

from typing import Any

# 品类属性 -> 中文标签。
ATTRIBUTE_NAMES: dict[str, str] = {
    "cushion": "缓震",
    "weight": "重量",
    "gender": "适合性别",
    "waterproof": "防水",
    "finish": "妆感",
    "color": "颜色",
    "movement": "机芯",
    "material": "材质",
}


def _humanize(attributes: dict[str, Any]) -> str:
    """把属性字典转成 ``标签是值`` 短语列表。"""
    parts: list[str] = []
    for key, value in attributes.items():
        label = ATTRIBUTE_NAMES.get(key, key)
        parts.append(f"{label}是{value}")
    return "，".join(parts)


def build(
    name: str,
    brand: str | None,
    category_l2: str,
    selling_points: str | None,
    description: str | None,
    attributes: dict[str, Any],
) -> str:
    """拼装可检索文本。

    结构：名称 + 品牌品类 + 卖点（重复两次以加权）+ 描述 + 属性，用 ``。`` 连接。
    检索质量主要取决于这段文本的质量，因此把卖点权重翻倍。
    """
    chunks: list[str] = [name]

    brand_category = " ".join(p for p in (brand, category_l2) if p)
    if brand_category:
        chunks.append(brand_category)

    if selling_points:
        chunks.extend([selling_points, selling_points])

    if description:
        chunks.append(description)

    attr_text = _humanize(attributes)
    if attr_text:
        chunks.append(attr_text)

    return "。".join(chunks)
