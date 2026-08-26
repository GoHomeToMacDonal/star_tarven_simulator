"""Web 测试界面的后端：一个零依赖的 JSON API + 静态页面服务器。

它在内存里维护一局 :class:`~star_tarven_simulator.simulator.game.Game`，把当前被操作
玩家（``Tarven``）的完整状态序列化成 JSON 供前端渲染，并把前端动作翻译成
``Tarven.action(...)`` 调用。所有卡牌只在进程启动时加载/解析一次。
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from star_tarven_simulator.expansions import EXPANSION_PACKS
from star_tarven_simulator.loader import build_game, load_cards
from star_tarven_simulator.parsing.text import strip_color
from star_tarven_simulator.simulator.action import (
    BuyAction,
    CacheEnterAction,
    ChooseCardAction,
    ChooseSynthesisAction,
    ChooseUpgradeAction,
    DeployAction,
    HeroChoiceAction,
    LockAction,
    RefreshAction,
    SellAction,
    SynthesisAction,
    UpgradeAction,
    UpgradeTarvenAction,
)
from star_tarven_simulator.simulator.card import Card
from star_tarven_simulator.simulator.game import Game, Tarven
from star_tarven_simulator.simulator.hero import SELECTABLE_HEROES

STATIC_DIR = Path(__file__).resolve().parent / "static"


# ======================================================================
# 会话：进程内单例，持有已加载卡牌与当前对局。
# ======================================================================
class Session:
    """进程内单例会话。线程安全（每个请求持锁）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cards, self.coverage = load_cards()
        self.game: Optional[Game] = None
        self.player_idx: int = 0
        self.log: List[str] = []

    # --- 生命周期 -----------------------------------------------------
    def new_game(
        self,
        *,
        hero: str = "default",
        expansions: Optional[List[str]] = None,
        random_pick: bool = False,
        user_count: int = 1,
        seed: Optional[int] = None,
    ) -> None:
        rng = random.Random(seed) if seed is not None else random.Random()
        heroes = None
        if hero and hero != "default":
            heroes = [hero] + ["default"] * (user_count - 1)
        self.game = build_game(
            self.cards,
            user_count=user_count,
            expansions=expansions or None,
            random_pick=random_pick,
            heroes=heroes,
            rng=rng,
        )
        self.player_idx = 0
        self.log = [
            "新对局已创建"
            + (f"（拓展包: {', '.join(self.game.enabled_expansions)}）"
               if self.game.enabled_expansions else "（仅核心）")
        ]
        # 第一回合开始，填充商店
        self.game.round_start()
        self.log.append(f"第 {self.game.round} 回合开始")

    @property
    def tarven(self) -> Tarven:
        if self.game is None:
            raise RuntimeError("对局尚未创建")
        return self.game.tarvens[self.player_idx]


SESSION = Session()


# ======================================================================
# 序列化
# ======================================================================
def _card_description(card: Card, gold: bool = False) -> List[str]:
    desc = card.gold_description if gold and card.gold_description else card.description
    out: List[str] = []
    for line in desc:
        text = strip_color(line).strip()
        if text:
            out.append(text)
    return out


def serialize_card(card: Union[Card, str, None]) -> Optional[Dict[str, Any]]:
    if card is None:
        return None
    if isinstance(card, str):
        # 缓存里可能是卡牌名字符串；尝试解析回 Card
        resolved = SESSION.tarven.pool.card_type_map.get(card) if SESSION.game else None
        if resolved is None:
            return {"name": card, "level": 0, "race": "", "price": 0.0,
                    "units": {}, "tags": [], "description": []}
        card = resolved
    return {
        "name": card.name,
        "level": card.level,
        "race": card.race,
        "price": round(card.price, 2),
        "units": dict(card.units),
        "tags": list(card.tags),
        "source": list(getattr(card, "source", []) or []),
        "description": _card_description(card),
    }


def serialize_slot(slot) -> Dict[str, Any]:
    empty = slot.card_type is None
    return {
        "index": slot.index,
        "empty": empty,
        "card_type": slot.card_type,
        "level": slot.level if not empty else None,
        "price": round(slot.price(), 2) if not empty else 0.0,
        "equivalent_power": round(slot.equivalent_power(), 2) if not empty else 0.0,
        "units": dict(slot.units) if not empty else {},
        "tags": list(slot.tags) if not empty else [],
        "upgrades": list(slot.upgrades) if not empty else [],
        "darkness": slot.darkness if not empty else 0,
        "gold": bool(slot.tags.has("金色")) if not empty else False,
        "has_deploy": (not empty) and _slot_is_aux_target(slot),
    }


