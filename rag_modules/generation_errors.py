class GenerationError(RuntimeError):
    """Generation did not complete successfully. Never cache this as an answer."""


def usable_answer(value):
    return isinstance(value, str) and bool(value.strip()) and not value.lstrip().startswith((
        "抱歉，生成回答时出现错误", "抱歉，生成回答时出现网络错误", "抱歉，处理您的问题时出现错误"))
