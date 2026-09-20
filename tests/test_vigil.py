"""Tests for the vigil: when the agent thinks, and when it goes quiet.

The bug these exist to prevent is specific and it already happened. The loop
backed off when idle, capped at 300s, and 300s is roughly how long Bedrock
keeps an imported model copy warm. So the agent settled into one call every
five minutes, for ever, which is the exact cadence that never lets the copy go
cold. Twelve calls an hour looks restrained and cost about 200 USD a day.

So most of what is asserted here is not "does it sleep" but "does the sleep
actually last long enough to be worth anything", and "does a person still get
answered immediately". A sleep shorter than the warm window saves nothing, and
an agent that makes its operator wait to save a few pence has the trade
backwards.
"""

import logging
import unittest

from jarvis.agent.vigil import (AWAKE, ASLEEP, NullVigil, Vigil, build, digest,
                                salient_change, WAKE_HEARTBEAT, WAKE_OPERATOR,
                                WAKE_SALIENCE)

LOG = logging.getLogger("test")


class FakeClock:
    """A clock that only moves when the test says so."""

    def __init__(self, start=1_000_000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)
        return self.t


def make(clock=None, **overrides):
    cfg = {
        "enabled": True,
        "warm_window_s": 300.0,
        "idle_after_s": 180.0,
        "heartbeat_s": 3600.0,
        "quiet_heartbeat_s": 10800.0,
        "burst_calls": 6,
        "burst_window_s": 180.0,
        "quiet_hours": [22, 7],
        "timezone": "Europe/London",
    }
    cfg.update(overrides)
    clock = clock or FakeClock()
    return Vigil(cfg, logger=LOG, clock=clock), clock


class TestFallingAsleep(unittest.TestCase):
    def test_starts_awake(self):
        v, _ = make()
        self.assertEqual(v.state, AWAKE)
        self.assertTrue(v.may_call())

    def test_sleeps_once_the_burst_is_spent_and_it_has_gone_quiet(self):
        v, clock = make()
        for _ in range(6):
            self.assertTrue(v.may_call())
            v.note_call()
        # Burst spent, but it has only just been working.
        self.assertFalse(v.may_call())
        self.assertEqual(v.state, AWAKE)
        clock.advance(181)
        v.tick()
        self.assertEqual(v.state, ASLEEP)

    def test_stays_awake_while_work_keeps_arriving(self):
        v, clock = make()
        for _ in range(6):
            v.may_call()
            v.note_call()
        for _ in range(20):
            clock.advance(60)
            v.note_work()          # a task ran; the agent is busy
            v.tick()
        self.assertEqual(v.state, AWAKE)

    def test_does_not_sleep_before_it_has_thought_at_all(self):
        """A fresh agent that has not yet planned should not doze off."""
        v, clock = make()
        clock.advance(10_000)
        v.tick()
        self.assertEqual(v.state, AWAKE)

    def test_sleep_records_when_the_copy_goes_cold(self):
        v, clock = make()
        v.may_call()
        v.note_call()
        clock.advance(181)
        for _ in range(6):
            v.may_call()
            v.note_call()
        clock.advance(181)
        v.tick()
        t = v.take_transition()
        while t and t["state"] != ASLEEP:
            t = v.take_transition()
        self.assertIsNotNone(t)
        self.assertEqual(t["state"], ASLEEP)
        self.assertIn("cold_in_s", t)


