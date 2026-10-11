"""He could not say who the coding agent was (11 October 2026): its messages
were filed in his memory as the operator's. Both prompts now say who works on
him, and say it in a form that does not credit Paul's words to anyone else."""

import unittest

from vigil.brain import llm


class TestWhoWorksOnYou(unittest.TestCase):

    def test_both_the_planner_and_the_chat_are_told(self):
        for prompt in (llm.SYSTEM_PROMPT, llm.ASK_PROMPT):
            self.assertIn(llm.WHO_WORKS_ON_YOU, prompt)
        self.assertIn("Claude Code", llm.WHO_WORKS_ON_YOU)
        self.assertIn("Paul is your operator", llm.WHO_WORKS_ON_YOU)

    def test_an_untagged_old_exchange_is_not_claimed_for_the_coding_agent(self):
        # Paul's own chats carry the same "operator" label. Vigil proposed
        # reading any untagged exchange as the coding agent's; that would
        # misattribute Paul's words, so the prompt says either may be speaking.
        text = llm.WHO_WORKS_ON_YOU
        self.assertIn("may be from\neither".replace("\n", " "), " ".join(text.split()))
        self.assertNotIn("no speaker tag", text)

    def test_the_chat_prompt_still_ends_with_the_choices_rules(self):
        self.assertTrue(llm.ASK_PROMPT.rstrip().endswith(
            llm._CHOICES_PROMPT.rstrip()))


if __name__ == "__main__":
    unittest.main()
