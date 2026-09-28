"""堆场与装船规则：落位可行性、理货闸口、吊机/箱区时段冲突（纯函数，不碰数据库）。"""
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .domain import Conflict, ValidationError, integer, number, text
from .yard_data import CRANE_CODES, HAZARD_CLASSES, incompatible


# 容器与装船计划状态
STATE_PENDING = "pending"      # 在待落区
STATE_PLACED = "placed"        # 已落位
STATE_LOADED = "loaded"        # 已装船

PLAN_OPEN = "open"             # 已锁定箱区/吊机/时段，未执行
PLAN_DONE = "done"             # 已装船完成
PLAN_CANCELLED = "cancelled"
ACTIVE_PLAN_STATES = {PLAN_OPEN}

# 落位失败原因码（进入待落区时附带原因）
REASON_NO_STACK = "no_compatible_stack"      # 没有相容空垛
REASON_WEIGHT = "block_weight_exceeded"      # 所有垛放下去超重
REASON_LAYERS = "block_layers_exceeded"      # 相容垛都已到限层
REASON_BLOCK_LOCKED = "block_locked"         # 箱区有装船计划锁定

# 角色权限
REGISTER_ROLES = {"gate_clerk"}
PLACE_ROLES = {"yard_planner"}
TALLY_ROLES = {"tally_clerk"}
PLAN_ROLES = {"vessel_planner"}

CONTAINER_NO_RE = re.compile(r"^[A-Za-z0-9]{6,12}$")


@dataclass(frozen=True)
class SlotDecision:
    stack_no: Optional[int] = None
    layer_no: Optional[int] = None
    reason: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.stack_no is not None