class TestWaking(unittest.TestCase):
    def sleep_it(self, v, clock):
        for _ in range(6):
            v.may_call()
            v.note_call()
        clock.advance(181)
        v.tick()
        assert v.state == ASLEEP, "fixture failed to put the vigil to sleep"

    def test_operator_wakes_it_immediately(self):
        """A person waiting costs more than the model being warm."""
        v, clock = make()
        self.sleep_it(v, clock)
        clock.advance(1)                    # far inside the min-sleep floor
        v.note_activity("ui message")
        v.tick()
        self.assertEqual(v.state, AWAKE)
        self.assertIn("operator", v.wake_reason)
        self.assertTrue(v.may_call())

    def test_salience_respects_the_minimum_sleep(self):
        """Otherwise a chatty observation stream holds the copy warm as before."""
        v, clock = make()
        self.sleep_it(v, clock)
        clock.advance(30)
        v.observe({"memory": {"percent": 10}})
        v.observe({"memory": {"percent": 90}})   # a real change, but too soon
        v.tick()
        self.assertEqual(v.state, ASLEEP)
        clock.advance(300)                       # past the warm window
        v.observe({"memory": {"percent": 20}})
        v.tick()
        self.assertEqual(v.state, AWAKE)
        self.assertIn("salience", v.wake_reason)

    def test_heartbeat_wakes_it(self):
        v, clock = make()
        self.sleep_it(v, clock)
        clock.advance(3601)
        v.tick()
        self.assertEqual(v.state, AWAKE)
        self.assertIn("heartbeat", v.wake_reason)

    def test_a_long_busy_spell_does_not_owe_a_heartbeat(self):
        """The heartbeat measures sleep, not wall clock since the last wake.

        Otherwise finishing two hours of real work would immediately trip the
        hourly heartbeat and wake the agent that had just stopped.
        """
        v, clock = make()
        for _ in range(6):
            v.may_call()
            v.note_call()
        for _ in range(120):            # two hours of steady work
            clock.advance(60)
            v.note_work()
            v.tick()
        self.assertEqual(v.state, AWAKE)
        clock.advance(181)              # goes quiet, and sleeps
        v.tick()
        self.assertEqual(v.state, ASLEEP)
        clock.advance(60)               # one minute later
        v.tick()
        self.assertEqual(v.state, ASLEEP, "a fresh sleep must not owe a heartbeat")

    def test_waking_resets_the_burst(self):
        v, clock = make()
        self.sleep_it(v, clock)
        clock.advance(3601)
        v.tick()
        self.assertEqual(v.status()["burst_used"], 0)
        for _ in range(6):
            self.assertTrue(v.may_call())
            v.note_call()

    def test_operator_outranks_a_pending_salience_wake(self):
        v, clock = make()
        self.sleep_it(v, clock)
        clock.advance(400)
        v.observe({"memory": {"percent": 10}})
        v.observe({"memory": {"percent": 90}})
        v.note_activity("ui message")
        v.tick()
        self.assertIn("operator", v.wake_reason)

    def test_operator_refreshes_the_burst_while_already_awake(self):
        """Answering a person must not fail because the budget happened to run out."""
        v, clock = make()
        for _ in range(6):
            v.may_call()
            v.note_call()
        self.assertFalse(v.may_call())
        v.note_activity("ui message")
        v.tick()
        self.assertTrue(v.may_call())


class TestQuietHours(unittest.TestCase):
    def test_quiet_hours_use_the_slower_heartbeat(self):
        # 03:00 UTC, which is 04:00 London in summer and 03:00 in winter;
        # either way inside the 22-07 window.
        clock = FakeClock(1_758_337_200.0)   # 2025-09-20T03:00:00Z
        v, clock = make(clock=clock)
        self.assertTrue(v.status()["quiet_hours_now"])
        for _ in range(6):
            v.may_call()
            v.note_call()
        clock.advance(181)
        v.tick()
        self.assertEqual(v.state, ASLEEP)
        clock.advance(3601)                  # past the day heartbeat...
        v.tick()
        self.assertEqual(v.state, ASLEEP, "the night heartbeat should be slower")
        clock.advance(10_801 - 3601)
        v.tick()
        self.assertEqual(v.state, AWAKE)

    def test_quiet_heartbeat_is_never_faster_than_the_day_one(self):
        v, _ = make(heartbeat_s=7200, quiet_heartbeat_s=60)
        self.assertGreaterEqual(v.quiet_heartbeat_s, v.heartbeat_s)


