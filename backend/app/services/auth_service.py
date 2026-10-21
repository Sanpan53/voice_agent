"""登录服务：校验用户名并签发 token。

登录逻辑本身很短，但**演示项目的安全边界要写在代码里**，不能只写在文档中——
见 :meth:`AuthService.login` 的说明。
"""

import logging

from app.repositories.user_repository import AppUserRepository
from app.security.tokens import issue_token

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """登录失败。接口层把它翻成 401。"""


class AuthService:
    """按用户名签发 token。

    Args:
        repo: 用户仓储。
    """

    def __init__(self, repo: AppUserRepository) -> None:
        self._repo = repo

    async def login(self, username: str, password: str | None = None) -> tuple[str, int]:
        """校验用户名并签发 token。

        .. danger::

           **这个实现不做密码校验，是本项目的刻意简化，不是遗漏。**

           ``app_user`` 表里压根没有 password 字段——本项目刻意这么设计
           （登录请求体里有 ``password``，但服务端从头到尾没用过它）。
           参数保留是为了**接口契约稳定**：前端现在就在传，将来补上校验时
           不用改前端。

           生产环境必须换成密码哈希校验（bcrypt / argon2）或短信验证码。
           这里显式收下 ``password`` 参数并忽略，比完全不接要好——
           至少调用方不会以为"不传密码就能登录"是个正常设计。

        Returns:
            ``(token, user_id)``。

        Raises:
            AuthError: 用户名为空 / 用户不存在 / 用户已注销。
        """
        if not username or not username.strip():
            raise AuthError("username 不能为空")

        user = await self._repo.find_by_username_and_status(username.strip())
        if user is None:
            # 用户不存在与已注销返回**同一个**错误——不区分，避免暴露
            # "这个用户名存在但被注销了"这种信息，统一按
            # "用户不存在或已注销" 这一条话术返回。
            logger.info("登录失败：用户不存在或已注销 username=%s", username)
            raise AuthError("用户不存在或已注销")

        token = issue_token(user.id)
        logger.info("登录成功 userId=%s", user.id)
        return token, user.id