def _slot_is_aux_target(slot) -> bool:
    # 目标槽只要有卡即可作为部署对象；这里仅标记为可选目标。
    return slot.card_type is not None


def _is_aux_card(card: Union[Card, str, None]) -> bool:
    """辅助卡：含 ``部署时`` (deployment) handler。"""
    if isinstance(card, str) and SESSION.game:
        card = SESSION.tarven.pool.card_type_map.get(card)
    if not isinstance(card, Card):
        return False
    from star_tarven_simulator.simulator.event import DeploymentEvent

    return any(h.event_name == DeploymentEvent.event_name for h in card.event_handlers)


def serialize_force_action(tarven: Tarven) -> Optional[Dict[str, Any]]:
    if not tarven.force_action:
        return None
    fa = tarven.force_action[0]

    def opt_label(o: Any) -> str:
        if isinstance(o, Card):
            return f"{o.name} (Lv{o.level})"
        return str(o)

    if isinstance(fa, ChooseCardAction):
        return {
            "kind": "choose_card",
            "prompt": "发现：选择 1 张卡牌",
            "options": [opt_label(c) for c in fa.cards],
            "cards": [serialize_card(c) for c in fa.cards],
        }
    if isinstance(fa, ChooseUpgradeAction):
        return {
            "kind": "choose_upgrade",
            "prompt": f"为卡槽 {fa.slot_idx} 选择升级",
            "options": list(fa.upgrade_names),
        }
    if isinstance(fa, ChooseSynthesisAction):
        return {
            "kind": "choose_synthesis",
            "prompt": "三连奖励：选择 1 项",
            "options": [opt_label(o) for o in fa.options],
            "cards": [serialize_card(o) if isinstance(o, Card) else None
                      for o in fa.options],
        }
    if isinstance(fa, HeroChoiceAction):
        return {
            "kind": "hero_choice",
            "prompt": f"英雄选择（{fa.kind}）",
            "options": [opt_label(o) for o in fa.options],
        }
    return {"kind": "unknown", "prompt": type(fa).__name__, "options": []}


def serialize_state() -> Dict[str, Any]:
    if SESSION.game is None:
        return {"active": False}
    t = SESSION.tarven
    g = SESSION.game
    return {
        "active": True,
        "round": g.round,
        "max_round": g.max_round,
        "player_idx": SESSION.player_idx,
        "user_count": len(g.tarvens),
        "enabled_expansions": list(g.enabled_expansions),
        "hero": t.hero_controller.hero_name,
        "level": t.level,
        "level_up_cost": t.hero_controller.level_up_cost(t.level_up_cost),
        "gas": t.gas,
        "gas_max": t.gas_max,
        "mineral": t.mineral,
        "mineral_max": t.mineral_max,
        "health": t.health,
        "lock": t.lock,
        "free_refresh": t.free_refresh,
        "total_power": round(t.total_power(), 2),
        "total_equivalent_power": round(t.total_equivalent_power(), 2),
        "shop": [
            {"idx": i, "card": serialize_card(c),
             "price": t.card_price(i) if c is not None else None,
             "is_aux": _is_aux_card(c)}
            for i, c in enumerate(t.shop)
        ],
        "cache": [
            {"idx": i, "card": serialize_card(c), "is_aux": _is_aux_card(c)}
            for i, c in enumerate(t.cache)
        ],
        "slots": [serialize_slot(s) for s in t.slots],
        "force_action": serialize_force_action(t),
        "log": SESSION.log[-40:],
    }


# ======================================================================
# 动作分发
# ======================================================================
def _apply_force_action(tarven: Tarven, index: int) -> bool:
    if not tarven.force_action:
        return False
    fa = tarven.force_action[0]
    if isinstance(fa, ChooseCardAction):
        if not (0 <= index < len(fa.cards)):
            return False
        fa.selected_card = fa.cards[index]
    elif isinstance(fa, ChooseUpgradeAction):
        if not (0 <= index < len(fa.upgrade_names)):
            return False
        fa.selected_upgrade_name = fa.upgrade_names[index]
    elif isinstance(fa, ChooseSynthesisAction):
        if not (0 <= index < len(fa.options)):
            return False
        fa.selected = fa.options[index]
    elif isinstance(fa, HeroChoiceAction):
        if not (0 <= index < len(fa.options)):
            return False
        fa.selected = fa.options[index]
    else:
        return False
    return tarven.action(fa)


