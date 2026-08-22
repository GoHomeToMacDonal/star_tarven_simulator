"""描述解析包。

注意：:mod:`parser` 会导入 :mod:`cards`，而 :mod:`cards` 又依赖本包的 :mod:`text`，
因此这里只在包层面暴露轻量的 ``text`` 工具，``parser`` 请直接从子模块导入
（``from star_tarven_simulator.parsing.parser import parse_card``）以避免循环导入。
"""

from star_tarven_simulator.parsing.text import (
    extract_colors,
    extract_units,
    normalize,
    strip_color,
    units_dict,
)

__all__ = [
    "extract_colors",
    "extract_units",
    "normalize",
    "strip_color",
    "units_dict",
]
