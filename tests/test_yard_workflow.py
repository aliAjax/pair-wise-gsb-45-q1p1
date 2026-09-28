import tempfile
import unittest
from pathlib import Path

from app import build_yard_service
from src.domain import Actor
from src.repository import Repository
from src.audit import AuditRecorder
from src.yard_rules import YardRules
from src.yard_service import YardService


PLANNER = Actor("p1", "yard_planner")
WORKER = Actor("w1", "yard_worker")
TALLY = Actor("t1", "tally_clerk")
VPLANNER = Actor("v1", "vessel_planner")


def box_payload(no, dg="3", weight=20.0, zone="B", shift="early"):
    return {"container_no": no, "dg_class": dg, "weight_t": weight, "zone_code": zone,
            "arrival_date": "2026-09-28", "arrival_shift": shift}


def plan_payload(nos, crane="QC-01", start=10, end=12, zone="B", vessel="MV-X"):
    return {"vessel": vessel, "zone_code": zone, "crane_code": crane, "plan_date": "2026-09-28",
            "start_hour": start, "end_hour": end, "container_nos": list(nos)}


class YardWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = str(Path(self.temp.name) / "test.db")
        self.repository = Repository(db)
        self.service = YardService(self.repository, YardRules(), AuditRecorder(self.repository))

    def tearDown(self):
        self.temp.cleanup()

    def test_seed_reference_data(self):
        data = self.service.reference_data(PLANNER)
        self.assertEqual({z["code"] for z in data["zones"]}, {"A", "B", "C"})
        self.assertEqual({c["code"] for c in data["cranes"]}, {"QC-01", "QC-02"})
        self.assertEqual(len(data["dg_classes"]), 9)

    def test_register_place_tally_plan_and_load(self):
        c1 = self.service.register_container(PLANNER, box_payload("ABCD0000001", dg="3"))
        c2 = self.service.register_container(PLANNER, box_payload("ABCD0000002", dg="3", weight=25))
        c1 = self.service.place_container(WORKER, c1["id"], c1["version"], None)
        c2 = self.service.place_container(WORKER, c2["id"], c2["version"], None)
        self.assertEqual(c1["state"], "placed")
        self.assertEqual(c1["stack_code"], "B-01")
        self.assertEqual(c1["layer_no"], 1)
        self.assertEqual(c2["stack_code"], "B-01")
        self.assertEqual(c2["layer_no"], 2)

        zones = {z["code"]: z for z in self.service.zones(WORKER)}
        self.assertEqual(zones["B"]["placed_count"], 2)
        b01 = next(s for s in zones["B"]["stacks"] if s["code"] == "B-01")
        self.assertEqual(b01["weight_t"], 45.0)
        self.assertEqual(b01["classes"], ["3"])

        tally = self.service.complete_tally(TALLY, "B")
        self.assertEqual(tally["data_version"], 2)

        plan = self.service.create_plan(
            VPLANNER, "LOAD-001", plan_payload(["ABCD0000001", "ABCD0000002"])
        )
        self.assertEqual(plan["state"], "draft")
        plan = self.service.act_plan(VPLANNER, plan["id"], plan["version"], "confirm", {})
        self.assertEqual(plan["state"], "confirmed")
        plan = self.service.act_plan(VPLANNER, plan["id"], plan["version"], "execute", {})
        self.assertEqual(plan["state"], "executed")

        c1 = self.service.get_container(WORKER, c1["id"])
        self.assertEqual(c1["state"], "loaded")
        self.assertIsNone(c1["stack_code"])
        zones = {z["code"]: z for z in self.service.zones(WORKER)}
        self.assertEqual(zones["B"]["placed_count"], 0)

        timeline = self.service.plan_timeline(VPLANNER, plan["id"])
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions, ["created", "confirm", "execute"])

    def test_incompatible_classes_stay_out_of_same_stack(self):
        liquid = self.service.register_container(PLANNER, box_payload("ABCD0000001", dg="3"))
        oxidizer = self.service.register_container(PLANNER, box_payload("ABCD0000002", dg="5"))
        liquid = self.service.place_container(WORKER, liquid["id"], liquid["version"], None)
        oxidizer = self.service.place_container(WORKER, oxidizer["id"], oxidizer["version"], None)
        self.assertEqual(liquid["stack_code"], "B-01")
        self.assertEqual(oxidizer["stack_code"], "B-02")

    def test_overflow_container_remains_pending_with_reason(self):
        # B箱区每垛限重65吨：B-01放40吨、B-02放36吨后，第三只30吨箱无论去哪垛都超重
        heavy = self.service.register_container(PLANNER, box_payload("ABCD0000001", weight=40))
        extra = self.service.register_container(PLANNER, box_payload("ABCD0000002", weight=36))
        heavy = self.service.place_container(WORKER, heavy["id"], heavy["version"], None)
        extra = self.service.place_container(WORKER, extra["id"], extra["version"], None)
        self.assertEqual(heavy["stack_code"], "B-01")
        self.assertEqual(extra["stack_code"], "B-02")

        # 第三只30吨箱：B-02与B-01都超重，整箱区放不下
        third = self.service.register_container(PLANNER, box_payload("ABCD0000003", weight=30))
        third = self.service.place_container(WORKER, third["id"], third["version"], "B-02")
        self.assertEqual(third["state"], "pending")
        self.assertIn("超过箱区限重", third["pending_reason"])

        # 版本递增，仍可重新落位；换至限重80吨的C箱区
        third = self.service.reassign_container(
            WORKER, third["id"], third["version"], "C", None
        )
        self.assertEqual(third["state"], "placed")
        self.assertEqual(third["payload"]["zone_code"], "C")
        self.assertEqual(third["stack_code"], "C-01")

    def test_reassign_releases_original_slot_and_invalidates_tally(self):
        c = self.service.register_container(PLANNER, box_payload("ABCD0000001"))
        c = self.service.place_container(WORKER, c["id"], c["version"], None)
        self.service.complete_tally(TALLY, "B")
        plan = self.service.create_plan(
            VPLANNER, "LOAD-002", plan_payload(["ABCD0000001"], start=14, end=16)
        )
        # 理货版本与箱区一致，可以锁定
        plan = self.service.act_plan(VPLANNER, plan["id"], plan["version"], "confirm", {})
        self.assertEqual(plan["state"], "confirmed")

        # 改配先释放原箱位，箱区版本前进，旧理货失效
        c = self.service.get_container(WORKER, c["id"])
        moved = self.service.reassign_container(WORKER, c["id"], c["version"], None, "B-02")
        self.assertEqual(moved["stack_code"], "B-02")
        zone_after = {z["code"]: z for z in self.service.zones(WORKER)}["B"]
        self.assertEqual(zone_after["data_version"], 2)
        plan = self.service.get_plan(VPLANNER, plan["id"])
        with self.assertRaises(Exception):
            self.service.act_plan(VPLANNER, plan["id"], plan["version"], "execute", {})

    def test_release_frees_slot(self):
        c1 = self.service.register_container(PLANNER, box_payload("ABCD0000001"))
        c1 = self.service.place_container(WORKER, c1["id"], c1["version"], "B-03")
        self.assertEqual(c1["layer_no"], 1)
        c1 = self.service.release_container(WORKER, c1["id"], c1["version"], "临时查验")
        self.assertEqual(c1["state"], "pending")
        self.assertIsNone(c1["stack_code"])
        self.assertEqual(c1["pending_reason"], "临时查验")
        c2 = self.service.register_container(PLANNER, box_payload("ABCD0000002"))
        c2 = self.service.place_container(WORKER, c2["id"], c2["version"], "B-03")
        self.assertEqual(c2["layer_no"], 1)
