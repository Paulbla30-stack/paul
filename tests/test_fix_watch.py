"""Regression test for the watch losing a salience wake to the sleep floor.

tick() used to take the pending wake before checking min_sleep_s. When the
floor refused it, the wake was gone, and observe() only asks again when two
consecutive digests differ -- so a change that arrived early in a sleep and
then stayed put was not acted on until the hourly heartbeat.
"""

import logging
import unittest

from vigil.agent.watch import ASLEEP, AWAKE, Watch

LOG = logging.getLogger("test.fix_watch")
LOG.addHandler(logging.NullHandler())


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)


def asleep_watch():
    clock = FakeClock()
    v = Watch({"enabled": True, "warm_window_s": 300.0, "idle_after_s": 180.0,
               "heartbeat_s": 3600.0, "burst_calls": 6, "burst_window_s": 180.0,
               "quiet_hours": []}, logger=LOG, clock=clock)
    v.observe({"disk": {"used_percent": 50}})
    for _ in range(6):
        v.may_call()
        v.note_call()
    clock.advance(181)
    v.tick()
    assert v.state == ASLEEP, "fixture failed to put the watch to sleep"
    return v, clock


class TestAPersistentChangeWakesAfterTheFloor(unittest.TestCase):

    def test_the_wake_waits_for_the_floor_and_then_happens(self):
        v, clock = asleep_watch()
        clock.advance(30)
        self.assertTrue(v.observe({"disk": {"used_percent": 95}}))
        v.tick()
        self.assertEqual(v.state, ASLEEP, "the floor must still hold")
        self.assertTrue(v._pending_wake.startswith("salience"),
                        "a wake the floor refused must stay pending")
        # The disk stays at 95%: the same digest, so no new wake is requested.
        v.observe({"disk": {"used_percent": 95}})
        clock.advance(v.min_sleep_s)
        v.tick()
        self.assertEqual(v.state, AWAKE)
        self.assertIn("salience", v.wake_reason)
        self.assertEqual(v._pending_wake, "")

    def test_it_does_not_wait_for_the_heartbeat(self):
        v, clock = asleep_watch()
        clock.advance(30)
        v.observe({"disk": {"used_percent": 95}})
        for _ in range(20):                 # ticks every 30s until past the floor
            clock.advance(30)
            v.observe({"disk": {"used_percent": 95}})
            v.tick()
        self.assertEqual(v.state, AWAKE)
        self.assertNotIn("heartbeat", v.wake_reason)

    def test_the_operator_still_wakes_it_at_once(self):
        v, clock = asleep_watch()
        clock.advance(1)
        v.observe({"disk": {"used_percent": 95}})
        v.note_activity("ui message")
        self.assertEqual(v.state, AWAKE)
        self.assertIn("operator", v.wake_reason)


if __name__ == "__main__":
    unittest.main()
