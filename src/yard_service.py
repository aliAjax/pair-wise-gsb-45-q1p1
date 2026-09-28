"""堆场与装船用例编排：身份、角色权限与规则/事务的组合。"""
from typing import Any, Dict, List, Optional

from .domain import Actor, Conflict, PermissionDenied, text
from .yard_repository import ENTITY_BLOCK, ENTITY_CONTAINER, ENTITY_PLAN, YardRepository
from .yard_rules import (
    PLAN_CANCELLED,
    PLAN_DONE,
    PLAN_ROLES,
    PLACE_ROLES,
    REGISTER_ROLES,
    REASON_BLOCK_LOCKED,
    TALLY_ROLES,
    YardRules,
)


class YardService:
    def __init__(self, repository: YardRepository, rules: YardRules = None) -> None:
        self.repository = repository
        self.rules = rules or YardRules()

    def _actor(self, actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")
        return actor

    def _require_role(self, actor: Actor, roles: set) -> None:
        if actor.role != "admin" and actor.role not in roles:
            raise PermissionDenied("角色无权执行该操作")

    # ---------- 主数据 ----------
    def list_blocks(self, actor: Optional[Actor] = None) -> List[Dict[str, Any]]:
        if actor is not None:
            self._actor(actor)
        return self.repository.list_blocks()

    def block_overview(self, actor: Actor, block_code: str) -> Dict[str, Any]:
        self._actor(actor)
        block = self.repository.get_block(block_code)
        block["stacks"] = self.repository.stack_snapshot(block_code)
        active = [p for p in self.repository.active_plans() if p["block_code"] == block_code]
        block["active_plan_id"] = active[0]["id"] if active else None
        return block

    def list_cranes(self, actor: Optional[Actor] = None) -> List[Dict[str, Any]]:
        if actor is not None:
            self._actor(actor)
        return self.repository.list_cranes()

    def set_tally(self, actor: Actor, block_code: str, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, TALLY_ROLES)
        text({"block_code": block_code}, "block_code")
        completed = self.rules.validate_tally(data or {})
        if completed:
            active = [p for p in self.repository.active_plans() if p["block_code"] == block_code]
            if active:
                raise Conflict("箱区已被装船计划锁定，不能重新理货")
        return self.repository.set_tally(block_code, int(expected_version), completed, actor.user_id)

    def block_timeline(self, actor: Actor, block_code: str) -> List[Dict[str, Any]]:
        self._actor(actor)
        self.repository.get_block(block_code)
        return self.repository.event_timeline(ENTITY_BLOCK, entity_ref=block_code)

    # ---------- 登记 ----------
    def register_container(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, REGISTER_ROLES)
        prepared = self.rules.validate_container(payload or {})
        preferred = (payload or {}).get("preferred_block_code")
        if preferred:
            block = self.repository.get_block(str(preferred))
            prepared["preferred_block_code"] = block["code"]
        return self.repository.create_container(prepared, actor.user_id)

    def list_containers(self, actor: Actor, state: Optional[str] = None, block_code: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.list_containers(state=state, block_code=block_code, limit=limit)

    def get_container(self, actor: Actor, container_id: int) -> Dict[str, Any]:
        self._actor(actor)
        return self.repository.get_container(container_id)

    def container_timeline(self, actor: Actor, container_id: int) -> List[Dict[str, Any]]:
        self._actor(actor)
        self.repository.get_container(container_id)
        return self.repository.event_timeline(ENTITY_CONTAINER, container_id)

    # ---------- 落位 ----------
    def place_container(self, actor: Actor, container_id: int, expected_version: int, data: Dict[str, Any], reallocate: bool = False) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, PLACE_ROLES)
        block_code = text(data or {}, "block_code")
        container = self.repository.get_container(container_id)
        self.rules.require_placement_state(container, allow_placed=reallocate)

        block = self.repository.get_block(block_code)
        self.rules.validate_block_capacity(container, block)
        active = [p for p in self.repository.active_plans() if p["block_code"] == block_code]
        if active:
            return self._hold(container_id, expected_version, block_code, REASON_BLOCK_LOCKED,
                              "箱区已被装船计划%s锁定" % active[0]["id"], actor.user_id)

        snapshot = self.repository.stack_snapshot(block_code, exclude_container_id=container_id)
        decision = self.rules.choose_slot(container, block, snapshot)
        if not decision.ok:
            return self._hold(container_id, expected_version, block_code, decision.reason, decision.detail, actor.user_id)

        return self.repository.commit_slot(
            container_id=container_id,
            expected_version=int(expected_version),
            block_code=block_code,
            stack_no=decision.stack_no,
            layer_no=decision.layer_no,
            reallocate=reallocate,
            actor_id=actor.user_id,
            redecide=self.rules.choose_slot,
        )

    def _hold(self, container_id: int, expected_version: int, block_code: str, reason: str, detail: str, actor_id: str) -> Dict[str, Any]:
        """放不下：留在待落区并写清原因。"""
        return self.repository.mark_pending(container_id, int(expected_version), block_code, reason, detail, actor_id=actor_id)

    # ---------- 装船计划 ----------
    def create_plan(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, PLAN_ROLES)
        plan = self.rules.validate_plan(payload or {})
        # 箱区必须存在
        self.repository.get_block(plan["block_code"])

        def validate(others: List[Dict[str, Any]], block: Dict[str, Any]) -> None:
            self.rules.require_block_tallied(block)
            self.rules.check_crane_conflict(plan, others)
            self.rules.check_block_conflict(plan, others)

        return self.repository.create_plan(plan, actor.user_id, validate)

    def list_plans(self, actor: Actor, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.list_plans(state=state, limit=limit)

    def get_plan(self, actor: Actor, plan_id: int) -> Dict[str, Any]:
        self._actor(actor)
        return self.repository.get_plan(plan_id)

    def plan_timeline(self, actor: Actor, plan_id: int) -> List[Dict[str, Any]]:
        self._actor(actor)
        self.repository.get_plan(plan_id)
        return self.repository.event_timeline(ENTITY_PLAN, plan_id)

    def cancel_plan(self, actor: Actor, plan_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, PLAN_ROLES)
        reason = text(data or {}, "reason")
        return self.repository.update_plan_state(
            plan_id=plan_id,
            expected_version=int(expected_version),
            new_state=PLAN_CANCELLED,
            actor_id=actor.user_id,
            action="released",
            details={"reason": reason},
        )

    def complete_plan(self, actor: Actor, plan_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, PLAN_ROLES)

        def on_open(connection: Any, plan: Dict[str, Any], block: Dict[str, Any]) -> Dict[str, Any]:
            # 执行瞬间再次确认理货完成
            self.rules.require_block_tallied(block)
            loaded = self.repository.mark_loaded(connection, plan["block_code"], plan_id, actor.user_id)
            if not loaded:
                raise Conflict("箱区内没有已落位待装的箱子")
            self.repository.reset_tally(connection, plan["block_code"], actor.user_id, plan_id)
            return {"loaded_container_ids": loaded, "summary": "装船完成，%s个箱已装船，箱区锁定与理货已复位" % len(loaded)}

        return self.repository.update_plan_state(
            plan_id=plan_id,
            expected_version=int(expected_version),
            new_state=PLAN_DONE,
            actor_id=actor.user_id,
            action="loaded",
            details={},
            on_open=on_open,
        )

    def yard_stats(self, actor: Actor) -> Dict[str, Any]:
        self._actor(actor)
        return self.repository.yard_stats()
