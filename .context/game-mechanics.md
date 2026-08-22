# 核心游戏机制（Game Mechanics）

> 来源：`ext/tarven_interface/src/tarven_interface/simulator/`。本文提炼旧版引擎的实体、
> 游戏循环、事件系统与四大种族机制，作为新版重写的语义基准。

## 1. 实体模型

### Game
- 一局对战，持有 `user_count` 个 `Tarven`（默认 8），`max_round`（默认 20）。
- `round_start()` / `round_end()` 只是遍历所有 tarven 逐个推进。

### Tarven（酒馆 / 单个玩家的状态机）
所有玩法逻辑都挂在这里。关键状态：
- **卡槽** `slots: List[Slot]`，固定 **7** 个（索引 0..6）。空槽 `card_type is None`。
- **商店** `shop`：本回合可购买的卡，数量随等级变化（见 `TARVEN_SHOP_CARD_NUMBER`）。
- **暂存区** `cache`：6 格，购买/发现的卡先进 cache，再从 cache 进场。
- **资源**：`mineral`（晶体矿，回合刷新、用于买卡/升级/刷新）、`gas`（瓦斯，用于升级卡牌），
  各有上限 `mineral_max` / `gas_max`。
- **等级** `level`（1..6），升级消耗 `level_up_cost`（`TARVEN_UPGRADE_COST`，每回合自动 -1）。
- `health` 生命值、`round` 回合数、`lock` 锁定商店、`free_refresh` 免费刷新次数。
- **强制动作队列** `force_action`：发现/选择升级/选择合成会产生一个必须先响应的动作。
- **延迟进场** `delay_enter_card`：某些“发现”卡延迟若干回合后进场。

### Slot（卡槽 = 一张在场卡牌的载体）
定义在 `simulator/slot.py`。核心字段：
- `card_type`（卡名，`None` 表示空）、`level`、`tags: Tags`、`units: Dict[str,int]`、
  `upgrades: List[str]`（上限 `upgrades_limit=5`）、`event_handlers: List[EventHandler]`。
- 特殊数值：`darkness`（黑暗值）、`task_vars: dict`（handler 私有的持久计数器）、
  `unit_count`（单位总数，硬上限 200）。
- **位置关系（property，只读）**：`left` / `right`（相邻单个，可能空槽）、
  `neighbors`（左右两侧非空）、`left_all` / `right_all` / `all`（非空槽集合）、
  `random`（随机一张非空）。
- **种族筛选**：`zerg` / `terran` / `protoss` / `neutral` 返回带对应 tag 的槽。
- **派生量**：`energy`（自身+相邻的 水晶塔+虚空水晶塔 数量之和）、`psi_level`
  （有“灵能”tag 时等于 level，否则 0）、`price()`（单位总价值）、
  `has_artanis` / `has_narud`（全场是否存在阿塔尼斯/纳鲁德）。

### Card（卡牌定义，静态数据）
定义在 `simulator/card.py`。由 JSON 反序列化（`Card.from_json`）。字段见
`.context/card-description-format.md`。`CardPool` 负责按等级加权抽卡（`draw` / `_sample` /
`place_back`），支持按 `tags` 过滤（用于“发现”）。

### 单位（unit）
纯粹是 `{unit_name: count}`。价值由 `constants/unit_prices.py` 的 `UNIT_PRICES` 决定。
单位有类别常量（`constants/unit_type.py`）：`ELITE_UNITS`（可精英化）、`HERO_UNITS`、
`BIOLOGICAL_UNITS`、`ROYAL_UNITS`、各种族单位表。精英单位命名约定为 `"<名>(精英)"`。

## 2. 游戏循环与玩家动作

### 回合流程
- `round_start()`：回合数 +1；刷新资源；`reload_shop()`（未锁定则重抽商店）；
  对每个非空槽触发 `RoundStartEvent`。
- 玩家在回合中通过 `Tarven.action(action)` 执行动作，返回 `bool` 表示是否合法执行。
- `round_end()`：对每个非空槽触发 `RoundEndEvent`；含“高级科技实验室”的槽额外触发一次
  `QuickProduceEvent`。

