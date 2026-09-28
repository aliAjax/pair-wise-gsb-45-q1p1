import tempfile
import unittest
from pathlib import Path

from app import build_yard_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.yard_rules import (
    REASON_LAYERS,
    REASON_NO_STACK,
    REASON_WEIGHT,
)


def reg(service, actor, no, hazard, weight, hour):
    return service.register_container(
        actor, {"container_no": no, "hazard_class": hazard, "weight_t": weight, "arrival_hour": hour},
    )


class YardFailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_yard_service(str(Path(self.temp.name) / "yard.db"))
        self.gate = Actor("gate-1", "gate_clerk")
        self.planner = Actor("yard-1", "yard_planner")
        self.tally = Actor("tally-1", "tally_clerk")
        self.vessel = Actor("vessel-1", "vessel_planner")

    def tearDown(self):
        self.temp.cleanup()

    def tally_block(self, code="A01"):
        block = [b for b in self.service.list_blocks() if b["code"] == code][0]
        self.service.set_tally(self.tally, code, block["version"], {"tally_completed": True})

    def test_permissions_are_separated_by_role(self):
        payload = {"container_no": "CBHU1001", "hazard_class": "3", "weight_t": 10.0, "arrival_hour": 6}
        with self.assertRaises(PermissionDenied):
            self.service.register_container(self.planner, payload)
        c = reg(self.service, self.gate, "CBHU1001", "3", 10.0, 6)
        with self.assertRaises(PermissionDenied):
            self.service.place_container(self.gate, c["id"], c["version"], {"block_code": "A01"})

    def test_unknown_role_rejected(self):
        with self.assertRaises(PermissionDenied):
            self.service.list_containers(Actor("x", "outsider"))

    def test_duplicate_container_no(self):
        reg(self.service, self.gate, "CBHU1002", "3", 10.0, 6)
        with self.assertRaises(Conflict):
            reg(self.service, self.gate, "CBHU1002", "3", 10.0, 6)

    def test_bad_registration_input(self):
        with self.assertRaises(ValidationError):
            reg(self.service, self.gate, "x", "3", 10.0, 6)  # 箱号太短
        with self.assertRaises(ValidationError):
            reg(self.service, self.gate, "CBHU1003", "13", 10.0, 6)  # 类别不在目录
        with self.assertRaises(ValidationError):
            reg(self.service, self.gate, "CBHU1004", "3", 99.0, 6)  # 超量程
        with self.assertRaises(ValidationError):
            reg(self.service, self.gate, "CBHU1005", "3", 10.0, 24)

    def test_single_box_over_limit_raises(self):
        c = reg(self.service, self.gate, "CBHU1006", "1.1", 25.0, 6)
        with self.assertRaises(ValidationError):
            self.service.place_container(self.planner, c["id"], c["version"], {"block_code": "C01"})

    def test_pending_reasons(self):
        # 超重原因：4垛各放一个25t同类箱（25+10>30）
        ids = []
        for i in range(4):
            c = reg(self.service, self.gate, "CBHUW%03d" % i, "3", 25.0, 6)
            c = self.service.place_container(self.planner, c["id"], c["version"], {"block_code": "A01"})
            self.assertEqual(c["state"], "placed")
            ids.append(c)
        heavy = reg(self.service, self.gate, "CBHUW999", "3", 10.0, 6)
        held = self.service.place_container(self.planner, heavy["id"], heavy["version"], {"block_code": "A01"})
        self.assertEqual(held["state"], "pending")
        self.assertEqual(held["pending_reason"], REASON_WEIGHT)

        # 限层原因：B01 六垛全部叠满3层同类轻箱（B01限3层、单垛40t）
        boxes = []
        for stack_no in range(1, 7):
            for layer in range(1, 4):
                boxes.append(reg(self.service, self.gate, "CBHUL%03d" % len(boxes), "8", 8.0, 6))
        for c in boxes:
            r = self.service.place_container(self.planner, c["id"], c["version"], {"block_code": "B01"})
            self.assertEqual(r["state"], "placed")
        more = reg(self.service, self.gate, "CBHUL999", "8", 8.0, 6)
        held = self.service.place_container(self.planner, more["id"], more["version"], {"block_code": "B01"})
        self.assertEqual(held["pending_reason"], REASON_LAYERS)

    def test_incompatible_classes_never_share_stack(self):
        a = reg(self.service, self.gate, "CBHUI001", "5.1", 10.0, 6)
        b = reg(self.service, self.gate, "CBHUI002", "3", 10.0, 6)
        a = self.service.place_container(self.planner, a["id"], a["version"], {"block_code": "A01"})
        b = self.service.place_container(self.planner, b["id"], b["version"], {"block_code": "A01"})
        self.assertNotEqual(a["stack_no"], b["stack_no"])

    def test_plan_requires_tally(self):
        with self.assertRaises(Conflict):
            self.service.create_plan(self.vessel, {
                "vessel": "MV1", "block_code": "A01", "crane_code": "QC-01",
                "start_hour": 8, "end_hour": 12,
            })

    def test_crane_time_window_exclusive(self):
        self.tally_block("A01")
        self.tally_block("A02")
        p1 = self.service.create_plan(self.vessel, {
            "vessel": "MV1", "block_code": "A01", "crane_code": "QC-01",
            "start_hour": 8, "end_hour": 12,
        })
        with self.assertRaises(Conflict):
            self.service.create_plan(self.vessel, {
                "vessel": "MV2", "block_code": "A02", "crane_code": "QC-01",
                "start_hour": 11, "end_hour": 15,
            })
        # 首尾相接（12点开始）不冲突
        p2 = self.service.create_plan(self.vessel, {
            "vessel": "MV2", "block_code": "A02", "crane_code": "QC-01",
            "start_hour": 12, "end_hour": 15,
        })
        self.assertEqual(p2["state"], "open")

    def test_block_time_window_exclusive(self):
        self.tally_block("A01")
        self.service.create_plan(self.vessel, {
            "vessel": "MV1", "block_code": "A01", "crane_code": "QC-01",
            "start_hour": 8, "end_hour": 12,
        })
        with self.assertRaises(Conflict):
            self.service.create_plan(self.vessel, {
                "vessel": "MV2", "block_code": "A01", "crane_code": "QC-02",
                "start_hour": 10, "end_hour": 12,
            })

    def test_locked_block_sends_box_to_pending(self):
        c = reg(self.service, self.gate, "CBHU1010", "3", 10.0, 6)
        self.tally_block("A01")
        self.service.create_plan(self.vessel, {
            "vessel": "MV1", "block_code": "A01", "crane_code": "QC-01",
            "start_hour": 8, "end_hour": 12,
        })
        result = self.service.place_container(self.planner, c["id"], c["version"], {"block_code": "A01"})
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["pending_reason"], "block_locked")

    def test_stale_version_rejected(self):
        c = reg(self.service, self.gate, "CBHU1011", "3", 10.0, 6)
        with self.assertRaises(Conflict):
            self.service.place_container(self.planner, c["id"], c["version"] - 1, {"block_code": "A01"})

    def test_placed_box_must_use_reallocate(self):
        c = reg(self.service, self.gate, "CBHU1012", "3", 10.0, 6)
        c = self.service.place_container(self.planner, c["id"], c["version"], {"block_code": "A01"})
        with self.assertRaises(Conflict):
            self.service.place_container(self.planner, c["id"], c["version"], {"block_code": "B01"})

    def test_cancel_releases_lock_and_allows_new_plan(self):
        self.tally_block("A01")
        p1 = self.service.create_plan(self.vessel, {
            "vessel": "MV1", "block_code": "A01", "crane_code": "QC-01",
            "start_hour": 8, "end_hour": 12,
        })
        cancelled = self.service.cancel_plan(self.vessel, p1["id"], p1["version"], {"reason": "船期调整"})
        self.assertEqual(cancelled["state"], "cancelled")
        # 释放后同时段可重排
        p2 = self.service.create_plan(self.vessel, {
            "vessel": "MV3", "block_code": "A01", "crane_code": "QC-01",
            "start_hour": 8, "end_hour": 12,
        })
        self.assertEqual(p2["state"], "open")

    def test_complete_empty_block_fails(self):
        self.tally_block("A02")
        plan = self.service.create_plan(self.vessel, {
            "vessel": "MV1", "block_code": "A02", "crane_code": "QC-01",
            "start_hour": 8, "end_hour": 12,
        })
        with self.assertRaises(Conflict):
            self.service.complete_plan(self.vessel, plan["id"], plan["version"], {})


if __name__ == "__main__":
    unittest.main()
