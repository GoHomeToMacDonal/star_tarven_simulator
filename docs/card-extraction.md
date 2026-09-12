# 从 SC2Map 提取卡牌数据

把 `data/maps/星际酒馆正式版-v4.6.1.7.SC2Map` 直接转成 `data/v20260826_card.json` 那种格式的卡牌 JSON。

```bash
uv run python extract_cards.py            # 解包 + 提取，一条命令
uv run python extract_cards.py --json     # 摘要以 JSON 输出
uv run python extract_cards.py --skip-unpack   # 复用已解包的地图
```

输出：

| 文件 | 内容 |
| --- | --- |
| `data/v4.6.1.7_card.json` | 155 张卡，字段与 `data/v20260826_card.json` 完全一致 |
| `artifacts/cards/v4.6.1.7_card_diagnostics.json` | 审计信息：每张卡的 specialString、词条字母、单位 id、未定价单位、与旧快照的逐字段差异 |
| `artifacts/extracted/…_extracted/` | 解包出的 `MapScript.galaxy`、本地化文本、`UnitData.xml` |

## 为什么要解释执行地图脚本

卡牌描述在游戏里是**运行时生成**的：注册卡牌时只存一条扁平的“特效字符串”
（`RushProductionFull_WhenRP_True_Self.Any.-1.-1_AddUnits.Marine-4.…,ReactorProduction_Marine_,`），
`gf_根据特效字符串生成描述文本` 再把它和本地化表拼成带颜色标记的文本。靠正则去猜这套渲染逻辑
只能得到碎片，所以本方案**跑地图自己的代码**。

`galaxy_interp.py` 是一个 Galaxy 子集解释器（编辑器生成的那部分语法：声明、赋值、
`if/else if/else`、`while`、`for`、`return`、数组、结构体、调用），引擎原生函数在 Python 里实现
（`StringExternal` 查本地化、`TextWithColor` 输出 `<c val="RRGGBB">`、`DataTable*`、`UnitTypeGetName`…）。
遇到没实现的语法或原生函数会抛 `GalaxyError`，绝不猜。

`card_extractor.py` 按地图自己的启动顺序驱动：

1. `InitGlobals`（逐语句容错，UI/多人相关失败不影响卡牌数据）
2. `gf_初始化特殊词条`、`gf_初始化卡牌升级`（描述文本要用到词条名与升级名）
3. 所有调用了 `CardPackInit` 的 `gt_*_Init`，登记卡池包
4. 逐包 `gv_LoadingCardPack = n` 后执行该包的触发器 → `gf_AddCardModule` 正常写入卡牌模板
5. `gf_初始化卡牌设计（补充）`（排序、特殊卡、辅助卡）
6. 读 `gv_已设计的卡牌模板[1..n]`

因为是地图自己的 `gf_AddCardModule` 在跑，星级、金色描述、`无法三连`/`建筑卡`/`辅助卡` 的描述前缀、
任务文本的 DataTable 存取、`TextExpressionSetToken` 的时序全部自动正确。

## 辅助工具：读懂十六进制标识符

SC2 编辑器把非 ASCII 名字改写成 UTF-8 十六进制（`gf_E5AD90…`），变量名还会把首字符小写。
`galaxy_explore.py` 负责互转与查看：

```bash
uv run python galaxy_explore.py --function 根据特效字符串生成描述文本
uv run python galaxy_explore.py --list-functions 子特效字符串
uv run python galaxy_explore.py --grep 'CardPackInit\(' --context 2
uv run python galaxy_explore.py --readable artifacts/MapScript.readable.galaxy
uv run python galaxy_explore.py --decode ge_E4BD9CE794A8E4BD8DE7BDAEA_E887AAE8BAAB
```

## 字段来源与约定

