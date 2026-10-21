"""会话范围契约。"""

from enum import StrEnum

from pydantic import Field

from app.schemas.common import CamelModel


class Channel(StrEnum):
    """会话入口类型，**决定本次会话的商家范围**。

    用 ``StrEnum`` 是为了让它能直接参与 JSON 序列化
    （``Channel.HOME_ENTRY`` 本身就是 ``"HOME_ENTRY"``），省掉
    ``.value`` 和 ``.name()`` 的来回转换。
    """

    #: 平台首页 → 全平台
    HOME_ENTRY = "HOME_ENTRY"
    #: 商详页 → 锁定到该商品所属商家
    PRODUCT_PAGE = "PRODUCT_PAGE"
    #: 商家店铺首页 → 锁定到该商家
    MERCHANT_HOME = "MERCHANT_HOME"
    #: 搜索无结果兜底 → 全平台
    SEARCH_FALLBACK = "SEARCH_FALLBACK"


#: 需要显式指定商家范围的入口
SCOPED_CHANNELS = frozenset({Channel.PRODUCT_PAGE, Channel.MERCHANT_HOME})


class SessionScope(CamelModel):
    """本次会话能看到哪些商家的商品。

    ``allowed_merchant_ids`` 为 ``None`` **或空列表**都表示"全平台"。

    > ⚠️ **空列表 = 全平台，不是"什么都看不到"**。这个方向搞反的后果是两个极端：
    > 把空列表当"无权限"会让首页入口的用户一条都搜不到；把 ``None`` 当"无权限"
    > 同理。而如果哪天变成"空列表 = 全平台"被误实现成"空列表 = 不过滤"——
    > 那反而是对的。真正的风险在于**将来有人用空列表表示"无权限"**，
    > 所以 :meth:`is_platform_wide` 是唯一判据，不要在各处自己写 ``if not ids``。
    """

    user_id: int
    #: ``None`` / 空 = 全平台；否则只允许这些商家的商品
    allowed_merchant_ids: list[int] | None = None
    #: 若从商详页进入，挂载的商品 ID（用于"这个商品怎么样"这类指代）
    bound_product_id: int | None = None

    def is_platform_wide(self) -> bool:
        """是否全平台可见。"""
        return not self.allowed_merchant_ids


class StartSessionRequest(CamelModel):
    """``POST /api/v1/session/start`` 的请求体。"""

    session_id: str = Field(min_length=1, max_length=64)
    channel: Channel
    #: ``MERCHANT_HOME`` 必填
    merchant_id: int | None = Field(default=None, gt=0)
    #: ``PRODUCT_PAGE`` 必填
    bound_product_id: int | None = Field(default=None, gt=0)
