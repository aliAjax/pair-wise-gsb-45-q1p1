"""堆场与装船规则：落位约束、吊机时段冲突、理货闸门与计划状态转换。

本模块保持纯函数风格，只做规则判定，不直接读写数据库；
需要快照的判断由事务层（repository）在同一事务内传入。
"""
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Conflict, ValidationError, choice, integer, number, text
from . import yard_data

CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 集装箱状态：待落 / 已落位 / 已装船
CONTAINER_STATES = ("pending", "placed", "loaded")

# 装船计划状态
PLAN_INITIAL_STATE = "draft"
PLAN_TRANSITIONS = {
    "confirm": {"draft": "confirmed"},
    "execute": {"confirmed": "executed"},
    "cancel": {"draft": "cancelled", "confirmed": "cancelled"},
}

# 堆场作业角色
CREATE_CONTAINER_ROLES = {"yard_planner", "port_controller"}
YARD_WRITE_ROLES = {
    "yard_planner",
    "yard_worker",
    "port_controller",
}
TALLY_ROLES = {"tally_clerk", "port_controller"}
PLAN_ROLES = {"vessel_planner", "port_controller"}
YARD_READ_ROLES = {
    "yard_planner",
    "yard_worker",
    "tally_clerk",
    "vessel_planner",
    "port_controller",
}


def compatible(class_a: str, class_b: str) -> bool:
    """两个危险品类别能否放在同一垛。"""
    if class_a == class_b:
        return True
    if class_a in yard_data.SOLO_CLASSES or class_b in yard_data.SOLO_CLASSES:
        return False
    return frozenset((class_a, class_b)) not in yard_data.INCOMPATIBLE_PAIRS


