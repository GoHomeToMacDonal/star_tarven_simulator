"""描述文本 -> handler 的注册表（用于不规则、无法参数化的效果）。

key = 归一化后的描述文本（见 :func:`parsing.text.normalize`）。
value = ``(event_name, handler_or_wrapper)``，``event_name`` 可为字符串或字符串列表。
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple, Union

# 描述文本 -> (event_name(s), handler)
ACTION_HANDLERS: Dict[str, Tuple[Union[str, List[str]], Callable]] = {}


def register(description: str, event_name: Union[str, List[str]]):
    """装饰器：把 handler 注册到 :data:`ACTION_HANDLERS`。"""

    def deco(fn: Callable) -> Callable:
        ACTION_HANDLERS[description] = (event_name, fn)
        return fn

    return deco


def register_value(description: str, event_name: Union[str, List[str]], handler: Callable) -> None:
    """直接注册一个 handler / 包装器（用于 TaskActionHandler 等非函数对象）。"""
    ACTION_HANDLERS[description] = (event_name, handler)
