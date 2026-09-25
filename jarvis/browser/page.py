"""What a page looks like once it has stopped being HTML.

The agent never sees markup. It sees this: text, a list of links it can
follow, a list of fields it can describe, and the provenance of all of it.

Three decisions are baked into the shape, and each one is a fence rather than
a convenience.

**Raw HTML never reaches the model.** Jarvis asked for this directly, and it
is right for a duller reason than the obvious one: markup is where a page puts
the things it does not want a reader to see. Hidden text, off-screen
elements, `aria-hidden`, white-on-white, a `<div>` positioned at -9999px --
all of it is invisible to Paul and perfectly legible to a model reading the
source. Extracting what the page *shows* closes that gap; it does not close
the injection problem, and nothing here claims to.

**Links carry a ref, not a click.** A click fires the page's own JavaScript,
and a handler can do anything -- POST, navigate elsewhere, change state.
Jarvis made this point against an earlier design of mine that tried to
classify clicks as safe or not by looking at the DOM, and it was right that
static classification cannot work: a plain-looking anchor with an `onclick`
is indistinguishable from a plain one until it runs. So navigation does not
click. It reads the `href` off the link and asks the browser to GET that URL,
which is the same act as typing it into the address bar and fires nothing.

**A password field is a hard stop, not a warning.** `has_password` is
computed here, in the extractor, rather than inferred later from the text,
because "is this a login page" asked of prose is a judgement and asked of the
DOM is a fact.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Optional

# Bounded, because the page is untrusted and the model's context is not free.
# A page longer than this is truncated and says so: TRUNCATED is the whole
# point, since a partial page presented as a whole one is the failure this
# codebase keeps returning to.
MAX_TEXT = 40000
MAX_LINKS = 200
MAX_FIELDS = 60
MAX_CONTROLS = 120
MAX_HREF = 2000

# Fields whose presence makes a form un-submittable at any rung. Not a
# heuristic about wording -- these are input types, which the page declares.
SECRET_TYPES = frozenset({"password"})
PERSONAL_TYPES = frozenset({"tel", "email"})
# Autocomplete tokens the HTML standard defines for payment and identity.
# A page that asks for these is asking for Paul, not for information.
PERSONAL_AUTOCOMPLETE = frozenset({
    "cc-number", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-csc", "cc-name",
    "cc-type", "cc-given-name", "cc-family-name",
    "new-password", "current-password", "one-time-code",
})


def _clean(text: str, limit: int) -> str:
    """Collapse runs of whitespace; keep paragraph breaks."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text.strip()[:limit]


@dataclass(frozen=True)
class Link:
    """One thing the agent can navigate to without firing an event."""

    ref: str
    text: str
    href: str

    def as_dict(self) -> dict:
        return {"ref": self.ref, "text": self.text, "href": self.href}


@dataclass(frozen=True)
class Field:
    """One input, described but not filled.

    The agent may *report* that a form exists and what it wants. Filling it in
    is a separate tool at a higher rung, and filling in a secret is refused at
    every rung.
    """

    ref: str
    label: str
    kind: str                       # the input's type attribute
    autocomplete: str = ""
    required: bool = False

    @property
    def is_secret(self) -> bool:
        return (self.kind in SECRET_TYPES
                or self.autocomplete in PERSONAL_AUTOCOMPLETE)

    @property
    def is_personal(self) -> bool:
        return self.is_secret or self.kind in PERSONAL_TYPES

    def as_dict(self) -> dict:
        return {"ref": self.ref, "label": self.label, "kind": self.kind,
                "autocomplete": self.autocomplete, "required": self.required,
                "secret": self.is_secret, "personal": self.is_personal}


@dataclass(frozen=True)
class Control:
    """Something clickable, and the two facts that decide whether it posts.

    ``in_form`` is structural and is the one that matters: a page chooses what
    its buttons say and can call a "post comment" button anything at all, but
    it cannot make a button inside a form not be inside a form.
    """

    ref: str
    text: str
    kind: str = "button"
    in_form: bool = False

    def as_dict(self) -> dict:
        return {"ref": self.ref, "text": self.text, "kind": self.kind,
                "in_form": self.in_form}


