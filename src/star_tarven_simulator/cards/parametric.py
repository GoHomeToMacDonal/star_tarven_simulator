"""参数化 handler 解析器。

新版描述用颜色标记语义类别、数值仍在文本里。对于结构规整的机制族（反应堆生产、快速生产、
集群、集结、灵能、供养，以及"触发时机 + 简单获得/折跃/注卵/孵化"），这里直接从文本抽取
数值/单位并合成 handler —— 无需为普通/金色、以及数值不同的同族卡各写一份。

每个解析器输入 ``(text, colors)``（``text`` 已归一化），返回
``(event_name, handler)`` 或 ``None``。
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

from star_tarven_simulator.cards.mechanics import feed, hatch, teleport
from star_tarven_simulator.parsing.text import (
    extract_units,
    is_unit_known,
    units_dict,
)
from star_tarven_simulator.simulator.event_handler import (
    GatheringActionHandler,
    TaskActionHandler,
)

Resolved = Optional[Tuple[object, Callable]]

# ---------------------------------------------------------------------------
# 触发时机识别
# ---------------------------------------------------------------------------
# 顺序敏感：更具体的前缀在前
_TRIGGERS: List[Tuple[str, str]] = [
    ("任意卡牌进场或出售时", "any_card_entered_or_sold"),
    ("任意卡牌进场时", "any_card_entered"),
    ("任意卡牌出售时", "any_card_sold"),
    ("任意卡牌注卵时", "any_card_larva"),
    ("任意卡牌折跃时", "any_card_teleport"),
    ("任意任务完成时", "any_task_finished"),
    ("获得黑暗值时", "gain_darkness"),
    ("每回合开始时", "round_start"),
    ("回合开始时", "round_start"),
    ("每回合结束时", "round_end"),
    ("提升酒馆等级时", "level_up"),
    ("刷新时", "refresh"),
    ("进场时", "entering"),
    ("出售时", "selling"),
]


def detect_trigger(text: str) -> Optional[Tuple[str, str]]:
    """返回 ``(event_name, body)``，body 为触发短语之后的正文。"""
    for prefix, event_name in _TRIGGERS:
        if text.startswith(prefix):
            body = text[len(prefix):].lstrip(",: ")
            return event_name, body
    return None


# ---------------------------------------------------------------------------
# 简单正文：获得 / 折跃 / 注卵 / 孵化 + "N单位[和M单位]"
# ---------------------------------------------------------------------------
_SIMPLE_BODY_RE = re.compile(r"^(获得|折跃|注卵|孵化)(.+)$")


def _is_pure_unit_list(rest: str) -> bool:
    """rest 是否仅由 'N单位' 以 和/,/、 连接构成（无其它多余内容）。"""
    units = extract_units(rest)
    if not units:
        return False
    # 逐个消费 "数字+单位"，其余只允许连接符/空白
    leftover = rest
    for n, unit in units:
        token = f"{n}{unit}"
        idx = leftover.find(token)
        if idx == -1:
            # 允许数字与单位间有空格
            token2 = re.sub(r"^(\d+)", r"\1\\s*", re.escape(token))
            m = re.search(token2, leftover)
            if not m:
                return False
            leftover = leftover[: m.start()] + leftover[m.end():]
        else:
            leftover = leftover[:idx] + leftover[idx + len(token):]
    leftover = re.sub(r"[和,、\s]", "", leftover)
    return leftover == ""


def simple_body_handler(body: str, times_scaled: bool = False) -> Optional[Callable]:
    """把 '获得/折跃/注卵/孵化 N单位...' 正文编译成 handler。

    ``times_scaled=True`` 时（集结），数量乘以 ``times``，handler 签名为 ``(slot, event, times)``。
    无法识别为纯单位列表时返回 ``None``（交给注册表兜底）。
    """
    m = _SIMPLE_BODY_RE.match(body)
    if not m:
        return None
    verb, rest = m.group(1), m.group(2)
    if not _is_pure_unit_list(rest):
        return None
    units = extract_units(rest)

    if times_scaled:
        def handler(slot, event, times, _verb=verb, _units=units):
            _apply(slot, event, _verb, [(n * times, u) for n, u in _units])
        return handler

    def handler(slot, event, _verb=verb, _units=units):
        _apply(slot, event, _verb, _units)

    return handler


def _apply(slot, event, verb: str, units: List[Tuple[int, str]]) -> None:
    if verb == "获得":
        for n, u in units:
            slot.add_unit(u, n)
    elif verb == "折跃":
        teleport(slot, event, {u: n for n, u in units})
    elif verb == "注卵":
        event.tarven.larva({u: n for n, u in units})
    elif verb == "孵化":
        hatch(slot, event, {u: n for n, u in units})


# ---------------------------------------------------------------------------
# 机制前缀解析器
# ---------------------------------------------------------------------------
def resolve_reactor(text: str, colors) -> Resolved:
    """反应堆生产X[和Y] —— 每回合结束，获得 (1 + 金色) 个每种单位。"""
    m = re.match(r"^反应堆生产(.+)$", text)
    if not m:
        return None
    body = m.group(1)
    # 形如 "陆战队员" 或 "陆战队员(精英)和医疗运输机"：每种各产 1（+金色）
    if is_unit_known(body):
        units = [(1, body)]
    else:
        units = []
        for part in re.split(r"和", body):
            part = part.strip()
            if not is_unit_known(part):
                return None
            units.append((1, part))
        if not units:
            return None

    def handler(slot, event, _units=units):
        bonus = slot.tags.has("金色")
        for n, u in _units:
            slot.add_unit(u, n + bonus)

    return ("round_end", handler)


def resolve_quick_produce(text: str, colors) -> Resolved:
    """快速生产:body。"""
    if not text.startswith("快速生产:"):
        return None
    body = text[len("快速生产:"):]
    handler = simple_body_handler(body)
    if handler is None:
        return None
    return ("quick_produce", handler)


def resolve_swarm(text: str, colors) -> Resolved:
    """集群(N):body —— 每回合结束，若 len(zerg)+has_narud >= N 则执行 body。"""
    m = re.match(r"^集群\((\d+)\):(.+)$", text)
    if not m:
        return None
    n = int(m.group(1))
    inner = simple_body_handler(m.group(2))
    if inner is None:
        return None

    def handler(slot, event, _n=n, _inner=inner):
        if (len(slot.zerg) + slot.has_narud) >= _n:
            _inner(slot, event)

    return ("round_end", handler)


def resolve_gathering(text: str, colors) -> Resolved:
    """集结(N):body —— 每回合结束，按集结次数执行 body（数量 × times）。"""
    m = re.match(r"^集结\((\d+)\):(.+)$", text)
    if not m:
        return None
    cost = int(m.group(1))
    inner = simple_body_handler(m.group(2), times_scaled=True)
    if inner is None:
        return None
    return ("round_end", GatheringActionHandler(inner, cost))


def resolve_psi(text: str, colors) -> Resolved:
    """灵能:body —— 每回合结束，若未达到全场最大灵能等级则执行 body。"""
    if not text.startswith("灵能:"):
        return None
    body = text[len("灵能:"):]
    inner = simple_body_handler(body)
    if inner is None:
        return None

    def handler(slot, event, _inner=inner):
        if slot.psi_level < event.tarven.psi_level_max:
            _inner(slot, event)

    return ("round_end", handler)


def resolve_feed(text: str, colors) -> Resolved:
    """供养(N):X —— 出售时，按精华 // N 给右侧卡牌 X。"""
    m = re.match(r"^供养\((\d+)\):(.+)$", text)
    if not m:
        return None
    price = int(m.group(1))
    unit = m.group(2)

    def handler(slot, event, _unit=unit, _price=price):
        feed(slot, event, _unit, _price)

    return ("selling", handler)