class TestTheGate(unittest.TestCase):
    def test_no_calls_while_asleep(self):
        v, clock = make()
        for _ in range(6):
            v.may_call()
            v.note_call()
        clock.advance(181)
        v.tick()
        self.assertEqual(v.state, ASLEEP)
        for _ in range(100):
            clock.advance(30)
            self.assertFalse(v.may_call(), "a sleeping agent must not call the model")

    def test_burst_is_a_ceiling(self):
        v, _ = make(burst_calls=3)
        allowed = 0
        for _ in range(10):
            if v.may_call():
                v.note_call()
                allowed += 1
        self.assertEqual(allowed, 3)

    def test_disabled_vigil_never_blocks(self):
        v, clock = make(enabled=False)
        for _ in range(50):
            clock.advance(600)
            self.assertTrue(v.may_call())


class TestWarmTime(unittest.TestCase):
    """Warm minutes, not call count, are what the invoice is a function of."""

    def test_a_single_call_costs_one_warm_window(self):
        v, clock = make()
        v.may_call()
        v.note_call()
        clock.advance(1000)
        self.assertAlmostEqual(v.warm_seconds(), 300.0, places=1)

    def test_clustered_calls_cost_little_more_than_one(self):
        v, clock = make(burst_calls=6)
        for _ in range(6):
            v.may_call()
            v.note_call()
            clock.advance(10)
        clock.advance(1000)
        # Six calls ten seconds apart: 50s of spread plus one warm window.
        self.assertAlmostEqual(v.warm_seconds(), 350.0, places=1)

    def test_the_old_cadence_would_have_been_warm_the_whole_time(self):
        """The regression this whole module exists to prevent.

        One call every 300s with a 300s warm window is continuous warmth: the
        copy never goes cold, so the meter never stops. Asserted here with the
        gate disabled, to show the cost of the behaviour we replaced.
        """
        v, clock = make(enabled=False)
        v = Vigil({"enabled": True, "warm_window_s": 300.0, "burst_calls": 10_000,
                   "idle_after_s": 10_000, "burst_window_s": 0}, logger=LOG, clock=clock)
        start = clock.t
        for _ in range(12):
            v.note_call()
            clock.advance(300)
        elapsed = clock.t - start
        self.assertAlmostEqual(v.warm_seconds(), elapsed, places=1)

    def test_sleeping_stops_the_meter(self):
        v, clock = make()
        for _ in range(6):
            v.may_call()
            v.note_call()
        clock.advance(181)
        v.tick()
        self.assertEqual(v.state, ASLEEP)
        clock.advance(7200)                  # two hours asleep
        warm = v.warm_seconds()
        self.assertLess(warm, 500.0,
                        "two hours asleep must not accrue two hours of warm time")

    def test_warm_fraction_reported(self):
        v, clock = make()
        v.may_call()
        v.note_call()
        clock.advance(3600)
        st = v.status()
        self.assertLess(st["warm_fraction"], 0.2)
        self.assertGreater(st["warm_fraction"], 0.0)


class TestDigest(unittest.TestCase):
    def test_noise_inside_a_band_is_not_a_change(self):
        a = digest({"memory": {"percent": 71.2}})
        b = digest({"memory": {"percent": 71.4}})
        self.assertEqual(a, b)
        self.assertEqual(salient_change(a, b), "")

    def test_crossing_a_band_is_a_change(self):
        a = digest({"memory": {"percent": 71.0}})
        b = digest({"memory": {"percent": 88.0}})
        self.assertNotEqual(a, b)
        self.assertIn("changed", salient_change(a, b))

    def test_an_error_appearing_is_always_material(self):
        a = digest({"memory": {"percent": 50}})
        b = digest({"memory": {"percent": 50}, "storage_error": "device gone"})
        reason = salient_change(a, b)
        self.assertIn("storage_error", reason)
        self.assertIn("appeared", reason)

    def test_an_error_clearing_is_material_too(self):
        a = digest({"storage_error": "device gone"})
        b = digest({})
        self.assertIn("cleared", salient_change(a, b))

    def test_timestamp_and_cycle_are_not_changes(self):
        a = digest({"timestamp": 1.0, "cycle": 1, "memory": {"percent": 50}})
        b = digest({"timestamp": 999.0, "cycle": 400, "memory": {"percent": 50}})
        self.assertEqual(salient_change(a, b), "")

    def test_a_device_appearing_is_a_change(self):
        a = digest({"storage": [{"name": "nvme0", "percent": 10}]})
        b = digest({"storage": [{"name": "nvme0", "percent": 10},
                                {"name": "nvme1", "percent": 10}]})
        self.assertNotEqual(salient_change(a, b), "")

    def test_first_observation_never_wakes(self):
        """There is nothing to compare against, so nothing has changed."""
        self.assertEqual(salient_change(None, digest({"memory": {"percent": 90}})), "")

    def test_observe_does_not_wake_on_the_first_look(self):
        v, _ = make()
        self.assertEqual(v.observe({"memory": {"percent": 90}}), "")


