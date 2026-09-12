# 终局配方库、依赖图谱与策略查询

实现入口：`src/star_tarven_simulator/recipes/`。设计依据见
[`terminal-recipe-library-design.md`](terminal-recipe-library-design.md)。该模块只读卡牌定义，不修改运行时对局。

## 快速使用

```bash
# 抽取覆盖率；--events 同时打印全部运行时事件矩阵
uv run python -m star_tarven_simulator.recipes.report --events

# 生成 JSON 配方库，全局硬上限为 100
uv run python -m star_tarven_simulator.recipes.catalog --max 100 \
  --output /tmp/terminal-recipes.json

# 按拓展包生成激活子图
uv run python -m star_tarven_simulator.recipes.catalog \
  --expansions 作战计划 身经百战 --max 100

# 从图谱输出可解释策略；可按模板、卡牌或事件过滤
uv run python -m star_tarven_simulator.recipes.strategy --max 8
uv run python -m star_tarven_simulator.recipes.strategy \
  --event any_card_teleport --max 5
uv run python -m star_tarven_simulator.recipes.strategy --card 万叉奔腾 --json
```

Python API：

```python
from star_tarven_simulator.loader import load_cards
from star_tarven_simulator.recipes import build_fact_graph, generate_recipe_catalog
from star_tarven_simulator.recipes import derive_strategy_guides

cards, _ = load_cards()
master = build_fact_graph(cards, dataset_id="v20260826", strict=False)
active = master.activate(["作战计划"])
catalog = generate_recipe_catalog(active, max_recipes=100)
guides = derive_strategy_guides(active, catalog, limit=8)
```

## 覆盖率含义

报告故意分开三种指标，不能用执行覆盖率冒充完整语义覆盖率：

- **执行覆盖率**：描述能否解析为运行时 handler；
- **handler 图谱覆盖率**：每个可执行描述是否至少有事件绑定和可审计图节点；
- **完整语义覆盖率**：条件、目标、数量和动作是否已由标准语法或受控 override 完整声明。

当前 `v20260826` 数据中，执行层为 629/629。图谱为全部可执行描述建立节点；尚未完整声明的不规则闭包使用 `partial`，保留事件、可可靠抽取的动作和 `execute_handler` 审计标记，但不会伪装成完整语义。`--strict` 会拒绝任何 `partial/opaque`。

`FactGraph.event_coverage()` 对 `Event` 枚举中的全部事件输出
`(event_name, listener_count, fully_semantic_listener_count)`；主图包含休眠事件，激活子图则只统计当前拓展包可用卡牌。

## 图谱与配方

主图关系包括：初始单位、事件监听/发射、生产/折跃/注卵/孵化、条件需求、目标范围、标签、拓展来源和拓展互斥。查询接口包括：

- `providers(unit)`：单位供给者；
- `listeners_to(event)` / `emitters_of(event)`：事件两端；
- `event_chains()`：通用的 `emitter -> event -> listener` 依赖链；
- `effects_requiring(kind)`：按条件类型反查；
- `dependency_relations(effect)`：解释某效果的输入、输出与目标；
- 神族能量、集结次数、虚空塔 MAX 倍率等专用求值器。

目录当前覆盖六类结构：

1. `protoss-energy-gathering`：神族能量、集结、全局加次与折跃反馈；
2. `event-feedback-engine`：任意声明式事件发射者与监听者的跨卡链；
3. `zerg-swarm-engine`：虫族卡数阈值与集群收益；
4. `unit-supply-engine`：单位/精华供给者与消费/转移核心；
5. `psi-ascension-engine`：低等级灵能产兵、最高等级锚点和回合末全体精英化；
6. `darkness-carousel`：双死亡舰队夹空槽的买入—出售黑暗值循环。

生成器执行七槽、激活来源、常驻卡合法性（部署辅助卡会被消耗，禁止进入终态）、硬条件复算、稳定 ID 和确定性排序校验。事件反馈配方若仍有未求解的硬条件会被统一 `validate_recipe()` 淘汰；多模板用轮询选择保证多样性；无论 `limit_per_template` 或 API 参数多大，最终目录全局不超过 100 条。金色卡按运行时三连语义，以三张普通卡的最小合并载荷建模。