### 动作类型（`simulator/action.py`）
- `UpgradeTarvenAction`：升级酒馆等级（花 mineral）。
- `BuyAction(shop_idx, slot_idx?)`：买卡；`slot_idx` 为空则进 cache，否则直接进场。
- `CacheEnterAction(cache_idx, slot_idx)`：把 cache 里的卡放到某槽进场。
- `SellAction(slot_idx)`：出售。
- `UpgradeAction(slot_idx)`：给卡牌加升级（花 gas，触发选择升级的 force_action）。
- `RefreshAction` / `LockAction`：刷新 / 锁定商店。
- `SynthesisAction(shop_idx? / cache_idx?)`：三连合成（见下）。
- **强制动作**：`ChooseCardAction` / `ChooseUpgradeAction` / `ChooseSynthesisAction`，
  由“发现/升级/合成”产生，必须在 `force_action` 里命中后才能继续。

### 进场与卡槽移动（`action()` 里 Buy/CacheEnter 分支）
放卡到已占用的 slot 时会做**右移/左移**腾位；进场后 `assign_card_to_slot` 再
`trigger_entering`。购买/合成有“同名非金色最多 2 张”的约束（第 3 张触发合成）。

### 三连 / 金色（`CardEngine.merge_slots`）
- 三张同名同级 → 合并到左槽：单位相加、tags 合并、upgrades 合并、加 `金色` tag。
- **换 handler**：移除 `card.description` 对应的普通 handler，替换为 `card.gold_event_handlers`
  （金色描述对应的 handler）。
- 右槽清空。合成时还会让玩家在“3 张随机卡 + 可能的聚能器升级”里做选择
  （`ChooseSynthesisAction`）。

## 3. 事件系统（关键！卡牌效果的驱动核心）

`simulator/event.py` 定义 `Event` 枚举与各 `*Event` 载荷类；`simulator/event_handler.py`
定义 `EventHandler`。

### EventHandler
```python
EventHandler(tarven, slot, description, action_handler, event_name, unique=False)
```
- 绑定到某个 `slot`，监听某个 `event_name`。
- `handle(event)`：若 `event.event_name == self.event_name` 则调用 `action_handler(slot, event)`。
- `unique=True`（描述以 `"唯一:"` 开头）：全场同描述效果只结算一次（扫描 `left_all` 去重）。
- `copy(tarven, slot)`：把“卡牌定义上的 handler 模板”实例化到具体槽位。
  `TaskActionHandler` 会深拷贝以隔离计数器。

### 触发分发（都在 `Tarven` 上）
- `slot.trigger([event])`：对该槽自身的 handler 派发。
- `trigger_any_card_event(event)`：对**全场每个非空槽**派发（用于 `any_card_*` 类事件）。
- 具体封装：`trigger_entering` / `trigger_selling` / `trigger_upgrade` / `trigger_refresh` /
  `trigger_any_card_teleport` / `trigger_any_card_larva` / `trigger_any_task_finished`。

### 事件目录（`Event` 枚举）
| 事件名 (value)                | 触发时机 / 语义 |
|------------------------------|-----------------|
| `round_start`                | 回合开始（对自身） |
| `round_end`                  | 回合结束（对自身） |
| `selling`                    | **自身**被出售时 |
| `entering`                   | **自身**进场时 |
| `any_card_sold`              | 任意卡出售时（对全场） |
| `any_card_entered`           | 任意卡进场时（对全场） |
| `any_card_entered_or_sold`   | 任意卡进场或出售时 |
| `gain_darkness`              | 获得黑暗值时（相邻出售可给黑暗值） |
| `upgrade`                    | 卡牌被加升级时（也用于“提升酒馆等级”） |
| `refresh`                    | 刷新商店时 |
| `level_up`                   | 提升酒馆等级时 |
| `quick_produce`              | 人族“快速生产”（刷新/换挂件/回合结束等触发） |
| `any_card_addon_changed`     | 任意卡挂件（反应堆↔科技实验室）变更时 |
| `any_task_finished`          | 任意任务完成时 |
| `any_card_larva`             | 任意卡注卵时 |
| `any_card_hatch`             | 任意卡孵化时 |
| `any_card_teleport`          | 任意卡折跃时 |
| `any_card_gain_void_crystal_tower` | 任意卡获得虚空水晶塔时 |
| `deployment`                 | 辅助卡“定点部署”时 |