class TestWhatTheModelIsTold(unittest.TestCase):
    def test_it_is_told_that_it_sleeps(self):
        v, _ = make()
        text = v.describe()["you_sleep"]
        self.assertIn("put to sleep", text)
        self.assertIn("rest, not a fault", text)

    def test_it_is_told_how_long_it_slept_and_why_it_woke(self):
        v, clock = make()
        for _ in range(6):
            v.may_call()
            v.note_call()
        clock.advance(181)
        v.tick()
        clock.advance(3601)
        v.tick()
        d = v.describe()
        self.assertGreater(d["you_were_asleep_for_s"], 3500)
        self.assertIn("heartbeat", d["why_you_are_awake"])

    def test_it_is_warned_on_its_last_call(self):
        v, _ = make(burst_calls=2)
        v.may_call(); v.note_call()
        self.assertIn("note", v.describe())
        self.assertIn("last call", v.describe()["note"])

    def test_no_note_while_there_is_budget_left(self):
        v, _ = make(burst_calls=6)
        v.may_call(); v.note_call()
        self.assertNotIn("note", v.describe())


class TestConfiguration(unittest.TestCase):
    def test_minimum_sleep_is_floored_at_the_warm_window(self):
        """A shorter sleep saves nothing, so config may not ask for one."""
        v, _ = make(warm_window_s=300.0, min_sleep_s=5.0)
        self.assertEqual(v.min_sleep_s, 300.0)

    def test_build_returns_null_vigil_when_disabled(self):
        self.assertIsInstance(build({"enabled": False}), NullVigil)
        self.assertIsInstance(build(None), NullVigil)
        self.assertIsInstance(build({}), NullVigil)

    def test_build_returns_a_vigil_when_enabled(self):
        self.assertIsInstance(build({"enabled": True}), Vigil)

    def test_null_vigil_is_always_awake_and_never_blocks(self):
        n = NullVigil()
        self.assertTrue(n.may_call())
        self.assertEqual(n.state, AWAKE)
        self.assertIsNone(n.tick())
        self.assertIsNone(n.take_transition())
        self.assertEqual(n.describe(), {})
        self.assertEqual(n.warm_seconds(), 0.0)
        n.note_activity(); n.note_work(); n.note_idle(); n.note_call()
        self.assertEqual(n.observe({"anything": 1}), "")


class TestTransitions(unittest.TestCase):
    def test_transitions_are_queued_for_the_ledger(self):
        v, clock = make()
        for _ in range(6):
            v.may_call()
            v.note_call()
        clock.advance(181)
        v.tick()
        clock.advance(3601)
        v.tick()
        kinds = []
        while True:
            t = v.take_transition()
            if t is None:
                break
            kinds.append(t["state"])
        self.assertEqual(kinds, [ASLEEP, AWAKE])

    def test_stats_count_wakes_by_reason(self):
        v, clock = make()
        for _ in range(6):
            v.may_call()
            v.note_call()
        clock.advance(181)
        v.tick()
        clock.advance(3601)
        v.tick()
        self.assertEqual(v.stats["wakes_heartbeat"], 1)
        self.assertEqual(v.stats["sleeps"], 1)
        self.assertGreater(v.stats["slept_s"], 3500)


