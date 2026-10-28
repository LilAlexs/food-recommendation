"""Strip conversational wrappers, never ingredients, negation or constraints."""
import re

TOPIC = re.compile(r'^(?:换个话题|换一个话题|换一道菜|换个菜|另外问一下|另外问个问题)[，,。:：\s]*')
POLITE = re.compile(r'^(?:请问|请告诉我|请教我|我想知道|我想学做|我想做)[，,：:\s]*')
DURATION_ACTION = r'(?:腌制|腌|炖|炒|煮|蒸|焖)'
DURATION_SUFFIX = r'(?:需要|要)?' + DURATION_ACTION + r'?(?:多久|多长时间)'


def has_topic_switch(query):
    return bool(TOPIC.match(POLITE.sub('',query.strip()).strip()))


def clean_query(query):
    value=query.strip()
    for _ in range(4):
        updated=POLITE.sub('',TOPIC.sub('',value)).strip()
        if updated==value:break
        value=updated
    return value