> 注意：旧版部分 handler 的注册事件与描述语义并不完全一致（例如某些“进场时”被挂在了
> `round_end` 上）。这是历史遗留，新版应以描述语义为准重新归类，见 card-coding-guide。

## 4. 四大种族机制

### 人族 Terran
- **挂件（addon）**：`反应堆` / `科技实验室` / `高级科技实验室`。`反应堆生产X` 类效果在
  `round_end` 给自身加单位（金色 +1）。`change_add_on()` 切换挂件并触发 `quick_produce` +
  `any_card_addon_changed`。
- **快速生产（quick_produce）**：刷新商店（含信号塔额外 2 次）、换挂件、回合结束（高级科技实验室）
  等会触发。
- **任务（task）**：`TaskActionHandler(handler, goal, auto_reset)`，计数达 `goal` 时结算奖励，
  并广播 `any_task_finished`。
- **科技挂件计数** `tech_addon_count(slot)`：全场含科技/高级科技实验室的卡数。

### 神族 Protoss
- **折跃（teleport）**：`teleport(slot, event, {unit:cnt})` 把单位加到 `slot.teleport` 目标并
  广播 `any_card_teleport`。（注意：旧版 `Slot.teleport` property 返回 `None`，是未完成占位。）
- **能量强度（energy）**：自身+相邻的 `水晶塔`+`虚空水晶塔` 数。
- **集结（gathering, 集结(N)）**：`GatheringActionHandler(handler, cost)`，触发次数 =
  `min(energy // cost, 2) + has_artanis`。
- **灵能（psi）**：`slot.psi_level`；`Tarven.psi_level_max` 为全场最大。多数灵能效果在
  `round_end` 且 `psi_level < psi_level_max` 时生效。
- **虚空投影 / 虚空水晶塔**：出售时虚空水晶塔转移到左侧并广播
  `any_card_gain_void_crystal_tower`。

### 虫族 Zerg
- **虫卵（egg / 虫卵）**：特殊卡，`assign_card_to_slot("虫卵", slot)`；`larva` 单位注入它。
- **注卵（larva）**：`Tarven.larva(units_dict)` —— 找到现有虫卵槽或空槽生成虫卵，注入单位，
  广播 `any_card_larva`。**入参是 dict**（见 coding-guide 里的已知 bug：部分旧代码误用
  `larva("名", n)` 两参数形式）。
- **孵化（hatch）**：虫卵在 `round_start` 若左右均为 zerg，则把生物单位复制给两侧并广播
  `any_card_hatch`，随后清空虫卵。
- **集群（swarm, 集群(N)）**：条件 `len(slot.zerg) + slot.has_narud >= N`。

### 中立 Neutral
- **黑暗值（darkness）/ 黑暗容器**：相邻卡出售时给黑暗值（`GainDarknessEvent`）；
  `gain_darkness` 事件驱动“获得黑暗值时……”效果。“具有黑暗容器”是 tag。
- **供养（feed, 供养(N)）** / **原始虫群** / **夺取（seize）**：`Tarven.seize(source, target)`
  把 source 的单位/升级转移给 target 并摧毁 source。
- **虚空投影**：作为 tag 影响其它加成。

## 5. 资源/常量速查（`constants/`）
- `tarven.py`：`TARVEN_MAX_LEVEL=6`、`TARVEN_UPGRADE_COST`、`TARVEN_SHOP_CARD_NUMBER`、
  `TARVEN_CACHE_SIZE=6`。
- `card_tag.py`：`CARD_TAGS`（合法 tag 白名单，超出会报错）、`CARD_PACKAGES`（卡包归类）及其
  倒排索引 `CARD_PACKAGE_INVERTED_INDEX`。
- `unit_type.py`：单位分类表。`unit_prices.py`：`UNIT_PRICES`（部分挂件/衍生物价格为 0）。