## 可行策略示例

以下是结构策略，不是随机商店中的保证购买路线；具体卡名由当前数据和拓展激活结果动态选出。

### 1. 神族能量—集结

- 把集结核心放中间槽，初始水晶塔/虚空塔供给者放闭邻域两侧；先满足第一档，再追第二档。
- 一鼓作气类全局修饰取 MAX，不叠加；阿塔尼斯类存在性加次可以放非邻位，节约供能邻位。
- 折跃监听形成的是“本次集结后”的反馈，不能拿未来产出的塔虚算当前阈值。
- 风险：折跃必须有合法神族落点，固定折跃标签会改变目标。

### 2. 折跃/注卵/孵化事件反馈

- 先拿稳定事件发射者，再补监听者；监听者单独在场通常没有收益。
- 折跃链需神族落点；注卵需已有虫卵或空槽；孵化需相邻虫族卡。
- 可通过 `strategy --event any_card_teleport` 等命令只查看某事件的合法组合及图谱证据。

### 3. 虫族集群

- 先用高质量虫族卡填满阈值，再在不跌破阈值的前提下替换低价值计数卡。
- 预留虫卵空槽；孵化核心放在虫族邻位。纳鲁德加成按存在性处理，不能重复叠加。
- 配方排除阈值为 1 的自给卡，因为它不构成可复用的跨卡依赖。

### 4. 原始虫群供养/单位供需

- 先部署原料或精华供给者，达到阈值后再结算消费、转移或转换核心。
- 供养卡必须放在目标左侧，出售前确认右侧卡存在；属于原始虫群的目标还能接收精华。
- 不要只看最终产物，图谱的 `REQUIRES` 与 `unit_supply` 证据会指出原料从何而来。

## 边界

配方库定义终局结构和条件性运营原则，不模拟商店随机、刷新成本、战斗或完整购买路径。`partial` 效果可以参与事件发现和保守候选查询，但策略会标注语义风险。要把指南升级为当前局面的逐步动作计划，应另接收 `Game` 状态并通过 simulator 回放每一步合法性。

### 5. 灵能精英化流水线

- 核心站位依次为：`步兵连队 / 势不可挡 / 黑暗预兆 / 虚空构造体`。回合结束按槽位顺序结算，虚空构造体必须在产兵组件之后。
- 灵能实际条件是“自身灵能等级低于场上最高值”：黑暗预兆以 5 星作为锚点，步兵连队 3 星、势不可挡 4 星和无灵能标签的虚空构造体（等级按 0）均会触发；黑暗预兆自身不会触发灵能奖励。
- 势不可挡需要在终局前累积到 10 座水晶塔，才能稳定完成两档 `集结(5)`。这是终局载荷要求，不是静态初始单位。
- 虚空构造体最后将灵能卡上的所有**可精英化**单位转换。当前 `ELITE_UNITS` 未覆盖的单位（如幽灵）不会转换。
- 四张核心只有人族、神族、中立三族；若还想触发黑暗预兆的四种族产出，需要用剩余槽补一张虫族卡。

### 6. 双死亡舰队刷牌黑暗值

- 固定站位：`槽0 死亡舰队 / 槽1 空位 / 槽2 同变体死亡舰队`，槽3~6放自带黑暗容器且监听 `gain_darkness` 产兵的卡（当前核心候选为不死队）。
- 在槽1反复买入并出售卡牌。每次出售会让槽0、槽2各直接获得一次黑暗值；槽0的唯一效果再向槽2和其他黑暗容器传播一次。
- 两张死亡舰队必须使用相同普通/金色变体，以便同描述 `唯一` 正确抑制二次广播。不要混用普通和金色唯一描述。
- 金色死亡舰队把传播的**黑暗值数额**翻倍，但一次传播仍只产生一次 `gain_darkness` 事件，因此不会把产兵触发次数翻倍。
- 鲜血猎手虽然监听黑暗值产兵，但默认没有“具有黑暗容器”标签，不能直接收到死亡舰队传播；必须先通过其它效果获得容器。
- 循环不是免费经济：购买再出售通常净亏晶体矿，实际次数受商店供给、矿量和 200 单位上限约束。
