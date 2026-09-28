"""审计事件封装，时间线查询保持只读。"""
from typing import Any, Dict, List

from .domain import NotFound


class AuditRecorder:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def timeline(self, entity_type: str, entity_id: Any) -> List[Dict[str, Any]]:
        if entity_type == "record":
            return self.repository.audit_timeline(entity_id)
        return self.repository.yard_timeline(entity_type, entity_id)

    def note(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        self.repository.add_audit(record_id, actor_id, action, details)
