# 终局配方库与可复用依赖图谱方案

> 状态：方案草案  
> 目标范围：只构建可重建的终局配方库；不负责当前对局的经济执行、购买路径或 MCTS 决策。

## 1. 背景与目标

酒馆中的终局组合不是简单的卡牌共现关系。卡牌之间往往通过单位、事件、阈值、位置和全局修饰器间接关联，例如：

- 供塔卡产生水晶塔；
- 水晶塔为相邻槽提供能量；
- 集结卡读取能量阈值，但不消耗能量；
- 折跃效果又可能触发其他卡牌产塔；
- 拓展包和独占规则决定某些理论组合是否合法。

本方案建立一层可复用的**机制事实图谱**，再基于图谱生成具体的七槽终局配方。卡牌数据或卡包发生增删、数值调整时，图谱和配方库应能自动重建，而不需要手工维护大量“卡 A 与卡 B 协同”的直接关系。

### 1.1 核心目标

1. 从卡牌静态数据和 handler 描述中提取声明式机制事实。
2. 使用稳定的类型化关系表达生产、触发、条件、位置、转换与全局修饰。
3. 支持普通/金色变体、拓展包过滤和数据版本差异。
4. 自动生成带槽位的具体终局配方，并保留配方成立原因。
5. 为未来的 GFlowNet 提供终态定义、动作掩码、图特征和奖励因子。
6. 对无法可靠自动解析的特殊效果提供显式 override 和覆盖率报告。

### 1.2 非目标

第一阶段不实现：

- 当前局面到终局的购买、刷新、出售路径；
- 对局内经济可达性规划；
- 用 GFlowNet 直接模拟完整酒馆动作序列；
- 战斗过程模拟；
- 从 Python 闭包或函数源码反向推断效果语义。

终局配方库回答的是“哪些完整阵容具有结构化协同”，而不是“这一局怎样买到该阵容”。

## 2. 核心设计原则

### 2.1 机制事实优先于卡牌直连

不直接存储：

```text
供能中心 -> 万叉奔腾
```

而是存储：

```text
供能中心 PRODUCES 水晶塔
水晶塔 CONTRIBUTES_TO 邻域能量
万叉奔腾 REQUIRES 集结能量阈值 3
```

具体卡牌之间的依赖由图谱推导。新增一张供塔卡后，它可以自动成为所有能量需求的候选供给者。

### 2.2 静态描述是语义入口，运行时 handler 用于校验

图谱应从以下信息联合构建：

- `Card.description` / `Card.gold_description`；
- `prepared_lines()` 合并后的规范化描述；
- 原始颜色标记；
- `resolve()` 得到的事件名；
- `EventHandler.unique`；
- `GatheringActionHandler.cost`；
- `TaskActionHandler.goal` / `auto_reset`；
- 卡牌的 `units`、`race`、`tags`、`gold_tags`、`source`。

不应只扫描运行时 `EventHandler`，因为运行时对象不保留原始颜色、普通/金色来源和统一的动作 AST；复杂 handler 还是普通 callable 或闭包，无法可靠反射。

### 2.3 自动抽取与声明式 override 并存

所有标准语法由通用解析器处理；特殊语义使用声明式 override。override 的输出仍是统一的 `EffectSpec`，而不是另建卡牌直连表。

### 2.4 全量主图与激活子图分离

主图包含全部卡牌和全部来源。生成配方时按启用拓展包得到激活子图。`source` 是可用性条件，不参与卡牌语义身份。

### 2.5 严格覆盖，不静默猜测

当一条描述已有可执行 handler，但无法生成可靠的声明式语义时：

- 严格模式：报告覆盖缺口并停止生成正式配方库；
- 宽松模式：标记为 `opaque`，保留审计信息，但不参与自动依赖闭包。

## 3. 总体架构

