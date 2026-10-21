"""澄清规则服务——按品类查必填槽位表，算缺失字段。

设计思路是**规则优先、LLM 包装**：

    阶段 1：规则层（快、稳）——按品类查"必填槽位表"，算差值
    阶段 2：LLM 层（生成话术）——拿到缺的槽位名，让 LLM 包装成自然追问

好处：判断不靠 LLM 的随机性，追问话术又能自然。
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: 规则文件位置
DEFAULT_RULES_PATH = (
    Path(__file__).resolve().parent.parent / "resources" / "clarify" / "required-slots.yml"
)

#: 品类不在表里时使用的兜底规则名
DEFAULT_CATEGORY = "default"


class ClarifyRuleError(RuntimeError):
    """规则文件缺失或格式非法。"""


@dataclass(frozen=True)
class CategoryRule:
    """一个品类的槽位规则。"""

    required: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    scenario_options: list[str] = field(default_factory=list)


class ClarifyRuleService:
    """品类必填槽位表。

    Args:
        rules_path: 规则 YAML 路径，默认 ``app/resources/clarify/required-slots.yml``。
            测试里可以指向临时文件。

    Note:
        规则 YAML 在 ``__init__`` 里一次性读进内存——规则文件是配置，改了要重启才生效。
    """

    def __init__(self, rules_path: Path | str | None = None) -> None:
        self._rules_path = Path(rules_path) if rules_path is not None else DEFAULT_RULES_PATH
        self._rules = self._load(self._rules_path)

    @staticmethod
    def _load(path: Path) -> dict[str, CategoryRule]:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ClarifyRuleError(f"加载澄清规则失败：{path}") from exc

        if not isinstance(raw, dict):
            raise ClarifyRuleError(f"澄清规则格式非法，应为品类到规则的映射：{path}")

        rules: dict[str, CategoryRule] = {}
        for category, config in raw.items():
            config = config or {}
            rules[str(category)] = CategoryRule(
                required=list(config.get("required") or []),
                nice_to_have=list(config.get("nice_to_have") or []),
                scenario_options=list(config.get("scenario_options") or []),
            )

        if DEFAULT_CATEGORY not in rules:
            raise ClarifyRuleError(f"澄清规则缺少 {DEFAULT_CATEGORY} 兜底项：{path}")
        return rules

    @property
    def categories(self) -> list[str]:
        """已配置的品类（含 ``default``）。"""
        return list(self._rules)

    def rule_for(self, category: str | None) -> CategoryRule:
        """取某个品类的规则；品类为空或未配置时回退到 ``default``。"""
        if category is None:
            return self._rules[DEFAULT_CATEGORY]
        return self._rules.get(category, self._rules[DEFAULT_CATEGORY])

    def missing_slots(self, category: str | None, slots: dict | None) -> list[str]:
        """算出还缺哪些必填槽位。

        返回顺序即优先级：按规则表 ``required`` 的书写顺序。

        Note:
            ``missing_slots`` **只遍历 ``required``**；``nice_to_have`` 加载后
            不参与计算，只作为规则表元数据保留，不影响 ``READY`` / ``ASK`` 的判定。
        """
        rule = self.rule_for(category)
        slots = slots or {}
        return [name for name in rule.required if slots.get(name) is None]
