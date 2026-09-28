import unittest

from src.domain import Conflict, ValidationError
from src.yard_rules import YardRules, compatible


def box(no="ABCD1234567", dg="3", weight=20.0, zone="B"):
    return {
        "container_no": no,
        "dg_class": dg,
        "weight_t": weight,
        "zone_code": zone,
        "arrival_date": "2026-09-28",
        "arrival_shift": "early",
        "remark": "",
    }


def zone_b():
    return {"code": "B", "max_layers": 3, "max_stack_weight_t": 65.0,
            "stack_codes": ["B-01", "B-02"]}


class YardRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = YardRules()

    def test_incompatibility_matrix(self):
        self.assertTrue(compatible("3", "3"))
        self.assertTrue(compatible("3", "9"))
        self.assertFalse(compatible("3", "5"))
        self.assertFalse(compatible("5", "4"))
        # 爆炸品、放射性物质只能与同类同垛
        self.assertFalse(compatible("1", "9"))
        self.assertFalse(compatible("7", "9"))
        self.assertTrue(compatible("1", "1"))

    def test_validate_container(self):
        prepared = self.rules.validate_container(box())
        self.assertEqual(prepared["container_no"], "ABCD1234567")
        with self.assertRaises(ValidationError):
            self.rules.validate_container(box(no="bad-no"))
        with self.assertRaises(ValidationError):
            self.rules.validate_container(box(dg="99"))
        with self.assertRaises(ValidationError):
            self.rules.validate_container(box(weight=-1))

    def test_choose_stack_first_fit_skips_incompatible_stack(self):
        stacks = {"B-01": {"layers": 1, "weight_t": 20.0, "classes": {"3"}},
                  "B-02": {"layers": 0, "weight_t": 0.0, "classes": set()}}
        stack, reason = self.rules.choose_stack(box(dg="5"), zone_b(), stacks, None)
        self.assertIsNone(reason)
        self.assertEqual(stack, "B-02")

    def test_choose_stack_rejects_by_layer_weight_and_class(self):
        full = {"B-01": {"layers": 3, "weight_t": 30.0, "classes": {"3"}},
                "B-02": {"layers": 3, "weight_t": 30.0, "classes": {"3"}}}
        stack, reason = self.rules.choose_stack(box(dg="3", weight=10), zone_b(), full, None)
        self.assertIsNone(stack)
        self.assertIn("层数已达箱区上限3层", reason)

        heavy = {"B-01": {"layers": 1, "weight_t": 50.0, "classes": {"3"}}}
        stack, reason = self.rules.choose_stack(box(dg="3", weight=20), zone_b(), heavy, "B-01")
        self.assertIsNone(stack)
        self.assertIn("超过箱区限重", reason)

        mixed = {"B-01": {"layers": 1, "weight_t": 20.0, "classes": {"3"}}}
        stack, reason = self.rules.choose_stack(box(dg="5"), zone_b(), mixed, "B-01")
        self.assertIsNone(stack)
        self.assertIn("不相容", reason)

        stack, reason = self.rules.choose_stack(box(), zone_b(), {}, "X-99")
        self.assertIsNone(stack)
        self.assertIn("不属于箱区B", reason)

    def test_crane_window_overlap(self):
        one = {"crane_code": "QC-01", "plan_date": "2026-09-28", "start_hour": 10, "end_hour": 12}
        hit = dict(one, start_hour=11, end_hour=13)
        miss = dict(one, start_hour=12, end_hour=14)
        other_crane = dict(hit, crane_code="QC-02")
        self.rules.check_crane_conflict(one, [{"id": 2, "state": "confirmed", "payload": miss}])
        with self.assertRaises(Conflict):
            self.rules.check_crane_conflict(one, [{"id": 2, "state": "confirmed", "payload": hit}])
        self.rules.check_crane_conflict(one, [{"id": 2, "state": "confirmed", "payload": other_crane}])
        self.rules.check_crane_conflict(one, [{"id": 2, "state": "cancelled", "payload": hit}])
        self.rules.check_crane_conflict(one, [{"id": 1, "state": "confirmed", "payload": one}], exclude_id=1)

    def test_tally_gate(self):
        payload = {"zone_code": "B"}
        with self.assertRaises(Conflict):
            self.rules.check_tally_gate(payload, None, {"B": {"data_version": 1}})
        with self.assertRaises(Conflict):
            self.rules.check_tally_gate(payload, 1, {"B": {"data_version": 2}})
        self.assertIsNone(
            self.rules.check_tally_gate(payload, 2, {"B": {"data_version": 2}})
        )

    def test_plan_container_gate(self):
        payload = {"zone_code": "B", "container_nos": ["AAAA0000000", "EEEE2222222", "FFFF3333333"]}
        containers = {
            "ABCD1234567": {"state": "placed", "zone_code": "B"},
            "EEEE2222222": {"state": "pending", "zone_code": "B"},
            "FFFF3333333": {"state": "placed", "zone_code": "C"},
        }
        with self.assertRaises(ValidationError) as ctx:
            self.rules.check_plan_containers(payload, containers)
        message = str(ctx.exception)
        self.assertIn("箱号不存在", message)
        self.assertIn("未处于已落位状态", message)
        self.assertIn("不在锁定箱区B", message)

    def test_plan_transitions(self):
        draft = {"state": "draft"}
        self.assertEqual(self.rules.require_plan_transition(draft, "confirm"), "confirmed")
        with self.assertRaises(Conflict):
            self.rules.require_plan_transition(draft, "execute")