class FakeLedger:
    """Records everything, gates nothing."""

    enabled = True
    available = True

    def __init__(self):
        self.entries = []

    def record(self, kind, body):
        self.entries.append((kind, body))
        return True

    def gate(self):
        return None

    def tick(self, force=False):
        pass

    def status(self):
        return {"enabled": True, "available": True}

    def kinds(self, kind):
        return [b for k, b in self.entries if k == kind]


class TestWiredIntoTheAgent(unittest.TestCase):
    """The parts that only matter once the vigil is attached to real objects."""

    def build(self, **sleep):
        from jarvis.agent.core import AgentCore
        cfg = {"enabled": True, "burst_calls": 2, "idle_after_s": 60,
               "warm_window_s": 300, "heartbeat_s": 3600, "burst_window_s": 120}
        cfg.update(sleep)
        ledger = FakeLedger()
        agent = AgentCore({"name": "t", "profile": "cloud", "sleep": cfg},
                          {"display": None, "input": None, "memory": None, "storage": None},
                          LOG, ledger=ledger)
        agent.planner._boot_tasks_generated = True
        return agent, ledger

    def test_the_agent_gets_a_real_vigil_when_configured(self):
        agent, _ = self.build()
        self.assertTrue(agent.vigil.enabled)
        self.assertEqual(agent.vigil.state, AWAKE)

    def test_the_agent_gets_a_null_vigil_when_not_configured(self):
        from jarvis.agent.core import AgentCore
        agent = AgentCore({"name": "t", "profile": "cloud"},
                          {"display": None, "input": None, "memory": None, "storage": None}, LOG)
        self.assertIsInstance(agent.vigil, NullVigil)
        self.assertTrue(agent.vigil.may_call())

    def test_sleep_and_wake_reach_the_ledger(self):
        agent, ledger = self.build()
        clock = FakeClock()
        agent.vigil = Vigil({"enabled": True, "burst_calls": 1, "idle_after_s": 60,
                             "warm_window_s": 300, "heartbeat_s": 3600},
                            logger=LOG, clock=clock)
        agent.vigil.may_call()
        agent.vigil.note_call()
        clock.advance(61)
        agent.vigil.tick()
        agent._record_vigil()
        recorded = ledger.kinds("vigil")
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["state"], ASLEEP)
        self.assertIn("cycle", recorded[0])

    def test_vigil_is_a_registered_ledger_kind(self):
        """An unregistered kind is refused, and under fail_closed that stops the agent."""
        from jarvis.ledger import chain
        self.assertIn("vigil", chain.KINDS)

    def test_note_operator_wakes_a_sleeping_agent(self):
        agent, ledger = self.build()
        clock = FakeClock()
        agent.vigil = Vigil({"enabled": True, "burst_calls": 1, "idle_after_s": 60,
                             "warm_window_s": 300, "heartbeat_s": 3600},
                            logger=LOG, clock=clock)
        agent.vigil.may_call()
        agent.vigil.note_call()
        clock.advance(61)
        agent.vigil.tick()
        self.assertEqual(agent.vigil.state, ASLEEP)
        agent.note_operator("chat")
        self.assertEqual(agent.vigil.state, AWAKE)
        self.assertIn("operator", agent.vigil.wake_reason)
        self.assertTrue(any(b["state"] == AWAKE for b in ledger.kinds("vigil")))

    def test_adding_a_goal_wakes_it(self):
        agent, _ = self.build()
        clock = FakeClock()
        agent.vigil = Vigil({"enabled": True, "burst_calls": 1, "idle_after_s": 60,
                             "warm_window_s": 300}, logger=LOG, clock=clock)
        agent.vigil.may_call()
        agent.vigil.note_call()
        clock.advance(61)
        agent.vigil.tick()
        self.assertEqual(agent.vigil.state, ASLEEP)
        agent.add_goal("look at the disk", 3)
        self.assertEqual(agent.vigil.state, AWAKE)

    def test_status_reports_warm_time(self):
        agent, _ = self.build()
        status = agent.get_status()
        self.assertIn("vigil", status)
        self.assertIn("warm_seconds", status["vigil"])

    def test_a_cycle_runs_and_records_state(self):
        agent, _ = self.build()
        result = agent.run_cycle()
        self.assertIn("vigil", result)
        self.assertIn(result["vigil"], (AWAKE, ASLEEP))


