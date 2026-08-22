# AGENTS.md — 星际酒馆模拟器（Star Tavern Simulator）

> 本文件及 `.context/*.md` 仅供 AI 助手阅读，用于快速理解本项目并正确地为卡牌编写模拟代码。
> 人类开发者请以实际代码为准。

## 这个项目是什么

模拟《星际争霸》主题的“酒馆战棋”类自走棋玩法（下称**酒馆 / Tavern**）。核心是：
玩家在 7 个**卡槽（slot）**上放置**卡牌（card）**，卡牌携带**单位（unit）**并拥有由
卡牌描述文本定义的**触发效果（event handler）**。游戏按回合推进，各种事件（回合开始/结束、
进场、出售、折跃、注卵……）触发卡牌效果，最终以卡牌内单位的总价值参与战斗。

本项目的核心工作是：**把卡牌的中文描述文本，翻译成可执行的效果代码。**

## 目录结构（重要）

```
star_tarven_simulator/            # 顶层：新版重写目标（当前基本为空壳）
├── AGENTS.md                     # 本文件
├── .context/                     # AI 上下文文档（详见下方）
├── data/
│   └── v260822_card.json         # 【新版】卡牌数据，描述格式已优化（本项目的输入）
├── src/star_tarven_simulator/    # 新版实现的落点（目前仅 __init__.py 空壳）
└── ext/
    └── tarven_interface/         # 【旧版】已有实现（git 子模块），是主要参考来源
```

旧版实现全部在 `ext/tarven_interface/`，是重写时的**参考蓝本**，不是要维护的目标：

```
ext/tarven_interface/src/tarven_interface/
├── simulator/            # 引擎核心：game / slot / card / event / event_handler / card_engine / parser
├── cards/actions/        # 【旧版】每条卡牌描述对应的效果 handler（按种族/机制分文件）
│   ├── psi.py            # 灵能
│   ├── terran/           # 人族：quick_produce（快速生产）、task（任务）
│   ├── protoss/          # 神族：teleport（折跃）、assembly（集结）
│   ├── zerg/             # 虫族：hatch（孵化）、larva（注卵）、swarm（集群）
│   └── neutral/          # 中立：darkness（黑暗值）、primal_swarm（原始虫群）
├── constants/            # card_tag / unit_type / unit_prices / tarven
├── card2program.py       # 【旧版】描述→代码的半自动生成脚本（历史遗留，勿照抄）
└── card_format.py
```

## 快速上手顺序（给 AI）

1. 先读 `.context/game-mechanics.md` —— 搞清楚游戏循环、实体、事件系统、四大种族机制。
2. 再读 `.context/card-description-format.md` —— 搞清楚新版 `v260822_card.json` 的字段与
   `<c val="...">` 颜色标记约定，以及它相对旧格式改了什么。
3. 要写/改卡牌效果代码时，读 `.context/card-coding-guide.md` —— handler 模式、可用 API、
   事件目录、常见坑。

## 关键事实（一句话速记）

- **一条描述 = 一个 handler。** 描述被拆成若干独立子句，每个子句映射到一个
  `action_handler(slot, event)`，并注册到 `ACTION_HANDLERS[描述文本] = (event_name, handler)`。
- **`slot` 是效果的执行主体**（“此卡牌”），`event` 携带触发上下文（谁进场/出售/触发了什么）。
- **金色（三连）卡**用 `gold_description` / `gold_tags`，合成时替换为金色 handler 并加 `金色` tag。
- **颜色标记是语义类别**：新版描述把关键词（任务/集结/集群/折跃/注卵/灵能/黑暗值……）用
  `<c val="十六进制色">关键词</c>` 包裹，颜色决定机制归类。这是相比旧版“纯字符串匹配”的核心优化，
  应据此做**确定性**的关键词/触发时机识别，而不是脆弱的字符串猜测。

## 工具链

- 使用 `uv`（Python ≥ 3.12）。


## 新版实现（src/star_tarven_simulator/）

重写已落地，可运行。分层：

```
src/star_tarven_simulator/
├── constants/        # 从旧版原样移植（card_tag / tarven / unit_type / unit_prices）
├── simulator/        # 重构后的引擎（已修正 .context 列出的已知 bug）
│   ├── event.py          # 事件；统一了载荷属性名与构造签名
│   ├── event_handler.py  # EventHandler + TaskActionHandler + GatheringActionHandler
│   ├── card.py           # Tags(容错,不再 assert 崩溃) / Card.from_json(新格式) / CardPool(纯 random)
│   ├── slot.py           # Slot；teleport 落点已实现（默认自身，或带"你的折跃…"tag 的槽）
│   ├── card_engine.py    # assign / merge_slots(修正金色 handler 替换 + 真正填充 gold)
│   └── game.py           # Tarven/Game；larva(dict)、gain_darkness 集中化
├── parsing/          # 新版颜色感知解析器（替代旧 parser.py/card2program.py）
│   ├── text.py           # strip_color/extract_colors(容错未闭合标签)/normalize/extract_units/register_units
│   └── parser.py         # parse_card(card) 填充 event_handlers/gold_event_handlers
├── cards/            # 描述 -> handler
│   ├── parametric.py     # 规整机制族的参数化解析（数值从文本抽取，普通/金色自动通用）
│   ├── overrides.py      # 不规则效果的文本注册表（key=归一化文本，普通/金色分别注册）
│   ├── mechanics.py      # teleport / hatch / feed 原语
│   └── __init__.py       # resolve() + is_passive()
└── loader.py         # load_cards()->(cards,Coverage)、build_game()、python -m ... 打印覆盖率
```

运行：
- 覆盖率报告：`uv run python -m star_tarven_simulator.loader`
- 测试：`uv run pytest tests/test_engine.py -q`

