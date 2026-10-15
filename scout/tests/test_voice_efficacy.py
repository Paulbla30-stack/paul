"""A claim about clinical effect has to say whose claim it is.

The rule came from the agent, reviewing the design for the Facebook page:
"no modality may introduce causal claims not explicitly in the source". One
rule across text, image and audio beats a separate judgement per medium.

What is enforceable here is the attribution half. Whether the source actually
supports the claim cannot be checked by a regex and is the operator's job at
the approval gate; these tests hold the half that can be mechanical.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import voice  # noqa: E402


def check(text):
    # links_own_work and known_dois are pinned so these tests exercise the
    # efficacy rule alone, not disclosure or citation checking.
    return voice.check(text, links_own_work=False, known_dois=set())


class UnattributedTest(unittest.TestCase):
    """Asserting effect in Paul's own voice. He is on the NMC register."""

    def test_the_example_the_agent_gave(self):
        r = check("This could change how we treat depression.")
        self.assertFalse(r.ok)
        self.assertIn("unattributed", " ".join(r.failures))

    def test_telling_clinicians_what_to_do(self):
        self.assertFalse(check("Clinicians should adopt this now.").ok)

    def test_asserting_proof(self):
        self.assertFalse(check("It proves that the tool reduces risk of harm.").ok)

    def test_asserting_efficacy(self):
        self.assertFalse(check("Evidence shows this is effective for older adults.").ok)

    def test_recommending(self):
        self.assertFalse(check("Recommended for community mental health teams.").ok)

    def test_the_failure_names_the_phrase_it_caught(self):
        r = check("Clinicians should adopt this now.")
        self.assertIn("Clinicians should", " ".join(r.failures))


class AttributedTest(unittest.TestCase):
    """Reporting what a paper says. Defensible on his register."""

    def test_the_counter_example_the_agent_gave(self):
        self.assertTrue(check("The authors propose a model for depression treatment.").ok)

    def test_the_same_strong_claim_attributed_passes(self):
        self.assertTrue(
            check("The paper reports that it could change how we treat depression.").ok)

    def test_a_finding_attributed_to_the_study(self):
        self.assertTrue(check("The study finds that the tool reduces risk of harm.").ok)

    def test_an_attributed_claim_is_flagged_for_the_operator(self):
        # It passes, and it says why the operator still has to look: the
        # regex cannot open the paper.
        r = check("The study finds that the tool reduces risk of harm.")
        self.assertTrue(r.ok)
        self.assertTrue(any("operator" in n for n in r.notes))


class NeutralTest(unittest.TestCase):
    """Ordinary description must not trip the rule, or it will be worked around."""

    def test_a_plain_description_passes(self):
        self.assertTrue(
            check("A readable account of deployment risk in community settings.").ok)

    def test_describing_a_method_passes(self):
        self.assertTrue(check("A hazard log worked through for an agentic system.").ok)

    def test_a_question_passes(self):
        self.assertTrue(check("What does post-implementation monitoring look like here?").ok)


class InteractionTest(unittest.TestCase):
    def test_it_does_not_displace_the_other_rules(self):
        # Marketing and efficacy in one draft: both should be reported, so a
        # fixed draft cannot pass by resolving only the first complaint.
        r = check("Book a call — evidence shows this is effective for older adults.")
        self.assertFalse(r.ok)
        joined = " ".join(r.failures)
        self.assertIn("marketing", joined)
        self.assertIn("unattributed", joined)


if __name__ == "__main__":
    unittest.main()
