"""描述文本 -> handler 的注册表（用于不规则、无法参数化的效果）。

key = 归一化后的描述文本（见 :func:`parsing.text.normalize`）。
value = ``(event_name, handler_or_wrapper)``，``event_name`` 可为字符串或字符串列表。

:func:`register` 与 :func:`register_value` 共用 :func:`_register` helper：
重复 key 一律抛 :class:`ValueError`（错误信息含 description），不允许静默覆盖。
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple, Union

# 描述文本 -> (event_name(s), handler)
ACTION_HANDLERS: Dict[str, Tuple[Union[str, List[str]], Callable]] = {}


def _register(description: str, event_name: Union[str, List[str]], handler: Callable) -> None:
    """共享注册 helper：重复 key 抛 ValueError，禁止静默覆盖。"""
    if description in ACTION_HANDLERS:
        existing = ACTION_HANDLERS[description]
        raise ValueError(
            f"ACTION_HANDLERS 重复注册 key: {description!r}（事件 {event_name}），"
            f"已存在: {existing!r}；禁止静默覆盖"
        )
    ACTION_HANDLERS[description] = (event_name, handler)


def register(description: str, event_name: Union[str, List[str]]):
    """装饰器：把 handler 注册到 :data:`ACTION_HANDLERS`。"""

    def deco(fn: Callable) -> Callable:
        _register(description, event_name, fn)
        return fn

    return deco


def register_value(description: str, event_name: Union[str, List[str]], handler: Callable) -> None:
    """直接注册一个 handler / 包装器（用于 TaskActionHandler 等非函数对象）。"""
    _register(description, event_name, handler)
