from rag_modules.query_normalization import clean_query, DURATION_SUFFIX
"""Conservative, local query preparation. Ambiguity keeps bounded context."""
import os
import re
from functools import lru_cache
from pathlib import Path
from dataclasses import dataclass


def enabled(name):
    return os.getenv(name, "0").lower() in {"1", "true", "yes"}


@lru_cache(maxsize=1)
def dish_names():
    directory = Path(__file__).resolve().parents[1] / "data" / "dishes"
    names = set()
    for path in directory.rglob("*.md"):
        for name in (path.stem, path.parent.name):
            if re.fullmatch(r"[\u4e00-\u9fff]{2,20}", name) and name != "示例菜":
                names.add(name)
    return tuple(sorted(names, key=lambda x: (-len(x), x)))


def mentioned_dishes(query):
    names = []
    for name in dish_names():
        if name in query and not any(name in old for old in names):
            names.append(name)
    return names


@dataclass(frozen=True)
class SimpleQuery:
    dish: str
    intent: str


def simple_query(query):
    # Full-string match prevents constraints, relationships, lists and pasted history
    # from accidentally taking the one-dish shortcut. No invented dishes/ingredients.
    value = clean_query(query).rstrip("？?。！!")
    for name in dish_names():
        for intent, templates in {
            "method": ["{d}怎么做", "{d}怎么做呢", "{d}的做法", "怎么做{d}", "如何做{d}"],
            "ingredients": ["{d}需要哪些材料", "{d}需要什么材料", "{d}用什么食材", "{d}的食材"]
        }.items():
            if any(value == template.format(d=name) for template in templates):
                return SimpleQuery(name, intent)
    return None


@dataclass(frozen=True)
class PreparedQuery:
    text: str
    dependent: bool
    reason: str


def prepare(query, history):
    query = clean_query(query)
    if not history:
        return PreparedQuery(query, False, "no_history")
    independent = bool(simple_query(query)) or bool(
        re.fullmatch(r"[\u4e00-\u9fff]{2,12}(?:怎么做|需要哪些材料)[？?]?", query.strip())
        and not re.search(r"它|这道|那道|这个|那个|不|只|换成|改成|再|更", query))
    # Explicit clearing only affects preparation, never destructively edits stored history.
    if re.search(r"(取消|清除|忽略).*(忌口|限制|条件|要求)", query):
        return PreparedQuery(query, False, "explicit_reset")
    # Preserve prior restrictions conservatively; structured lifetime/scope is future work.
    reset_index = -1
    for i, item in enumerate(history):
        if re.search(r"(取消|清除|忽略).*(忌口|限制|条件|要求)", item["query"]):
            reset_index = i
    active = history[reset_index+1:]
    constraints = [item["query"] for item in active if re.search(
        r"过敏|忌口|不能吃|不吃|不要|不放|不含|[0-9一二三四五六七八九十]+分钟|人份", item["query"])]
    if constraints:
        notes = "\n".join(constraints[-3:])
        # Preserve history for dependent questions as well, e.g. '它炖多久'.
        if independent:
            return PreparedQuery("历史用户要求（判断对当前菜谱是否适用）：\n"+notes+
                                 "\n当前问题："+query, True, "constraints_preserved")
    if independent or re.match(r"^(换个话题|不聊之前|别管刚才)", query):
        if not constraints:
            return PreparedQuery(query, False, "independent")
    # Only resolve a pronoun when exactly one dish was explicit in the last USER turn.
    # Do not guess from a multi-dish recommendation or from generated assistant content.
    previous = mentioned_dishes(history[-1]["query"])
    if not constraints and len(previous)==1 and re.fullmatch(
        r"(?:它|这道菜|那道菜)" + DURATION_SUFFIX + r"[？?]?", query.strip()):
        text = re.sub(r"^(它|这道菜|那道菜)", previous[0], query.strip())
        return PreparedQuery(text, True, "pronoun_resolved")
    parts=[]
    context_history = history[max(0, reset_index):]
    for item in context_history[-3:]:
        parts.extend(["用户问: "+item["query"], "AI答: "+item["response"][:100]])
    if constraints:
        parts.insert(0, "历史用户要求："+"；".join(constraints[-3:]))
    parts.append("当前问题: "+query)
    return PreparedQuery("\n".join(parts), True, "ambiguous_context_fallback")
