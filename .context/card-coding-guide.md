# 卡牌效果编码指南（Card Coding Guide）

> 从旧版 `ext/tarven_interface/src/tarven_interface/cards/actions/` 提炼的“描述→代码”实战经验：
> handler 模式、可用 API、按机制的写法模板、以及踩过的坑。新版重写时沿用这套心智模型，但按
> `card-description-format.md` 的颜色标记做确定性解析。

## 1. 核心心智模型：一条描述 = 一个 handler

每条能力子句写成一个纯函数并注册进 `ACTION_HANDLERS`：

```python
def action_handler_xxxx(slot: Slot, event):
    # <这里写原始描述文本作注释，便于对照>
    slot.add_unit("陆战队员", 4)

ACTION_HANDLERS["快速生产:获得4陆战队员"] = ("quick_produce", action_handler_xxxx)
```

- **key = 归一化后的描述文本**（旧版：去色/去标点/合并任务奖励后的中文串）。
- **value = `(event_name, handler_or_wrapper)`**。`event_name` 可为单个字符串或字符串列表
  （一条效果监听多个事件）。
- 函数签名固定 `(slot, event)`：
  - `slot` = **效果宿主**（“此卡牌”）。
  - `event` = 触发上下文，通过它拿 `event.tarven`（酒馆全局）以及事件专属字段
    （如 `event.sold_slot` / `event.entered_slot` / `event.amount`）。
- 引擎在加载卡牌时（`CardEngine.__init__`）遍历 `card.description`，用描述文本查 `ACTION_HANDLERS`
  得到 `(event_name, handler)`，实例化成 `EventHandler` 挂到卡上；进场时 `copy` 到具体 slot。

### 唯一效果
描述以 `唯一:` 开头 → 注册时 `unique=True`，全场同名效果只结算一次。

### 特殊 handler 包装器
| 机制 | 包装器 | 关键点 |
|------|--------|--------|
| 任务 | `TaskActionHandler(handler, goal, auto_reset=False)` | 内部 `counter`，达 `goal` 调 handler 并广播 `any_task_finished`；`copy()` 隔离计数 |
| 集结 | `GatheringActionHandler(handler, cost)` | handler 签名多一个 `times` 参数；`times=min(energy//cost,2)+has_artanis` |

## 2. Slot API 速查（写效果时最常用）

**加/减/替换单位**
```python
slot.add_unit(unit, cnt)                     # 加单位（受 200 总量上限约束）
slot.remove_unit(unit, cnt)
slot.replace_unit(u, max_cnt, new_u, new_cnt)      # 数量≥max_cnt 时整组替换一次
slot.replace_all_units(u, old_cnt, new_u, new_cnt) # 按 old_cnt 为单位成组替换全部
slot.count(unit)                             # 该单位数量
```

**位置/集合（property）**
```python
slot.left / slot.right            # 相邻单格（可能空槽，用前判空 card_type）
slot.neighbors                    # 左右两侧非空
slot.all / slot.left_all / slot.right_all
slot.zerg / slot.terran / slot.protoss / slot.neutral   # 按 tag 过滤
slot.random                       # 随机一张非空
```

**派生量/标记**
```python
slot.tags.has("金色")             # 金色 +1 的惯用写法：1 + slot.tags.has("金色")
slot.energy                       # 神族能量强度
slot.psi_level                    # 灵能等级
slot.darkness                     # 黑暗值
slot.count("反应堆")              # 挂件计数
slot.task_vars["<md5>_xxx"]       # handler 私有持久状态（跨回合），务必用唯一前缀
slot.change_add_on([name])        # 切换人族挂件
```

**Tarven（全局）API**（经 `event.tarven` 或 `slot.state`）
```python
event.tarven.mineral += 1
event.tarven.larva({"跳虫": 2})            # 注卵，入参是 dict！
event.tarven.discover(level=[1], tags=["灵能"])   # 发现
event.tarven.seize(source_slot, target_slot)      # 夺取（转移单位/升级并摧毁 source）
event.tarven.destroy(slot)                         # 摧毁
event.tarven.trigger_any_card_event(SomeEvent(...))
event.tarven.psi_level_max
```

## 3. 按机制的写法模板

**人族·反应堆生产（round_end，金色 +1）**
```python
def h(slot, event):
    slot.add_unit("陆战队员", 1 + slot.tags.has("金色"))
ACTION_HANDLERS["反应堆生产陆战队员"] = ("round_end", h)
```

**人族·快速生产**
```python
def h(slot, event):
    slot.add_unit("攻城坦克", 1)
ACTION_HANDLERS["快速生产:获得1攻城坦克"] = ("quick_produce", h)
```

**人族·任务 + 奖励**（把任务行与奖励行合成一个 handler）
```python
def h(slot, event):
    event.tarven.mineral += 1              # 奖励逻辑
ACTION_HANDLERS["任务:让1张卡牌进场 奖励:获得1晶体矿"] = (
    "any_card_entered", TaskActionHandler(h, 1))   # goal 从任务行解析
```

**神族·折跃**
```python
def h(slot, event):
    teleport(slot, event, {"狂热者": 1, "追猎者": 2})
ACTION_HANDLERS["出售时,折跃1狂热者和2追猎者"] = ("selling", h)
```

**神族·集结(N)**（handler 多 `times` 参数）
```python
def h(slot, event, times):
    teleport(slot, event, {"虚空辉光舰": times})
ACTION_HANDLERS["集结(5):折跃1虚空辉光舰"] = ("round_end", GatheringActionHandler(h, 5))
```

