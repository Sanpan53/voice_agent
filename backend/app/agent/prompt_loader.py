"""Prompt 文件加载器。

把每个 Agent 的 System Prompt 放在独立的 ``.txt`` 文件里，业务迭代改 Prompt
不用动代码，Prompt 版本管理也容易。

Prompt 从相对于 ``app/resources/prompts/`` 的普通文件读取。调用方依然沿用
``prompts/xxx.txt`` 这种写法，前缀由本模块负责剥离。
"""

from pathlib import Path

#: Prompt 根目录：``app/resources/prompts/``
DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "resources" / "prompts"

#: 调用方书写路径时统一带的前缀
PROMPT_PREFIX = "prompts/"


class PromptNotFoundError(RuntimeError):
    """Prompt 文件不存在或无法读取。"""


class PromptLoader:
    """从磁盘加载 Prompt 文本。

    Args:
        base_dir: Prompt 根目录，默认 ``app/resources/prompts/``。
            测试里可以指向临时目录。
    """

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir is not None else DEFAULT_PROMPTS_DIR

    @property
    def base_dir(self) -> Path:
        """Prompt 根目录。"""
        return self._base_dir

    def load(self, path: str) -> str:
        """读取 Prompt 文本。

        Args:
            path: 形如 ``prompts/intent.txt`` 的相对路径。前缀 ``prompts/``
                可以省略，写 ``intent.txt`` 也等价。

        Raises:
            PromptNotFoundError: 文件不存在或读取失败。
        """
        relative = path[len(PROMPT_PREFIX):] if path.startswith(PROMPT_PREFIX) else path
        target = self._base_dir / relative
        try:
            return target.read_text(encoding="utf-8")
        except OSError as exc:
            raise PromptNotFoundError(f"加载 prompt 失败：{path}") from exc
