"""Tests for what the agent costs and what it leaves behind.

Two things nobody was watching: an imported model billing per model-minute
while the agent wakes every thirty seconds to conclude nothing is required,
and a trail of old images, snapshots and renamed buckets still sitting there
because they were on a human's list rather than the agent's.

The important property is that this only ever reads.
"""

import logging
import unittest

from jarvis.agent import authority
from jarvis.agent.estate import Estate, _join
from jarvis.agent.planner import Task, TaskType

LOG = logging.getLogger("test")
NOW = 1_800_000_000.0


class FakeCE:
    def __init__(self, amounts=None, fail=None):
        self.amounts = amounts or {"Amazon Bedrock": "4.20", "Amazon EC2": "1.10"}
        self.fail = fail

    def get_cost_and_usage(self, **kw):
        if self.fail:
            raise self.fail
        return {"ResultsByTime": [{"Groups": [
            {"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": v, "Unit": "USD"}}}
            for k, v in self.amounts.items()]}]}


class FakeEC2:
    def __init__(self, images=None, snapshots=None, fail=False):
        self.images = images or []
        self.snapshots = snapshots or []
        self.fail = fail

    def describe_images(self, **kw):
        if self.fail:
            raise RuntimeError("AccessDenied")
        return {"Images": self.images}

    def describe_snapshots(self, **kw):
        if self.fail:
            raise RuntimeError("AccessDenied")
        return {"Snapshots": self.snapshots}


class FakeS3:
    def __init__(self, buckets=None):
        self.buckets = buckets or []

    def list_buckets(self):
        return {"Buckets": self.buckets}


def old_iso(days):
    from datetime import datetime, timedelta, timezone
    return (datetime.fromtimestamp(NOW, timezone.utc)
            - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def estate(**clients):
    defaults = {"ce": FakeCE(), "ec2": FakeEC2(), "s3": FakeS3()}
    defaults.update(clients)
    return Estate("us-west-2", LOG, {}, clients=defaults, clock=lambda: NOW)


class TestSpend(unittest.TestCase):

    def test_it_reports_the_real_number_by_service(self):
        cost = estate().cost(7)
        self.assertTrue(cost["available"])
        self.assertAlmostEqual(cost["total"], 5.30)
        self.assertAlmostEqual(cost["per_day"], 0.76)
        self.assertEqual(cost["by_service"][0]["service"], "Amazon Bedrock")

    def test_no_permission_is_a_reason_not_a_crash(self):
        """A partial view is still worth having."""
        cost = estate(ce=FakeCE(fail=RuntimeError("AccessDenied"))).cost(7)
        self.assertFalse(cost["available"])
        self.assertIn("AccessDenied", cost["reason"])
        lines = estate(ce=FakeCE(fail=RuntimeError("AccessDenied"))).lines()
        self.assertTrue(any("not visible" in l for l in lines))

    def test_rounding_noise_is_left_out(self):
        cost = estate(ce=FakeCE({"Tax": "0.001", "Amazon EC2": "1.00"})).cost(7)
        services = [c["service"] for c in cost["by_service"]]
        self.assertNotIn("Tax", services)


class TestAccumulation(unittest.TestCase):

    def test_it_finds_what_has_been_sitting_a_while(self):
        e = estate(
            ec2=FakeEC2(
                images=[{"ImageId": "ami-old", "Name": "jarvis-1",
                         "CreationDate": old_iso(90)},
                        {"ImageId": "ami-new", "Name": "jarvis-2",
                         "CreationDate": old_iso(2)}],
                snapshots=[{"SnapshotId": "snap-1", "VolumeSize": 8,
                            "StartTime": old_iso(120)}]),
            s3=FakeS3([{"Name": "openclaw-ledger", "CreationDate": old_iso(200)},
                       {"Name": "new-bucket", "CreationDate": old_iso(1)}]))
        left = e.leftovers()
        self.assertEqual([i["id"] for i in left["images"]], ["ami-old"])
        self.assertEqual(left["images"][0]["age_days"], 90)
        self.assertEqual([s["id"] for s in left["snapshots"]], ["snap-1"])
        self.assertEqual([b["name"] for b in left["buckets"]], ["openclaw-ledger"])
        self.assertEqual(left["total"], 3)

    def test_it_says_what_it_could_not_look_at(self):
        left = estate(ec2=FakeEC2(fail=True)).leftovers()
        self.assertIn("images: RuntimeError", left["unavailable"])
        self.assertIn("snapshots: RuntimeError", left["unavailable"])

    def test_it_counts_in_english(self):
        e = estate(ec2=FakeEC2(
            images=[{"ImageId": "ami-1", "Name": "x", "CreationDate": old_iso(90)}],
            snapshots=[{"SnapshotId": "s1", "VolumeSize": 8, "StartTime": old_iso(90)},
                       {"SnapshotId": "s2", "VolumeSize": 8, "StartTime": old_iso(91)}]))
        line = " ".join(e.lines())
        self.assertIn("1 machine image and 2 snapshots", line)
        self.assertNotIn("1 machine images", line)

    def test_it_reports_the_count_even_when_nothing_is_old(self):
        """Nine images built this week, of which one is deployed, are eight
        nobody needs and none of them are old. The count says that."""
        e = estate(ec2=FakeEC2(
            images=[{"ImageId": f"ami-{i}", "Name": "x", "CreationDate": old_iso(1)}
                    for i in range(9)],
            snapshots=[{"SnapshotId": f"s{i}", "VolumeSize": 8,
                        "StartTime": old_iso(1)} for i in range(9)]))
        left = e.leftovers()
        self.assertEqual(left["total"], 0)
        self.assertEqual(left["totals"]["images"], 9)
        line = " ".join(e.lines())
        self.assertIn("holds 9 machine images and 9 snapshots", line)

    def test_join_reads_like_a_sentence(self):
        self.assertEqual(_join([]), "")
        self.assertEqual(_join(["a"]), "a")
        self.assertEqual(_join(["a", "b"]), "a and b")
        self.assertEqual(_join(["a", "b", "c"]), "a, b and c")


class TestItOnlyReads(unittest.TestCase):

    def test_there_is_no_delete_anywhere_in_the_module(self):
        """'Tell me what is piling up' must not be able to become
        'tidy it away'."""
        import inspect
        from jarvis.agent import estate as module
        source = inspect.getsource(module)
        for verb in ("delete_", "deregister_", "terminate_", "remove_",
                     ".delete(", "put_object", "revoke_"):
            self.assertNotIn(verb, source, f"{verb} appears in estate.py")

    def test_the_report_is_a_read_at_every_rung(self):
        task = Task(task_type=TaskType.ESTATE_REPORT,
                    description="what are we spending", priority=2)
        self.assertEqual(authority.classify_task(task), authority.READ)
        self.assertTrue(authority.review(task, authority.OBSERVER).allowed)

    def test_disabled_reports_nothing(self):
        e = Estate("us-west-2", LOG, {"enabled": False}, clients={}, clock=lambda: NOW)
        self.assertEqual(e.report()["enabled"], False)
        self.assertEqual(e.lines(), [])


if __name__ == "__main__":
    unittest.main()