**神族·灵能**（round_end 且未达最大灵能等级）
```python
def h(slot, event):
    if event.tarven.psi_level_max > slot.psi_level:
        slot.add_unit("机械哨兵", 1)
ACTION_HANDLERS["灵能:获得1机械哨兵"] = ("round_end", h)
```

**虫族·集群(N)**（条件 `len(zerg)+has_narud >= N`）
```python
def h(slot, event):
    if (len(slot.zerg) + slot.has_narud) >= 1:
        slot.add_unit("跳虫", 2)
ACTION_HANDLERS["集群(1):获得2跳虫"] = ("round_end", h)
```

**虫族·注卵 / 孵化**
```python
def h(slot, event):
    event.tarven.larva({"蟑螂": 2})        # 注意 dict
ACTION_HANDLERS["出售时,注卵2蟑螂"] = ("selling", h)
```

**中立·黑暗值**（gain_darkness 事件驱动）
```python
def h(slot, event):
    slot.add_unit("不死队", 1)
ACTION_HANDLERS["获得黑暗值时,获得1不死队"] = ("gain_darkness", h)
```

**引用触发方的槽**（any_card_* 事件）
```python
def h(slot, event):
    if event.sold_slot.tags.has("terran"):
        for u, n in event.sold_slot.units.items():
            slot.add_unit(u, n)
ACTION_HANDLERS["..."] = ("any_card_sold", h)
```

## 4. 触发时机 → event_name 映射（写代码时查这个）

| 描述里的触发短语 | event_name |
|------------------|------------|
| 每回合开始时 / 回合开始时 | `round_start` |
| 每回合结束时 | `round_end` |
| 出售时（自身） | `selling` |
| 进场时（自身） | `entering` |
| 任意卡牌进场时 | `any_card_entered` |
| 任意卡牌出售时 | `any_card_sold` |
| 任意卡牌进场或出售时 | `any_card_entered_or_sold` |
| 获得黑暗值时 | `gain_darkness` |
| 提升酒馆等级时 | `level_up`（部分旧代码误用 `upgrade`，见坑 §5） |
| 卡牌被升级时 | `upgrade` |
| 刷新时 | `refresh` |
| 快速生产 | `quick_produce` |
| 任意卡牌挂件变更时 | `any_card_addon_changed` |
| 任意任务完成时 | `any_task_finished` |
| 任意卡牌注卵时 | `any_card_larva` |
| 任意卡牌折跃时 | `any_card_teleport` |
| 定点部署 / 部署时 | `deployment` |

集结/集群/灵能/供养等是“机制前缀 + 常驻结算时机”，一般挂 `round_end`，把机制条件写在函数体里。

## 5. 已知坑与不一致（重写时修正）

1. **`larva` 入参不一致**：正确是 `larva(dict)`；但旧代码有多处误写成 `larva("名", n)`
   两参数（见 `zerg/__init__.py` 的 `2192cb7c`、`larva.py` 的 `daeb67fa`/`3eef8cee`）。
   新版统一用 dict。
2. **event_name 与描述语义不符**：不少“进场时/回合开始时”的效果被错挂到 `round_end`
   （如 `terran/__init__.py` 的 `1843bd4a`“进场时…”挂了 `round_end`；`zerg` 里“进场时,发现”
   同样）。新版应按描述短语归类。
3. **`AnyCardHatchEvent` 构造参数不一致**：`event.py` 里需要 `(tarven, slot, units)`，
   但 `card_engine.larva_action_handler` 只传了 `(tarven, slot)` → 会抛错。重写时统一签名。
4. **`gold_event_handlers` 从未被填充**：`Card.from_json` 不解析 `gold_description`，
   而 `merge_slots` 却依赖 `card.gold_event_handlers`。新版必须在加载时把 `gold_description`
   也解析成 handler 填入。
5. **`Slot.teleport` 是占位 `None`**：折跃目标槽逻辑未实现，`teleport()` 调用会失败。
   重写折跃时要落实“折跃目标”的定位规则。
6. **`EventHandler` 的属性命名**：构造用 `description`，但 `card_engine`/`parser` 里有的地方
   传参顺序/字段名混用（`desc` vs `description`、handler 与 event_name 顺序）。以
   `event_handler.py` 的定义为准。
7. **重复 key 覆盖**：`ACTION_HANDLERS` 是 dict，同描述文本会互相覆盖（如
   `反应堆生产陆战队员` 在 terran 里定义多次）。相同描述本就应同实现，注意别写出语义不同的重名。
8. **`调试 print`**：`EventHandler.handle` 非 unique 分支有 `print(...)` 调试输出，
   `parser.py` 也大量 print。新版应移除或改日志。

## 6. 新版解析建议（落到重写）

- 逐条 `description` 处理；先剥离 `<c val>` 得到纯文本用于**归一化 key**，同时保留颜色用于
  **机制分类**（见 format 文档颜色表）。
- 被动 tag 声明（“具有黑暗容器”“能够定点部署”“…折跃效果总是添加到这张牌上”等）不生成 handler，
  只影响 `tags`。
- 任务卡：识别 `任务:` 行（`008000`）与紧邻 `奖励:` 行（`FFFF80`）合成一个 `TaskActionHandler`。
- 数值（数量/阈值）仍需正则从文本抽取；颜色只定类别不定量。
- 金色：对 `gold_description` 走同一套解析，产出 `gold_event_handlers`。
- 保留“描述文本 → handler”的注册表思路（便于人工核对与复用），但 key 建议用
  (归一化文本) 或 (卡 uuid + 行号)，避免旧版 md5 兜底那种不可读命名。
