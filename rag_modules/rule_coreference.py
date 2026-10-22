# rag_modules/rule_coreference.py
import re
from typing import Tuple, List

# 指代代词词表（美食场景专用）
PRONOUNS = {"它", "这", "那", "这个", "那个", "它的", "这道菜", "那道菜", "这道菜的", "那道菜的"}

# 实体提取正则：简单抓取菜名（你可以后续扩充，这里简易版）
# 匹配：红烧肉、番茄炒蛋这类中文菜名，按需扩充
DISH_PATTERN = re.compile(r"([\u4e00-\u9fa5]{2,6}(?:菜|煲|汤|面|肉|鸡|鱼|排|饼|粥))")

def has_pronoun(query: str) -> bool:
    """规则判断：用户当前问句是否包含指代代词"""
    for p in PRONOUNS:
        if p in query:
            return True
    return False

def extract_dishes(text: str) -> List[str]:
    """从一段文本里提取菜名实体，去重"""
    res = DISH_PATTERN.findall(text)
    return list(set(res))

def rewrite_query_with_history(current_query: str, history: List[dict]) -> Tuple[str, bool]:
    """
    纯规则改写检索query
    :param current_query: 用户本轮原始问题
    :param history: 对话历史 [{"role":"user", "content":xxx}, {"role":"assistant", "content":xxx}]
    :return: (改写后的query, 是否进行了改写)
    """
    if not has_pronoun(current_query):
        return current_query, False
    
    # 从历史，倒序遍历，拿到最近出现的菜名（优先最新）
    recent_dish = None
    for msg in reversed(history):
        dishes = extract_dishes(msg["content"])
        if dishes:
            recent_dish = dishes[0]
            break
    
    # 如果抓到菜名，替换代词；抓不到，保留原query
    if recent_dish:
        rewritten = current_query
        # 简单替换，可扩充替换逻辑
        for p in PRONOUNS:
            if p in rewritten:
                rewritten = rewritten.replace(p, recent_dish)
        return rewritten, True
    else:
        # 有代词但历史找不到菜，直接使用原query
        return current_query, False
