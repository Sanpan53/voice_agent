"""订单查询接口。

**当前只有读路径**；下单、状态流转尚未实现。

```bash
TOKEN=...  # 见 /api/v1/auth/login

curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/orders/mine
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/orders/1
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/orders/999   # → 404
```

.. danger::

   **越权必须是 404，不能是 403。** 403 等于承认"这个 id 在库里，只是不归你"，
   拿它当探针就能枚举出平台上订单总量和 id 分布。404 的语义是"对调用方而言
   它不存在"，什么都不泄露。Service 层返回 ``None``，由这里翻成 404——
   两处都要保证**没有任何分支**会返回 403。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.api.dependencies import get_order_service
from app.schemas.order import OrderOut
from app.security.current_user import CurrentUserId
from app.services.order_service import OrderService

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])

#: 越权与不存在共用这一句——不区分的原因见模块 docstring
NOT_FOUND_DETAIL = "订单不存在或无权访问"


@router.get("/mine", response_model=list[OrderOut])
async def list_my_orders(
    user_id: CurrentUserId,
    service: Annotated[OrderService, Depends(get_order_service)],
) -> list[OrderOut]:
    """我的订单列表，按时间倒序。

    ``user_id`` 来自 token，**不接受查询参数**——否则改个 ``?userId=2``
    就能看别人的订单。
    """
    records = await service.list_mine(user_id)
    return [OrderOut.from_record(record) for record in records]


@router.get("/{order_id}", response_model=OrderOut)
async def get_my_order(
    order_id: Annotated[int, Path(gt=0)],
    user_id: CurrentUserId,
    service: Annotated[OrderService, Depends(get_order_service)],
) -> OrderOut:
    """我的某一笔订单。

    Raises:
        HTTPException: 404——**不存在和不是你的一律同一个响应**。
    """
    record = await service.get_for_user(order_id, user_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return OrderOut.from_record(record)