```text
卡牌 JSON
   │
   ▼
Card.from_json / 单位词典注册
   │
   ▼
prepared_lines + normalize + extract_colors
   │
   ▼
resolve 校验事件、unique、Gathering/Task 包装器
   │
   ├── 标准语法抽取
   └── 声明式 effect override
   │
   ▼
EffectSpec 机制事实层
   │
   ▼
全量 FactGraph
   │
   ▼ 依据 source / expansions 激活
ActiveFactGraph
   │
   ▼ 需求反向闭包 + 位置/槽位约束求解
RecipeTemplate / RecipeInstance
   │
   ▼ 规范化、评分、去重、版本化
RecipeCatalog
```

建议新增独立只读分析包，不把配方分析逻辑放入运行时 `simulator`：

```text
src/star_tarven_simulator/recipes/
├── effect_ir.py          # CardRef / Condition / Action / EffectSpec
├── extractor.py          # 描述与现有 wrapper -> EffectSpec
├── overrides.py          # 特殊描述的声明式语义
├── graph.py              # 全量事实图、激活子图、关系索引
├── templates.py          # 机制配方模板
├── generator.py          # 终局候选生成与约束求解
├── scoring.py            # 结构协同与配方评分
├── catalog.py            # 规范化、去重、版本和序列化
└── report.py             # 抽取覆盖率与版本 diff
```

## 4. 机制事实中间表示

### 4.1 卡牌引用

```python
@dataclass(frozen=True)
class CardRef:
    canonical_id: str
    name: str
    race: str
    uuid: int
    dataset_id: str | None
    sources: tuple[str, ...]
```

身份策略：

- `canonical_id`：跨版本语义身份，第一版可使用规范化名称与种族，并预留 alias manifest；
- `(dataset_id, uuid)`：精确回放某个数据快照；
- UUID 不作为跨版本永久身份；
- `source` 不进入 canonical ID，因为卡牌来源可能调整。

### 4.2 卡牌变体

普通和金色必须是两个独立变体：

```python
@dataclass(frozen=True)
class CardVariantRef:
    card: CardRef
    variant: Literal["normal", "gold"]
```

变体直接来自 `description` / `gold_description` 和 `tags` / `gold_tags`，不能依赖文本猜测。

### 4.3 效果声明

```python
@dataclass(frozen=True)
class EffectSpec:
    effect_id: str
    card: CardVariantRef
    ordinal: int
    normalized_text: str
    raw_text: str
    colors: tuple[tuple[str, str], ...]
    events: tuple[str, ...]
    unique: bool
    mechanism: str | None
    conditions: tuple[Condition, ...]
    actions: tuple[Action, ...]
    extraction: Literal["automatic", "template", "override", "opaque"]
```

`effect_id` 是规范化语义内容的哈希，不依赖 JSON 行号、原始颜色标签格式或当前启用的拓展包。

### 4.4 条件

第一版条件类型：

```text
energy_threshold
race_count_threshold
unit_count_threshold
has_unit
has_tag
has_upgrade
neighbor_filter
board_full
board_has_space
is_highest
value_compare
event_source_filter
expansion_enabled
```

条件必须保存：

- 比较方式：`>=`、`>`、`==`；
- 数值或表达式；
- 作用域：自身、邻居、全场、其他卡牌、左侧、右侧；
- 聚合方式：求和、计数、MAX、ANY、ALL；
- 是否属于硬约束。

### 4.5 动作

第一版动作类型：

```text
produce              # 直接获得单位
teleport             # 折跃单位
larva                 # 注卵
hatch                 # 孵化
transform_units       # 单位转换
move_units            # 单位移动/抽取
convert_units         # 如水晶塔 -> 虚空水晶塔
destroy_card
seize_card
add_upgrade
modify_global
modify_attribute
emit_event
discover_card
transform_card
```

动作应显式保存：

- `target_scope`；
- `object` / `unit_filter`；
- `quantity` 或数量表达式；
- 是否随机；
- 是否消耗输入；
- 是否触发后续事件；
- 与触发次数的关系。

例如应区分：

```text
teleport_call_count
teleported_unit_count
```

因为一次折跃十个单位与十次分别折跃一个单位，对“任意卡牌折跃时”的监听者价值不同。

## 5. 描述抽取策略