| 字段 | 来源 |
| --- | --- |
| `name` / `level` / `race` | 模板的 `lv_name` / `lv_star` / `lv_race` |
| `description` / `gold_description` | 模板的 `lv_description` / `lv_description2`，按 `<n/>` 拆行 |
| `units` | `lv_unitType[1..lv_units]` 计数，id 经 `UnitTypeGetName` 转中文名 |
| `price` | 单位数量 × 单价之和；单价取 `UnitData.xml` 的 Minerals+Vespene，缺失时用 `data/maps/unit_info.json` / `data/maps/card_overrides.json` |
| `uuid` | 模板下标 − 1（即地图排序后的注册顺序） |
| `source` | 卡池包名；`核心` 按种族拆成 `核心人族/核心神族/核心虫族/核心中立`，拓展包只用包名；辅助卡为 `辅助卡`、补充注册的特殊卡为 `特殊`；`比赛冠军` 包留空表示常驻 |
| `tags` | 种族 + 派生标签（见下） |
| `gold_tags` | 与 `tags` 相同（旧快照里两者从未出现差异） |

**`tags` 是派生约定，不是地图里的字段。** 规则：

- 卡牌子类 `原始虫群` → `属于原始虫群`；`埃蒙` → `具有虚空投影`
- 描述中独立成行的关键词句 → `具有黑暗容器` / `能够定点部署` / `拥有卵鞘` / `无法三连` / `辅助卡`
- 以关键词开头的效果行 → `灵能`

卡面左侧的词条图标字母（`lv_introductionString`）**不**用来做标签：它同时标记
任务/集结/快速生产等机制，且与卡面文字互相矛盾（如 `马拉什` 有虚空投影图标但旧快照没有该标签）。
字母与词条名的对应关系记录在诊断文件的 `keywords` 里备查。

**辅助卡**（`ge_卡牌识别符_辅助卡`）的 `units` 输出为空、`price` 为 `null`：它们的单位槽只放
`盒子`、`显示卡牌名称标记` 这类展示道具，部署后即被消耗，不参与战斗。

## `data/maps/card_overrides.json`

只用来补地图自己回答不了的两件事：本地化表里没有的**基础游戏单位中文名**，以及
`UnitData.xml` 里没有造价的单位**单价**。当前 19 条，其中数值多由旧快照的卡牌总价反推，
道具类记 0。三条只改名字的（`Reaver`→掠夺者、`Monitor`→浩劫、`PrimalFlyer`→守卫）是修正
`data/maps/unit_info.json` 的命名冲突：那里把 `Reaver` 叫 `毁灭者`（游戏里那是 `VoidRayTaldarim`），
把 `PrimalFlyer` 叫 `原始异龙`（游戏里那是 `PrimalMutalisk`）。

## 与旧快照 `data/v20260826_card.json` 的差异

提取结果会自动和旧快照对比（`--no-compare` 可关闭），当前：

- 149 张同名卡中 **90 张所有字段完全一致**，另有 7 张只差颜色/数值
- 旧快照独有 5 张（`凶猛巨兽` `唯一` `眼中无人` `虚空大军` `黑暗预兆`），本次独有 6 张
  （`不法之徒` `利维坦` `坚守信念` `复制中心` `战士的财宝` `挂件仓库`）——版本内容变动
- 剩余描述差异全部可归类：`卵鞘` 颜色从 `FF0080` 改成 `F9007C`（地图里就是
  `Color(97.65, 0.00, 48.63)`）、数值平衡改动、以及旧快照在 `gold_description` 里
  多插一行 `属于原始虫群`（地图两条描述都不生成这行）
- `uuid` 不可比：旧快照给 154 张卡编到 195 号，说明它是一次更大提取的过滤结果
- `price`/`units` 差异来自版本改动（`卵鞘` 现在会给模板加一个 `刺蛇卵` 单位）和旧数据里
  个别单位的占位价（如 `原始点火虫` 旧价高出 20000）

## 校验

```bash
uv run python -c "import sys; sys.path.insert(0, 'src'); \
from pathlib import Path; from star_tarven_simulator.loader import load_cards; \
cards, coverage = load_cards(Path('data/v4.6.1.7_card.json')); \
print(len(cards), coverage.handled_lines, '/', coverage.total_lines)"
```

模拟器能直接加载提取结果：155 张卡、623 行描述中 612 行匹配到已有 handler，
未匹配的 11 行都是本版新增或改数值的卡（`复制中心` `战士的财宝` `入景随风` 等），
说明描述文本格式与 `parsing/` 的预期完全一致。
