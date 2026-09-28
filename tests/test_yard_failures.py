import tempfile
import unittest
from pathlib import Path

from app import build_yard_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError
from src.repository import Repository
from src.audit import AuditRecorder
from src.yard_rules import YardRules
from src.yard_service import YardService

from tests.test_yard_workflow import box_payload, plan_payload, PLANNER, WORKER, TALLY, VPLANNER


class YardFailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = str(Path(self.temp.name) / "test.db")
        self.repository = Repository(db)
        self.service = YardService(self.repository, YardRules(), AuditRecorder(self.repository))

    def tearDown(self):
        self.temp.cleanup()

    def _ready_planned_container(self, no="ABCD0000001"):
        c = self.service.register_container(PLANNER, box_payload(no))
        c = self.service.place_container(WORKER, c["id"], c["version"], None)
        self.service.complete_tally(TALLY, "B")
        return c

    def test_duplicate_container_and_bad_zone(self):
        self.service.register_container(PLANNER, box_payload("ABCD0000001"))
        with self.assertRaises(Conflict):
            self.service.register_container(PLANNER, box_payload("ABCD0000001"))
        with self.assertRaises(NotFound):
            self.service.register_container(PLANNER, box_payload("ABCD0000002", zone="Z"))

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_container(Actor("x", "yard_worker"), box_payload("ABCD0000001"))
        with self.assertRaises(PermissionDenied):
            self.service.complete_tally(Actor("x", "yard_planner"), "B")
        with self.assertRaises(PermissionDenied):
            self.service.create_plan(Actor("x", "yard_planner"), "P1", plan_payload(["X"]))
        with self.assertRaises(PermissionDenied):
            self.service.list_containers(Actor("x", "outsider"))

    def test_layer_limit_pending_reason(self):
        # A箱区最多2层，类别1只能与1同垛：装满2层后第3只留待落区
        for suffix in (1, 2):
            c = self.service.register_container(
                PLANNER, box_payload("ABCD000000%d" % suffix, dg="1", zone="A")
            )
            c = self.service.place_container(WORKER, c["id"], c["version"], "A-01")
            self.assertEqual(c["state"], "placed")
        c = self.service.register_container(PLANNER, box_payload("ABCD0000003", dg="1", zone="A"))
        c = self.service.place_container(WORKER, c["id"], c["version"], "A-01")
        self.assertEqual(c["state"], "pending")
        self.assertIn("层数已达箱区上限2层", c["pending_reason"])

    def test_stale_version_rejected(self):
        c = self.service.register_container(PLANNER, box_payload("ABCD0000001"))
        with self.assertRaises(Conflict):
            self.service.place_container(WORKER, c["id"], c["version"] - 1, None)

    def test_crane_slot_serves_one_vessel(self):
        self._ready_planned_container("ABCD0000001")
        self._ready_planned_container("ABCD0000002")
        self._ready_planned_container("ABCD0000003")
        p1 = self.service.create_plan(VPLANNER, "LOAD-1", plan_payload(["ABCD0000001"], start=10, end=12))
        p1 = self.service.act_plan(VPLANNER, p1["id"], p1["version"], "confirm", {})
        # 首尾相接（12-14）不算冲突
        p2 = self.service.create_plan(VPLANNER, "LOAD-2", plan_payload(
            ["ABCD0000002"], start=12, end=14, vessel="MV-Y"))
        p2 = self.service.act_plan(VPLANNER, p2["id"], p2["version"], "confirm", {})
        self.assertEqual(p2["state"], "confirmed")
        # 与已确认计划（10-12）重叠：创建时即被拒
        with self.assertRaises(Conflict):
            self.service.create_plan(VPLANNER, "LOAD-3", plan_payload(
                ["ABCD0000003"], start=11, end=13, vessel="MV-Z"))
        # 不同吊机可以同时段服务另一船
        p4 = self.service.create_plan(VPLANNER, "LOAD-4", plan_payload(
            ["ABCD0000003"], crane="QC-02", start=11, end=13, vessel="MV-W"))
        p4 = self.service.act_plan(VPLANNER, p4["id"], p4["version"], "confirm", {})
        self.assertEqual(p4["state"], "confirmed")

    def test_crane_conflict_checked_at_creation_too(self):
        self._ready_planned_container("ABCD0000001")
        self._ready_planned_container("ABCD0000002")
        p1 = self.service.create_plan(VPLANNER, "LOAD-1", plan_payload(["ABCD0000001"], start=10, end=12))
        p1 = self.service.act_plan(VPLANNER, p1["id"], p1["version"], "confirm", {})
        with self.assertRaises(Conflict):
            self.service.create_plan(
                VPLANNER, "LOAD-2", plan_payload(["ABCD0000002"], start=10, end=12, vessel="MV-Y")
            )

    def test_plan_requires_fresh_tally(self):
        c = self.service.register_container(PLANNER, box_payload("ABCD0000001"))
        c = self.service.place_container(WORKER, c["id"], c["version"], None)
        # 未理货不能锁定
        plan = self.service.create_plan(VPLANNER, "LOAD-1", plan_payload(["ABCD0000001"]))
        with self.assertRaises(Conflict):
            self.service.act_plan(VPLANNER, plan["id"], plan["version"], "confirm", {})
        # 理货通过
        self.service.complete_tally(TALLY, "B")
        plan = self.service.act_plan(VPLANNER, plan["id"], plan["version"], "confirm", {})
        # 理货后再释放箱位，箱区版本变化；重新落位后必须重新理货才能执行
        c = self.service.get_container(WORKER, c["id"])
        self.service.release_container(WORKER, c["id"], c["version"], "查验")
        c = self.service.get_container(WORKER, c["id"])
        self.service.place_container(WORKER, c["id"], c["version"], None)
        with self.assertRaises(Conflict) as ctx:
            self.service.act_plan(VPLANNER, plan["id"], plan["version"], "execute", {})
        self.assertIn("理货已过期", str(ctx.exception))
        self.service.complete_tally(TALLY, "B")
        plan = self.service.get_plan(VPLANNER, plan["id"])
        plan = self.service.act_plan(VPLANNER, plan["id"], plan["version"], "execute", {})
        self.assertEqual(plan["state"], "executed")

    def test_plan_manifest_validation(self):
        with self.assertRaises(ValidationError):
            self.service.create_plan(VPLANNER, "LOAD-X", plan_payload([]))
        payload = plan_payload(["ABCD0000001", "ABCD0000001"])
        with self.assertRaises(ValidationError):
            self.service.create_plan(VPLANNER, "LOAD-Y", payload)
        payload = plan_payload(["ABCD0000001"])
        with self.assertRaises(ValidationError):
            self.service.create_plan(VPLANNER, "LOAD-Z", payload)  # 箱子不存在
        c = self.service.register_container(PLANNER, box_payload("ABCD0000001"))
        with self.assertRaises(ValidationError):
            self.service.create_plan(VPLANNER, "LOAD-W", payload)  # 未落位

    def test_invalid_plan_window_and_crane(self):
        c = self.service.register_container(PLANNER, box_payload("ABCD0000001"))
        self.service.place_container(WORKER, c["id"], c["version"], None)
        with self.assertRaises(ValidationError):
            self.service.create_plan(VPLANNER, "P1", plan_payload(["ABCD0000001"], start=18, end=10))
        with self.assertRaises(NotFound):
            self.service.create_plan(VPLANNER, "P2", plan_payload(["ABCD0000001"], crane="QC-99"))

    def test_reassign_and_release_state_gates(self):
        c = self.service.register_container(PLANNER, box_payload("ABCD0000001"))
        # 待落区箱子没有箱位，不能释放；但可以改配到其他箱区
        with self.assertRaises(Conflict):
            self.service.release_container(WORKER, c["id"], c["version"], "r")
        moved = self.service.reassign_container(WORKER, c["id"], c["version"], "C", None)
        self.assertEqual(moved["payload"]["zone_code"], "C")
        self.assertEqual(moved["state"], "placed")
        # 已装船的箱子不能再改配或释放
        self.service.complete_tally(TALLY, "C")
        plan = self.service.create_plan(VPLANNER, "L1", plan_payload(
            ["ABCD0000001"], zone="C", crane="QC-02"))
        plan = self.service.act_plan(VPLANNER, plan["id"], plan["version"], "confirm", {})
        plan = self.service.act_plan(VPLANNER, plan["id"], plan["version"], "execute", {})
        c = self.service.get_container(WORKER, c["id"])
        with self.assertRaises(Conflict):
            self.service.reassign_container(WORKER, c["id"], c["version"], "B", None)
        with self.assertRaises(Conflict):
            self.service.release_container(WORKER, c["id"], c["version"], "r")