@dataclass(frozen=True)
class Page:
    """One page load, as the agent is allowed to know it."""

    url: str
    title: str
    text: str
    links: tuple = ()
    fields: tuple = ()
    controls: tuple = ()
    headings: tuple = ()
    # What a person would actually be looking at, versus what is further down.
    # Paul's point is that browsing is an experience rather than a fetch, and
    # "it is on the page somewhere" is not the same as "it is on the screen".
    on_screen: str = ""
    below_fold: bool = False
    # A visible multi-line field currently holds text. One press away from
    # publishing it, whoever put it there.
    composing: bool = False
    fetched_at: float = field(default_factory=time.time)
    truncated: bool = False
    status: Optional[int] = None
    error: str = ""

    @property
    def has_password(self) -> bool:
        return any(f.is_secret for f in self.fields)

    def link(self, ref: str) -> Optional[Link]:
        for item in self.links:
            if item.ref == ref:
                return item
        return None

    def field_by_ref(self, ref: str) -> Optional[Field]:
        for item in self.fields:
            if item.ref == ref:
                return item
        return None

    def control(self, ref: str) -> Optional[Control]:
        for item in self.controls:
            if item.ref == ref:
                return item
        return None

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "links": [l.as_dict() for l in self.links],
            "fields": [f.as_dict() for f in self.fields],
            "controls": [c.as_dict() for c in self.controls],
            "headings": list(self.headings),
            "on_screen": self.on_screen,
            "below_fold": self.below_fold,
            "composing": self.composing,
            "fetched_at": self.fetched_at,
            "truncated": self.truncated,
            "status": self.status,
            "has_password": self.has_password,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Page":
        raw = raw or {}
        links = tuple(
            Link(ref=str(l.get("ref") or ""), text=str(l.get("text") or ""),
                 href=str(l.get("href") or ""))
            for l in (raw.get("links") or [])[:MAX_LINKS])
        fields = tuple(
            Field(ref=str(f.get("ref") or ""), label=str(f.get("label") or ""),
                  kind=str(f.get("kind") or "text"),
                  autocomplete=str(f.get("autocomplete") or ""),
                  required=bool(f.get("required")))
            for f in (raw.get("fields") or [])[:MAX_FIELDS])
        controls = tuple(
            Control(ref=str(c.get("ref") or ""), text=str(c.get("text") or "")[:120],
                    kind=str(c.get("kind") or "button"),
                    in_form=bool(c.get("in_form")))
            for c in (raw.get("controls") or [])[:MAX_CONTROLS])
        return cls(
            controls=controls,
            headings=tuple(_clean(str(h), 160) for h in (raw.get("headings") or [])[:40]),
            on_screen=_clean(str(raw.get("on_screen") or ""), 6000),
            below_fold=bool(raw.get("below_fold")),
            composing=bool(raw.get("composing")),
            url=str(raw.get("url") or ""),
            title=_clean(str(raw.get("title") or ""), 300),
            text=_clean(str(raw.get("text") or ""), MAX_TEXT),
            links=links, fields=fields,
            fetched_at=float(raw.get("fetched_at") or time.time()),
            truncated=bool(raw.get("truncated")),
            status=raw.get("status"),
            error=str(raw.get("error") or ""),
        )