### 5.1 必须复用现有预处理

抽取入口必须使用 `parsing.parser.prepared_lines()`，而不是直接逐项遍历 JSON 描述数组，因为当前数据存在：

- 跨片段颜色标签；
- 括号续行；
- 相邻的任务/奖励行；
- 归一化标点；
- 普通/金色不同文本。

加载顺序应在所有初始单位完成 `register_units()` 后进行，确保新单位能被 `extract_units()` 识别。

### 5.2 可自动抽取的标准语法

| 描述模式 | EffectSpec 语义 |
|---|---|
| `获得N单位` | `produce` |
| `折跃N单位` | `teleport` |
| `注卵N单位` | `larva` |
| `孵化N单位` | `hatch` |
| `每回合开始/结束时` | 对应事件监听 |
| `进场/出售/刷新时` | 对应事件监听 |
| `快速生产:` | `quick_produce` 机制 |
| `反应堆生产` | 回合结束生产，数量受金色影响 |
| `集群(N):` | 种族数量阈值 |
| `集结(N):` | 能量阈值与触发次数表达式 |
| `灵能:` | 灵能等级条件 |
| `供养(N):` | 出售、精华除数、右侧目标 |
| `任务:... 奖励:...` | 计数事件、目标值、奖励、重置规则 |
| `唯一:` | 同描述效果的全场去重语义 |

颜色仅作为机制提示和审计 provenance，不作为唯一判定依据。未知颜色或缺色时仍使用规范化文本语法。

### 5.3 受控模板抽取

以下关系可以解析文本候选，但必须由受控模板确认语义：

- `将A变为B`；
- `将N个A精英化`；
- `相邻两侧...`；
- `场上所有...`；
- `随机N个...`；
- `每有N...`；
- `若...则...`。

不能把所有“变为”统一处理，因为引擎区分：

- 最多 N 个的一对一转换；
- 必须凑满 N 个的成组兑换；
- 随机筛选；
- 全场逐卡处理；
- 卡牌本身变身。

### 5.4 声明式 override

建议建立与执行注册表并行的图谱注册表：

```python
GRAPH_EFFECT_OVERRIDES: dict[str, EffectSpecBuilder]
```

key 使用与 `ACTION_HANDLERS` 相同的规范化描述文本。加载时执行覆盖审计：

1. 描述是否存在执行 handler；
2. 是否被标准语法或受控模板完整解析；
3. 若未完整解析，是否存在 graph override；
4. 都不存在则标记覆盖缺口。

长期可考虑扩展 `register_value()`，使执行 handler 和声明式 spec 在同一处注册，减少语义漂移；第一版保持并行注册，降低对现有执行层的影响。

## 6. 图谱模型

### 6.1 节点类型

```text
Card
CardVariant
Effect
Unit
Upgrade
Event
Mechanism
Attribute
Threshold
Scope
Expansion
```

### 6.2 关系类型

```text
STARTS_WITH       卡牌初始包含单位
LISTENS_TO        效果监听事件
PRODUCES          直接生产单位
TELEPORTS         折跃单位
LARVAS             注卵单位
HATCHES           孵化单位
TRANSFORMS        单位或卡牌转换
MOVES             移动/抽取单位
REQUIRES          依赖属性、阈值或原料
CONTRIBUTES_TO    对能量、计数等派生量提供贡献
MODIFIES          修改倍率或属性
EMITS             产生后续事件
TARGETS           作用目标范围
AVAILABLE_FROM    卡牌来源
EXCLUSIVE_WITH    拓展包互斥
BEFORE            事件时序
SUPPRESSES        负依赖
```

复杂关系通过 Effect/Condition 因子节点表达，不强行压缩成普通二元边。

### 6.3 全量主图与激活

```python
graph = build_fact_graph(all_cards, dataset_id="...")
active = graph.activate(expansions=["作战计划", "身经百战"])
```

激活逻辑必须与 `expansions.py` 保持一致：

- 核心和基础来源常驻；
- `source` 为空的卡牌常驻；
- 多来源卡只要命中任一启用来源即可用；
- 独占拓展包规则属于硬约束。

