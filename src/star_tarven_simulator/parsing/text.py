"""描述文本的处理工具：颜色标记剥离、标点归一化、单位短语抽取。

新版描述用 ``<c val="RRGGBB">关键词</c>`` 包裹关键词，颜色即语义类别。本模块提供：

* :func:`strip_color` —— 去掉 ``<c val>`` 标记，得到纯文本。
* :func:`extract_colors` —— 抽出所有 ``(颜色, 关键词)`` 标记，颜色统一大写。
* :func:`normalize` —— 归一化标点（全角冒号/逗号/括号/分号 → 半角），得到稳定的匹配 key。
* :func:`extract_units` —— 从一段文本里抽取 ``[(数量, 单位名), ...]``。
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from star_tarven_simulator.constants.unit_prices import UNIT_PRICES
from star_tarven_simulator.constants import unit_type as _ut

_COLOR_TAG_RE = re.compile(r'<c\s+val="([0-9A-Fa-f]+)">(.*?)</c>', re.DOTALL)
_OPEN_TAG_RE = re.compile(r'<c\s+val="([0-9A-Fa-f]+)">')
_CLOSE_TAG_RE = re.compile(r"</c>")

# 近义色归一化（大小写已忽略）：把变体色映射到主色
_COLOR_ALIASES = {
    "7F003F": "800040",  # 注卵
}


def strip_color(text: str) -> str:
    """去掉 ``<c val>`` 标记，只保留关键词文本。

    容错处理：新版数据里同一逻辑描述会被切成多个 JSON 片段，导致标签跨片段而在
    单个片段里不闭合（如片段 ``<c val="FF8000">无法三连`` 与下一片段 ``</c>...``）。
    因此这里依次：① 消除成对标签 ② 删除落单的 ``</c>`` ③ 删除落单的开标签。
    """
    text = _COLOR_TAG_RE.sub(lambda m: m.group(2), text)
    text = _CLOSE_TAG_RE.sub("", text)
    text = _OPEN_TAG_RE.sub("", text)
    return text


def extract_colors(text: str) -> List[Tuple[str, str]]:
    """返回 ``[(颜色HEX大写, 关键词), ...]``，容忍未闭合标签，并做近义色归一化。"""
    result: List[Tuple[str, str]] = []
    for m in _OPEN_TAG_RE.finditer(text):
        color = m.group(1).upper()
        color = _COLOR_ALIASES.get(color, color)
        # 关键词 = 开标签之后、到下一个 '<' 或字符串结尾为止
        rest = text[m.end():]
        end = rest.find("<")
        keyword = rest if end == -1 else rest[:end]
        result.append((color, keyword))
    return result


def normalize(text: str) -> str:
    """归一化标点并去除首尾空白，得到稳定 key。保留顿号 ``、``。"""
    text = strip_color(text)
    replacements = {
        "：": ":",
        "，": ",",
        "（": "(",
        "）": ")",
        "；": ";",
        "\u3000": " ",  # 全角空格
        "\n": " ",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    # 折叠多余空白
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 单位词典（用于从文本抽取"数量 + 单位"）
# ---------------------------------------------------------------------------
def _build_unit_lexicon() -> List[str]:
    names = set(UNIT_PRICES.keys())
    for attr in dir(_ut):
        val = getattr(_ut, attr)
        if isinstance(val, list) and attr.isupper():
            names.update(val)
    # 为可精英化单位补充 "(精英)" 变体
    for base in list(names):
        if not base.endswith("(精英)"):
            names.add(base + "(精英)")
    # 一些描述中出现、但不在价格表里的衍生物 / 挂件 / 效果专属单位
    names.update(
        [
            "零件",
            "精华",
            "信号塔",
            "刺蛇卵",
            "虚空裂隙",
            "被感染的陆战队员",
            "混合体天罚者",
            "混合体巨兽",
            "末日巨兽",
            "黄昏之翼",
            "女妖(精英)",
            "雷诺(狙击手)",
            "英雄不朽者",
            "德哈卡的分身",
            "德哈卡的化身",
            "凯达林巨石",
            "重工厂",
            "行星要塞",
            "警戒机器人",
            "先锋",
            "蜘蛛雷",
            "劫掠者(皇家卫队)",
            "攻城坦克(皇家卫队)",
            "萨尔纳加充能水晶",
        ]
    )
    # 按长度降序，保证正则优先匹配更长的单位名
    return sorted(names, key=len, reverse=True)


_UNIT_LEXICON = _build_unit_lexicon()
_UNIT_SET = set(_UNIT_LEXICON)
_UNIT_ALT = "|".join(re.escape(name) for name in _UNIT_LEXICON)
_UNIT_RE = re.compile(rf"(\d+)\s*({_UNIT_ALT})")


def register_units(names) -> None:
    """向单位词典追加新单位名（含其"(精英)"变体），并重建匹配正则。

    加载器在解析前用数据里出现的全部单位名调用它，以覆盖旧价格表中缺失的新单位。
    """
    global _UNIT_LEXICON, _UNIT_SET, _UNIT_ALT, _UNIT_RE
    changed = False
    for name in names:
        if not name:
            continue
        if name not in _UNIT_SET:
            _UNIT_SET.add(name)
            changed = True
        elite = name + "(精英)"
        if not name.endswith("(精英)") and elite not in _UNIT_SET:
            _UNIT_SET.add(elite)
            changed = True
    if changed:
        _UNIT_LEXICON = sorted(_UNIT_SET, key=len, reverse=True)
        _UNIT_ALT = "|".join(re.escape(n) for n in _UNIT_LEXICON)
        _UNIT_RE = re.compile(rf"(\d+)\s*({_UNIT_ALT})")


def extract_units(text: str) -> List[Tuple[int, str]]:
    """从文本抽取 ``[(数量, 单位名), ...]``。

    例如 ``"获得1狂热者和2追猎者"`` -> ``[(1, "狂热者"), (2, "追猎者")]``。
    只匹配"数字 + 已知单位名"的组合，因此形容词/其它数字不会误伤。
    """
    return [(int(n), unit) for n, unit in _UNIT_RE.findall(text)]


def units_dict(text: str) -> Dict[str, int]:
    """:func:`extract_units` 的 dict 形式（同名单位数量相加）。"""
    result: Dict[str, int] = {}
    for n, unit in extract_units(text):
        result[unit] = result.get(unit, 0) + n
    return result


def is_unit_known(name: str) -> bool:
    return name in _UNIT_SET
