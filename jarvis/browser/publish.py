"""Saying something in public is not the same as reading, and needs asking.

Paul's hard rule, 23 September 2026, given in the same breath as widening
everything else: *"if jarvis wants to post on something he get approval first.
This is just a safety precaution."*

It sits ABOVE the grant rather than under it. `browse_act` being open means
the agent may press "next page" without asking; it does not mean it may leave
a comment. Those are different acts that happen to share a mouse.

**The classifier fails closed, and the direction matters.** The tempting
version asks "does this look like posting?" and lets everything else through
-- which is keyword matching, the thing we already threw out for injection
defence, and it fails in the worst direction: anything the list does not name
is published without asking. So this asks the other question. An action is
publishing unless it is *recognisably* one of a short list of things that
plainly are not.

That is not paranoia about wording, it is about who writes the wording. A page
controls what its buttons say and can call a "post comment" button anything it
likes; it cannot make a form submission not be a form submission. So the
signals that force approval are structural where possible -- is this a submit,
is this element inside a form, has the agent typed into a multi-line field on
this page -- and the word list is only ever an ADDITIONAL trigger, never the
only gate.

What this deliberately does not try to do is judge whether the posting is a
good idea. That is Paul's, which is the whole point of asking him.
"""

# Things that are plainly not publishing. Short, and it stays short: every
# entry is a promise that this action cannot say anything to anyone.
#
# Note what is absent. "click" is not here, because a click is how you press
# "Send". "press" is not here, because Enter in a comment box posts it.
READING_ACTIONS = frozenset({"scroll", "back", "forward", "reload"})

# Field types that hold prose. Typing into one of these is composing, and a
# page with something composed on it is one action away from publishing it.
COMPOSING_KINDS = frozenset({"textarea"})

# An additional trigger, never the only one. These are the words a button uses
# when it is honest; a dishonest one is caught by the structural rules above.
PUBLISHING_WORDS = (
    "post", "comment", "reply", "send", "publish", "share", "tweet", "submit",
    "upload", "review", "rate", "message", "sign", "subscribe", "apply",
    "book", "order", "buy", "pay", "checkout", "confirm", "donate", "bid",
    "register", "join", "create account", "delete", "remove", "report",
)

# What the caller gets back.
FREE = "free"                 # go ahead, this says nothing to anybody
NEEDS_APPROVAL = "needs-approval"


def _words_in(text: str) -> bool:
    low = " ".join(str(text or "").lower().split())
    return any(word in low for word in PUBLISHING_WORDS)


def classify(kind: str, *, element_text: str = "", in_form: bool = False,
             page_has_composed_text: bool = False,
             field_kind: str = "") -> tuple:
    """(verdict, reason) for one action.

    ``page_has_composed_text`` is whether anything on this page currently
    holds prose the agent put there. It is the signal that turns an ordinary
    click into the last step of publishing something.
    """
    kind = (kind or "").strip().lower()

    if kind in READING_ACTIONS:
        return FREE, ""

    if kind == "submit":
        # The one that needs no interpretation. A form submission is the act
        # of sending something somewhere, whatever the button was called.
        return NEEDS_APPROVAL, "submitting a form sends something to the site"

    if kind == "type":
        if (field_kind or "").lower() in COMPOSING_KINDS:
            # Typing is not yet saying. It is allowed, and it arms the page:
            # the NEXT action here will need asking.
            return FREE, ""
        return FREE, ""

    if kind == "select":
        return FREE, ""

    # click and press, which is where nearly everything interesting lives.
    if page_has_composed_text:
        return NEEDS_APPROVAL, (
            "there is text composed on this page, so this could be the button "
            "that sends it")
    if in_form:
        return NEEDS_APPROVAL, (
            "this is inside a form, and pressing things inside forms is how "
            "pages send data")
    if _words_in(element_text):
        return NEEDS_APPROVAL, (
            f"the control says {' '.join(str(element_text).split())[:60]!r}, "
            "which reads like it says something to someone")
    if kind == "press":
        # Enter in a search box is fine; Enter in anything else may be Send,
        # and the caller could not tell us which. Fails closed.
        return NEEDS_APPROVAL, (
            "a key press can be the thing that sends, and there is no way to "
            "tell from here which key this was")

    return FREE, ""


def describe() -> str:
    """The sentence the model is given, so it is not surprised by a refusal."""
    return ("You may read, move about and fill things in freely. Anything that "
            "would SAY something -- post, comment, reply, send, submit a form, "
            "buy, book, sign up -- is recorded as a proposal for Paul and waits "
            "for him, whatever else you have been granted. That is his standing "
            "rule and nothing lifts it. Fill the form in, then say what you are "
            "ready to send and why.")