主图不因拓展选择变化而重新分配节点或关系 ID。

## 7. 终局配方模型

配方分为抽象模板和具体实例。

### 7.1 RecipeTemplate

模板描述机制结构，不绑定所有具体卡名：

```yaml
id: protoss-energy-gathering
name: 神族能量集结
core_requirements:
  - gathering_consumer
  - stable_energy_supply
constraints:
  - max_slots: 7
  - active_expansions_only: true
optional_roles:
  - void_pylon_multiplier
  - teleport_listener
  - shield_charge_carrier
```

模板用于定义配方类型、保证多样性和解释实例。

### 7.2 RecipeInstance

实例是可作为 GFlowNet 终态的具体七槽阵容：

```python
@dataclass(frozen=True)
class RecipeSlot:
    index: int
    card: CardVariantRef
    upgrades: tuple[str, ...]
    roles: tuple[str, ...]

@dataclass(frozen=True)
class RecipeInstance:
    recipe_id: str
    template_id: str
    expansions: tuple[str, ...]
    slots: tuple[RecipeSlot, ...]
    extra_neighbors: tuple[tuple[int, int], ...]
    satisfied_factors: tuple[str, ...]
    unsatisfied_factors: tuple[str, ...]
    derived_metrics: Mapping[str, object]
    score: float
    provenance: tuple[str, ...]
```

第一版配方决策变量只包含：

- 卡牌；
- 槽位；
- 普通/金色；
- 终局升级；
- 合法的额外邻接关系。

不把购买顺序、刷新次数和中间过渡卡写入配方。

### 7.3 配方规范化与去重

配方 ID 来自规范化实例内容的哈希。规范化至少包括：

- 按槽位排序；
- Card canonical ID；
- 普通/金色变体；
- 排序后的升级；
- 排序后的额外邻接边；
- extraction schema version。

位置会影响能量、相邻效果和结算顺序，因此不能把七张卡当作无序集合去重。只有经图同构证明等价的镜像布局才可以选择性合并。

## 8. 配方生成算法

### 8.1 需求反向闭包

1. 从高价值终端效果中选择种子，例如集结产出、精英化、护盾充能或稳定折跃。
2. 展开效果的硬条件和输入需求。
3. 在激活图中查找所有供给者。
4. 继续展开供给者自身需求，直到闭包完成或达到深度/槽位上限。
5. 将 OR 候选保留为不同分支，将 AND 条件保留为同一超边。

示例：

```text
集结产出
  REQUIRES energy >= 7
    <- 水晶塔
    <- 虚空水晶塔
       <- 一鼓作气倍率（可选强化）
       <- 水晶塔转换来源
```

### 8.2 七槽约束求解

候选组件进入位置求解器，约束包括：

- 最多七个槽；
- 同一槽只能有一个 CardVariant；
- 当前拓展包合法；
- 独占拓展包合法；
- 邻接效果必须有合法目标；
- 能量按闭邻域计算；
- 固定折跃目标等 tag 约束；
- `unique` 效果的重复收益规则；
- 必要原料单位或升级存在。

第一版可以使用 beam search 或约束回溯；后续 GFlowNet 以相同动作掩码构造终态。

### 8.3 结构评分

在不模拟经济路径的前提下，配方评分由机制事实组成：

```text
score =
    已满足的核心效果价值
  + 阈值档位价值
  + 正反馈闭环价值
  + 槽位复用价值
  + 稳定性价值
  - 未满足硬条件惩罚
  - 随机性风险
  - 单位上限风险
  - 负反馈与互斥冲突
```

建议保留分项而不是只保存总分：

```yaml
score:
  total: 0.87
  payoff: 0.91
  threshold_completion: 1.00
  slot_efficiency: 0.75
  stability: 0.82
  conflict_penalty: 0.10
```

GFlowNet 使用正奖励，例如：

```text
R(recipe) = exp(score / temperature)
```

但 GFlowNet 训练不属于首个图谱实现里程碑。

## 9. 神族能量体系示例

