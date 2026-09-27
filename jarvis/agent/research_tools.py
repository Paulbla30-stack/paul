"""The tools a research project may use, once Paul has checked its plan.

Paul, 27 September 2026: Jarvis "needs to be able to [use] the tools etc to be
able to complete the research project. Whether that use the Internet. Search
this that etc or try this code or that. This should be a part of the action
plan at the beginning of the brainstorming section of the project. Once
verified by myself. Jarvis then can go off and do it."

Three tools, and only three:

  * ``search`` -- one query, through Jarvis's own fenced browser, to a search
    results page. The query is exactly the one Paul approved in the plan; it
    is never rewritten at run time, because a query is also something sent to
    a stranger.
  * ``read``   -- one page, through the same browser, by an address in the
    plan or one a search in the same thread brought back.
  * ``code``   -- one script, in the code sandbox (``sandbox.py``).

None of them changes anything on the other end. Posting, signing up, buying
and sending are not research tools and no plan can name them: they stay
proposals under the permission spine, whatever a project says.

Everything these return is someone else's words or the output of code the
model wrote from someone else's words. It goes back to the model as quoted
evidence with its source, never as an instruction, and every browser use is
written to the browser journal marked ``research`` so it shows in the daily
debrief beside everything else the browser did.
"""

import logging
import urllib.parse
from typing import Optional

TOOLS = ("search", "read", "code")
DESCRIPTIONS = {
    "search": "one web search: the exact query goes in the plan, and each query is one use",
    "read": "read one web page: an address in the plan, or 'from search' to read what a "
            "search in the same thread found; each page is one use",
    "code": "run one Python 3.11 script, standard library only, no network, 384 MB, 60 s, "
            "on data from earlier steps; each run is one use",
}
DEFAULT_SEARCH_URL = "https://search.brave.com/search?q={q}"
MAX_RESULTS = 8
MAX_PAGE_TEXT = 6000


class ResearchTools:

    def __init__(self, browser=None, sandbox=None, search_url: str = DEFAULT_SEARCH_URL,
                 logger: Optional[logging.Logger] = None):
        self.browser = browser
        self.sandbox = sandbox
        self.search_url = search_url if "{q}" in str(search_url or "") else DEFAULT_SEARCH_URL
        self.log = logger or logging.getLogger("jarvis.research.tools")

    # ---- what is there ------------------------------------------------------

    def available(self, tool: str) -> Optional[str]:
        """None when the tool can be used now; otherwise why not."""
        if tool not in TOOLS:
            return f"'{tool}' is not a research tool"
        if tool in ("search", "read"):
            if self.browser is None:
                return "the browser is not switched on"
            try:
                if not self.browser.available():
                    return "the browser service is not answering"
            except Exception as exc:
                return f"the browser could not be reached: {exc}"
            return None
        if self.sandbox is None:
            return "the code sandbox is not switched on"
        return self.sandbox.available()

    def describe(self) -> dict:
        return {t: DESCRIPTIONS[t] for t in TOOLS}

    # ---- using them -----------------------------------------------------------

    def search(self, query: str) -> list:
        """Results of one query: [{"source": url, "text": title}]."""
        query = " ".join(str(query or "").split())[:300]
        if not query:
            raise ValueError("a search needs a query")
        url = self.search_url.format(q=urllib.parse.quote_plus(query))
        page = self._open(url, f"research search: {query[:120]}")
        engine = urllib.parse.urlsplit(url).hostname or ""
        out, seen = [], set()
        for link in page.get("links") or []:
            href = _unwrap(str(link.get("href") or ""))
            host = urllib.parse.urlsplit(href).hostname or ""
            if not href.startswith(("http://", "https://")) or not host:
                continue
            if host == engine or host.endswith("." + _root(engine)) or host == _root(engine):
                continue
            if href in seen:
                continue
            seen.add(href)
            out.append({"source": href, "text": " ".join(str(link.get("text") or "").split())[:300]})
            if len(out) >= MAX_RESULTS:
                break
        return out

    def read(self, url: str) -> dict:
        """One page: {"source": url, "text": what it says, "title": ...}."""
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("only http and https addresses can be read")
        page = self._open(url, "research read")
        return {"source": str(page.get("url") or url), "title": str(page.get("title") or "")[:300],
                "text": str(page.get("text") or "")[:MAX_PAGE_TEXT]}

    def code(self, source: str) -> dict:
        if self.sandbox is None:
            raise RuntimeError("the code sandbox is not switched on")
        return self.sandbox.run(source)

    def _open(self, url: str, did: str) -> dict:
        why = self.available("read")
        if why:
            raise RuntimeError(why)
        page = self.browser.open(url)
        journal = getattr(self.browser, "journal", None)
        if page.get("error"):
            if journal is not None:
                journal.record(did, url, "failed", by="research", reason=str(page["error"])[:200])
            raise RuntimeError(str(page["error"])[:300])
        if journal is not None:
            journal.record(did, str(page.get("url") or url), "ok", by="research")
        return page


def _root(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _unwrap(href: str) -> str:
    """A search engine's redirect link, turned back into where it goes."""
    if href.startswith("//"):
        href = "https:" + href
    parts = urllib.parse.urlsplit(href)
    query = urllib.parse.parse_qs(parts.query)
    for key in ("uddg", "u", "url", "q"):
        if key in query and query[key] and query[key][0].startswith(("http://", "https://")):
            if parts.path.rstrip("/").endswith(("/l", "/url", "/ck/a")):
                return query[key][0]
    return href


def build_tools(agent=None, config: Optional[dict] = None, logger=None) -> Optional[ResearchTools]:
    """Tools for research, or None unless research.toolbox.enabled is set."""
    cfg = ((config or {}).get("research") or {}).get("toolbox")
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    from jarvis.agent.sandbox import build_sandbox
    return ResearchTools(browser=getattr(agent, "browser", None),
                         sandbox=build_sandbox(config, logger),
                         search_url=str(cfg.get("search_url") or DEFAULT_SEARCH_URL), logger=logger)
