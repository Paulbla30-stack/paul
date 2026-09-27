"""The option window in chat: Jarvis offers choices, Paul taps one."""

import json
import tempfile
import unittest

from jarvis.agent import choices
from jarvis.cloud.headless import UI_HTML_PATH
from tests.test_brain import FakeClaude, make_brain, message, needs_sdk
from tests.test_ui import TestUiListener, call, make_agent

GOOD = ('Two readings of "redo":\n\n```choices\n'
        '{"question": "What do you mean by redo?", "options": ['
        '{"label": "Roll the new pages in", "description": "Add the three pages, same look."},'
        '{"label": "Full redesign", "description": "New look and structure."}], "multi": false}\n```')


class TestSplit(unittest.TestCase):

    def test_the_block_is_taken_out_and_checked(self):
        text, c = choices.split(GOOD)
        self.assertEqual(text, 'Two readings of "redo":')
        self.assertEqual(c["question"], "What do you mean by redo?")
        self.assertEqual([o["label"] for o in c["options"]], ["Roll the new pages in", "Full redesign"])
        self.assertFalse(c["multi"])

    def test_no_block_is_no_choices(self):
        self.assertEqual(choices.split("Just an answer."), ("Just an answer.", None))
        self.assertEqual(choices.split(None), (None, None))

    def test_a_bad_block_is_dropped_but_the_reply_survives(self):
        for bad in ('```choices\nnot json\n```', '```choices\n{"question": "q", "options": ["only one"]}\n```',
                    '```choices\n{"options": ["a", "b"]}\n```', '```choices\n[1, 2]\n```'):
            text, c = choices.split("Answer.\n" + bad)
            self.assertIsNone(c, bad)
            self.assertEqual(text, "Answer.")

    def test_caps_dedupe_and_invisible_characters(self):
        raw = {"question": "q" * 900, "options": [
            {"label": "A‮​B" + "x" * 100, "description": "d" * 900},
            {"label": "a‮​b" + "x" * 100}, "Plain", "Third", "Fourth", "Fifth"]}
        c = choices.check(raw)
        self.assertEqual(len(c["question"]), choices.MAX_QUESTION)
        self.assertTrue(c["options"][0]["label"].startswith("AB"))
        self.assertEqual(len(c["options"][0]["label"]), choices.MAX_LABEL)
        self.assertEqual(len(c["options"][0]["description"]), choices.MAX_DESCRIPTION)
        self.assertEqual([o["label"] for o in c["options"]][1:], ["Plain", "Third"],
                         "duplicate dropped, at most four considered")

    def test_a_second_block_cannot_ride_along(self):
        text, c = choices.split(GOOD + '\n```choices\n{"question": "x", "options": ["p", "q"]}\n```')
        self.assertEqual(c["question"], "What do you mean by redo?")
        self.assertNotIn("```", text)

    def test_the_prompt_tells_the_model_about_it(self):
        from jarvis.brain.llm import ASK_PROMPT
        self.assertIn("choices", ASK_PROMPT)
        self.assertIn("never for yes/no", ASK_PROMPT)


class TestPage(unittest.TestCase):

    def test_the_page_renders_options_as_text_and_sends_them_as_paul(self):
        with open(UI_HTML_PATH, encoding="utf-8") as fh:
            html = fh.read()
        start = html.index("function choiceCard")
        body = html[start:html.index("function saveChat", start)]
        self.assertNotIn("innerHTML", body)
        self.assertIn("sendChat()", body)
        self.assertIn("Options offered", html)


@needs_sdk
class TestChatRoute(TestUiListener):

    def test_chat_returns_choices_separately_and_records_the_clean_answer(self):
        with FakeClaude() as api, tempfile.TemporaryDirectory() as tmp:
            agent = make_agent(make_brain(api.url))
            runner, port = self._runner(agent, tmp)
            base = f"http://127.0.0.1:{port}"
            auth = {"Authorization": "Bearer t0k", "Content-Type": "application/json"}
            try:
                api.respond_with(lambda r: message(GOOD))
                ans, _ = call(base, "/chat", data=json.dumps(
                    {"messages": [{"role": "user", "content": "redo the website"}]}).encode(), headers=auth)
                self.assertEqual(ans["answer"], 'Two readings of "redo":')
                self.assertEqual(len(ans["choices"]["options"]), 2)
                api.respond_with(lambda r: message("Plain reply."))
                ans, _ = call(base, "/chat", data=json.dumps(
                    {"messages": [{"role": "user", "content": "Full redesign"}]}).encode(), headers=auth)
                self.assertIsNone(ans["choices"], "options do not linger into the next reply")
            finally:
                runner.stop_status_server()


if __name__ == "__main__":
    unittest.main()