def dispatch_action(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把前端动作翻译成 Tarven 调用。返回 {ok, message}。"""
    if SESSION.game is None:
        return {"ok": False, "message": "对局尚未创建"}
    t = SESSION.tarven
    kind = payload.get("type")

    def _int(name: str) -> Optional[int]:
        v = payload.get(name)
        return int(v) if v is not None else None

    # 回合推进作用于全局，其它作用于当前玩家。
    if kind == "round_start":
        SESSION.game.round_start()
        return {"ok": True, "message": f"第 {SESSION.game.round} 回合开始"}
    if kind == "round_end":
        SESSION.game.round_end()
        return {"ok": True, "message": f"第 {SESSION.game.round} 回合结束"}
    if kind == "select_player":
        idx = _int("player_idx") or 0
        if 0 <= idx < len(SESSION.game.tarvens):
            SESSION.player_idx = idx
            return {"ok": True, "message": f"切换到玩家 {idx}"}
        return {"ok": False, "message": "玩家索引无效"}

    if kind == "force":
        ok = _apply_force_action(t, _int("index") or 0)
        return {"ok": ok, "message": "已提交选择" if ok else "选择无效"}

    action: Any = None
    if kind == "buy":
        action = BuyAction(shop_idx=_int("shop_idx"), slot_idx=_int("slot_idx"))
    elif kind == "cache_enter":
        action = CacheEnterAction(cache_idx=_int("cache_idx"), slot_idx=_int("slot_idx"))
    elif kind == "sell":
        action = SellAction(slot_idx=_int("slot_idx"))
    elif kind == "refresh":
        action = RefreshAction()
    elif kind == "lock":
        action = LockAction()
    elif kind == "upgrade_tarven":
        action = UpgradeTarvenAction()
    elif kind == "upgrade_card":
        action = UpgradeAction(slot_idx=_int("slot_idx"))
    elif kind == "synthesis":
        action = SynthesisAction(shop_idx=_int("shop_idx"), cache_idx=_int("cache_idx"))
    elif kind == "deploy":
        action = DeployAction(
            slot_idx=_int("slot_idx"),
            cache_idx=_int("cache_idx"),
            shop_idx=_int("shop_idx"),
        )
    else:
        return {"ok": False, "message": f"未知动作: {kind}"}

    ok = t.action(action)
    return {"ok": ok, "message": ("动作成功" if ok else "动作被引擎拒绝")}


# ======================================================================
# HTTP 处理
# ======================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "TarvenWebUI/1.0"

    def log_message(self, *args) -> None:  # 静音默认访问日志
        pass

    # --- helpers ------------------------------------------------------
    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404, "Not found")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # --- routes -------------------------------------------------------
    def do_GET(self) -> None:
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if route == "/api/options":
            with SESSION.lock:
                self._send_json({
                    "heroes": ["default"] + sorted(h for h in SELECTABLE_HEROES if h != "default"),
                    "expansions": list(EXPANSION_PACKS),
                    "card_count": len(SESSION.cards),
                    "coverage": round(SESSION.coverage.rate, 4),
                })
            return
        if route == "/api/state":
            with SESSION.lock:
                self._send_json(serialize_state())
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:
        route = self.path.split("?", 1)[0]
        data = self._read_json()
        try:
            with SESSION.lock:
                if route == "/api/new_game":
                    SESSION.new_game(
                        hero=data.get("hero", "default"),
                        expansions=data.get("expansions"),
                        random_pick=bool(data.get("random_pick", False)),
                        user_count=int(data.get("user_count", 1)),
                        seed=data.get("seed"),
                    )
                    result = serialize_state()
                    self._send_json(result)
                    return
                if route == "/api/action":
                    result = dispatch_action(data)
                    SESSION.log.append(
                        f"[{data.get('type')}] {result['message']}"
                    )
                    state = serialize_state()
                    state["last_action"] = result
                    self._send_json(state)
                    return
            self.send_error(404, "Not found")
        except ValueError as exc:
            self._send_json({"active": SESSION.game is not None,
                             "error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001 - surface to UI for debugging
            traceback.print_exc()
            self._send_json({"error": f"{type(exc).__name__}: {exc}",
                             "trace": traceback.format_exc()}, status=500)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"星际酒馆模拟器 WebUI: http://{host}:{port}")
    print("（Ctrl-C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="星际酒馆模拟器 Web 测试界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