# ---------------------------------------------------------------------------
# 任务 + 奖励
# ---------------------------------------------------------------------------
_TASK_GOAL_PATTERNS: List[Tuple[str, str]] = [
    # (正则, 触发事件)；goal 从第 1 个捕获组取
    (r"让(\d+)张卡牌进场", "any_card_entered"),
    (r"进场(\d+)张.*卡牌", "any_card_entered"),
    (r"进场或出售(\d+)张卡牌", "any_card_entered_or_sold"),
    (r"刷新(\d+)次", "refresh"),
    (r"出售(\d+)张.*卡牌", "any_card_sold"),
]


def resolve_task(text: str, colors) -> Resolved:
    """任务:… 奖励:… —— 合成 TaskActionHandler。"""
    if not text.startswith("任务:") or " 奖励:" not in text:
        return None
    task_part, reward_part = text.split(" 奖励:", 1)
    task_body = task_part[len("任务:"):]

    goal = None
    event_name = None
    for pat, ev in _TASK_GOAL_PATTERNS:
        m = re.search(pat, task_body)
        if m:
            goal = int(m.group(1))
            event_name = ev
            break
    if goal is None:
        return None

    reward = _reward_handler(reward_part)
    if reward is None:
        return None

    auto_reset = "重置" in reward_part
    return (event_name, TaskActionHandler(reward, goal, auto_reset=auto_reset))