### 9.1 能量事实

当前引擎中，能量是动态派生量，不是可储存或扣除的资源：

```text
E(i) = Σ[j ∈ {i} ∪ neighbors(i)] (
    normal_pylon_count(j)
    + void_pylon_count(j) * void_pylon_multiplier
)
```

其中虚空水晶塔倍率为：

```text
3：场上存在金色一鼓作气
2：否则场上存在普通一鼓作气
1：否则
```

多个“一鼓作气”取 MAX，不相加。

对应事实：

```text
水晶塔 CONTRIBUTES_TO local_energy，value=1
虚空水晶塔 CONTRIBUTES_TO local_energy，value=global_multiplier
一鼓作气 MODIFIES global_multiplier，aggregation=MAX
邻接关系 TARGETS closed_neighborhood
```

### 9.2 集结事实

标准集结触发次数：

```text
times = min(energy // cost, 2) + has_artanis
```

图谱必须保存：

- cost；
- 两档能量上限；
- 阿塔尼斯全局额外次数；
- 产出数量是否乘以 times；
- 集结只是读取能量，不消耗能量。

### 9.3 示例 EffectSpec

万叉奔腾：

```yaml
card: 万叉奔腾
variant: normal
event: round_end
mechanism: gathering
conditions:
  - kind: energy_threshold
    cost: 3
    cap: 2
    artanis_bonus: true
actions:
  - kind: produce
    target: self
    unit: 狂热者
    quantity: 2 * trigger_times
```

一鼓作气：

```yaml
card: 一鼓作气
variant: normal
mechanism: global_modifier
actions:
  - kind: modify_global
    attribute: void_pylon_energy_value
    aggregation: max
    value: 2
```

净化者军团：

```yaml
card: 净化者军团
variant: normal
event: round_end
mechanism: gathering
unique: true
conditions:
  - kind: energy_threshold
    cost: 13
    cap: 2
    artanis_bonus: true
actions:
  - kind: move_units
    source:
      board: all
      race: protoss
      exclude_self: true
    target: self
    unit_filter:
      elite: true
    quantity: all
```

莫汉达尔与折跃链：

```text
集结效果 TELEPORTS 单位
teleport 成功 EMITS any_card_teleport（按调用计数）
莫汉达尔 LISTENS_TO any_card_teleport
莫汉达尔 PRODUCES 水晶塔
水晶塔 CONTRIBUTES_TO energy
```

这形成可由图谱识别的正反馈闭环。

### 9.4 位置求解示例

假设槽位 2 是集结核心，槽位 1 和 3 的塔都能供能：

```text
slot 1: 供塔组件
slot 2: 集结消费者
slot 3: 一鼓作气/虚空塔组件
```

同样三张卡放在不相邻位置可能无法跨越阈值，因此 RecipeInstance 必须保存槽位，而不是只保存卡牌集合。

## 10. 对外 API 草案

```python
def extract_card_effects(
    card: Card,
    *,
    dataset_id: str | None = None,
    strict: bool = True,
) -> tuple[EffectSpec, ...]: ...


def build_fact_graph(
    cards: Iterable[Card],
    *,
    dataset_id: str | None = None,
    strict: bool = True,
) -> FactGraph: ...


def active_subgraph(
    graph: FactGraph,
    expansions: Iterable[str] | None = None,
) -> FactGraph: ...


def generate_recipe_catalog(
    graph: FactGraph,
    *,
    templates: Iterable[RecipeTemplate] | None = None,
    max_slots: int = 7,
    limit_per_template: int = 100,
) -> RecipeCatalog: ...


def diff_catalogs(
    old: RecipeCatalog,
    new: RecipeCatalog,
) -> CatalogDiff: ...
```

命令行入口可设计为：

```bash
uv run python -m star_tarven_simulator.recipes.report
uv run python -m star_tarven_simulator.recipes.catalog --expansions 作战计划 身经百战
```

## 11. 持久化与版本

建议 RecipeCatalog 输出 JSON，Markdown 只用于报告。

