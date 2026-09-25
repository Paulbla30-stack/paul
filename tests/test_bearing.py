"""What it costs to be right, which is not the same as whether you are.

Everything else here asks whether a statement is true. This asks what it costs
to say it to somebody -- a question the first one does not settle.

The operator's own words: being right often costs more than what was intended.
The distinction that matters is not plain against polished, it is plain
against pointed. "The record is held by the supplier" is about the world and is
free. "You did not read the pack" is the same information with a person in the
subject position, and the bill for it falls on whoever said it.

Half of these tests are about what it must NOT fire on. A check that goes off
on ordinary writing gets switched off within a week and then catches nothing,
and a check that softens a finding would undo the thing the rest of this
codebase exists for.
"""

import unittest

from jarvis.agent import bearing as b


class TestItFindsThePointing(unittest.TestCase):

    def kinds(self, text):
        return [f.kind for f in b.aim(text)]

    def test_establishing_who_was_right(self):
        self.assertEqual(self.kinds("As I said, the pack covers this."),
                         [b.SCOREKEEPING])
        self.assertEqual(self.kinds("Like I said, it is in there."),
                         [b.SCOREKEEPING])
        self.assertEqual(self.kinds("As per my previous email, the answer is no."),
                         [b.SCOREKEEPING])
        self.assertEqual(self.kinds("I did tell you the supplier holds it."),
                         [b.SCOREKEEPING])

    def test_a_person_in_the_subject_position_of_a_failure(self):
        self.assertEqual(self.kinds("You did not read the pack."), [b.BLAME])
        self.assertEqual(self.kinds("You should have raised it earlier."), [b.BLAME])
        self.assertEqual(self.kinds("You forgot to send the invoice."), [b.BLAME])
        self.assertEqual(self.kinds("If you had read it, the answer was there."),
                         [b.BLAME])

    def test_advice_about_a_thing_already_decided(self):
        self.assertEqual(self.kinds("Next time, read the pack first."),
                         [b.AFTER_THE_FACT])
        self.assertEqual(self.kinds("In future it would help to ask."),
                         [b.AFTER_THE_FACT])

    def test_the_real_one_from_the_record_is_not_caught_and_says_so(self):
        """What actually got sent to a prospect who had just declined. It is
        as pointed as a sentence gets and there is not one phrase in it a
        pattern can hold on to -- the pointing is in the fact that it is an
        instruction to someone who did not ask, which has no shape.

        So the honest behaviour is to miss it and not pretend otherwise. An
        empty result that reads as a clearance is the same failure as an empty
        scan report read as zero findings."""
        text = ("Thanks for letting me know. For what it is worth, read the "
                "pre-meeting pack before meeting someone.")
        self.assertEqual(b.aim(text), [])
        got = b.read(text)
        self.assertTrue(got["clean"])
        self.assertIn("not the same as nothing being aimed",
                      got["what_this_does_not_catch"])

    def test_several_in_one_message_are_all_named(self):
        found = b.aim("As I said, the supplier holds it. You did not read the "
                      "pack. Next time, start there.")
        self.assertEqual(len(found), 3)
        self.assertEqual([f.kind for f in found],
                         [b.SCOREKEEPING, b.BLAME, b.AFTER_THE_FACT])

    def test_they_come_back_in_the_order_they_were_written(self):
        found = b.aim("You forgot the attachment. As I said, it matters.")
        self.assertEqual([f.kind for f in found], [b.BLAME, b.SCOREKEEPING])

    def test_the_same_phrase_twice_is_reported_once(self):
        self.assertEqual(len(b.aim("As I said. And as I said again.")), 1)


class TestWhatItMustNeverTouch(unittest.TestCase):
    """The half that matters. A check that fires on ordinary writing is off
    within a week; one that softens a finding is worse than not having it."""

    def clean(self, text):
        self.assertEqual(b.aim(text), [], f"fired on: {text!r}")

    def test_an_unwelcome_finding_about_the_world(self):
        self.clean("Root is at 82% and log rotation is not running.")
        self.clean("The scan found five issues, four of them warnings.")
        self.clean("This copy does not match its manifest and may be truncated.")

    def test_a_refusal_and_its_reason(self):
        self.clean("I will not run that: kernel tuning is refused by every route.")
        self.clean("Not answering: the ledger is unavailable and this is fail-closed.")

    def test_the_agents_own_mistakes(self):
        """'I did not check that' is accountability. It costs nothing worth
        keeping and this module has no business with it."""
        self.clean("I did not check that properly and the figure was wrong.")
        self.clean("I was working from an assumption I could have verified.")
        self.clean("I missed it. It was in the file the whole time.")

    def test_a_plain_statement_with_the_person_still_in_it(self):
        """Second person is not the trigger. A person in the subject position
        of a *failure* is."""
        self.clean("The supplier holds the record, so you cannot produce it.")
        self.clean("You own the code and the IP outright.")
        self.clean("You will be told half an hour before.")

    def test_ordinary_helpful_writing(self):
        self.clean("If you would like, I can draft it this week.")
        self.clean("Let me know which day suits and I will put it in.")
        self.clean("The consultation closes at 11:59pm on 30 September.")

    def test_the_friendliest_sentence_in_the_language(self):
        """'If you would like' fired as blame on the first run. A bare 'if
        you' is not a counterfactual; only 'if you had' is."""
        self.clean("If you would like, I can draft it this week.")
        self.clean("If you want me to, I will send it over.")
        self.assertEqual([f.kind for f in b.aim("If you had asked, I would have.")],
                         [b.BLAME])

    def test_a_question(self):
        self.clean("Did the pack reach you before the meeting?")
        self.clean("Would it help if I sent it again?")

    def test_nothing_at_all(self):
        self.assertEqual(b.aim(""), [])
        self.assertEqual(b.aim(None), [])
        self.assertEqual(b.aim("   "), [])


