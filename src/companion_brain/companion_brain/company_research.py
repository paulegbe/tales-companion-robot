"""Company research for interview practice, with a 30-day SQLite cache.

Plain Python, no ROS imports. The Anthropic client is passed in, so tests use
a fake client and never touch the network.

Flow (get_brief):
    normalize company name -> newest cached brief for it
    cache hit younger than 30 days -> use it, no API call
    miss, stale, or forced refresh -> on_research_start() (Tales says
        "Give me a minute"), then one research call with server-side web
        search -> store the brief -> return it
    if research runs past slow_notice_s -> on_slow() once ("still looking")

Research (CompanyResearcher.research):
    One non-streamed messages.create with the web search tool (max 5
    searches). Server tool loops can stop with pause_turn; the call is
    resent with the partial answer, a bounded number of times. Search
    failures do not raise: they come back as a web_search_tool_result whose
    content is an error object instead of a list. If no search succeeded,
    or the call failed or was refused, a second call without tools writes
    the brief from general knowledge. Those fallback briefs are not cached,
    so the next interview tries the web again.
"""

from dataclasses import dataclass, field
import threading
import time

CACHE_MAX_AGE_S = 30 * 86400
MAX_SEARCHES = 5
MAX_SOURCES = 10

# Models that support the dynamic filtering web search variant. Anything
# else (for example claude-haiku-4-5) gets the basic variant.
_DYNAMIC_SEARCH_MODELS = (
    'claude-opus-5-5', 'claude-opus-5', 'claude-opus-4-8', 'claude-opus-4-7',
    'claude-opus-4-6', 'claude-sonnet-5-5', 'claude-sonnet-5', 'claude-sonnet-4-6',
)

FALLBACK_NOTE = 'Web search failed, so this brief is from general knowledge.'
NO_SEARCH_NOTE = 'No web search was run, so this brief is from general knowledge.'

_BRIEF_REQUEST = (
    'Research {company} for a candidate preparing for a {role} interview there. '
    'Use web search. Then write a brief in plain prose, under 400 words, covering: '
    'what the company does; its stated values and culture; recent news relevant to '
    'a candidate; the publicly reported interview process for junior or entry-level '
    'developer roles; and likely focus areas for this interview. Interview process '
    'details come from public reports, so mark them as approximate. No markdown, '
    'no lists, no preamble.'
)

_FALLBACK_REQUEST = (
    'Web search is unavailable. From general knowledge only, write a brief in plain '
    'prose, under 400 words, about {company} for a candidate preparing for a {role} '
    'interview: what the company does, values, anything notable, the typical interview '
    'process for junior or entry-level developer roles (approximate), and likely focus '
    'areas. Say plainly where you are unsure. No markdown, no lists, no preamble.'
)


def normalize_company(name):
    """Return the cache key for a company name: lowercase, surrounding space stripped."""
    return (name or '').strip().lower()


def web_search_tool_for(model, max_uses=MAX_SEARCHES):
    """Return the web search tool definition for model."""
    if any(model == m or model.startswith(m + '-') for m in _DYNAMIC_SEARCH_MODELS):
        tool_type = 'web_search_20260209'
    else:
        tool_type = 'web_search_20250305'
    return {'type': tool_type, 'name': 'web_search', 'max_uses': max_uses}


@dataclass
class ResearchResult:
    """Outcome of one research run."""

    brief: str
    sources: list = field(default_factory=list)
    used_web: bool = False
    note: str = ''


@dataclass
class BriefResult:
    """What get_brief hands back to the bridge."""

    brief: str
    sources: list
    origin: str  # 'cache', 'web' or 'fallback'
    reason: str  # 'hit', 'miss', 'stale' or 'refresh'
    age_s: float = 0.0
    note: str = ''


def _text_of(contents):
    """Concatenate ALL text blocks. Citations split one answer into many blocks."""
    return ''.join(
        getattr(block, 'text', '') or ''
        for content in contents
        for block in content
        if getattr(block, 'type', None) == 'text'
    ).strip()


def _scan_searches(contents):
    """Return (successful search count, error codes, sources) from response blocks."""
    ok, errors, sources, seen = 0, [], [], set()

    def add(url, title):
        if url and url not in seen and len(sources) < MAX_SOURCES:
            seen.add(url)
            sources.append({'url': url, 'title': title or ''})

    for content in contents:
        for block in content:
            kind = getattr(block, 'type', None)
            if kind == 'web_search_tool_result':
                result = getattr(block, 'content', None)
                # Success is a list of results; failure is a single error object.
                if isinstance(result, list):
                    ok += 1
                    for item in result:
                        add(getattr(item, 'url', None), getattr(item, 'title', None))
                else:
                    errors.append(getattr(result, 'error_code', 'unknown'))
            elif kind == 'text':
                for citation in getattr(block, 'citations', None) or []:
                    add(getattr(citation, 'url', None), getattr(citation, 'title', None))
    return ok, errors, sources


