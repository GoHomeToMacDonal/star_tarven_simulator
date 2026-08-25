"""星际酒馆模拟器 Web 测试界面。

一个零依赖（标准库 ``http.server`` + 单页前端）的交互式界面，用于手动驱动一局
酒馆经济：新建对局、购买/出售/刷新/锁定/升级/三连/定点部署卡牌，并观察每个卡槽
内单位与总价值的变化。

运行::

    uv run python -m star_tarven_simulator.webui
    uv run python -m star_tarven_simulator.webui --port 8080
"""

from star_tarven_simulator.webui.server import main, serve

__all__ = ["main", "serve"]
