"""堆场与装船 HTTP 路由，由主 Handler 委托；只做参数解析，规则在 yard_service。"""
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs

from .domain import Actor, ValidationError

BLOCK_RE = re.compile(r"^/api/yard/blocks/([A-Za-z0-9_-]+)$")
BLOCK_AUDIT_RE = re.compile(r"^/api/yard/blocks/([A-Za-z0-9_-]+)/audit$")
TALLY_RE = re.compile(r"^/api/yard/blocks/([A-Za-z0-9_-]+)/tally$")
CONTAINER_RE = re.compile(r"^/api/yard/containers/(\d+)$")
CONTAINER_AUDIT_RE = re.compile(r"^/api/yard/containers/(\d+)/audit$")
PLACE_RE = re.compile(r"^/api/yard/containers/(\d+)/(place|reallocate)$")
PLAN_RE = re.compile(r"^/api/yard/plans/(\d+)$")
PLAN_AUDIT_RE = re.compile(r"^/api/yard/plans/(\d+)/audit$")
PLAN_ACTION_RE = re.compile(r"^/api/yard/plans/(\d+)/(cancel|complete)$")


def _query_value(query: Dict[str, list], key: str, default: Optional[str] = None) -> Optional[str]:
    return query.get(key, [default])[0]


def _version(body: Dict[str, Any]) -> int:
    version = body.get("expected_version")
    if not isinstance(version, int):
        raise ValidationError("expected_version必须是整数")
    return version


def yard_get(service: Any, path: str, raw_query: str, actor: Actor) -> Optional[Any]:
    query = parse_qs(raw_query)
    if path == "/api/yard/blocks":
        return {"items": service.list_blocks(actor)}
    if path == "/api/yard/cranes":
        return {"items": service.list_cranes(actor)}
    if path == "/api/yard/containers":
        limit = int(_query_value(query, "limit", "200"))
        return {"items": service.list_containers(
            actor,
            state=_query_value(query, "state"),
            block_code=_query_value(query, "block"),
            limit=limit,
        )}
    if path == "/api/yard/plans":
        limit = int(_query_value(query, "limit", "200"))
        return {"items": service.list_plans(actor, state=_query_value(query, "state"), limit=limit)}
    if path == "/api/yard/stats":
        return service.yard_stats(actor)

    match = BLOCK_AUDIT_RE.match(path)
    if match:
        return {"items": service.block_timeline(actor, match.group(1))}
    match = BLOCK_RE.match(path)
    if match:
        return service.block_overview(actor, match.group(1))
    match = CONTAINER_AUDIT_RE.match(path)
    if match:
        return {"items": service.container_timeline(actor, int(match.group(1)))}
    match = CONTAINER_RE.match(path)
    if match:
        return service.get_container(actor, int(match.group(1)))
    match = PLAN_AUDIT_RE.match(path)
    if match:
        return {"items": service.plan_timeline(actor, int(match.group(1)))}
    match = PLAN_RE.match(path)
    if match:
        return service.get_plan(actor, int(match.group(1)))
    return None


def yard_post(service: Any, path: str, body: Dict[str, Any], actor: Actor) -> Optional[Tuple[int, Any]]:
    if path == "/api/yard/containers":
        return 201, service.register_container(actor, body.get("data", {}))
    if path == "/api/yard/plans":
        return 201, service.create_plan(actor, body.get("data", {}))

    match = PLACE_RE.match(path)
    if match:
        result = service.place_container(
            actor, int(match.group(1)), _version(body), body.get("data", {}),
            reallocate=match.group(2) == "reallocate",
        )
        return 200, result
    match = TALLY_RE.match(path)
    if match:
        return 200, service.set_tally(actor, match.group(1), _version(body), body.get("data", {}))
    match = PLAN_ACTION_RE.match(path)
    if match:
        plan_id = int(match.group(1))
        action = match.group(2)
        if action == "cancel":
            return 200, service.cancel_plan(actor, plan_id, _version(body), body.get("data", {}))
        return 200, service.complete_plan(actor, plan_id, _version(body), body.get("data", {}))
    return None
