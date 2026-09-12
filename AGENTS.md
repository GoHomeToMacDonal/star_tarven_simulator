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
├── loader.py         # load_cards()->(cards,Coverage)、build_game(expansions=/random_pick=)、python -m ... 报告
└── expansions.py     # 拓展包(source)过滤：核心默认启用、拓展包按需开启(1-2个)、随机挑选(独占规则)
```

运行：
- 覆盖率报告：`uv run python -m star_tarven_simulator.loader`
- 拓展包过滤演示：`uv run python -m star_tarven_simulator.loader --expansions 作战计划 比特狂潮`
- 随机拓展包：`uv run python -m star_tarven_simulator.loader --random`
- 测试：`uv run pytest tests/test_engine.py tests/test_expansions.py -q`

当前效果解析覆盖率 **100%**（625/625 合并后描述行）。所有描述行均已解析为 handler；其中部分依赖
"休眠事件"（效果体已实现，触发时机需上层驱动，见下）。

- **回归起源**：`每回合结束时,若场上其他卡牌星级与种族均不同,则摧毁所有其他卡牌并获得相同价值的原始单位和3瓦斯`
  —— 按约定：把被摧毁卡牌的**总价值换算成等值的「原始异龙」**（价值 250），并 +3 瓦斯。换算基准单位写在
  `overrides._ORIGIN_PRIMAL_UNIT`，如需改成其它原始单位改这里即可。

### 拓展包过滤（source 字段）
`v260822_card.json` 每张卡带 `source` 列表，标注卡牌来源。`expansions.py` 据此过滤卡池：

- **常驻启用（不可关闭）**：核心种族 `核心人族/核心神族/核心虫族/核心中立`，以及基础内容
  `辅助卡`（定点部署所需）、`特殊`；`source` 为空的卡牌也视为基础内容常驻启用。
- **可选拓展包（默认关闭）**：`作战计划/时不我待/重装上阵/穷兵黩武/一念之差/身经百战/比特狂潮/中世纪集市`，
  一局至多开 `MAX_EXPANSIONS`（=2）个。
- **独占拓展包**：`时不我待` 与 `中世纪集市` 互斥——选中其一后不能再搭配任何其它拓展包
  （该局只能开这一个）。随机挑选（`random_expansions`）与手动校验（`validate_selection`）都强制此规则。
- **过滤规则**：卡牌 `source` 为空则保留；否则当 `source` 与「启用来源集合」有交集时保留。
  因此既属核心又属拓展的双来源卡（如 `核心人族+重装上阵`）默认也会保留。

接口（`expansions.py`）：`validate_selection` / `random_expansions(count=,rng=)` /
`enabled_sources` / `filter_cards(cards, expansions)`。`loader.build_game(cards, expansions=None,
random_pick=False)` 会用过滤后的卡牌构建卡池 / 引擎，并把最终选择记到 `game.enabled_expansions`。
命中数据：默认卡池 114/154；开任一拓展包后按其卡数扩充（如 `时不我待`/`中世纪集市` 各 +8）。

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

### 瓦斯升级与等效战力
- 升级定义来自 `src/star_tarven_simulator/data/upgrades.json`（同步自仓库根目录 `data/upgrades/upgrades.json`）。
- 花费 2 瓦斯从“公共 + 合法专属”池随机发现最多 3 项，自动排除已拥有升级；人/神/虫按种族开放，原始升级只对“属于原始虫群”标签开放，虚影升级只对“具有虚空投影”标签开放；不可叠加升级不能通过其它来源重复添加。
- 一次性升级直接添加单位；持续战斗 Buff 由 `Slot.equivalent_power()` / `Tarven.total_equivalent_power()` 按伤害、攻速、生命、生存与功能倍率估值。
- 原有 `price()` / `total_power()` 仍只统计单位基础价值，确保旧 RL 奖励与 checkpoint 协议不变。详细规则见 `docs/upgrade-system.md`。

### 卡池采样与克隆性能（`simulator/card.py`）
`CardPool` 内部不是"uuid 可重复列表"，改卡池相关代码前先看这里：

- 桶 `_buckets[level]` 存**密集下标**（`_dense`/`_uuids`/`_cards`），并同步维护
  `_counts[level][dense]`（剩余份数）与标签合格性位串（`bytearray`，按标签组合缓存）。
- `pool.sample(levels=, tags=, excepts=)` 是公开接口（`_sample` 仅作兼容别名）。
  `levels` 接受 `None` / int / 任意可迭代。无标签走 O(等级数) 快路径，带标签走
  O(候选卡种数) 求权重 + 单桶一次定位扫描。
- **不可破坏的契约**：桶按位置保序、移除用 swap-remove、每次采样只消耗一个
  `rng.randrange` —— 同一 seed 下抽卡结果与随机数序列必须与历史行为位级一致
  （RL 侧有 bit-identical 测试）。`tests/test_card_pool.py` 内置旧实现作 oracle 逐步对拍，
  任何改动都要让它保持通过。
- 桶内部不对外暴露，用 `bucket_uuids` / `bucket_size` / `total_size` / `set_bucket` /
  `clear_bucket`；`assert_consistent()` 供测试校验计数表同步。
- `Card` / `CardPool` / `CardEngine` 定义了 `__deepcopy__`：卡牌定义与引擎运行时只读，
  克隆整局只复制桶/计数表/RNG（`Card` 因此**必须**保持不可变，改卡请用
  `dataclasses.replace` 造新对象）。
- 基准：`uv run python benchmarks/bench_card_pool.py`。当前 vs 优化前：满级刷新
  `draw(7,6)` 950→12 µs、带标签发现 460→25 µs、`deepcopy(game)` 7.7→0.48 ms，
  `mud_agent` 带 MC 搜索端到端 1.42→0.22 s/局。
- **极低概率卡牌权重**：描述文本含 `"极低概率"`（被动标签"作为极低概率出现的卡牌"或
  风味"极低概率特典卡"）的卡，初始份数 = `CARD_POOL_NUMBER[level]`，普通同等级卡 =
  `CARD_POOL_NUMBER[level] * LOW_PROBABILITY_SCALE`（`LOW_PROBABILITY_SCALE=10`），
  即极低概率卡恰好是普通卡的 1/10。判定与份数见 `card_copies()` / `is_low_probability_card()`，
  当前命中 7 张卡（全部 6 星，含斯旺舰队）。

### 本轮新增/改动的引擎能力
- `simulator/action.py`：新增 `DeployAction(slot_idx, cache_idx=/shop_idx=)`（定点部署辅助卡）。
- `simulator/event.py`：新增 `Event.ROUND_WIN` / `RoundWinEvent`、
  `Event.OTHER_PLAYER_SOLD_HERO_CARD` / `OtherPlayerSoldHeroCardEvent`（均为休眠事件）。
- `simulator/game.py`：`Tarven.trigger_round_win()`、`Tarven.trigger_other_player_hero_card()`、
  `Tarven.trigger_deployment()` + `_handle_deploy()`（`action()` 分发 `DeployAction`）。
- `simulator/slot.py`：`Slot.energy` 按场上"一鼓作气"handler 的描述动态计算能量，
  使虚空水晶塔提供 2/3 点能量（不依赖任何状态字段）。
- `cards/overrides.py`：新增 `_transform(slot,event,card_name,reset_units=)` 自定义变身原语
  （难民营地/刀锋女王/望梅止渴随机卡牌），以及 `_upgrade_shop_card` 商店提星原语。

### 精英化语义：`replace_upto`（不是 `replace_unit`）
`Slot.replace_upto(unit, max_cnt, new_unit) -> int` 是**「最多 N 个」**的 1:1 转化，
转化 `min(现有, max_cnt)` 个并返回实际数量。精英化 / 变形类效果必须用它：
「将5陆战队员精英化」在场上只有 3 个时要精英化 3 个。

`replace_unit(u, max_cnt, new_u, new_cnt)` 保留原语义（**成组兑换**，凑不满一组则不发生，
如「3陆战队员换1牛头人陆战队员」），不要用于精英化——曾导致泰凯斯/尖端科技在兵源
不足时整回合空转（供兵 4/回合 < 精英化 5/回合 时周期性完全不触发）。已改用
`replace_upto` 的 handler：`_elite_marine_marauder`（泰凯斯）、`_elite_marine_to_shield`
（沃菲尔德）、`_quick_elite_goliath_viking`（尖端科技）。
`_elite_entered_protoss` / `_elite_tank_wolf` 本来就是逐个 1:1，语义已正确。

### 已知数据缺口
- `步兵连队`（uuid 23）的 `units` 原本缺 `反应堆`，是全数据里唯一"有『反应堆生产』
  描述却无反应堆单位"的卡，导致步坦协同（判定 `count("反应堆") > 0`）喂不到它。
  已在 `data/v20260826_card.json` 补 `"反应堆": 1`（旧快照 `v20260822_card.json` 未改）。
- `UNIT_PRICES` 现已覆盖两份快照里出现的**全部**单位（`斯旺` 是最后一项，值 200）。
  `docs/missing-unit-prices.txt` 只是历史清单，不代表当前缺口。
- `data/v20260826_card.json` 的 `price` 列曾有 11 处笔误（多打一位前导数字、漏乘份数等）
  和 1 处单位数量错误（`孵化蟑螂` 的 `破坏者` 1→2），已按下节的定价审计修正；现在该快照
  **每张卡的 price 都等于其单位价值之和**，可作回归基线。
- 仍为 `null` 的 price：12 张辅助卡（无单位）、`清理裂隙` / `牛牛冲鸭`（`虚空裂隙` /
  `牛头人兵营` 无实盘定价）。`null` 表示未知，不要当 0 用。

### 单位定价与 price 审计（`scripts/audit_card_prices.py`）
卡牌 `price` = 各单位价值之和，于是整份快照是一组超定线性方程。两张表都声称持有单位价值：

- `constants/unit_prices.py` —— 从**游戏内显示的卡牌价值**反推，是运行时 `Slot.price()` 的依据。
- 地图 `UnitData.xml` + `data/maps/card_overrides.json` —— `card_extractor.py` 用它累加出
  提取快照的 `price`。

**只有手抄快照能当裁判**：`v20260826_card.json` 的价格是照着游戏读下来的，独立于两张表；
而 `v4.6.1.7_card.json` 的 `price` 本身就是用地图成本算出来的，拿它评分是循环论证。
审计脚本因此穷举所有有争议单位的取值组合，取"能让最多手抄卡牌平账"的那组：

```
uv run python scripts/audit_card_prices.py            # 审计 v20260826（默认）
uv run python scripts/audit_card_prices.py --fix      # 把不平账的 price 改成单位之和
uv run python scripts/audit_card_prices.py --cards data/v4.6.1.7_card.json --no-arbitrate
```

据此定案的 5 项修正（已写入 `unit_prices.py`，注释里带证据卡牌）：`幽灵(皇家卫队)` 900→1000、
`战列巡航舰(皇家卫队)` 1700→1500（这两项**必须成对改**，旧值靠两处误差在 `御驾亲征` 上
互相抵消，却让 `cloudplayer` 多出 200）、`旋风狂热者` 150→100、`维京战机(精英)` 225→275、
新增 `斯旺` 200。

### ⚠ 地图 UnitData.xml 是补丁层，单价会缺一半（已用 overrides 补齐）
`gf_获取商店卡牌总价值` → `gf_GetUnitTypeValue` 就是 `UnitTypeGetCost(Minerals) + (Vespene)`，
所以地图**公式**没问题；问题在数据层：地图自带的 `UnitData.xml` 只是**补丁层**，
运行时 `UnitTypeGetCost` 解析的是「基础游戏 + 依赖 mod + 地图补丁」的合并结果，而
`star_tavern_cards.resolved_costs()` 只能看到地图补丁。**只补了一种资源的单位会丢掉另一种**
（补齐两种资源的 `MengskBC2` 1000+500、`GhostRG` 1000 则完全正确）：

| 单位 | 地图补丁 | 显示值 | 丢失 | 佐证卡（手抄价） |
|---|---|---|---|---|
| `不朽者(精英)` ImmortalShakuras | Vespene 250 | 500 | 250 Minerals | 一鼓作气 2000 |
| `地堡` Bunker2 | Minerals 400 | 500 | 100 | 孤军奋战 1150 |
| `异龙(精英)` MutaliskViper | Minerals 200 | 300 | 100 | 军事学院 1800 / 空中管制 1600 / 入景随风 2400 |
| `浩劫` Monitor | Minerals 100 | 200 | 100 | 鲜血猎手 950 / 战时经济 1000 |
| `维京战机(精英)` HelsAngel* | Minerals 175 | 275 | 100 | 斯台特曼 1275 |
| `斯台特曼` Stetmann | Minerals 150 | 175 | 25 | 斯台特曼 1275 |
| `掠夺者` Reaver | 不在地图 xml 里 | 500 | 走 unit_info.json 的 400 | 净化者军团 1400 / 菲尼克斯 2100 |

这 7 项（8 个 unit id）已写进 `data/maps/card_overrides.json`，每条带 `_why` 注明补丁值与佐证卡。
重跑 `extract_cards.py` 后，`v4.6.1.7_card.json` 与 `UNIT_PRICES` 之和不符的卡从 12 张降到 0。

另有 2 项方程无法裁决，已按实盘确认取地图补丁值（因此**不需要** override，只改了 `UNIT_PRICES`）：
`托什` 100→600、`歌利亚(精英)` 300→250。前者连带把手抄快照 `唯一` 的 price 3100 修正为 3600
（= 600 + `泰凯斯` + `奥丁`），正是 v4.6.1.7 里改名后的 `坚守信念` 所印的值。

**当前状态**：两张定价表**完全一致**（审计脚本报"有争议单位 0"），两份快照各自 100% 平账，
同名卡的 `price` 全部相同，只剩 `望梅止渴`/`能量矩阵`/`风暴英雄` 是 OLD `null` 对 NEW `0.0`
的约定差异（这三张所有单位价值都是 0）。

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

## 从 SC2Map 重新提取卡牌数据（新增）

卡牌 JSON 不再需要手抄：`uv run python extract_cards.py` 直接从
`data/maps/星际酒馆正式版-v4.6.1.7.SC2Map` 生成 `data/v4.6.1.7_card.json`（157 张，字段与
`v20260826_card.json` 一致）+ `artifacts/cards/v4.6.1.7_card_diagnostics.json`（审计信息）。
详细说明见 `docs/card-extraction.md`。

根目录脚本分工：

```
paths.py              # 管线用到的所有路径常量（地图、解包目录、输出、覆盖文件）
sc2map_unpacker.py    # MPQ 解包（mpyq，dev 依赖）
galaxy_explore.py     # 十六进制标识符编解码 / 按中文名打印函数体 / --readable 全文解码
galaxy_interp.py      # Galaxy 子集解释器（生成式语法 + 引擎原生函数），不支持的东西直接抛错
card_extractor.py     # 按地图启动顺序跑注册流程，再读 gv_已设计的卡牌模板 输出 JSON
extract_cards.py      # 解包 + 提取 的一键入口
star_tavern_cards.py  # 旧的“正则匹配 Galaxy 源码”提取器，现仅复用其本地化/单价/拆行工具函数
```

**关键事实**：卡牌描述是运行时由 `gf_根据特效字符串生成描述文本(特效字符串, 三连, 不换行)` 渲染的，
特效字符串是一条扁平分隔串。所以提取器**解释执行地图脚本**而不是猜文本：跑
`InitGlobals` → `gf_初始化特殊词条` / `gf_初始化卡牌升级` → 各 `gt_*_Init`（登记卡池包）→
逐包执行触发器（`gf_AddCardModule` 自己填模板表）→ `gf_初始化卡牌设计（补充）`。
描述按 `<n/>` 拆行，与 `parsing/` 的输入格式一致（模拟器加载新文件：629 行中 618 行命中已有 handler；
未命中的 11 行见"扩展效果覆盖率的方法"，多为数值改动 + `复制中心` / `战士的财宝` 两张新卡）。

改这套东西时的坑：

- 编辑器把非 ASCII 标识符写成 UTF-8 十六进制，**变量名（`gv_`/`lv_`/`lp_`）首字符会小写**，
  `ge_`/`gf_`/`gt_`/`gs_` 不会；结构体字段名同样是十六进制（用 `CardExtractor.field()` 访问）。
- Galaxy 的 `text` 变量默认是 `null` 而不是 `""`，`StringSub` 越界返回 `null`。地图大量用
  `if ((文本 != null) == false) return null;` 表达“没话说”，把 null 当空串会渲染出
  `反应堆生产陆战队员和`、`灵能：若且，则…` 这类残句。
- `tags` 是派生约定（种族 + 子类 + 描述行关键词），不是地图字段；词条图标字母不可用作标签。
- 单位中文名优先取地图本地化，缺失才用 `data/maps/unit_info.json`；单价缺失走
  `data/maps/card_overrides.json`（24 条，含 7 条带 `name` 的命名修正，以及 8 条修补
  "UnitData.xml 只补一种资源"的定价，见上节表格）。
- **大厅属性会 gate 卡牌注册**。`Interpreter` 默认让 `GameAttributeGameValue` 返回 `""`，
  于是 `gf_核心中立初始化` 里 `if (attr[…]4 == "0002")` 的两张卡（`虚空大军` / `黑暗预兆`）
  根本不注册。`card_extractor.STANDARD_MODE_ATTRIBUTES` 把属性 4 钉成 `"0002"`（标准模式；
  `"0001"` 是新手模式：只开核心包、无拓展）。全脚本扫描确认属性 4 是唯一 gate 住
  `gf_AddCardModule` 的条件；属性 14 只在 `同卵双狗`（组队模式包）里 early-return，
  而该包 `CardPackInit(valid=组队模式)` 本就不启用。改属性前先扫一遍，别把卡改没了。
- `CardPackInit(valid=false)` 的包会被跳过并记进诊断 `skipped_packs`：`同卵双狗`（组队模式）、
  `卷土重来`（硬关）、`PVE`（PvE 模式）。它们不属于本模拟器建模的 8 人酒馆。
- **`比赛冠军`（特典卡）包的 `Identify` 是每局随机的**：触发器打乱 7 张卡、只启用 1 张，
  给其余 6 张盖上 `ge_卡牌识别符_无法进入卡池`。所以读到这个识别符**不代表**该卡永不进池，
  必须按包名排除（`pack_name != CHAMPION_PACK_NAME`）。
- **模板索引在排序前后会变**：`gf_初始化卡牌设计（补充）` 先 `gf_卡牌模板选择排序()` 打乱常规区，
  再 `gf_SpecialCardsInit()` 追加特殊卡/辅助卡。所以包触发器里记下的索引区间（如
  `gv_比赛冠军模板索引开始/结束`）在读模板时已经失效；辅助卡区间（`gv_辅助卡模板索引开始`）
  在排序之后才分配，可以放心比较。
- `galaxy_explore.readable()` 对 `gv__模板IDxxx` 这类标识符只能半解码（`ID` 的 `D` 会被吞进
  后面的十六进制串），grep 全文时用原始十六进制形式更可靠。

### `不进卡池` 来源标签
提取器给"永远不会被商店/发现抽到"的卡追加 `source` 标签 `不进卡池`
（`card_extractor.SOURCE_NO_POOL`），按三条地图事实推导：

1. `ge_卡牌识别符_无法进入卡池`（`不法之徒` / `母舰核心`，比赛冠军包除外，见上）；
2. 星级 > 6 —— 高于最高酒馆等级，即 4 张"只用于沙盒模式测试"卡
   （`测试沙包` / `大型兵团` / `能量矩阵` / `挂件仓库`）；
3. 辅助卡且不在"发现辅助卡"窗口 `gv_辅助卡模板索引开始 .. + gv_诺娃可以抽取前几张辅助卡(=7)`
   内 —— 只能由专属效果或英雄技能授予（`引导核弹` / `冷钱包` / `矿簇` / `战士的财宝` /
   `核弹天劫`；例如 `战士的财宝` 来自狂热者英雄技能）。窗口内的 7 张正好等于
   `simulator/hero.AUXILIARY_CARD_NAMES`。

该标签**与其它来源标签并存**（如 `["辅助卡", "不进卡池"]`），只影响能否被抽到，不影响是否启用：
`expansions.enabled_sources` 始终把它算作启用来源，`loader.build_game` 则把它并入
`no_draw_uuids`。诊断里有 `cards_not_in_pool` 完整清单（当前 11 张）。
