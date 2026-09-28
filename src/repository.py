"""SQLite 表结构与事务访问。

泊位调度表（records/audit_events）与堆场表（yard_*）相互独立，
落位、改配、装船等涉及多行的修改均在同一事务内完成。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import yard_data
from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
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
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);

                CREATE TABLE IF NOT EXISTS yard_zones (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    max_layers INTEGER NOT NULL,
                    max_stack_weight_t REAL NOT NULL,
                    data_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS yard_stacks (
                    code TEXT PRIMARY KEY,
                    zone_code TEXT NOT NULL REFERENCES yard_zones(code),
                    seq INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS yard_cranes (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS yard_containers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    container_no TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    stack_code TEXT,
                    layer_no INTEGER,
                    pending_reason TEXT,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS loading_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS yard_tallies (
                    zone_code TEXT PRIMARY KEY REFERENCES yard_zones(code),
                    data_version INTEGER NOT NULL,
                    completed_by TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS yard_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_yard_containers_state ON yard_containers(state);
                CREATE INDEX IF NOT EXISTS idx_yard_containers_stack ON yard_containers(stack_code);
                CREATE INDEX IF NOT EXISTS idx_plans_state ON loading_plans(state);
                CREATE INDEX IF NOT EXISTS idx_yard_audit_entity ON yard_audit_events(entity_type, entity_id, id);
                """
            )
            self._seed_yard(connection)

    def _seed_yard(self, connection: sqlite3.Connection) -> None:
        """首次启动时把堆场资料（箱区/垛位/吊机）写入数据库。"""
        count = connection.execute("SELECT COUNT(*) AS total FROM yard_zones").fetchone()["total"]
        if int(count) > 0:
            return
        now = _now()
        for zone in yard_data.DEFAULT_ZONES:
            connection.execute(
                "INSERT INTO yard_zones(code,name,max_layers,max_stack_weight_t,data_version) VALUES(?,?,?,?,0)",
                (zone["code"], zone["name"], zone["max_layers"], zone["max_stack_weight_t"]),
            )
            for seq, stack_code in enumerate(zone["stacks"], start=1):
                connection.execute(
                    "INSERT INTO yard_stacks(code,zone_code,seq) VALUES(?,?,?)",
                    (stack_code, zone["code"], seq),
                )
        for crane in yard_data.DEFAULT_CRANES:
            connection.execute(
                "INSERT INTO yard_cranes(code,name) VALUES(?,?)", (crane["code"], crane["name"])
            )
        connection.execute(
            "INSERT INTO yard_audit_events(entity_type,entity_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
            ("yard", "seed", "seeded", "system", 0, json.dumps({"at": now}, ensure_ascii=False), now),
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------
    # 堆场资料与理货
    # ------------------------------------------------------------------
    @staticmethod
    def _yard_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        if "payload" in item and item["payload"]:
            item["payload"] = json.loads(item["payload"])
        return item

    def _fetch_zone(self, connection: sqlite3.Connection, code: str) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM yard_zones WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFound("箱区不存在")
        stacks = [
            str(item["code"])
            for item in connection.execute(
                "SELECT code FROM yard_stacks WHERE zone_code=? ORDER BY seq", (code,)
            ).fetchall()
        ]
        return {
            "code": row["code"],
            "name": row["name"],
            "max_layers": int(row["max_layers"]),
            "max_stack_weight_t": float(row["max_stack_weight_t"]),
            "data_version": int(row["data_version"]),
            "stack_codes": stacks,
        }

    def list_zones(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM yard_zones ORDER BY code").fetchall()
            return [self._fetch_zone(connection, row["code"]) for row in rows]

    def get_zone(self, code: str) -> Dict[str, Any]:
        with self._connect() as connection:
            return self._fetch_zone(connection, code)

    def list_cranes(self) -> List[Dict[str, str]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM yard_cranes ORDER BY code")]

    def list_tallies(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM yard_tallies ORDER BY zone_code")]

    def complete_tally(self, zone_code: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            zone = self._fetch_zone(connection, zone_code)
            version = int(zone["data_version"])
            existing = connection.execute(
                "SELECT zone_code FROM yard_tallies WHERE zone_code=?", (zone_code,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO yard_tallies(zone_code,data_version,completed_by,completed_at) VALUES(?,?,?,?)",
                    (zone_code, version, actor_id, now),
                )
            else:
                connection.execute(
                    "UPDATE yard_tallies SET data_version=?,completed_by=?,completed_at=? WHERE zone_code=?",
                    (version, actor_id, now, zone_code),
                )
            self._add_yard_audit(
                connection, "zone", zone_code, "tally_completed", actor_id, version,
                {"data_version": version}, now,
            )
            connection.commit()
        return {"zone_code": zone_code, "data_version": version, "completed_by": actor_id, "completed_at": now}

    def zone_overview(self) -> List[Dict[str, Any]]:
        """箱区平面图：每个垛位的层数、总重与已堆放类别。"""
        with self._connect() as connection:
            zones = [self._fetch_zone(connection, row["code"]) for row in
                     connection.execute("SELECT code FROM yard_zones ORDER BY code").fetchall()]
            placed = connection.execute(
                "SELECT stack_code, payload FROM yard_containers WHERE state='placed'"
            ).fetchall()
            pending_rows = connection.execute(
                "SELECT json_extract(payload,'$.zone_code') AS zone_code, COUNT(*) AS total "
                "FROM yard_containers WHERE state='pending' GROUP BY zone_code"
            ).fetchall()
        pending_counts = {str(row["zone_code"]): int(row["total"]) for row in pending_rows}
        occupancy: Dict[str, Dict[str, Any]] = {}
        for row in placed:
            payload = json.loads(row["payload"])
            view = occupancy.setdefault(
                row["stack_code"], {"layers": 0, "weight_t": 0.0, "classes": set()}
            )
            view["layers"] += 1
            view["weight_t"] = round(view["weight_t"] + float(payload["weight_t"]), 2)
            view["classes"].add(str(payload["dg_class"]))
        result = []
        for zone in zones:
            stacks = []
            placed_count = 0
            for stack_code in zone["stack_codes"]:
                view = occupancy.get(stack_code, {"layers": 0, "weight_t": 0.0, "classes": set()})
                placed_count += int(view["layers"])
                stacks.append({
                    "code": stack_code,
                    "layers": int(view["layers"]),
                    "weight_t": float(view["weight_t"]),
                    "classes": sorted(view["classes"]),
                })
            item = dict(zone)
            item["stacks"] = stacks
            item["placed_count"] = placed_count
            item["pending_count"] = pending_counts.get(zone["code"], 0)
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # 集装箱登记、落位、改配、释放
    # ------------------------------------------------------------------
    def create_container(self, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO yard_containers(container_no,state,version,payload,stack_code,layer_no,"
                    "pending_reason,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (payload["container_no"], "pending", 1,
                     json.dumps(payload, ensure_ascii=False, sort_keys=True), None, None,
                     "已登记，等待落位", actor_id, actor_id, now, now),
                )
                container_id = int(cursor.lastrowid)
                self._add_yard_audit(connection, "container", container_id, "registered",
                                     actor_id, 1, {"payload": payload}, now)
                row = connection.execute("SELECT * FROM yard_containers WHERE id=?", (container_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("箱号已存在") from exc
        return self._yard_row(row)

    def get_container(self, container_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM yard_containers WHERE id=?", (container_id,)).fetchone()
        if row is None:
            raise NotFound("集装箱不存在")
        return self._yard_row(row)

    def get_container_by_no(self, container_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM yard_containers WHERE container_no=?", (container_no,)
            ).fetchone()
        if row is None:
            raise NotFound("集装箱不存在")
        return self._yard_row(row)

    def list_containers(self, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            if state:
                rows = connection.execute(
                    "SELECT * FROM yard_containers WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM yard_containers ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._yard_row(row) for row in rows]

    def _stack_view(
        self, connection: sqlite3.Connection, zone_code: str, exclude_container_id: Optional[int] = None
    ) -> Dict[str, Dict[str, Any]]:
        query = ("SELECT stack_code, payload FROM yard_containers WHERE state='placed' "
                 "AND stack_code IN (SELECT code FROM yard_stacks WHERE zone_code=?)")
        params: List[Any] = [zone_code]
        if exclude_container_id is not None:
            query += " AND id<>?"
            params.append(exclude_container_id)
        view: Dict[str, Dict[str, Any]] = {}
        for row in connection.execute(query, params):
            payload = json.loads(row["payload"])
            stack = view.setdefault(row["stack_code"], {"layers": 0, "weight_t": 0.0, "classes": set()})
            stack["layers"] += 1
            stack["weight_t"] = round(stack["weight_t"] + float(payload["weight_t"]), 2)
            stack["classes"].add(str(payload["dg_class"]))
        return view

    def place_container(
        self, container_id: int, expected_version: int, preferred_stack: Optional[str],
        actor_id: str, rules: Any,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM yard_containers WHERE id=?", (container_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("集装箱不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            container = self._yard_row(row)
            if container["state"] != "pending":
                connection.rollback()
                raise Conflict("只有待落区的箱子可以落位")
            zone = self._fetch_zone(connection, container["payload"]["zone_code"])
            stacks = self._stack_view(connection, zone["code"], exclude_container_id=container_id)
            stack_code, reason = rules.choose_stack(
                container["payload"], zone, stacks, preferred_stack
            )
            version = int(expected_version) + 1
            if stack_code is None:
                # 放不下：留在待落区并说明原因，箱区布局未变，不提升数据版本
                connection.execute(
                    "UPDATE yard_containers SET version=?,pending_reason=?,updated_by=?,updated_at=? WHERE id=?",
                    (version, reason, actor_id, now, container_id),
                )
                self._add_yard_audit(
                    connection, "container", container_id, "place_rejected", actor_id, version,
                    {"zone_code": zone["code"], "reason": reason}, now,
                )
            else:
                layer_no = int(stacks.get(stack_code, {"layers": 0})["layers"]) + 1
                connection.execute(
                    "UPDATE yard_containers SET state='placed',version=?,stack_code=?,layer_no=?,"
                    "pending_reason=NULL,updated_by=?,updated_at=? WHERE id=?",
                    (version, stack_code, layer_no, actor_id, now, container_id),
                )
                connection.execute(
                    "UPDATE yard_zones SET data_version=data_version+1 WHERE code=?", (zone["code"],)
                )
                self._add_yard_audit(
                    connection, "container", container_id, "placed", actor_id, version,
                    {"zone_code": zone["code"], "stack_code": stack_code, "layer_no": layer_no}, now,
                )
            result = connection.execute(
                "SELECT * FROM yard_containers WHERE id=?", (container_id,)
            ).fetchone()
            connection.commit()
        return self._yard_row(result)

    def reassign_container(
        self, container_id: int, expected_version: int, new_zone_code: Optional[str],
        preferred_stack: Optional[str], actor_id: str, rules: Any,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM yard_containers WHERE id=?", (container_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("集装箱不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            container = self._yard_row(row)
            if container["state"] not in ("placed", "pending"):
                connection.rollback()
                raise Conflict("已装船的箱子不能改配")
            old_zone = container["payload"]["zone_code"]
            old_stack = container["stack_code"]
            target = self._fetch_zone(connection, new_zone_code or old_zone)
            # 改配先释放原箱位：快照排除本箱后重新选垛（待落区箱子无箱位可释放）
            stacks = self._stack_view(connection, target["code"], exclude_container_id=container_id)
            stack_code, reason = rules.choose_stack(container["payload"], target, stacks, preferred_stack)
            version = int(expected_version) + 1
            new_payload = dict(container["payload"])
            new_payload["zone_code"] = target["code"]
            if stack_code is None:
                connection.execute(
                    "UPDATE yard_containers SET state='pending',version=?,payload=?,stack_code=NULL,"
                    "layer_no=NULL,pending_reason=?,updated_by=?,updated_at=? WHERE id=?",
                    (version, json.dumps(new_payload, ensure_ascii=False, sort_keys=True), reason,
                     actor_id, now, container_id),
                )
            else:
                layer_no = int(stacks.get(stack_code, {"layers": 0})["layers"]) + 1
                connection.execute(
                    "UPDATE yard_containers SET state='placed',version=?,payload=?,stack_code=?,"
                    "layer_no=?,pending_reason=NULL,updated_by=?,updated_at=? WHERE id=?",
                    (version, json.dumps(new_payload, ensure_ascii=False, sort_keys=True), stack_code,
                     layer_no, actor_id, now, container_id),
                )
            # 原箱位释放即令原箱区数据版本失效；无原箱位或同箱区则不重复推进
            if old_stack is not None:
                connection.execute("UPDATE yard_zones SET data_version=data_version+1 WHERE code=?", (old_zone,))
                if target["code"] != old_zone:
                    connection.execute(
                        "UPDATE yard_zones SET data_version=data_version+1 WHERE code=?", (target["code"],)
                    )
            elif target["code"] != old_zone and stack_code is not None:
                connection.execute(
                    "UPDATE yard_zones SET data_version=data_version+1 WHERE code=?", (target["code"],)
                )
            self._add_yard_audit(
                connection, "container", container_id, "reassigned", actor_id, version,
                {"from_zone": old_zone, "from_stack": old_stack, "to_zone": target["code"],
                 "to_stack": stack_code, "reason": reason}, now,
            )
            result = connection.execute(
                "SELECT * FROM yard_containers WHERE id=?", (container_id,)
            ).fetchone()
            connection.commit()
        return self._yard_row(result)

    def release_container(
        self, container_id: int, expected_version: int, actor_id: str, reason: str
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM yard_containers WHERE id=?", (container_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("集装箱不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            container = self._yard_row(row)
            if container["state"] != "placed":
                connection.rollback()
                raise Conflict("只有已落位的箱子可以释放")
            zone_code = container["payload"]["zone_code"]
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE yard_containers SET state='pending',version=?,stack_code=NULL,layer_no=NULL,"
                "pending_reason=?,updated_by=?,updated_at=? WHERE id=?",
                (version, reason, actor_id, now, container_id),
            )
            connection.execute("UPDATE yard_zones SET data_version=data_version+1 WHERE code=?", (zone_code,))
            self._add_yard_audit(
                connection, "container", container_id, "released", actor_id, version,
                {"zone_code": zone_code, "stack_code": container["stack_code"], "reason": reason}, now,
            )
            result = connection.execute(
                "SELECT * FROM yard_containers WHERE id=?", (container_id,)
            ).fetchone()
            connection.commit()
        return self._yard_row(result)

    # ------------------------------------------------------------------
    # 装船计划
    # ------------------------------------------------------------------
    def _plan_rows(self, connection: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = connection.execute("SELECT * FROM loading_plans ORDER BY id").fetchall()
        return [self._yard_row(row) for row in rows]

    def _containers_by_no(
        self, connection: sqlite3.Connection, nos: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        if not nos:
            return {}
        placeholders = ",".join("?" for _ in nos)
        rows = connection.execute(
            "SELECT * FROM yard_containers WHERE container_no IN (%s)" % placeholders, nos
        ).fetchall()
        result = {}
        for row in rows:
            item = self._yard_row(row)
            result[item["container_no"]] = {
                "state": item["state"],
                "zone_code": item["payload"]["zone_code"],
            }
        return result

    def _containers_full_rows(
        self, connection: sqlite3.Connection, nos: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        if not nos:
            return {}
        placeholders = ",".join("?" for _ in nos)
        rows = connection.execute(
            "SELECT * FROM yard_containers WHERE container_no IN (%s)" % placeholders, nos
        ).fetchall()
        items = [self._yard_row(row) for row in rows]
        return {item["container_no"]: item for item in items}

    def create_plan(
        self, reference: str, payload: Dict[str, Any], actor_id: str, rules: Any
    ) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._fetch_zone(connection, payload["zone_code"])
                crane = connection.execute(
                    "SELECT code FROM yard_cranes WHERE code=?", (payload["crane_code"],)
                ).fetchone()
                if crane is None:
                    connection.rollback()
                    raise NotFound("吊机不存在")
                containers = self._containers_by_no(connection, payload["container_nos"])
                rules.check_plan_containers(payload, containers)
                rules.check_crane_conflict(payload, self._plan_rows(connection))
                cursor = connection.execute(
                    "INSERT INTO loading_plans(reference,state,version,payload,created_by,updated_by,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, "draft", 1, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     actor_id, actor_id, now, now),
                )
                plan_id = int(cursor.lastrowid)
                self._add_yard_audit(connection, "plan", plan_id, "created", actor_id, 1,
                                     {"reference": reference, "payload": payload}, now)
                row = connection.execute("SELECT * FROM loading_plans WHERE id=?", (plan_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("装船计划编号已存在") from exc
        return self._yard_row(row)

    def get_plan(self, plan_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM loading_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("装船计划不存在")
        return self._yard_row(row)

    def list_plans(self, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            if state:
                rows = connection.execute(
                    "SELECT * FROM loading_plans WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM loading_plans ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._yard_row(row) for row in rows]

    def mutate_plan(
        self, plan_id: int, expected_version: int, action: str, actor_id: str,
        rules: Any, cancel_reason: str = "",
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM loading_plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("装船计划不存在")
            plan = self._yard_row(row)
            if int(plan["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            new_state = rules.require_plan_transition(plan, action)
            payload = dict(plan["payload"])
            if action == "confirm":
                containers = self._containers_by_no(connection, payload["container_nos"])
                rules.check_plan_containers(payload, containers)
                rules.check_crane_conflict(payload, self._plan_rows(connection), exclude_id=plan_id)
                zones = {
                    item["code"]: {"data_version": item["data_version"]}
                    for item in [self._fetch_zone(connection, payload["zone_code"])]
                }
                tally = connection.execute(
                    "SELECT data_version FROM yard_tallies WHERE zone_code=?",
                    (payload["zone_code"],),
                ).fetchone()
                tally_version = int(tally["data_version"]) if tally else None
                rules.check_tally_gate(payload, tally_version, zones)
                payload["confirmed_at"] = now
            elif action == "execute":
                full_rows = self._containers_full_rows(connection, payload["container_nos"])
                slim = {no: {"state": row["state"], "zone_code": row["payload"]["zone_code"]}
                        for no, row in full_rows.items()}
                rules.check_plan_containers(payload, slim)
                zone = self._fetch_zone(connection, payload["zone_code"])
                zones = {zone["code"]: {"data_version": zone["data_version"]}}
                tally = connection.execute(
                    "SELECT data_version FROM yard_tallies WHERE zone_code=?",
                    (payload["zone_code"],),
                ).fetchone()
                tally_version = int(tally["data_version"]) if tally else None
                # 锁定后箱位若有变动（改配/释放），旧理货失效，必须重新理货
                rules.check_tally_gate(payload, tally_version, zones)
                zone_code = payload["zone_code"]
                for no in payload["container_nos"]:
                    container = full_rows[no]
                    next_version = int(container["version"]) + 1
                    connection.execute(
                        "UPDATE yard_containers SET state='loaded',version=?,stack_code=NULL,"
                        "layer_no=NULL,pending_reason=NULL,updated_by=?,updated_at=? WHERE id=?",
                        (next_version, actor_id, now, container["id"]),
                    )
                    self._add_yard_audit(
                        connection, "container", container["id"], "loaded", actor_id, next_version,
                        {"plan_id": plan_id, "vessel": payload["vessel"], "zone_code": zone_code}, now,
                    )
                # 装船后垛位腾空，箱区数据版本前进
                connection.execute(
                    "UPDATE yard_zones SET data_version=data_version+1 WHERE code=?", (zone_code,)
                )
                payload["executed_at"] = now
            elif action == "cancel":
                payload["cancel_reason"] = cancel_reason
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE loading_plans SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (new_state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 actor_id, now, plan_id),
            )
            self._add_yard_audit(
                connection, "plan", plan_id, action, actor_id, version,
                {"from": plan["state"], "to": new_state, "summary": self._plan_summary(action, new_state)},
                now,
            )
            result = connection.execute("SELECT * FROM loading_plans WHERE id=?", (plan_id,)).fetchone()
            connection.commit()
        return self._yard_row(result)

    @staticmethod
    def _plan_summary(action: str, new_state: str) -> str:
        return {
            "confirm": "装船计划已锁定箱区、吊机与时段",
            "execute": "装船完成，原箱位已释放",
            "cancel": "装船计划已取消",
        }.get(action, "状态变更为%s" % new_state)

    # ------------------------------------------------------------------
    # 堆场审计
    # ------------------------------------------------------------------
    @staticmethod
    def _add_yard_audit(
        connection: sqlite3.Connection, entity_type: str, entity_id: Any, action: str,
        actor_id: str, version: int, details: Dict[str, Any], now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO yard_audit_events(entity_type,entity_id,action,actor_id,version,details,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (entity_type, str(entity_id), action, actor_id, version,
             json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )

    def add_yard_audit(
        self, entity_type: str, entity_id: Any, actor_id: str, action: str, details: Dict[str, Any]
    ) -> None:
        with self._connect() as connection:
            self._add_yard_audit(connection, entity_type, entity_id, action, actor_id, 0, details, _now())

    def yard_timeline(self, entity_type: str, entity_id: Any) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM yard_audit_events WHERE entity_type=? AND entity_id=? ORDER BY id",
                (entity_type, str(entity_id)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result
