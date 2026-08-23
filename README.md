# 星际酒馆模拟器（Star Tavern Simulator）

模拟《星际争霸》主题的“酒馆战棋”自走棋玩法：把卡牌的中文描述文本翻译成可执行的效果代码，
并驱动一套回合制事件引擎。数据输入为 `data/v260822_card.json`（155 张卡）。

> AI 助手请先读 `AGENTS.md` 与 `.context/*.md`。本文只给人类开发者一个运行入口。

## 环境

- Python ≥ 3.12，使用 [`uv`](https://docs.astral.sh/uv/) 工具链。

## 运行

```bash
# 加载全部卡牌并打印效果解析覆盖率报告
uv run python -m star_tarven_simulator.loader

# 运行测试
uv run pytest tests/test_engine.py tests/test_expansions.py -q
```

## 拓展包过滤（source）

`v260822_card.json` 每张卡带 `source` 字段标注来源。默认只启用**核心种族**
（人族 / 神族 / 虫族 / 中立）与基础内容（辅助卡 / 特殊）。可另外开启 1~2 个**拓展包**。

可选拓展包：`作战计划 / 时不我待 / 重装上阵 / 穷兵黩武 / 一念之差 / 身经百战 / 比特狂潮 / 中世纪集市`。

规则：一局至多开 2 个；其中 `时不我待` 与 `中世纪集市` **互斥且独占**——选中其一后不能再搭配任何其它拓展包。

```bash
# 手动指定拓展包（至多 2 个）
uv run python -m star_tarven_simulator.loader --expansions 作战计划 比特狂潮

# 随机挑选一组合法拓展包（自动遵守独占规则）
uv run python -m star_tarven_simulator.loader --random
```

代码里构建对局：

```python
from star_tarven_simulator.loader import load_cards, build_game

cards, _ = load_cards()

game = build_game(cards, expansions=["作战计划"])   # 手动开一个拓展包
game = build_game(cards, random_pick=True)          # 随机挑选拓展包
game = build_game(cards)                             # 默认只开核心 + 基础内容

print(game.enabled_expansions)                       # 本局实际启用的拓展包
```

过滤 / 校验 / 随机挑选的实现见 `src/star_tarven_simulator/expansions.py`，
更多设计说明见 `AGENTS.md` 与 `.context/card-description-format.md` §6。
