"""堆场与装船计划用例编排：权限、校验、事务调用。

本层不直接写SQL，所有多步操作通过repository在单事务内完成。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, text
from .repository import Repository
from .yard_rules import YardRules
from . import yard_data


class YardService:
    def __init__(self, repository: Repository, rules: YardRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问堆场服务")

    def _ensure_read(self, actor: Actor) -> None:
        self._ensure_known_role(actor)
        if not self.rules.can_read(actor.role):
            raise PermissionDenied("角色无权查看堆场资料")

    # ---------- 堆场资料 ----------
    def reference_data(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_read(actor)
        return {
            "dg_classes": [
                {"code": code, "name": yard_data.DG_CLASS_NAMES[code]} for code in yard_data.DG_CLASSES
            ],
            "shifts": [
                {"code": code, "name": item[0], "start_hour": item[1], "end_hour": item[2]}
                for code, item in yard_data.SHIFTS.items()
            ],
            "zones": self.repository.list_zones(),
            "cranes": self.repository.list_cranes(),
            "tallies": self.repository.list_tallies(),
        }

    def zones(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_read(actor)
        return self.repository.zone_overview()

    def zone(self, actor: Actor, zone_code: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_read(actor)
        zone = self.repository.get_zone(zone_code)
        overview = {item["code"]: item for item in self.repository.zone_overview()}
        return overview.get(zone_code, zone)

    # ---------- 集装箱 ----------
    def register_container(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.can_register_container(actor.role):
            raise PermissionDenied("角色无权登记危险品箱")
        prepared = self.rules.validate_container(payload or {})
        self.repository.get_zone(prepared["zone_code"])  # 箱区不存在抛NotFound
        return self.repository.create_container(prepared, actor.user_id)

    def list_containers(self, actor: Actor, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_read(actor)
        return self.repository.list_containers(state=state, limit=limit)

    def get_container(self, actor: Actor, container_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_read(actor)
        return self.repository.get_container(container_id)

    def place_container(
        self, actor: Actor, container_id: int, expected_version: int, preferred_stack: Optional[str]
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.can_place(actor.role):
            raise PermissionDenied("角色无权执行落位")
        stack = text({"preferred_stack": preferred_stack}, "preferred_stack") if preferred_stack else None
        return self.repository.place_container(
            container_id, int(expected_version), stack, actor.user_id, self.rules
        )

    def reassign_container(
        self, actor: Actor, container_id: int, expected_version: int,
        new_zone_code: Optional[str], preferred_stack: Optional[str],
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.can_place(actor.role):
            raise PermissionDenied("角色无权执行改配")
        zone_code = new_zone_code.strip() if isinstance(new_zone_code, str) and new_zone_code.strip() else None
        stack = text({"preferred_stack": preferred_stack}, "preferred_stack") if preferred_stack else None
        return self.repository.reassign_container(
            container_id, int(expected_version), zone_code, stack, actor.user_id, self.rules
        )

    def release_container(
        self, actor: Actor, container_id: int, expected_version: int, reason: str
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.can_place(actor.role):
            raise PermissionDenied("角色无权释放箱位")
        reason = text({"reason": reason}, "reason")
        return self.repository.release_container(
            container_id, int(expected_version), actor.user_id, reason
        )

    # ---------- 理货 ----------
    def complete_tally(self, actor: Actor, zone_code: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.can_tally(actor.role):
            raise PermissionDenied("角色无权确认理货")
        zone_code = text({"zone_code": zone_code}, "zone_code")
        return self.repository.complete_tally(zone_code, actor.user_id)

    # ---------- 装船计划 ----------
    def create_plan(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.can_plan(actor.role):
            raise PermissionDenied("角色无权编制装船计划")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.validate_plan(payload or {})
        return self.repository.create_plan(reference, prepared, actor.user_id, self.rules)

    def list_plans(self, actor: Actor, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_read(actor)
        return self.repository.list_plans(state=state, limit=limit)

    def get_plan(self, actor: Actor, plan_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_read(actor)
        return self.repository.get_plan(plan_id)

    def act_plan(
        self, actor: Actor, plan_id: int, expected_version: int, action: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.can_plan(actor.role):
            raise PermissionDenied("角色无权操作装船计划")
        action = text({"action": action}, "action")
        cancel_reason = ""
        if action == "cancel":
            cancel_reason = text(data or {}, "cancel_reason")
        return self.repository.mutate_plan(
            plan_id, int(expected_version), action, actor.user_id, self.rules, cancel_reason
        )

    # ---------- 审计 ----------
    def container_timeline(self, actor: Actor, container_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_read(actor)
        self.repository.get_container(container_id)
        return self.audit.timeline("container", container_id)

    def plan_timeline(self, actor: Actor, plan_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_read(actor)
        self.repository.get_plan(plan_id)
        return self.audit.timeline("plan", plan_id)

    def zone_timeline(self, actor: Actor, zone_code: str) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_read(actor)
        self.repository.get_zone(zone_code)
        return self.audit.timeline("zone", zone_code)