class TestTheBrainGate(unittest.TestCase):
    """should_plan must refuse while asleep, and only for the vigil's reason."""

    def make_brain(self, **cfg):
        from jarvis.brain.bedrock import BedrockBrain
        from jarvis.brain.llm import BaseBrain

        class OfflineBrain(BaseBrain):
            provider = "test"

            def _make_client(self):
                return object()          # present, never called

            def _complete(self, *a, **k):
                raise AssertionError("no call should be made in this test")

            def _handle_error(self, exc):
                return None

        config = {"max_calls_per_hour": 100}
        config.update(cfg)
        return OfflineBrain(config, LOG)

    def test_a_bare_brain_is_always_awake(self):
        brain = self.make_brain()
        self.assertTrue(brain.should_plan(1))

    def test_an_attached_sleeping_vigil_closes_the_gate(self):
        clock = FakeClock()
        brain = self.make_brain()
        v = Vigil({"enabled": True, "burst_calls": 1, "idle_after_s": 60,
                   "warm_window_s": 300, "heartbeat_s": 3600}, logger=LOG, clock=clock)
        brain.attach_vigil(v)
        self.assertTrue(brain.should_plan(1))
        v.note_call()
        clock.advance(61)
        v.tick()
        self.assertEqual(v.state, ASLEEP)
        self.assertFalse(brain.should_plan(2))

    def test_taking_budget_marks_the_copy_warm(self):
        """Every kind of call warms the copy, so all of them go through one place."""
        clock = FakeClock()
        brain = self.make_brain()
        v = Vigil({"enabled": True, "warm_window_s": 300, "burst_calls": 10},
                  logger=LOG, clock=clock)
        brain.attach_vigil(v)
        self.assertTrue(brain._take_budget())
        clock.advance(1000)
        self.assertAlmostEqual(v.warm_seconds(), 300.0, places=1)

    def test_the_gate_is_asked_last(self):
        """A call blocked by the hourly budget was never the vigil's to suppress."""
        clock = FakeClock()
        brain = self.make_brain(max_calls_per_hour=1)
        v = Vigil({"enabled": True, "burst_calls": 10, "warm_window_s": 300},
                  logger=LOG, clock=clock)
        brain.attach_vigil(v)
        brain._take_budget()                    # hourly budget now spent
        self.assertFalse(brain.should_plan(1))
        self.assertEqual(v.stats["calls_suppressed"], 0)

    def test_the_model_is_told_it_sleeps(self):
        from jarvis.agent.core import AgentCore
        clock = FakeClock()
        brain = self.make_brain()
        v = Vigil({"enabled": True, "burst_calls": 2, "warm_window_s": 300},
                  logger=LOG, clock=clock)
        brain.attach_vigil(v)
        agent = AgentCore({"name": "t", "profile": "cloud"},
                          {"display": None, "input": None, "memory": None, "storage": None},
                          LOG, brain=brain)
        agent.planner._boot_tasks_generated = True
        context = brain.build_context(agent, {})
        self.assertIn("rest", context)
        self.assertIn("put to sleep", context["rest"]["you_sleep"])

    def test_no_rest_block_without_a_vigil(self):
        from jarvis.agent.core import AgentCore
        brain = self.make_brain()
        agent = AgentCore({"name": "t", "profile": "cloud"},
                          {"display": None, "input": None, "memory": None, "storage": None},
                          LOG, brain=brain)
        agent.planner._boot_tasks_generated = True
        self.assertNotIn("rest", brain.build_context(agent, {}))


if __name__ == "__main__":
    unittest.main()