```yaml
schema_version: 1
dataset_id: v20260826
extractor_version: 1
expansions:
  - 身经百战
graph_fingerprint: sha256:...
recipes:
  - recipe_id: sha256:...
    template_id: protoss-energy-gathering
    slots: []
    derived_metrics: {}
    score: {}
    provenance: []
```

版本差异报告至少包含：

- 新增/删除卡牌；
- EffectSpec 语义变化；
- 自动解析变为 override 或 opaque；
- 新增/失效终局配方；
- 配方分数变化原因；
- 卡包来源变化。

## 12. 覆盖率与正确性验证

### 12.1 抽取覆盖率

分别统计：

```text
总描述数
automatic 数
template 数
override 数
opaque 数
无执行 handler 数
```

“执行解析覆盖率 100%”不等于“图谱语义覆盖率 100%”，两者必须分开报告。

### 12.2 双向审计

- 每个非被动描述都必须有执行 handler；
- 每个参与配方生成的效果都必须有非 opaque EffectSpec；
- EffectSpec 的事件名必须与 resolve 结果一致；
- Gathering cost、Task goal/auto-reset 必须与 wrapper 一致；
- 普通/金色必须分别覆盖；
- override key 必须对应真实规范化描述，孤立 override 报错。

### 12.3 图谱不变量

- 稳定输入产生稳定排序和稳定 fingerprint；
- 仅改变 source 不改变效果语义 ID；
- 仅改变颜色标签但语义不变时，EffectSpec ID 不变；
- 数量、阈值、事件或目标变化时，EffectSpec ID 必须变化；
- 激活子图不得包含未启用拓展包的单来源卡牌；
- 配方不得违反七槽和独占拓展约束。

## 13. 实施阶段

### 阶段 1：机制事实基础

1. 建立 `effect_ir.py` 的不可变数据结构。
2. 复用 `prepared_lines()` 构造普通/金色描述输入。
3. 支持事件、unique、初始单位和标准简单动作。
4. 输出图谱语义覆盖率报告。

完成标准：可以确定性重建基础 FactGraph，并对数据版本做 diff。

### 阶段 2：神族能量纵向切片

1. 增加能量、塔、集结和阿塔尼斯事实。
2. 增加邻接、MAX 全局倍率和折跃事件粒度。
3. 为发电站、一鼓作气、莫汉达尔、虚空舰队、净化者军团等复杂效果补充 override。
4. 生成并解释第一批神族能量 RecipeInstance。

完成标准：配方可以解释每个集结阈值由哪些槽位和塔满足。

### 阶段 3：通用配方生成

1. 实现需求反向闭包。
2. 实现七槽位置约束求解。
3. 实现模板、评分、规范化和去重。
4. 实现按拓展包生成 Catalog。

完成标准：卡包增删后可自动重建配方，并输出新增/失效原因。

### 阶段 4：扩展机制覆盖

依次覆盖：

1. 精英化与单位转换；
2. 折跃、注卵、孵化；
3. 人族挂件与生产；
4. 虫族集群和原始虫群；
5. 任务与跨事件反馈；
6. 升级与特殊卡牌变身。

### 阶段 5：GFlowNet 接口

在 RecipeInstance 和动作掩码稳定后，再实现：

- 部分配方状态编码；
- `PlaceCardVariant(slot, card)`；
- `AddUpgrade(slot, upgrade)`；
- `STOP`；
- 基于结构评分的正终态奖励；
- 多样化终局采样。

## 14. 当前默认决策

若后续没有新的约束，第一版按以下默认值实现：

1. 图谱权威来源是静态 Card 描述和声明式 override，不反射 Python handler 源码。
2. 普通和金色是独立 CardVariant。
3. 终局配方包含卡牌、槽位、变体和升级，不包含经济路径。
4. 配方位置有序，不把七张卡当作集合。
5. 主图加载全部卡牌，生成时按拓展包激活。
6. opaque 效果不参与正式配方闭包。
7. 第一条纵向切片是神族能量—集结—折跃体系。
8. 先实现确定性图谱与配方生成，再接入 GFlowNet。
