"""堆场与装船 SQLite 表结构与事务访问。

写操作在 BEGIN IMMEDIATE 锁内重读垛位快照并由 rules 回调复核，
保证两个班次并发落位时不会把不相容箱或超重箱塞进同一垛。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound
from .yard_data import BLOCK_CATALOG, CRANE_CATALOG


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


ENTITY_CONTAINER = "container"
ENTITY_PLAN = "plan"
ENTITY_BLOCK = "block"


class YardRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS yard_blocks (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    max_stack_weight_t REAL NOT NULL,
                    max_layers INTEGER NOT NULL,
                    stack_count INTEGER NOT NULL,
                    tally_completed INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS yard_cranes (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS yard_containers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    container_no TEXT NOT NULL UNIQUE,
                    hazard_class TEXT NOT NULL,
                    weight_t REAL NOT NULL,
                    arrival_hour INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    block_code TEXT REFERENCES yard_blocks(code),
                    stack_no INTEGER,
                    layer_no INTEGER,
                    target_block_code TEXT,
                    pending_reason TEXT,
                    pending_detail TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS loading_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vessel TEXT NOT NULL,
                    block_code TEXT NOT NULL REFERENCES yard_blocks(code),
                    crane_code TEXT NOT NULL,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS yard_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    entity_ref TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_yard_containers_state ON yard_containers(state);
                CREATE INDEX IF NOT EXISTS idx_yard_containers_slot ON yard_containers(block_code, stack_no, layer_no);
                CREATE INDEX IF NOT EXISTS idx_loading_plans_state ON loading_plans(state);
                CREATE INDEX IF NOT EXISTS idx_yard_events_entity ON yard_events(entity_type, entity_id, id);
                CREATE INDEX IF NOT EXISTS idx_yard_events_ref ON yard_events(entity_type, entity_ref, id);
                """
            )
            self._seed(connection)

    def _seed(self, connection: sqlite3.Connection) -> None:
        now = _now()
        for block in BLOCK_CATALOG:
            connection.execute(
                "INSERT OR IGNORE INTO yard_blocks(code,name,max_stack_weight_t,max_layers,stack_count,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (block["code"], block["name"], block["max_stack_weight_t"], block["max_layers"], block["stack_count"], now),
            )
        for crane in CRANE_CATALOG:
            connection.execute("INSERT OR IGNORE INTO yard_cranes(code,name) VALUES(?,?)", (crane["code"], crane["name"]))

    # ---------- 事件 ----------
    def _event(
        self,
        connection: sqlite3.Connection,
        entity_type: str,
        entity_id: int,
        action: str,
        actor_id: str,
        version: int,
        details: Dict[str, Any],
        entity_ref: str = "",
    ) -> None:
        connection.execute(
            "INSERT INTO yard_events(entity_type,entity_id,entity_ref,action,actor_id,version,details,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (entity_type, entity_id, entity_ref, action, actor_id, version,
             json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
        )

    def event_timeline(self, entity_type: str, entity_id: int = 0, entity_ref: str = "") -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if entity_ref:
                rows = connection.execute(
                    "SELECT * FROM yard_events WHERE entity_type=? AND entity_ref=? ORDER BY id", (entity_type, entity_ref)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM yard_events WHERE entity_type=? AND entity_id=? ORDER BY id", (entity_type, entity_id)
                ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    # ---------- 箱区与吊机 ----------
    def list_blocks(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM yard_blocks ORDER BY code").fetchall()
        return [self._block(row) for row in rows]

    def get_block(self, code: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM yard_blocks WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFound("箱区不存在")
        return self._block(row)

    @staticmethod
    def _block(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["tally_completed"] = bool(item["tally_completed"])
        return item

    def list_cranes(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM yard_cranes ORDER BY code").fetchall()
        return [dict(row) for row in rows]

    def set_tally(self, block_code: str, expected_version: int, completed: bool, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM yard_blocks WHERE code=?", (block_code,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("箱区不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE yard_blocks SET tally_completed=?,version=?,updated_at=? WHERE code=?",
                (1 if completed else 0, version, now, block_code),
            )
            self._event(connection, ENTITY_BLOCK, 0, "tally" if completed else "tally_reset", actor_id, version,
                        {"tally_completed": completed}, entity_ref=block_code)
            result = connection.execute("SELECT * FROM yard_blocks WHERE code=?", (block_code,)).fetchone()
            connection.commit()
        return self._block(result)

    # ---------- 垛位快照 ----------
    def stack_snapshot(self, block_code: str, exclude_container_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = (
            "SELECT stack_no, COUNT(*) AS box_count, MAX(layer_no) AS top_layer,"
            " SUM(weight_t) AS total_weight_t, GROUP_CONCAT(hazard_class) AS classes"
            " FROM yard_containers WHERE state='placed' AND block_code=?"
        )
        params: List[Any] = [block_code]
        if exclude_container_id is not None:
            sql += " AND id<>?"
            params.append(exclude_container_id)
        sql += " GROUP BY stack_no ORDER BY stack_no"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        stacks = []
        for row in rows:
            classes = (row["classes"] or "").split(",") if row["classes"] else []
            stacks.append({
                "stack_no": int(row["stack_no"]),
                "box_count": int(row["box_count"]),
                "top_layer": int(row["top_layer"]),
                "total_weight_t": round(float(row["total_weight_t"]), 2),
                "classes": classes,
            })
        return stacks

    # ---------- 箱子 ----------
    @staticmethod
    def _container(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("stack_no", "layer_no"):
            item[key] = int(item[key]) if item[key] is not None else None
        item["weight_t"] = float(item["weight_t"])
        item["arrival_hour"] = int(item["arrival_hour"])
        return item

    def create_container(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO yard_containers(container_no,hazard_class,weight_t,arrival_hour,state,"
                    "block_code,stack_no,layer_no,target_block_code,pending_reason,pending_detail,"
                    "created_by,updated_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (data["container_no"], data["hazard_class"], data["weight_t"], data["arrival_hour"], "pending",
                     None, None, None, data.get("preferred_block_code"), None, None,
                     actor_id, actor_id, now, now),
                )
                container_id = int(cursor.lastrowid)
                self._event(connection, ENTITY_CONTAINER, container_id, "registered", actor_id, 1, {
                    "container_no": data["container_no"], "hazard_class": data["hazard_class"],
                    "weight_t": data["weight_t"], "arrival_hour": data["arrival_hour"],
                })
                row = connection.execute("SELECT * FROM yard_containers WHERE id=?", (container_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("箱号已存在") from exc
        return self._container(row)

    def get_container(self, container_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM yard_containers WHERE id=?", (container_id,)).fetchone()
        if row is None:
            raise NotFound("箱子不存在")
        return self._container(row)

    def list_containers(self, state: Optional[str] = None, block_code: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        sql = "SELECT * FROM yard_containers WHERE 1=1"
        params: List[Any] = []
        if state:
            sql += " AND state=?"
            params.append(state)
        if block_code:
            sql += " AND block_code=?"
            params.append(block_code)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._container(row) for row in rows]

    def mark_pending(self, container_id: int, expected_version: int, target_block_code: str, reason: str, detail: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM yard_containers WHERE id=?", (container_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("箱子不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE yard_containers SET state='pending',block_code=NULL,stack_no=NULL,layer_no=NULL,"
                "target_block_code=?,pending_reason=?,pending_detail=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                (target_block_code, reason, detail, version, actor_id, now, container_id),
            )
            self._event(connection, ENTITY_CONTAINER, container_id, "held_in_pending", actor_id, version, {
                "target_block_code": target_block_code, "reason": reason, "detail": detail,
            })
            result = connection.execute("SELECT * FROM yard_containers WHERE id=?", (container_id,)).fetchone()
            connection.commit()
        return self._container(result)

    def commit_slot(
        self,
        container_id: int,
        expected_version: int,
        block_code: str,
        stack_no: int,
        layer_no: int,
        reallocate: bool,
        actor_id: str,
        redecide: Callable[[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]], Any],
    ) -> Dict[str, Any]:
        """在写锁内重读快照，回调 rules 复核垛位，再提交落位/改配。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM yard_containers WHERE id=?", (container_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("箱子不存在")
            current = self._container(row)
            if int(current["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            allowed = ("pending", "placed") if reallocate else ("pending",)
            if current["state"] not in allowed:
                connection.rollback()
                raise Conflict("当前状态不允许落位")
            block_row = connection.execute("SELECT * FROM yard_blocks WHERE code=?", (block_code,)).fetchone()
            if block_row is None:
                connection.rollback()
                raise NotFound("箱区不存在")
            block = self._block(block_row)

            stacks = self.stack_snapshot(block_code, exclude_container_id=container_id)
            decision = redecide(current, block, stacks)
            if not decision.ok or decision.stack_no != stack_no or decision.layer_no != layer_no:
                connection.rollback()
                raise Conflict("垛位刚被占用或箱区情况已变化，请重新落位")

            old_slot = None
            if current["state"] == "placed":
                old_slot = {"block_code": current["block_code"], "stack_no": current["stack_no"], "layer_no": current["layer_no"]}
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE yard_containers SET state='placed',block_code=?,stack_no=?,layer_no=?,"
                "target_block_code=NULL,pending_reason=NULL,pending_detail=NULL,version=?,updated_by=?,updated_at=? WHERE id=?",
                (block_code, stack_no, layer_no, version, actor_id, now, container_id),
            )
            self._event(connection, ENTITY_CONTAINER, container_id, "relocated" if reallocate and old_slot else "placed", actor_id, version, {
                "block_code": block_code, "stack_no": stack_no, "layer_no": layer_no, "released": old_slot,
            })
            result = connection.execute("SELECT * FROM yard_containers WHERE id=?", (container_id,)).fetchone()
            connection.commit()
        return self._container(result)

    # ---------- 装船计划 ----------
    @staticmethod
    def _plan(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["start_hour"] = int(item["start_hour"])
        item["end_hour"] = int(item["end_hour"])
        return item

    def list_plans(self, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM loading_plans WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM loading_plans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._plan(row) for row in rows]

    def get_plan(self, plan_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM loading_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("装船计划不存在")
        return self._plan(row)

    def active_plans(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM loading_plans WHERE state='open' ORDER BY id").fetchall()
        return [self._plan(row) for row in rows]

    def create_plan(self, plan: Dict[str, Any], actor_id: str, validate: Callable[[List[Dict[str, Any]], Dict[str, Any]], None]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            block_row = connection.execute("SELECT * FROM yard_blocks WHERE code=?", (plan["block_code"],)).fetchone()
            if block_row is None:
                connection.rollback()
                raise NotFound("箱区不存在")
            others = [self._plan(row) for row in connection.execute("SELECT * FROM loading_plans WHERE state='open'").fetchall()]
            validate(others, self._block(block_row))
            cursor = connection.execute(
                "INSERT INTO loading_plans(vessel,block_code,crane_code,start_hour,end_hour,state,"
                "created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (plan["vessel"], plan["block_code"], plan["crane_code"], plan["start_hour"], plan["end_hour"],
                 "open", actor_id, actor_id, now, now),
            )
            plan_id = int(cursor.lastrowid)
            self._event(connection, ENTITY_PLAN, plan_id, "locked", actor_id, 1, {
                "vessel": plan["vessel"], "block_code": plan["block_code"], "crane_code": plan["crane_code"],
                "start_hour": plan["start_hour"], "end_hour": plan["end_hour"],
            })
            row = connection.execute("SELECT * FROM loading_plans WHERE id=?", (plan_id,)).fetchone()
            connection.commit()
        return self._plan(row)

    def update_plan_state(
        self,
        plan_id: int,
        expected_version: int,
        new_state: str,
        actor_id: str,
        action: str,
        details: Dict[str, Any],
        on_open: Optional[Callable[[sqlite3.Connection, Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM loading_plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("装船计划不存在")
            plan = self._plan(row)
            if int(plan["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if plan["state"] != "open":
                connection.rollback()
                raise Conflict("只有未执行的装船计划可以%s" % action)
            version = int(expected_version) + 1
            block_row = connection.execute("SELECT * FROM yard_blocks WHERE code=?", (plan["block_code"],)).fetchone()
            extra: Dict[str, Any] = {}
            if on_open is not None:
                returned = on_open(connection, plan, self._block(block_row))
                if returned:
                    extra.update(returned)
            connection.execute(
                "UPDATE loading_plans SET state=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                (new_state, version, actor_id, now, plan_id),
            )
            event_details = dict(details)
            event_details.update(extra)
            self._event(connection, ENTITY_PLAN, plan_id, action, actor_id, version, event_details)
            result = connection.execute("SELECT * FROM loading_plans WHERE id=?", (plan_id,)).fetchone()
            connection.commit()
        result = self._plan(result)
        if extra:
            result.update(extra)
        return result

    def mark_loaded(self, connection: sqlite3.Connection, block_code: str, plan_id: int, actor_id: str) -> List[int]:
        now = _now()
        rows = connection.execute(
            "SELECT * FROM yard_containers WHERE state='placed' AND block_code=? ORDER BY stack_no,layer_no", (block_code,)
        ).fetchall()
        ids = []
        for row in rows:
            cid = int(row["id"])
            version = int(row["version"]) + 1
            connection.execute(
                "UPDATE yard_containers SET state='loaded',version=?,updated_by=?,updated_at=? WHERE id=?",
                (version, actor_id, now, cid),
            )
            self._event(connection, ENTITY_CONTAINER, cid, "loaded", actor_id, version, {"plan_id": plan_id})
            ids.append(cid)
        return ids

    def reset_tally(self, connection: sqlite3.Connection, block_code: str, actor_id: str = "", plan_id: int = 0) -> int:
        row = connection.execute("SELECT version FROM yard_blocks WHERE code=?", (block_code,)).fetchone()
        version = int(row["version"]) + 1
        connection.execute(
            "UPDATE yard_blocks SET tally_completed=0,version=?,updated_at=? WHERE code=?",
            (version, _now(), block_code),
        )
        self._event(connection, ENTITY_BLOCK, 0, "tally_reset", actor_id or "system", version,
                    {"reason": "plan_loaded", "plan_id": plan_id}, entity_ref=block_code)
        return version

    def yard_stats(self) -> Dict[str, Any]:
        with self._connect() as connection:
            crows = connection.execute("SELECT state,COUNT(*) AS total FROM yard_containers GROUP BY state").fetchall()
            prows = connection.execute("SELECT state,COUNT(*) AS total FROM loading_plans GROUP BY state").fetchall()
            brows = connection.execute("SELECT COUNT(*) AS total FROM yard_blocks WHERE tally_completed=1").fetchone()
        return {
            "containers": {str(r["state"]): int(r["total"]) for r in crows},
            "plans": {str(r["state"]): int(r["total"]) for r in prows},
            "blocks_tallied": int(brows["total"]),
        }