class YardRules:
    def known_role(self, role: str) -> bool:
        return role == "admin" or role in (
            YARD_WRITE_ROLES | TALLY_ROLES | PLAN_ROLES
        )

    def can_read(self, role: str) -> bool:
        return role == "admin" or role in YARD_READ_ROLES

    def can_register_container(self, role: str) -> bool:
        return role == "admin" or role in CREATE_CONTAINER_ROLES

    def can_place(self, role: str) -> bool:
        return role == "admin" or role in YARD_WRITE_ROLES

    def can_tally(self, role: str) -> bool:
        return role == "admin" or role in TALLY_ROLES

    def can_plan(self, role: str) -> bool:
        return role == "admin" or role in PLAN_ROLES

    # ---------- 登记 ----------
    def validate_container(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        container_no = text(payload, "container_no").upper()
        if not CONTAINER_RE.match(container_no):
            raise ValidationError("箱号必须为4位字母加7位数字，例如ABCD1234567")
        dg_class = choice(payload, "dg_class", yard_data.DG_CLASSES)
        weight = round(number(payload, "weight_t", 0.01, 200.0), 2)
        zone_code = text(payload, "zone_code")
        shift = choice(payload, "arrival_shift", list(yard_data.SHIFTS.keys()))
        return {
            "container_no": container_no,
            "dg_class": dg_class,
            "weight_t": weight,
            "zone_code": zone_code,
            "arrival_date": self._date(payload, "arrival_date"),
            "arrival_shift": shift,
            "remark": str(payload.get("remark", "") or "").strip(),
        }

    @staticmethod
    def _date(payload: Dict[str, Any], key: str) -> str:
        value = text(payload, key)
        if not DATE_RE.match(value):
            raise ValidationError("%s必须为YYYY-MM-DD格式" % key)
        year, month, day = (int(part) for part in value.split("-"))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            raise ValidationError("%s不是合法日期" % key)
        return value

    # ---------- 落位 ----------
    def choose_stack(
        self,
        container: Dict[str, Any],
        zone: Dict[str, Any],
        stacks: Dict[str, Dict[str, Any]],
        preferred: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """在箱区内为箱子选垛。

        stacks: 垛位编号 -> {"layers": 当前层数, "weight_t": 当前总重,
                           "classes": 该垛已有类别集合}
        返回 (垛位编号, 放不下原因)；指定垛位但放不下时返回原因。
        """
        candidates = [preferred] if preferred else list(zone["stack_codes"])
        if preferred and preferred not in zone["stack_codes"]:
            return None, "垛位%s不属于箱区%s" % (preferred, zone["code"])
        reasons: List[str] = []
        for stack_code in candidates:
            stack = stacks.get(stack_code, {"layers": 0, "weight_t": 0.0, "classes": set()})
            blocker = self._stack_blocker(container, zone, stack)
            if blocker is None:
                return stack_code, None
            reasons.append("%s：%s" % (stack_code, blocker))
        return None, "；".join(reasons) if reasons else "箱区内没有可用垛位"

    @staticmethod
    def _stack_blocker(
        container: Dict[str, Any], zone: Dict[str, Any], stack: Dict[str, Any]
    ) -> Optional[str]:
        if int(stack["layers"]) >= int(zone["max_layers"]):
            return "层数已达箱区上限%s层" % zone["max_layers"]
        total_weight = round(float(stack["weight_t"]) + float(container["weight_t"]), 2)
        if total_weight > float(zone["max_stack_weight_t"]):
            return "总重%s吨超过箱区限重%s吨" % (total_weight, zone["max_stack_weight_t"])
        for other_class in stack["classes"]:
            if not compatible(str(container["dg_class"]), str(other_class)):
                return "类别%s与已堆放类别%s不相容" % (container["dg_class"], other_class)
        return None

    # ---------- 装船计划 ----------
    def validate_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        vessel = text(payload, "vessel")
        zone_code = text(payload, "zone_code")
        crane_code = text(payload, "crane_code")
        plan_date = self._date(payload, "plan_date")
        start = integer(payload, "start_hour", 0, 23)
        end = integer(payload, "end_hour", 1, 24)
        if end <= start:
            raise ValidationError("end_hour必须晚于start_hour")
        containers = payload.get("container_nos", [])
        if not isinstance(containers, list) or not containers:
            raise ValidationError("container_nos至少需要一个箱号")
        normalized: List[str] = []
        for item in containers:
            if not isinstance(item, str) or not item.strip():
                raise ValidationError("container_nos必须是箱号列表")
            no = item.strip().upper()
            if no in normalized:
                raise ValidationError("箱号%s在清单中重复" % no)
            normalized.append(no)
        return {
            "vessel": vessel,
            "zone_code": zone_code,
            "crane_code": crane_code,
            "plan_date": plan_date,
            "start_hour": start,
            "end_hour": end,
            "container_nos": normalized,
        }

    @staticmethod
    def windows_overlap(one: Dict[str, Any], other: Dict[str, Any]) -> bool:
        if one["crane_code"] != other["crane_code"] or one["plan_date"] != other["plan_date"]:
            return False
        return int(one["start_hour"]) < int(other["end_hour"]) and int(one["end_hour"]) > int(
            other["start_hour"]
        )

    def check_crane_conflict(
        self, candidate: Dict[str, Any], existing: Iterable[Dict[str, Any]], exclude_id: int = None
    ) -> None:
        """同一吊机同一时段只能服务一船（已取消计划不占位）。"""
        for plan in existing:
            if exclude_id is not None and int(plan["id"]) == int(exclude_id):
                continue
            if plan["state"] == "cancelled":
                continue
            if self.windows_overlap(candidate, plan["payload"]):
                raise Conflict(
                    "吊机%s在%s %s-%s时已服务%s"
                    % (
                        candidate["crane_code"],
                        candidate["plan_date"],
                        candidate["start_hour"],
                        candidate["end_hour"],
                        plan["payload"].get("vessel", ""),
                    )
                )

    def require_plan_transition(self, plan: Dict[str, Any], action: str) -> str:
        new_state = PLAN_TRANSITIONS.get(action, {}).get(plan["state"])
        if new_state is None:
            raise Conflict("装船计划当前状态不允许执行%s" % action)
        return new_state

    def check_tally_gate(
        self, plan_payload: Dict[str, Any], tally_version: int, zones: Dict[str, Dict[str, Any]]
    ) -> None:
        """箱区未完成理货不能装船：理货版本必须与箱区当前数据版本一致。"""
        zone = zones.get(plan_payload["zone_code"])
        if zone is None:
            raise ValidationError("箱区%s不存在" % plan_payload["zone_code"])
        if tally_version is None:
            raise Conflict("箱区%s尚未完成理货，不能装船" % plan_payload["zone_code"])
        if int(tally_version) != int(zone["data_version"]):
            raise Conflict(
                "箱区%s理货已过期（理货版本%s，当前版本%s），请重新理货"
                % (plan_payload["zone_code"], tally_version, zone["data_version"])
            )

    def check_plan_containers(
        self, plan_payload: Dict[str, Any], containers: Dict[str, Dict[str, Any]]
    ) -> None:
        """装船清单中的箱子必须已落位且位于锁定箱区。"""
        missing: List[str] = []
        not_ready: List[str] = []
        wrong_zone: List[str] = []
        for no in plan_payload["container_nos"]:
            container = containers.get(no)
            if container is None:
                missing.append(no)
            elif container["state"] != "placed":
                not_ready.append(no)
            elif container["zone_code"] != plan_payload["zone_code"]:
                wrong_zone.append(no)
        problems: List[str] = []
        if missing:
            problems.append("箱号不存在：%s" % "、".join(missing))
        if not_ready:
            problems.append("箱子未处于已落位状态：%s" % "、".join(not_ready))
        if wrong_zone:
            problems.append("箱子不在锁定箱区%s：%s" % (plan_payload["zone_code"], "、".join(wrong_zone)))
        if problems:
            raise ValidationError("；".join(problems))
