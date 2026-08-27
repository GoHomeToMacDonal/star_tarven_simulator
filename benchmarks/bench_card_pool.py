"""CardPool 采样基准。

用法::

    uv run python benchmarks/bench_card_pool.py
    uv run python benchmarks/bench_card_pool.py --repeat 5000

不进入 pytest 默认收集范围；只做相对比较，绝对数字与机器相关。
"""

from __future__ import annotations

import argparse
import random
import time
from typing import Callable, List

from star_tarven_simulator.expansions import BASE_EXTRA_SOURCES
from star_tarven_simulator.loader import load_cards
from star_tarven_simulator.simulator.card import Card, CardPool


def _no_draw(cards: List[Card]) -> set:
    return {
        c.uuid
        for c in cards
        if set(getattr(c, "source", []) or []) & set(BASE_EXTRA_SOURCES)
    }


def _sample(pool: CardPool, **kwargs):
    fn = getattr(pool, "sample", None) or pool._sample
    return fn(**kwargs)


def _bench(name: str, setup: Callable[[], CardPool], body: Callable[[CardPool], None], repeat: int):
    """每 ``chunk`` 次操作重建一次卡池，避免抽空影响可比性。"""
    chunk = 64
    pool = setup()
    done = 0
    elapsed = 0.0
    while done < repeat:
        n = min(chunk, repeat - done)
        pool = setup()
        t0 = time.perf_counter()
        for _ in range(n):
            body(pool)
        elapsed += time.perf_counter() - t0
        done += n
    per_op_us = elapsed / repeat * 1e6
    print(f"{name:<38} {per_op_us:9.2f} us/op   ({repeat} ops, {elapsed:.3f}s)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=2000)
    args = ap.parse_args()

    cards, _ = load_cards()
    nodraw = _no_draw(cards)

    def setup() -> CardPool:
        return CardPool(cards, no_draw_uuids=nodraw, rng=random.Random(0))

    probe = setup()
    sizes = [
        len(probe.bucket_uuids(lv)) if hasattr(probe, "bucket_uuids") else len(probe.pool[lv])
        for lv in range(7)
    ]
    total = sum(sizes)
    print(f"卡牌 {len(cards)} 张，可抽 {len(cards) - len(nodraw)} 张")
    print(f"各级桶份数 {sizes[1:]}，合计 {total} 份拷贝")
    print()

    levels_all = [1, 2, 3, 4, 5, 6]
    _bench("draw(7, 6)  满级刷新", setup, lambda p: p.draw(7, 6), args.repeat // 4)
    _bench("draw(3, 1)  一本刷新", setup, lambda p: p.draw(3, 1), args.repeat)
    _bench("sample(levels=[1..6])", setup, lambda p: _sample(p, levels=levels_all), args.repeat)
    _bench("sample(levels=[1])", setup, lambda p: _sample(p, levels=[1]), args.repeat)
    _bench(
        "sample(levels=[1..6], tags=[3 races])",
        setup,
        lambda p: _sample(p, levels=levels_all, tags=["terran", "protoss", "zerg"]),
        args.repeat,
    )
    _bench(
        "sample(levels=[1..6], tags=[zerg])",
        setup,
        lambda p: _sample(p, levels=levels_all, tags=["zerg"]),
        args.repeat,
    )

    card = next(c for c in cards if c.level == 1 and c.uuid not in nodraw)
    _bench("take(level-1 card)", setup, lambda p: p.take(card), args.repeat)
    _bench("count(level-1 card)", setup, lambda p: p.count(card), args.repeat)

    _bench_clone(cards, args.repeat // 20 or 1)


def _bench_clone(cards, repeat: int) -> None:
    """整局深拷贝（mud_agent 每次 rollout 采样都要做一次）。"""
    import copy

    from star_tarven_simulator.loader import build_game

    game = build_game(cards, user_count=1, rng=random.Random(0))
    tarven = game.tarvens[0]
    tarven.reload_shop()

    t0 = time.perf_counter()
    for _ in range(repeat):
        copy.deepcopy(game)
    elapsed = time.perf_counter() - t0
    print(f"{'deepcopy(game) 整局克隆':<36} {elapsed / repeat * 1e6:9.2f} us/op   ({repeat} ops)")


if __name__ == "__main__":
    main()