def _reward_handler(reward_part: str) -> Optional[Callable]:
    """把奖励正文编译成 handler。目前支持：获得N晶体矿、简单获得单位、降低升级费用、发现。"""
    m = re.search(r"获得(\d+)晶体矿", reward_part)
    if m:
        amount = int(m.group(1))

        def h(slot, event, _amount=amount):
            event.tarven.mineral += _amount

        return h

    m = re.search(r"降低(\d+)点酒馆升级费用", reward_part)
    if m:
        amount = int(m.group(1))

        def h(slot, event, _amount=amount):
            event.tarven.level_up_cost = max(0, event.tarven.level_up_cost - _amount)

        return h

    # 发现一张星级等于当前酒馆等级的卡牌
    if "发现" in reward_part and "酒馆等级" in reward_part:
        def h(slot, event):
            event.tarven.discover(level=[event.tarven.level])

        return h

    # 随机获得 N 张一星卡牌
    m = re.search(r"随机获得(\d+)张一星卡牌", reward_part)
    if m:
        count = int(m.group(1))

        def h(slot, event, _count=count):
            for _ in range(_count):
                drawn = event.tarven.pool.draw(1, 1)
                if drawn:
                    event.tarven.store_card_to_cache(drawn[0].name)

        return h

    # 简单"获得 N单位"
    if units_dict(reward_part):
        handler = simple_body_handler("获得" + reward_part) or simple_body_handler(reward_part)
        if handler is not None:
            return handler
        units = extract_units(reward_part)
        if units:
            def h(slot, event, _units=units):
                for n, u in _units:
                    slot.add_unit(u, n)
            return h

    return None


# 解析器链（顺序无关，各自靠正则/前缀独占）
RESOLVERS: List[Callable[[str, object], Resolved]] = [
    resolve_task,
    resolve_reactor,
    resolve_quick_produce,
    resolve_swarm,
    resolve_gathering,
    resolve_psi,
    resolve_feed,
]


def resolve_parametric(text: str, colors) -> Resolved:
    """尝试机制前缀解析器；失败后尝试"触发时机 + 简单正文"。"""
    for resolver in RESOLVERS:
        result = resolver(text, colors)
        if result is not None:
            return result

    # 触发时机 + 简单获得/折跃/注卵/孵化（允许"此卡牌/此牌/自身"前缀）
    trig = detect_trigger(text)
    if trig is not None:
        event_name, body = trig
        body = re.sub(r"^(此卡牌|此牌|自身|自己)", "", body)
        handler = simple_body_handler(body)
        if handler is not None:
            return (event_name, handler)

    return None
