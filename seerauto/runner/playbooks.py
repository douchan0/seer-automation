"""Node playbooks: how a goal node turns into a concrete skill sequence.

The goal graph says *what* must become true (``puni.prepare.level_up`` requires
the main pet at level 36).  It deliberately does not say *which* clicks achieve
it, because that changes as the account moves.  The playbook is the middle
layer: an explicit, auditable recipe the scheduler expands into verified skills.

Only nodes with a playbook can be executed.  A node without one is reported as
"no playbook" instead of being improvised — improvisation is exactly how an
agent ends up clicking into unknown UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class Step:
    """One skill invocation inside a node's playbook."""

    skill: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    #: ``always`` | ``no_monster`` | ``not_in_battle``
    when: str = "always"
    #: Stop the node early when this skill fails (otherwise continue).
    required: bool = True
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"skill": self.skill, "inputs": dict(self.inputs), "when": self.when,
                "required": self.required, "label": self.label}


#: Farming spot that Round 4 proved: 克洛斯星林间, wild 蘑菇怪.
FARM_BOSS = "蘑菇怪"

PLAYBOOKS: dict[str, tuple[Step, ...]] = {
    # 练级：先确保人在刷怪场景（可验证的传送），再连战。
    # 谱尼入口：纯导航，不需要战斗，因此即使宠物全员 0 血也能完成。
    "puni.seal.entry_nav": (
        Step("game.goto_puni_entrance", {}, label="SPT→太空站左翼→谱尼→前往"),
    ),
    "puni.prepare.tutor_room": (
        Step("game.goto_tutor_room", {}, label="地图→辅导员训练室"),
    ),
    # 刷塔：先到辅导员训练室，再进塔读层数。读到的就是可验证的进度证据。
    "puni.prepare.tower_farm": (
        Step("game.travel", {"destination": "tutor_room"}, label="地图→辅导员训练室"),
        Step("game.tower_status", {}, label="进试炼之塔读「你当前到达第N层」"),
    ),
    "puni.prepare.level_up": (
        Step("game.navigate_spt_boss", {"boss": FARM_BOSS}, when="no_monster_idle",
             label="传送到已验证的刷怪场景"),
        Step("game.run_battle", {"battles": 10, "monster": FARM_BOSS,
                                 "max_seconds": 420}, label="连战刷经验"),
    ),
}


def playbook(node_id: str) -> tuple[Step, ...]:
    return PLAYBOOKS.get(node_id, ())


def has_playbook(node_id: str) -> bool:
    return node_id in PLAYBOOKS


def describe() -> dict[str, Any]:
    return {node: [s.to_dict() for s in steps] for node, steps in PLAYBOOKS.items()}


__all__ = ["Step", "PLAYBOOKS", "playbook", "has_playbook", "describe", "FARM_BOSS"]
