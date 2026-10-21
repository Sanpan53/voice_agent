"""确认 / 取消的本地判定规则。

**为什么单独拎成一个模块**：这两条规则很容易写出会下错单的 bug，
独立成模块才能被穷举测试。核心风险在于**肯定词是单字**：``"好"`` 和 ``"对"``
在中文里没有词边界——用户说 **"不好"** 会命中 ``"好"``，说 **"不对"** 会命中
``"对"``。如果先判 YES、后判 NO，``"不好，换一款"`` 走的就是**下单**分支，
而且**订单是真的落库的**——用户明确表达了否定，系统给他下了单。
这不是体验问题，是事故。

因此这里有三条约束：

1. **否定优先**——先判 NO，命中就直接结束，不再看肯定词；
2. **否定短语显式入表**——"不好" / "不对" / "不行" 都在 NO 里；
3. **单字肯定词只在整句很短时才算数**——真正的确认就是"好"/"嗯"/"行"这种
   一两个字的回答；"好贵啊"（3 个字）不再被当成确认，而是走"没听懂"追问。

> 🔑 **判错的代价是不对称的**：把"不要"判成确认 = 给用户下了一单他没要的
> 东西；把"确认"判成没听懂 = 多问一句"你是要还是不要"。
> **所以所有规则一律朝"宁可多问一句"的方向偏。**
"""

from enum import StrEnum

#: 明确的否定短语。**先于肯定判断匹配**，所以这里列的每一条都是从 bug 里来的。
NO_PHRASES = (
    "不要",
    "不用",
    "不下",
    "不买",
    "不确认",
    "不合适",
    "不划算",
    "不行",
    "不对",
    "不好",
    "别下",
    "别买",
    "算了",
    "取消",
    "再想想",
    "再考虑",
    "再看看",
    "等下",
    "等会",
    "先不",
    "不是",
)

#: 明确的肯定短语。这些**没有歧义**，多长都算数。
YES_PHRASES = (
    "确认",
    "可以",
    "就这",
    "就要",
    "好的",
    "要了",
    "买单",
    "下单",
    "ok",
    "OK",
)

#: **单字**肯定词——只在整句很短时才算数（见模块 docstring）。
BARE_YES_CHARS = ("好", "对", "行", "嗯", "要", "买")

#: 单字肯定词允许的最大整句长度（去空白去标点之后）。
#:
#: 取 2 是量出来的：真实的确认回答是"好"/"嗯"/"好的"/"行吧"/"嗯嗯"，
#: 都是 1-2 个字；而"好贵啊"/"好像不行"这类误命中都是 3 个字起。
#: 调大它会放进误判，调小到 1 会把"好的"漏掉。
MAX_BARE_YES_LENGTH = 2

#: 判定前要剥掉的字符：空白 + 中英文标点。
#: **不剥的话** "好。" 的长度是 2、"嗯，好的" 是 4，长度判断就没意义了。
_STRIPPED = " \t\r\n，。！？、；：,.!?;:~～…"


class Confirmation(StrEnum):
    """用户这一轮对"确认下单吗"的回答。"""

    YES = "YES"
    NO = "NO"
    #: 没听懂——调用方应当追问，**绝不能当成 YES**
    UNCLEAR = "UNCLEAR"


def _normalize(utterance: str | None) -> str:
    """去空白、去标点、转小写。

    只用于**判定**，不改写用户原话本身（原话要进短期记忆和审计日志）。
    """
    text = utterance or ""
    for char in _STRIPPED:
        text = text.replace(char, "")
    return text.lower()


def classify_confirmation(utterance: str | None) -> Confirmation:
    """判定用户是在确认、取消，还是没听懂。

    Args:
        utterance: 用户原话。

    Returns:
        :class:`Confirmation`。空输入一律 ``UNCLEAR``。
    """
    text = _normalize(utterance)
    if not text:
        return Confirmation.UNCLEAR

    # 1. 否定优先——"不好"里也有"好"，顺序反了就下错单
    if any(phrase in text for phrase in NO_PHRASES):
        return Confirmation.NO

    # 2. 无歧义的肯定短语
    if any(phrase in text for phrase in YES_PHRASES):
        return Confirmation.YES

    # 3. 单字肯定词：只在整句很短时才算数（"好"算，"好贵啊"不算）
    if len(text) <= MAX_BARE_YES_LENGTH and any(
        char in text for char in BARE_YES_CHARS
    ):
        return Confirmation.YES

    # 4. 其余一律"没听懂"——宁可多问一句，也不替用户做主
    return Confirmation.UNCLEAR


def contains_yes(utterance: str | None) -> bool:
    """用户是否确认下单。"""
    return classify_confirmation(utterance) is Confirmation.YES


def contains_no(utterance: str | None) -> bool:
    """用户是否取消下单。"""
    return classify_confirmation(utterance) is Confirmation.NO


__all__ = [
    "BARE_YES_CHARS",
    "MAX_BARE_YES_LENGTH",
    "NO_PHRASES",
    "YES_PHRASES",
    "Confirmation",
    "classify_confirmation",
    "contains_no",
    "contains_yes",
]
