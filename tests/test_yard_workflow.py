import tempfile
import threading
import unittest
from pathlib import Path

from app import build_yard_service
from src.domain import Actor, Conflict


def reg(service, no, hazard, weight, hour):
    return service.register_container(
        Actor("gate-1", "gate_clerk"),
        {"container_no": no, "hazard_class": hazard, "weight_t": weight, "arrival_hour": hour},
    )


class YardWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_yard_service(str(Path(self.temp.name) / "yard.db"))
        self.gate = Actor("gate-1", "gate_clerk")
        self.planner = Actor("yard-1", "yard_planner")
        self.tally = Actor("tally-1", "tally_clerk")
        self.vessel = Actor("vessel-1", "vessel_planner")

    def tearDown(self):
        self.temp.cleanup()

    def test_register_place_tally_load(self):
        c1 = reg(self.service, "CBHU0001", "3", 10.0, 6)
        c2 = reg(self.service, "CBHU0002", "2.1", 10.0, 7)
        self.assertEqual(c1["state"], "pending")

        c1 = self.service.place_container(self.planner, c1["id"], c1["version"], {"block_code": "A01"})
        c2 = self.service.place_container(self.planner, c2["id"], c2["version"], {"block_code": "A01"})
        self.assertEqual(c1["state"], "placed")
        self.assertEqual(c2["state"], "placed")
        # 不相容类别不能同垛：第二个箱开新垛
        self.assertEqual(c1["stack_no"], 1)
        self.assertEqual(c2["stack_no"], 2)

        overview = self.service.block_overview(self.planner, "A01")
        self.assertEqual(len(overview["stacks"]), 2)

        # 理货后才能编计划
        block = [b for b in self.service.list_blocks() if b["code"] == "A01"][0]
        block = self.service.set_tally(self.tally, "A01", block["version"], {"tally_completed": True})
        self.assertTrue(block["tally_completed"])

        plan = self.service.create_plan(self.vessel, {
            "vessel": "MV-PACIFIC", "block_code": "A01", "crane_code": "QC-01",
            "start_hour": 8, "end_hour": 14,
        })
        self.assertEqual(plan["state"], "open")

        done = self.service.complete_plan(self.vessel, plan["id"], plan["version"], {})
        self.assertEqual(done["state"], "done")
        self.assertEqual(set(done["loaded_container_ids"]), {c1["id"], c2["id"]})

        self.assertEqual(self.service.get_container(self.planner, c1["id"])["state"], "loaded")
        block = [b for b in self.service.list_blocks() if b["code"] == "A01"][0]
        self.assertFalse(block["tally_completed"])

        timeline = [e["action"] for e in self.service.container_timeline(self.planner, c1["id"])]
        self.assertEqual(timeline, ["registered", "placed", "loaded"])
        block_events = [e["action"] for e in self.service.block_timeline(self.planner, "A01")]
        self.assertEqual(block_events, ["tally", "tally_reset"])

    def test_reallocate_releases_original_slot(self):
        c1 = reg(self.service, "CBHU0003", "3", 10.0, 6)
        c1 = self.service.place_container(self.planner, c1["id"], c1["version"], {"block_code": "A01"})
        self.assertEqual((c1["block_code"], c1["stack_no"]), ("A01", 1))

        moved = self.service.place_container(self.planner, c1["id"], c1["version"], {"block_code": "B01"}, reallocate=True)
        self.assertEqual(moved["state"], "placed")
        self.assertEqual((moved["block_code"], moved["stack_no"]), ("B01", 1))

        a01 = self.service.block_overview(self.planner, "A01")
        self.assertEqual(a01["stacks"], [])
        events = [e["action"] for e in self.service.container_timeline(self.planner, c1["id"])]
        self.assertIn("relocated", events)

    def test_pending_box_waits_until_space_appears(self):
        c1 = reg(self.service, "CBHU0004", "1.1", 10.0, 6)
        c2 = reg(self.service, "CBHU0005", "1.4", 10.0, 7)
        c3 = reg(self.service, "CBHU0006", "1.1", 10.0, 8)
        c4 = reg(self.service, "CBHU0007", "1.4", 10.0, 9)
        c1 = self.service.place_container(self.planner, c1["id"], c1["version"], {"block_code": "C01"})
        c2 = self.service.place_container(self.planner, c2["id"], c2["version"], {"block_code": "C01"})
        c3 = self.service.place_container(self.planner, c3["id"], c3["version"], {"block_code": "C01"})
        c4 = self.service.place_container(self.planner, c4["id"], c4["version"], {"block_code": "C01"})
        # C01 只有3垛且限1层，1.1/1.4互不相容：第4箱进待落区
        self.assertEqual(c4["state"], "pending")
        self.assertEqual(c4["target_block_code"], "C01")
        self.assertTrue(c4["pending_reason"])

        # 改配腾出一垛后，待落区箱子可以重新落位
        c1 = self.service.get_container(self.planner, c1["id"])
        self.service.place_container(self.planner, c1["id"], c1["version"], {"block_code": "A01"}, reallocate=True)
        c4 = self.service.get_container(self.planner, c4["id"])
        c4 = self.service.place_container(self.planner, c4["id"], c4["version"], {"block_code": "C01"})
        self.assertEqual(c4["state"], "placed")

    def test_concurrent_placement_is_serialised_by_lock(self):
        """两个班次同时把同类箱落进同一垛（限2层）：要么串行成功，要么一人遇到版本/锁冲突重试，绝不出现同层两箱。"""
        containers = [reg(self.service, "CBHUC%03d" % i, "3", 10.0, 6) for i in range(2)]
        results, errors = [], []
        barrier = threading.Barrier(2)

        def worker(c):
            barrier.wait()
            for attempt in range(5):
                fresh = self.service.get_container(self.planner, c["id"])
                try:
                    results.append(self.service.place_container(
                        self.planner, fresh["id"], fresh["version"], {"block_code": "A01"}))
                    return
                except Conflict:
                    continue
            errors.append("container %s failed after retries" % c["id"])

        threads = [threading.Thread(target=worker, args=(c,)) for c in containers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["state"] == "placed" for r in results))
        stacks = self.service.block_overview(self.planner, "A01")["stacks"]
        self.assertEqual(len(stacks), 1)
        self.assertEqual(stacks[0]["box_count"], 2)
        self.assertEqual(stacks[0]["top_layer"], 2)


if __name__ == "__main__":
    unittest.main()