class YardRules:
    def known_role(self, role: str) -> bool:
        return role == "admin" or role in (
            REGISTER_ROLES | PLACE_ROLES | TALLY_ROLES | PLAN_ROLES
        )

    # ---------- 登记 ----------
    def validate_container(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        container_no = text(p, "container_no").upper()
        if not CONTAINER_NO_RE.match(container_no):
            raise ValidationError("箱号必须为6-12位字母或数字")
        p["container_no"] = container_no
        p["hazard_class"] = text(p, "hazard_class")
        if p["hazard_class"] not in HAZARD_CLASSES:
            raise ValidationError("危险品类别不在目录中")
        p["weight_t"] = round(number(p, "weight_t", 0.01, 60.0), 2)
        p["arrival_hour"] = integer(p, "arrival_hour", 0, 23)
        return p

    # ---------- 落位 ----------
    def validate_block_capacity(self, container: Dict[str, Any], block: Dict[str, Any]) -> None:
        if float(container["weight_t"]) > float(block["max_stack_weight_t"]):
            raise ValidationError("单箱重量超过箱区单垛限重")

    def choose_slot(self, container: Dict[str, Any], block: Dict[str, Any], stacks: List[Dict[str, Any]]) -> SlotDecision:
        """在给定箱区的现有垛中选位。

        stacks: 每垛当前状态 [{"stack_no":1,"top_layer":2,"total_weight_t":24.0,"classes":["5.1"]}]
        规则：只进同类垛（同类必相容，便于两班次作业）；先同类别后空垛；
        放不下时给出最具体的失败原因。
        """
        max_weight = float(block["max_stack_weight_t"])
        max_layers = int(block["max_layers"])
        stack_count = int(block["stack_count"])
        weight = float(container["weight_t"])
        hazard = container["hazard_class"]

        same_class: List[Dict[str, Any]] = []
        compatible_other: List[Dict[str, Any]] = []
        for stack in stacks:
            classes = stack.get("classes") or []
            if any(incompatible(hazard, other) for other in classes):
                continue
            if classes and all(other == hazard for other in classes):
                same_class.append(stack)
            else:
                compatible_other.append(stack)

        weight_failed = False
        layers_failed = False
        for stack in same_class + compatible_other:
            top = int(stack.get("top_layer", 0))
            total = float(stack.get("total_weight_t", 0.0))
            if top >= max_layers:
                layers_failed = True
                continue
            if total + weight > max_weight:
                weight_failed = True
                continue
            return SlotDecision(stack_no=int(stack["stack_no"]), layer_no=top + 1)

        # 现有垛放不下，尝试开新垛（空垛必相容）
        used = {int(s["stack_no"]) for s in stacks}
        for stack_no in range(1, stack_count + 1):
            if stack_no not in used:
                return SlotDecision(stack_no=stack_no, layer_no=1)

        if weight_failed:
            return SlotDecision(reason=REASON_WEIGHT, detail="相容垛落位后超过单垛限重")
        if layers_failed:
            return SlotDecision(reason=REASON_LAYERS, detail="相容垛已达到限层")
        return SlotDecision(reason=REASON_NO_STACK, detail="没有相容箱垛")

    def require_placement_state(self, container: Dict[str, Any], allow_placed: bool) -> None:
        state = container["state"]
        if state == STATE_LOADED:
            raise Conflict("箱子已装船，不能落位")
        if state == STATE_PLACED and not allow_placed:
            raise Conflict("箱子已落位，如需调整请走改配")
        if state != STATE_PLACED:
            # pending
            return

    def require_block_unlocked(self, block: Dict[str, Any]) -> None:
        if not block.get("tally_completed"):
            return
        # 理货完成本身不锁箱；锁定来自未完成的装船计划，由 service 查计划后调用本方法
        plan_id = block.get("active_plan_id")
        if plan_id:
            raise Conflict("箱区已被装船计划%s锁定" % plan_id)

    # ---------- 理货 ----------
    def validate_tally(self, data: Dict[str, Any]) -> bool:
        value = data.get("tally_completed")
        if not isinstance(value, bool):
            raise ValidationError("tally_completed必须是布尔值")
        return value

    # ---------- 装船计划 ----------
    def validate_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        p["vessel"] = text(p, "vessel")
        p["crane_code"] = text(p, "crane_code")
        p["block_code"] = text(p, "block_code")
        p["start_hour"] = integer(p, "start_hour", 0, 23)
        p["end_hour"] = integer(p, "end_hour", 1, 24)
        if p["end_hour"] <= p["start_hour"]:
            raise ValidationError("end_hour必须晚于start_hour")
        if p["crane_code"] not in CRANE_CODES:
            raise ValidationError("吊机不在目录中")
        return p

    def _overlap(self, start: int, end: int, other: Dict[str, Any]) -> bool:
        return start < int(other["end_hour"]) and end > int(other["start_hour"])

    def check_crane_conflict(self, plan: Dict[str, Any], others: List[Dict[str, Any]]) -> None:
        """同一吊机同一时段只能服务一船。"""
        for other in others:
            if other["state"] not in ACTIVE_PLAN_STATES:
                continue
            if other["crane_code"] != plan["crane_code"]:
                continue
            if self._overlap(int(plan["start_hour"]), int(plan["end_hour"]), other):
                raise Conflict("吊机%s在该时段已服务%s" % (plan["crane_code"], other["vessel"]))

    def check_block_conflict(self, plan: Dict[str, Any], others: List[Dict[str, Any]]) -> None:
        """同一箱区同一时段只能锁定给一船。"""
        for other in others:
            if other["state"] not in ACTIVE_PLAN_STATES:
                continue
            if other["block_code"] != plan["block_code"]:
                continue
            if self._overlap(int(plan["start_hour"]), int(plan["end_hour"]), other):
                raise Conflict("箱区%s在该时段已锁定给%s" % (plan["block_code"], other["vessel"]))

    def require_block_tallied(self, block: Dict[str, Any]) -> None:
        if not block.get("tally_completed"):
            raise Conflict("箱区尚未完成理货，不能编制装船计划")

    def require_open_plan(self, plan: Dict[str, Any]) -> None:
        if plan["state"] == PLAN_CANCELLED:
            raise Conflict("装船计划已取消")
        if plan["state"] == PLAN_DONE:
            raise Conflict("装船计划已完成")

    def containers_for_loading(self, containers: List[Dict[str, Any]], block_code: str) -> List[int]:
        return [c["id"] for c in containers if c["state"] == STATE_PLACED and c["block_code"] == block_code]