当前效果解析覆盖率 **100%**（629/629 合并后描述行）。所有描述行均已解析为 handler；其中部分依赖
"休眠事件"（效果体已实现，触发时机需上层驱动，见下）。

- **回归起源**：`每回合结束时,若场上其他卡牌星级与种族均不同,则摧毁所有其他卡牌并获得相同价值的原始单位和3瓦斯`
  —— 按约定：把被摧毁卡牌的**总价值换算成等值的「原始异龙」**（价值 250），并 +3 瓦斯。换算基准单位写在
  `overrides._ORIGIN_PRIMAL_UNIT`，如需改成其它原始单位改这里即可。

### 休眠事件（已实现、需外部驱动，默认不触发）
部分效果的触发时机在"酒馆经济"里没有对应动作，但效果体已正确实现，接入上层驱动即可生效：
- `round_win`（回合胜利，即战斗获胜）：`Tarven.trigger_round_win()` 派发，需上层接入战斗结果。
  相关卡：屠猎者（`回合胜利时或提升酒馆等级时…`，其中 `level_up` 部分正常触发）、海盗商人（任务:回合胜利）。
- `other_player_sold_hero_card`（其他玩家出售/出局英雄卡）：`Tarven.trigger_other_player_hero_card(source)`
  派发，需多人驱动（玩家 X 出售/出局英雄卡时，对每个 Y≠X 调用）。相关卡：英灵殿。

### 定点部署（DeployAction，已接线）
`deployment` 事件现在由玩家动作 `DeployAction(slot_idx, cache_idx=/shop_idx=)` 驱动（`simulator/action.py`）：
- 辅助卡（`辅助卡`，0 星、无单位、带 `部署时` 效果）不进场为常驻卡，而是"部署"到某张**已在场**的
  卡牌上（`slot_idx` 目标槽必须有卡），对其结算部署效果后被消耗。来源二选一：`cache_idx`（暂存区）
  或 `shop_idx`（商店，扣晶体矿）。
- 结算走 `Tarven.trigger_deployment(card, target_slot)`：对该辅助卡 `event_handlers` 里的 `deployment`
  handler 派发 `DeploymentEvent(deployment_slot=目标槽)`，handler 用 `event.deployment_slot` 作用于目标。
- **判定要点**：辅助卡的 `辅助卡` 标记有的在 `tags`、有的只写在描述里（如星灵科技/生化实验室/隐秘行动/
  私人团队），因此 `_handle_deploy` 以"该卡是否含 deployment handler"为准，而非查 tag。
- 相关卡：私人团队/尖端科技/星灵科技/生化实验室/秽暗饵食/超负荷/隐秘行动/冷钱包/矿簇。
  （引导核弹/核弹天劫属战斗风味，无 deployment handler，不可部署。）

### 本轮新增/改动的引擎能力
- `simulator/action.py`：新增 `DeployAction(slot_idx, cache_idx=/shop_idx=)`（定点部署辅助卡）。
- `simulator/event.py`：新增 `Event.ROUND_WIN` / `RoundWinEvent`、
  `Event.OTHER_PLAYER_SOLD_HERO_CARD` / `OtherPlayerSoldHeroCardEvent`（均为休眠事件）。
- `simulator/game.py`：`Tarven.trigger_round_win()`、`Tarven.trigger_other_player_hero_card()`、
  `Tarven.trigger_deployment()` + `_handle_deploy()`（`action()` 分发 `DeployAction`）；
  `Tarven.void_tower_energy`（默认 1）。
- `simulator/slot.py`：`Slot.energy` 读取 `state.void_tower_energy`，使"一鼓作气"能让虚空水晶塔提供 2/3 点能量。
- `cards/overrides.py`：新增 `_transform(slot,event,card_name,reset_units=)` 自定义变身原语
  （难民营地/刀锋女王/望梅止渴随机卡牌），以及 `_upgrade_shop_card` 商店提星原语。

### 关键数据格式坑（.context 未提及，务必注意）
`v260822_card.json` 的 `description` 列表**并非**"一元素一子句"：同一逻辑描述可能被切成多个片段，
且 `<c val>` 标签会**跨片段**（例如 `["<c val=\"FF8000\">无法三连", "</c><c val=\"008000\">任务：</c>刷新5次"]`）。
括号说明（如 `(F8可查看所有精英单位)`）也常被切成独立片段。此外，个别金色描述的触发词会被数据管线
替换成占位符（如军事学院金色行的 `任意卡牌~A~时…`），前缀也可能残留碎片（如 `n/>部署时…`）——
这类文本按语义在 `overrides.py` 里另注册一条同义 key。因此解析时：
1. `strip_color` 必须容忍未闭合标签（先消对，再删落单 `</c>`，再删落单开标签）。
2. 解析前把以 `(` 开头的"续行"并回上一子句（见 `parser._merge_continuations`）。
3. `任务:` 行与紧邻 `奖励:` 行合并成一个 `TaskActionHandler`（见 `parser._pair_task_reward`）。

### 扩展效果覆盖率的方法
- 能参数化的（触发时机 + 获得/折跃/注卵/孵化 N 单位，或 反应堆/快速生产/集群/集结/灵能/供养/任务奖励）
  优先加到 `cards/parametric.py`，可同时覆盖普通与金色、以及同族不同数值的卡。
- 不规则的写进 `cards/overrides.py`，key 用 `parsing.text.normalize` 后的文本；普通/金色数值不同就各注册一条。
- 新单位名不在旧价格表时，加载器会自动把数据里的单位注册进词典；纯效果衍生单位可在
  `text._build_unit_lexicon` 的补充列表里添加。
