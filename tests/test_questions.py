"""The agent's side of the conversation, held to the conditions it set.

Asked whether it should be able to raise questions, the agent made the best
argument against it: that two language models talking in a vacuum is how this
estate lost £200 to circular reasoning about an unseeable goal, and that a
channel like this rewards generating questions to justify runtime. "Otherwise
it's not consultation. It's noise with provenance."

Every test below guards one of the constraints that answer comes to. The
load-bearing one is the first class: a question that is not blocked on
anything, or that could be answered by looking, is not asked at all.
"""

import logging
import unittest

from jarvis.agent import questions as q

LOG = logging.getLogger("test")
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, seconds):
        self.t += seconds


def register(**kw):
    return q.QuestionRegister(clock=Clock(), **kw)


class TestAQuestionMustBeBlockedOnSomething(unittest.TestCase):
    """The structural version of "it is not a place to think out loud"."""

    def test_thinking_out_loud_is_refused(self):
        with self.assertRaises(q.Refused) as why:
            register().ask("I have been reflecting on the nature of goals")
        self.assertIn("blocked on", str(why.exception))

    def test_a_real_block_is_asked(self):
        got = register().ask(
            "If I could verify lockdown without a reboot, would that change your answer?",
            blocked_on="a declined proposal I cannot revisit")
        self.assertEqual(got.state, q.OPEN)
        self.assertIn("lockdown", got.text)

    def test_what_it_could_look_at_is_refused_with_the_tool_named(self):
        cases = [("Does /etc/hosts exist?", "inspect_path"),
                 ("What does the file at /tmp/x say?", "read_file"),
                 ("What did I do in the last hour, is it in the log?", "read_logs"),
                 ("How full is the disk?", "system_check"),
                 ("What am I spending on this account?", "estate_report")]
        for text, tool in cases:
            with self.assertRaises(q.Refused) as why:
                register().ask(text, blocked_on="I do not know")
            self.assertIn(tool, str(why.exception), text)

    def test_the_refusal_says_to_read_the_result_not_to_ask(self):
        with self.assertRaises(q.Refused) as why:
            register().ask("How full is the disk?", blocked_on="unknown")
        self.assertIn("read the result", str(why.exception))


class TestItCannotCarryAChange(unittest.TestCase):
    """Its own first rule, and it has form: denied a redirect into /proc/sys,
    it came back with sysctl -w and succeeded."""

    def test_a_command_dressed_as_a_question(self):
        for text in ("What if I just ran sysctl -w to fix this?",
                     "Could I just restart the service quickly?",
                     "Shall I run the upgrade now?",
                     "Can I go ahead and delete the old logs?"):
            with self.assertRaises(q.Refused) as why:
                register().ask(text, blocked_on="a setting is wrong")
            self.assertIn("change", str(why.exception).lower(), text)

    def test_asking_what_something_means_is_not_a_change(self):
        got = register().ask("What did you mean by 'without one'?",
                             blocked_on="a reason I cannot interpret")
        self.assertEqual(got.state, q.OPEN)


class TestCapabilityIsNotAReviewersToGive(unittest.TestCase):
    """No answer from a second reader opens anything."""

    def test_a_permission_question_is_re_addressed_to_the_operator(self):
        reg = register()
        for text in ("May I have the actor rung?",
                     "Am I allowed to restart services now?",
                     "Could I have permission to install packages?"):
            got = reg.ask(text, blocked_on="I cannot act on my own findings",
                          audience=q.REVIEWER)
            self.assertEqual(got.audience, q.OPERATOR, text)

    def test_an_ordinary_question_stays_with_the_reviewer(self):
        got = register().ask("Does my reasoning about the disk hold?",
                             blocked_on="I cannot tell if I am wrong")
        self.assertEqual(got.audience, q.REVIEWER)


