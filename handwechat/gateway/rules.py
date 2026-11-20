# -*- coding: utf-8 -*-
"""应用在 hello 上报的消息匹配规则：解析、编译与匹配。"""

from __future__ import annotations

from dataclasses import dataclass
import re

from handwechat.gateway.models import IncomingMessage

_MAX_RULES = 32
_MAX_PATTERN_LENGTH = 200
_MAX_TERMS = 32
_MAX_CONTENT_LENGTH = 2000
_ALLOWED_CHAT_TYPES = ("group", "friend")


class RuleError(ValueError):
    """规则格式非法。"""


@dataclass(frozen=True)
class Rule:
    keywords: tuple[str, ...] = ()
    pattern: re.Pattern[str] | None = None
    senders: tuple[str, ...] = ()
    chat_types: tuple[str, ...] = ()

    def matches(
        self, *, content: str, sender: str, chat_type: str | None
    ) -> bool:
        text = content[:_MAX_CONTENT_LENGTH]
        if self.keywords and not any(term in text for term in self.keywords):
            return False
        if self.pattern is not None and self.pattern.search(text) is None:
            return False
        if self.senders and sender not in self.senders:
            return False
        if self.chat_types and (chat_type or "") not in self.chat_types:
            return False
        return True


def parse_hello_rules(hello: dict) -> tuple[Rule, ...] | None:
    """字段缺省返回 None（兼容模式），显式空数组表示全匹配。"""

    if "rules" not in hello:
        return None
    return parse_rules(hello.get("rules"))


def parse_rules(raw) -> tuple[Rule, ...]:
    if not isinstance(raw, list):
        raise RuleError("rules 必须是数组")
    if len(raw) > _MAX_RULES:
        raise RuleError(f"rules 最多 {_MAX_RULES} 条")
    return tuple(_parse_rule(item) for item in raw)


def matches(
    rules: tuple[Rule, ...] | None,
    message: IncomingMessage,
    chat_type: str | None,
) -> bool:
    """None 表示应用未上报规则，按兼容模式全匹配。"""

    if rules is None or not rules:
        return True
    return any(
        rule.matches(
            content=message.content,
            sender=message.sender,
            chat_type=chat_type,
        )
        for rule in rules
    )


def summarize(rules: tuple[Rule, ...] | None) -> dict:
    if rules is None:
        return {"mode": "legacy"}
    if not rules:
        return {"mode": "all"}
    return {
        "mode": "rules",
        "count": len(rules),
        "keywords": sorted({term for rule in rules for term in rule.keywords}),
        "regex": [
            rule.pattern.pattern for rule in rules if rule.pattern is not None
        ],
        "senders": sorted({term for rule in rules for term in rule.senders}),
        "chat_types": sorted(
            {term for rule in rules for term in rule.chat_types}
        ),
    }


def _parse_rule(item) -> Rule:
    if not isinstance(item, dict):
        raise RuleError("rules 中的每一项必须是对象")

    keywords = _parse_terms(item.get("keywords"), "keywords")
    senders = _parse_terms(item.get("senders"), "senders")
    chat_types = _parse_terms(item.get("chat_types"), "chat_types")
    for value in chat_types:
        if value not in _ALLOWED_CHAT_TYPES:
            raise RuleError(f"chat_types 只支持 group / friend：{value!r}")

    pattern = None
    raw_pattern = item.get("regex")
    if raw_pattern is not None:
        if not isinstance(raw_pattern, str) or not raw_pattern:
            raise RuleError("regex 必须是非空字符串")
        if len(raw_pattern) > _MAX_PATTERN_LENGTH:
            raise RuleError(f"regex 最长 {_MAX_PATTERN_LENGTH} 个字符")
        try:
            pattern = re.compile(raw_pattern)
        except re.error as exc:
            raise RuleError(f"regex 非法：{exc}") from exc

    if not keywords and pattern is None and not senders and not chat_types:
        raise RuleError("每条 rules 至少需要一个匹配字段")

    return Rule(
        keywords=keywords,
        pattern=pattern,
        senders=senders,
        chat_types=chat_types,
    )


def _parse_terms(value, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuleError(f"{name} 必须是数组")
    if len(value) > _MAX_TERMS:
        raise RuleError(f"{name} 最多 {_MAX_TERMS} 项")
    terms: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RuleError(f"{name} 中的每一项必须是字符串")
        term = item.strip()
        if not term:
            raise RuleError(f"{name} 不能包含空字符串")
        if term not in terms:
            terms.append(term)
    return tuple(terms)