class CompanyResearcher:
    """Writes a company brief using Claude with server-side web search."""

    def __init__(self, client, model, max_continuations=3, max_tokens=16000, log=None,
                 deadline_s=None):
        """Store settings. deadline_s stops pause_turn continuations once exceeded."""
        self.client = client
        self.model = model
        self.max_continuations = max_continuations
        self.max_tokens = max_tokens
        self.log = log or (lambda msg: None)
        self.deadline_s = deadline_s

    @property
    def search_tool(self):
        """Return the web search tool definition used for research."""
        return web_search_tool_for(self.model)

    def research(self, company, role):
        """Return a ResearchResult. Never raises."""
        messages = [{'role': 'user', 'content': _BRIEF_REQUEST.format(company=company, role=role)}]
        contents = []
        started = time.monotonic()
        try:
            for _ in range(self.max_continuations + 1):
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    tools=[self.search_tool],
                    messages=messages,
                )
                contents.append(response.content)
                if response.stop_reason != 'pause_turn':
                    break
                if self.deadline_s is not None and time.monotonic() - started > self.deadline_s:
                    # Tales is blocked while this runs; settle for the partial answer.
                    self.log('Research: deadline reached, using what arrived.')
                    break
                # Resend with the partial turn so the server picks up where it paused.
                messages = messages + [{'role': 'assistant', 'content': response.content}]
            else:
                self.log('Research: pause_turn limit reached, using what arrived.')
        except Exception as e:
            # Any API failure (network, rate limit, 5xx, timeout) means fallback.
            return self._fallback(company, role, 'API error: %s' % e)

        if response.stop_reason == 'refusal':
            return self._fallback(company, role, 'refusal')

        text = _text_of(contents)
        ok, errors, sources = _scan_searches(contents)
        if response.stop_reason == 'max_tokens':
            if not text:
                return self._fallback(company, role, 'max_tokens with no text')
            self.log('Research: hit max_tokens, using the partial brief.')
        if ok == 0 and errors:
            return self._fallback(company, role, 'search errors: %s' % ', '.join(errors))
        if not text:
            return self._fallback(company, role, 'empty answer')
        if ok == 0:
            # Claude answered without searching. Usable, but not web research.
            return ResearchResult(text, [], used_web=False, note=NO_SEARCH_NOTE)
        return ResearchResult(text, sources, used_web=True)

    def _fallback(self, company, role, reason):
        """Write the brief from general knowledge, with no tools."""
        self.log('Research: falling back to general knowledge (%s).' % reason)
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[{
                    'role': 'user',
                    'content': _FALLBACK_REQUEST.format(company=company, role=role),
                }],
            )
            text = '' if response.stop_reason == 'refusal' else _text_of([response.content])
        except Exception as e:
            self.log('Research: fallback failed too (%s).' % e)
            text = ''
        return ResearchResult(text, [], used_web=False, note=FALLBACK_NOTE)


def get_brief(store, researcher, company, role, now=None, force_refresh=False,
              max_age_s=CACHE_MAX_AGE_S, on_research_start=None,
              slow_notice_s=30.0, on_slow=None):
    """Return a BriefResult from the cache, or research and cache a new one."""
    now = time.time() if now is None else now
    key = normalize_company(company)

    cached = store.latest_brief(key)
    if cached is not None and not force_refresh:
        age = now - cached['created_at']
        if age < max_age_s:
            return BriefResult(cached['brief'], cached['sources'], 'cache', 'hit', age)

    if force_refresh:
        reason = 'refresh'
    elif cached is None:
        reason = 'miss'
    else:
        reason = 'stale'

    if on_research_start is not None:
        on_research_start()

    # Speak one "still looking" line if research outlasts slow_notice_s.
    timer = None
    if on_slow is not None:
        timer = threading.Timer(slow_notice_s, on_slow)
        timer.daemon = True
        timer.start()
    try:
        result = researcher.research(company.strip(), role)
    finally:
        if timer is not None:
            timer.cancel()

    if not result.used_web:
        return BriefResult(result.brief, [], 'fallback', reason, 0.0, result.note)

    store.save_brief(key, company.strip(), role, result.brief, result.sources, now)
    return BriefResult(result.brief, result.sources, 'web', reason, 0.0, result.note)