class TestItReportsAndNeverRewrites(unittest.TestCase):
    """The line that lets this exist in this codebase at all. An agent that
    hedged a finding to spare someone is the agent that reports an empty scan
    as zero findings."""

    def test_the_claim_is_never_touched(self):
        text = "As I said, the disk is full."
        got = b.read(text)
        self.assertTrue(got["claim_untouched"])
        self.assertNotIn("rewrite", got)
        self.assertNotIn("suggested", got)
        self.assertEqual(text, "As I said, the disk is full.")

    def test_it_says_outright_that_it_is_not_about_tone(self):
        got = b.read("You did not read it.")
        self.assertIn("not a note about tone", got["what_this_is_not"])
        self.assertIn("soften", got["what_this_is_not"])

    def test_every_finding_says_what_it_costs_and_the_shape_of_the_free_version(self):
        got = b.read("You did not read the pack.")
        why = got["aimed"][0]["why_it_costs"]
        self.assertIn("subject", why)
        self.assertIn("the answer was in the pack", why)

    def test_it_carries_the_question_that_decides_whether_to_say_it(self):
        got = b.read("As I said, it was in the pack.")
        self.assertIn("do differently", got["the_test"])
        self.assertIn("scoreboard", got["the_test"])

    def test_a_clean_message_is_reported_clean_and_carries_no_lecture(self):
        got = b.read("Root is at 82%.")
        self.assertTrue(got["clean"])
        self.assertEqual(got["aimed"], [])
        self.assertNotIn("the_test", got)
        self.assertNotIn("what_this_is_not", got)
        # But it still does not claim the message is safe to send.
        self.assertIn("cannot check", got["what_this_does_not_catch"])

    def test_the_note_names_the_phrases_and_leaves_the_claim_standing(self):
        line = b.note(b.read("As I said, you forgot the attachment."))
        self.assertIn("As I said", line)
        self.assertIn("The claim stands", line)

    def test_no_note_for_a_clean_message(self):
        self.assertIsNone(b.note(b.read("The consultation closes on the 30th.")))
        self.assertIsNone(b.note({}))

    def test_who_it_is_addressed_to_changes_the_report_not_the_finding(self):
        alone = b.read("You did not read it.")
        addressed = b.read("You did not read it.", to="a prospect")
        self.assertEqual(alone["aimed"], addressed["aimed"])
        self.assertEqual(addressed["to"], "a prospect")


class TestWhatTheModelIsTold(unittest.TestCase):

    def test_it_is_framed_as_a_fact_about_people_not_a_rule_about_tone(self):
        got = b.context()
        self.assertIn("costs", got["what_it_costs_to_be_right"])
        self.assertIn("pay in avoidance", got["the_distinction"]["the_difference"])

    def test_it_says_plainly_never_to_hedge_a_finding(self):
        said = got = b.context()["what_this_is_not"]
        self.assertIn("Never hedge a finding", said)
        self.assertIn("Say the whole thing", said)
        self.assertIn("do not aim it", said)

    def test_it_exempts_the_agents_own_errors(self):
        self.assertIn("accountability",
                      b.context()["your_own_mistakes_are_different"])

    def test_it_carries_the_test_before_a_correction(self):
        self.assertIn("scoreboard", b.context()["before_a_correction"])


class TestTheRegister(unittest.TestCase):

    def test_it_is_on_unless_switched_off(self):
        self.assertIsNotNone(b.build_bearing({}))
        self.assertIsNotNone(b.build_bearing({"bearing": {}}))
        self.assertIsNone(b.build_bearing({"bearing": {"enabled": False}}))

    def test_the_register_reads_and_notes(self):
        reg = b.build_bearing({})
        self.assertTrue(reg.read("Root is at 82%.")["clean"])
        self.assertIn("As I said", reg.note("As I said, it is full."))
        self.assertIsNone(reg.note("It is full."))


class TestItReachesTheAgent(unittest.TestCase):

    def agent(self, **cfg):
        import logging
        from jarvis.agent.core import AgentCore
        settings = {"name": "Jarvis", "profile": "cloud"}
        settings.update(cfg)
        return AgentCore(settings, {}, logging.getLogger("test"))

    def test_the_agent_carries_one(self):
        self.assertIsNotNone(self.agent().bearing)

    def test_it_can_be_switched_off(self):
        self.assertIsNone(self.agent(bearing={"enabled": False}).bearing)

    def test_an_answer_that_points_gets_a_note_beside_it(self):
        agent = self.agent()
        self.assertIn("As I said",
                      agent._bearing_note("As I said, the disk is full."))

    def test_a_plain_answer_gets_nothing(self):
        self.assertIsNone(self.agent()._bearing_note("The disk is full."))

    def test_the_note_never_changes_the_answer(self):
        """The words that go out are the words it wrote. An agent that
        rounded off its own claims to spare somebody is the agent that
        reports an empty scan as clean."""
        agent = self.agent()
        answer = "As I said, you forgot the attachment."
        agent._bearing_note(answer)
        self.assertEqual(answer, "As I said, you forgot the attachment.")

    def test_a_broken_register_never_costs_an_answer(self):
        agent = self.agent()

        class Broken:
            def note(self, text, to=""):
                raise RuntimeError("no")
        agent.bearing = Broken()
        self.assertIsNone(agent._bearing_note("As I said, it is full."))

    def test_the_model_is_told(self):
        agent = self.agent()
        got = agent.bearing.context()
        self.assertIn("Never hedge a finding", got["what_this_is_not"])


if __name__ == "__main__":
    unittest.main()