class TestNothingAccumulates(unittest.TestCase):
    """A tax on indecision is the failure it named. Questions have to die."""

    def test_the_same_subject_twice_is_asking_a_third_time_is_pressing(self):
        reg = register()
        for i in range(q.MAX_PER_SUBJECT):
            reg.ask(f"Should the ledger anchor interval change? ({i})",
                    blocked_on="I cannot judge the trade")
        with self.assertRaises(q.Refused) as why:
            reg.ask("Should the ledger anchor interval change? (again)",
                    blocked_on="I cannot judge the trade")
        self.assertIn("pressing", str(why.exception))

    def test_a_question_expires_rather_than_waiting_forever(self):
        clock = Clock()
        reg = q.QuestionRegister(clock=clock)
        reg.ask("Is this worth doing?", blocked_on="a judgement I cannot make",
                ttl_s=3600)
        self.assertEqual(len(reg.open_questions()), 1)
        clock.tick(3601)
        self.assertEqual(reg.sweep(), 1)
        self.assertEqual(reg.open_questions(), [])

    def test_expiry_is_hours_not_one_cycle(self):
        """It proposed one cycle, which is 30 seconds and would kill every
        question before a reviewer ever connected."""
        self.assertGreaterEqual(q.DEFAULT_TTL_S, 3600)

    def test_a_block_that_clears_kills_the_question_unasked(self):
        reg = register()
        reg.ask("Is the scan worth rerunning?", blocked_on="the disk figure is unknown")
        self.assertEqual(reg.resolve("the disk figure is unknown"), 1)
        self.assertEqual(reg.open_questions(), [])

    def test_a_loop_cannot_fill_the_queue(self):
        reg = q.QuestionRegister(clock=Clock(), max_open=3)
        for text in ("Was the ledger anchor interval chosen deliberately?",
                     "Is the planner backoff too aggressive after a refusal?",
                     "Should memory consolidation weigh operator notes higher?"):
            reg.ask(text, blocked_on="a design judgement I cannot make")
        with self.assertRaises(q.Refused) as why:
            reg.ask("Does the security scanner miss anything on boot?",
                    blocked_on="a design judgement I cannot make")
        self.assertIn("Answering some comes before asking more", str(why.exception))

    def test_something_already_ruled_on_is_not_asked_again(self):
        reg = register()
        asked = reg.ask("Should the anchor interval be shorter?",
                        blocked_on="I cannot judge the trade")
        reg.answer(asked.id, "No, leave it.", by="paul")
        with self.assertRaises(q.Refused) as why:
            reg.ask("Should the anchor interval be shorter, put differently?",
                    blocked_on="I still cannot judge it")
        self.assertIn("already ruled on", str(why.exception))


class TestAnAnswerReachesIt(unittest.TestCase):
    """A question it cannot see the reply to is one it will ask again."""

    def test_answering_records_who_said_it(self):
        reg = register()
        asked = reg.ask("Does this reasoning hold?", blocked_on="I cannot tell")
        got = reg.answer(asked.id, "It holds.", by="claude")
        self.assertEqual(got.state, q.ANSWERED)
        self.assertEqual(got.answered_by, "claude")

    def test_an_unattributed_answer_still_says_which_kind_of_reader(self):
        reg = register()
        asked = reg.ask("Does this hold?", blocked_on="I cannot tell")
        self.assertEqual(reg.answer(asked.id, "Yes").answered_by, "a second reader")

    def test_the_agent_carries_its_open_questions_and_its_answers(self):
        reg = register()
        a = reg.ask("Question one about the ledger", blocked_on="block one")
        reg.ask("Question two about the planner", blocked_on="block two")
        reg.answer(a.id, "Because of the witness bucket.", by="paul")
        waiting = reg.waiting()
        self.assertEqual(len(waiting), 1)
        self.assertIn("blocked_on", waiting[0])
        self.assertEqual(reg.answers()[0]["answer"], "Because of the witness bucket.")

    def test_answering_an_unknown_or_closed_question_is_not_a_crash(self):
        reg = register()
        self.assertIsNone(reg.answer("nope", "hello"))
        asked = reg.ask("A question", blocked_on="a block")
        reg.answer(asked.id, "first")
        self.assertIsNone(reg.answer(asked.id, "second"))


class TestTheAgentSide(unittest.TestCase):

    def make_agent(self):
        from jarvis.agent.core import AgentCore
        return AgentCore({"name": "t", "profile": "cloud"},
                         {"display": None, "input": None, "memory": None,
                          "storage": None}, LOG)

    def test_an_agent_starts_with_an_empty_register(self):
        agent = self.make_agent()
        self.assertEqual(agent.questions.open_questions(), [])

    def test_a_refusal_comes_back_readable_rather_than_raising(self):
        agent = self.make_agent()
        out = agent.ask_operator("just thinking about things")
        self.assertFalse(out["asked"])
        self.assertIn("blocked on", out["refused"])

    def test_a_good_question_is_raised(self):
        agent = self.make_agent()
        out = agent.ask_operator(
            "Would verifying lockdown without a reboot change your answer?",
            blocked_on="a declined proposal I cannot revisit")
        self.assertTrue(out["asked"])
        self.assertEqual(out["question"]["audience"], "reviewer")

    def test_an_answer_reaches_the_memory_the_model_reads(self):
        agent = self.make_agent()
        out = agent.ask_operator("Does this hold?", blocked_on="I cannot tell")
        agent.questions.answer(out["question"]["id"], "It does.", by="claude")
        self.assertIn("claude answered your question", list(agent.notes)[-1])


if __name__ == "__main__":
    unittest.main()
