import unittest

from src.yard_data import incompatible
from src.yard_rules import (
    REASON_LAYERS,
    REASON_NO_STACK,
    REASON_WEIGHT,
    YardRules,
)


BLOCK_A01 = {"code": "A01", "max_stack_weight_t": 30.0, "max_layers": 2, "stack_count": 4}
BLOCK_C01 = {"code": "C01", "max_stack_weight_t": 24.0, "max_layers": 1, "stack_count": 3}


def stack(stack_no, top_layer, total_weight, *classes):
    return {"stack_no": stack_no, "top_layer": top_layer, "total_weight_t": total_weight, "classes": list(classes)}


def container(hazard_class, weight_t):
    return {"hazard_class": hazard_class, "weight_t": weight_t}


class IncompatibilityTest(unittest.TestCase):
    def test_matrix(self):
        self.assertTrue(incompatible("3", "2.1"))
        self.assertTrue(incompatible("5.1", "4.2"))
        self.assertTrue(incompatible("1.1", "1.4"))
        self.assertTrue(incompatible("1.1", "9"))
        self.assertFalse(incompatible("3", "3"))
        self.assertFalse(incompatible("9", "8") or incompatible("9", "3"))
        self.assertFalse(incompatible("2.2", "3"))


class SlotRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = YardRules()

    def test_empty_block_opens_first_stack(self):
        d = self.rules.choose_slot(container("3", 10), BLOCK_A01, [])
        self.assertTrue(d.ok)
        self.assertEqual((d.stack_no, d.layer_no), (1, 1))

    def test_same_class_stacks_together(self):
        stacks = [stack(1, 1, 10.0, "3")]
        d = self.rules.choose_slot(container("3", 10), BLOCK_A01, stacks)
        self.assertEqual((d.stack_no, d.layer_no), (1, 2))

    def test_incompatible_class_opens_new_stack(self):
        stacks = [stack(1, 1, 10.0, "3")]
        d = self.rules.choose_slot(container("2.1", 10), BLOCK_A01, stacks)
        self.assertEqual(d.stack_no, 2)
        self.assertEqual(d.layer_no, 1)

    def test_weight_failure(self):
        # 四垛都已开垛且每垛一个25t同类箱：加10t必超重，且没有空垛
        stacks = [stack(no, 1, 25.0, "3") for no in range(1, 5)]
        d = self.rules.choose_slot(container("3", 10), BLOCK_A01, stacks)
        self.assertFalse(d.ok)
        self.assertEqual(d.reason, REASON_WEIGHT)

    def test_layers_failure(self):
        stacks = []
        for no in range(1, 5):
            stacks.append(stack(no, 2, 20.0, "3", "3"))
        d = self.rules.choose_slot(container("3", 10), BLOCK_A01, stacks)
        self.assertFalse(d.ok)
        self.assertEqual(d.reason, REASON_LAYERS)

    def test_no_compatible_stack(self):
        stacks = [stack(1, 1, 10, "3"), stack(2, 1, 10, "2.1"), stack(3, 1, 10, "8"), stack(4, 1, 10, "9")]
        d = self.rules.choose_slot(container("1.1", 5), BLOCK_A01, stacks)
        self.assertFalse(d.ok)
        self.assertEqual(d.reason, REASON_NO_STACK)

    def test_single_box_over_block_limit(self):
        from src.domain import ValidationError
        with self.assertRaises(ValidationError):
            self.rules.validate_block_capacity(container("1.1", 25), BLOCK_C01)

    def test_time_window_conflicts(self):
        from src.domain import Conflict
        plan = {"crane_code": "QC-01", "block_code": "A01", "start_hour": 8, "end_hour": 12, "vessel": "MV-A"}
        same_crane = [{"state": "open", "crane_code": "QC-01", "block_code": "A02",
                       "start_hour": 10, "end_hour": 14, "vessel": "MV-B"}]
        with self.assertRaises(Conflict):
            self.rules.check_crane_conflict(plan, same_crane)
        # 不同吊机同时段不冲突
        other_crane = [dict(same_crane[0], crane_code="QC-02")]
        self.rules.check_crane_conflict(plan, other_crane)
        # 同一箱区时段只能锁给一船
        same_block = [{"state": "open", "crane_code": "QC-02", "block_code": "A01",
                       "start_hour": 11, "end_hour": 13, "vessel": "MV-C"}]
        with self.assertRaises(Conflict):
            self.rules.check_block_conflict(plan, same_block)
        # 已取消的计划不参与冲突
        cancelled = [dict(same_crane[0], state="cancelled")]
        self.rules.check_crane_conflict(plan, cancelled)


if __name__ == "__main__":
    unittest.main()