# The script that turns a live DOM into the structure above. It runs in the
# page, which is the only place that knows what is actually visible: a server
# -side HTML parse cannot tell a hidden element from a shown one, and hidden
# elements are exactly where a page puts what it does not want Paul to read.
EXTRACT_JS = r"""
() => {
  const MAX_LINKS = %(max_links)d, MAX_FIELDS = %(max_fields)d, MAX_CONTROLS = %(max_controls)d;
  const MAX_HREF = %(max_href)d;
  const vh = window.innerHeight || 900, vw = window.innerWidth || 1280;
  const shown = (el) => {
    if (!el) return false;
    const s = window.getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (parseFloat(s.opacity || '1') < 0.05) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    // Parked off-screen is the classic way to hide text from a person while
    // leaving it in the document for anything that reads the source.
    if (r.bottom < -2000 || r.right < -2000) return false;
    return true;
  };
  // What a person looking at the screen right now can actually see. "It is
  // on the page" and "it is on the screen" are different facts, and the
  // agent is told both.
  const inView = (el) => {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
  };
  const label = (el) => {
    const bits = [el.getAttribute('aria-label'), el.getAttribute('placeholder'),
                  el.getAttribute('name'), el.getAttribute('title')];
    if (el.labels && el.labels.length) bits.unshift(el.labels[0].innerText);
    for (const b of bits) if (b && b.trim()) return b.trim().slice(0, 120);
    return '';
  };

  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    if (links.length >= MAX_LINKS) break;
    if (!shown(a)) continue;
    let href = '';
    try { href = new URL(a.getAttribute('href'), document.baseURI).href; }
    catch (e) { continue; }
    if (!/^https?:/i.test(href)) continue;   // no javascript:, no data:, no mailto:
    const text = (a.innerText || a.getAttribute('aria-label') || '').trim();
    const lref = 'L' + (links.length + 1);
    a.setAttribute('data-jarvis-ref', lref);
    links.push({ref: lref, text: text.slice(0, 200), href: href.slice(0, MAX_HREF)});
  }

  // Things that can be pressed and are not links. in_form is the structural
  // fact that decides whether pressing one sends something: a page chooses
  // what its buttons say and can call "post comment" anything at all, but it
  // cannot make a button inside a form not be inside a form.
  const controls = [];
  for (const el of document.querySelectorAll(
        'button, input[type=submit], input[type=button], input[type=image], [role=button], summary')) {
    if (controls.length >= MAX_CONTROLS) break;
    if (!shown(el)) continue;
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || label(el) || '').trim();
    const cref = 'C' + (controls.length + 1);
    el.setAttribute('data-jarvis-ref', cref);
    controls.push({ref: cref, text: text.slice(0, 120),
                   kind: (el.tagName.toLowerCase() === 'input' ? (el.type || 'button') : el.tagName.toLowerCase()),
                   in_form: !!el.closest('form')});
  }

  const fields = [];
  let composing = false;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    if (fields.length >= MAX_FIELDS) break;
    if (el.type === 'hidden') {
      // Not shown, but it IS the thing that carries state into a submit, so
      // a hidden secret still has to count towards has_password.
      if (el.type === 'password') fields.push({ref: 'F' + (fields.length + 1),
        label: label(el), kind: 'password', autocomplete: '', required: false});
      continue;
    }
    if (!shown(el)) continue;
    const kind = (el.type || el.tagName.toLowerCase() || 'text').toLowerCase();
    if (el.tagName.toLowerCase() === 'textarea' && (el.value || '').trim()) composing = true;
    const fref = 'F' + (fields.length + 1);
    el.setAttribute('data-jarvis-ref', fref);
    fields.push({
      ref: fref,
      label: label(el),
      kind: el.tagName.toLowerCase() === 'textarea' ? 'textarea' : kind,
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
      required: !!el.required,
    });
  }

  let fi = 0;
  for (const f of document.querySelectorAll('form')) { fi += 1; f.setAttribute('data-jarvis-form', 'M' + fi); }

  const headings = [];
  for (const h of document.querySelectorAll('h1, h2, h3')) {
    if (headings.length >= 40) break;
    if (!shown(h)) continue;
    const t = (h.innerText || '').trim();
    if (t) headings.push(h.tagName.toLowerCase() + ': ' + t.slice(0, 160));
  }

  // The visible viewport, as text: block-level elements that intersect it.
  const seen = new Set(); const onScreen = [];
  for (const el of document.querySelectorAll('h1,h2,h3,h4,p,li,td,th,dd,dt,blockquote,pre,label,summary,figcaption')) {
    if (onScreen.length >= 120) break;
    if (!shown(el) || !inView(el)) continue;
    const t = (el.innerText || '').trim();
    if (!t || seen.has(t)) continue;
    seen.add(t); onScreen.push(t.slice(0, 400));
  }
  const body = document.body;
  const docH = Math.max(body ? body.scrollHeight : 0, document.documentElement.scrollHeight || 0);
  return {
    url: document.location.href,
    title: document.title || '',
    text: body ? (body.innerText || '') : '',
    links: links,
    controls: controls,
    fields: fields,
    headings: headings,
    on_screen: onScreen.join('\n'),
    below_fold: docH > (window.scrollY || 0) + vh + 40,
    composing: composing,
  };
}
""" % {"max_links": MAX_LINKS, "max_fields": MAX_FIELDS, "max_controls": MAX_CONTROLS,
       "max_href": MAX_HREF}
