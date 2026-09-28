"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")

CONTAINER_RE = re.compile(r"^/api/containers/(\d+)$")
CONTAINER_AUDIT_RE = re.compile(r"^/api/containers/(\d+)/audit$")
CONTAINER_ACTION_RE = re.compile(r"^/api/containers/(\d+)/actions/([a-z_]+)$")
PLAN_RE = re.compile(r"^/api/plans/(\d+)$")
PLAN_AUDIT_RE = re.compile(r"^/api/plans/(\d+)/audit$")
PLAN_ACTION_RE = re.compile(r"^/api/plans/(\d+)/actions/([a-z_]+)$")
ZONE_AUDIT_RE = re.compile(r"^/api/zones/([A-Za-z0-9_-]+)/audit$")


def make_handler(service: Any, yard_service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "port-yard/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "port-yard", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/yard":
                    page = (static_dir / "yard.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/records":
                    query = parse_qs(parsed.query)
                    records = service.list_records(self._actor(), state=query.get("state", [None])[0], limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": records})
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                # ---------------- 堆场与装船 ----------------
                if parsed.path == "/api/yard/reference":
                    self._send(200, yard_service.reference_data(self._actor()))
                    return
                if parsed.path == "/api/zones":
                    self._send(200, {"items": yard_service.zones(self._actor())})
                    return
                match = ZONE_AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": yard_service.zone_timeline(self._actor(), match.group(1))})
                    return
                if parsed.path == "/api/containers":
                    query = parse_qs(parsed.query)
                    items = yard_service.list_containers(
                        self._actor(), state=query.get("state", [None])[0],
                        limit=int(query.get("limit", ["200"])[0]),
                    )
                    self._send(200, {"items": items})
                    return
                match = CONTAINER_AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": yard_service.container_timeline(self._actor(), int(match.group(1)))})
                    return
                match = CONTAINER_RE.match(parsed.path)
                if match:
                    self._send(200, yard_service.get_container(self._actor(), int(match.group(1))))
                    return
                if parsed.path == "/api/plans":
                    query = parse_qs(parsed.query)
                    items = yard_service.list_plans(
                        self._actor(), state=query.get("state", [None])[0],
                        limit=int(query.get("limit", ["200"])[0]),
                    )
                    self._send(200, {"items": items})
                    return
                match = PLAN_AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": yard_service.plan_timeline(self._actor(), int(match.group(1)))})
                    return
                match = PLAN_RE.match(parsed.path)
                if match:
                    self._send(200, yard_service.get_plan(self._actor(), int(match.group(1))))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                # ---------------- 堆场与装船 ----------------
                if parsed.path == "/api/containers":
                    record = yard_service.register_container(self._actor(), body.get("data", {}))
                    self._send(201, record)
                    return
                if parsed.path == "/api/plans":
                    record = yard_service.create_plan(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                if parsed.path == "/api/tallies":
                    data = body.get("data", {})
                    zone_code = data.get("zone_code", "")
                    record = yard_service.complete_tally(self._actor(), zone_code)
                    self._send(200, record)
                    return
                match = CONTAINER_ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    data = body.get("data", {}) or {}
                    action = match.group(2)
                    actor = self._actor()
                    container_id = int(match.group(1))
                    if action == "place":
                        record = yard_service.place_container(
                            actor, container_id, version, data.get("preferred_stack")
                        )
                    elif action == "reassign":
                        record = yard_service.reassign_container(
                            actor, container_id, version, data.get("new_zone_code"),
                            data.get("preferred_stack"),
                        )
                    elif action == "release":
                        record = yard_service.release_container(
                            actor, container_id, version, str(data.get("reason", ""))
                        )
                    else:
                        self._send(404, {"error": "not_found", "message": "动作不存在"})
                        return
                    self._send(200, record)
                    return
                match = PLAN_ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = yard_service.act_plan(
                        self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {})
                    )
                    self._send(200, record)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, yard_service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, yard_service, static_dir))
