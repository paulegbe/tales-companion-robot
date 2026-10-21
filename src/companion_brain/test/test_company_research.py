"""
Tests for company research: normalization, tool choice, cache logic, researcher.

The Anthropic client is faked with SimpleNamespace responses, so nothing here
touches the network or needs the anthropic package.
"""

import threading
import time
from types import SimpleNamespace as NS

from companion_brain import company_research as cr
from companion_brain.company_research import (
    CACHE_MAX_AGE_S,
    CompanyResearcher,
    FALLBACK_NOTE,
    get_brief,
    MAX_SOURCES,
    NO_SEARCH_NOTE,
    normalize_company,
    ResearchResult,
    web_search_tool_for,
)
from companion_brain.interview_store import InterviewStore
import pytest

DAY = 86400.0
NOW = 1_800_000_000.0


# Fakes


class FakeMessages:
    """Records create() kwargs and replays scripted responses or exceptions."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        """Return the next scripted response, or raise it if it is an exception."""
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError('unexpected extra create() call')
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClient:
    """Stands in for anthropic.Anthropic with only messages.create."""

    def __init__(self, *script):
        self.messages = FakeMessages(script)

    @property
    def calls(self):
        """Return the recorded create() kwargs."""
        return self.messages.calls


class FakeResearcher:
    """Returns a fixed ResearchResult, optionally after a delay."""

    def __init__(self, result=None, delay=0.0, events=None):
        self.result = result or ResearchResult('fresh brief', [{'url': 'u', 'title': 't'}],
                                               used_web=True)
        self.delay = delay
        self.calls = []
        self.events = events if events is not None else []

    def research(self, company, role):
        """Record the call and return the scripted result."""
        self.events.append('research')
        self.calls.append((company, role))
        if self.delay:
            time.sleep(self.delay)
        return self.result


def text(t, citations=None):
    """Return a text block, optionally with citations."""
    return NS(type='text', text=t, citations=citations)


def cite(url, title='T'):
    """Return a web search citation."""
    return NS(type='web_search_result_location', url=url, title=title, cited_text='x')


def search_ok(*urls):
    """Return a successful web_search_tool_result block."""
    items = [NS(type='web_search_result', url=u, title='title ' + u) for u in urls]
    return NS(type='web_search_tool_result', tool_use_id='s1', content=items)


def search_err(code='unavailable'):
    """Return a failed web_search_tool_result block whose content is an error object."""
    return NS(type='web_search_tool_result', tool_use_id='s1',
              content=NS(type='web_search_tool_result_error', error_code=code))


def server_tool_use():
    """Return a server_tool_use block."""
    return NS(type='server_tool_use', id='s1', name='web_search', input={'query': 'acme'})


def resp(content, stop='end_turn'):
    """Return a fake Messages response."""
    return NS(content=content, stop_reason=stop)


def researcher(client, **kw):
    """Return a CompanyResearcher with a log list attached."""
    logs = []
    r = CompanyResearcher(client, 'claude-sonnet-5', log=logs.append, **kw)
    r.logs = logs
    return r


@pytest.fixture
def store(tmp_path):
    """Open a real InterviewStore on a temp database."""
    s = InterviewStore(str(tmp_path / 'facts.db'))
    yield s
    s.close()


# Normalization


@pytest.mark.parametrize('raw,expected', [
    ('  Acme Corp ', 'acme corp'),
    ('ACME CORP', 'acme corp'),
    ('acme corp', 'acme corp'),
    ('\tAcme Corp\n', 'acme corp'),
    ('', ''),
    (None, ''),
])
def test_normalize_company(raw, expected):
    """Lowercase and strip company names for the cache key."""
    assert normalize_company(raw) == expected


def test_name_variants_hit_same_cache_row(store):
    """Serve a brief cached under one spelling to other case and space variants."""
    r = FakeResearcher()
    first = get_brief(store, r, '  Acme Corp ', 'dev', now=NOW)
    assert first.origin == 'web'
    assert store.latest_brief('acme corp')['company_display'] == 'Acme Corp'
    for variant in ('ACME CORP', 'acme corp', ' Acme Corp'):
        got = get_brief(store, r, variant, 'dev', now=NOW + 1)
        assert got.origin == 'cache'
        assert got.reason == 'hit'
    assert len(r.calls) == 1
    assert r.calls[0] == ('Acme Corp', 'dev')


# Tool picker


@pytest.mark.parametrize('model', [
    'claude-sonnet-5', 'claude-opus-4-6', 'claude-opus-5-5', 'claude-sonnet-4-6',
    'claude-opus-4-6-20260101',
])
def test_dynamic_search_models(model):
    """Use web_search_20260209 for models that support dynamic filtering."""
    tool = web_search_tool_for(model)
    assert tool == {'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': 5}


@pytest.mark.parametrize('model', [
    'claude-haiku-4-5', 'some-unknown-model', 'claude-sonnet-5x', 'claude-opus-4-60',
    'claude-opus-4', 'claude-sonnet-4-5',
])
def test_basic_search_models(model):
    """Use web_search_20250305 for every other model, including near misses."""
    tool = web_search_tool_for(model)
    assert tool == {'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 5}


# get_brief cache logic


def test_cache_hit_at_29_days(store):
    """Use a 29 day old brief without calling the researcher or the start hook."""
    store.save_brief('acme', 'Acme', 'dev', 'old brief', [{'url': 'u', 'title': 't'}], NOW)
    r = FakeResearcher()
    started = []
    got = get_brief(store, r, 'Acme', 'dev', now=NOW + 29 * DAY,
                    on_research_start=lambda: started.append(1))
    assert got.origin == 'cache'
    assert got.reason == 'hit'
    assert got.brief == 'old brief'
    assert got.sources == [{'url': 'u', 'title': 't'}]
    assert got.age_s == pytest.approx(29 * DAY)
    assert r.calls == []
    assert started == []


@pytest.mark.parametrize('age_days', [30, 31])
def test_cache_stale_at_30_and_31_days(store, age_days):
    """Treat a brief 30 days or older as stale: research and store a new row."""
    assert CACHE_MAX_AGE_S == 30 * DAY
    store.save_brief('acme', 'Acme', 'dev', 'old brief', [], NOW)
    r = FakeResearcher()
    later = NOW + age_days * DAY
    got = get_brief(store, r, 'Acme', 'dev', now=later)
    assert got.origin == 'web'
    assert got.reason == 'stale'
    assert got.brief == 'fresh brief'
    assert len(r.calls) == 1
    latest = store.latest_brief('acme')
    assert latest['brief'] == 'fresh brief'
    assert latest['created_at'] == later


def test_cache_miss_researches_and_saves(store):
    """Research on a miss and cache the result with its sources."""
    r = FakeResearcher()
    got = get_brief(store, r, 'Globex', 'SWE', now=NOW)
    assert (got.origin, got.reason) == ('web', 'miss')
    assert got.sources == [{'url': 'u', 'title': 't'}]
    row = store.latest_brief('globex')
    assert row['brief'] == 'fresh brief'
    assert row['role'] == 'SWE'
    assert row['sources'] == [{'url': 'u', 'title': 't'}]


def test_force_refresh_on_fresh_brief(store):
    """Research again on a forced refresh even when the cache is fresh."""
    store.save_brief('acme', 'Acme', 'dev', 'old brief', [], NOW)
    r = FakeResearcher()
    got = get_brief(store, r, 'Acme', 'dev', now=NOW + 1, force_refresh=True)
    assert (got.origin, got.reason) == ('web', 'refresh')
    assert len(r.calls) == 1
    assert store.latest_brief('acme')['brief'] == 'fresh brief'


def test_force_refresh_on_miss_reports_refresh(store):
    """Report reason refresh when forced even if nothing was cached."""
    got = get_brief(store, FakeResearcher(), 'Acme', 'dev', now=NOW, force_refresh=True)
    assert got.reason == 'refresh'


def test_fallback_result_not_saved(store):
    """Return a fallback brief without caching it, so the web is tried next time."""
    fb = ResearchResult('general knowledge brief', [], used_web=False, note=FALLBACK_NOTE)
    r = FakeResearcher(result=fb)
    got = get_brief(store, r, 'Acme', 'dev', now=NOW)
    assert got.origin == 'fallback'
    assert got.reason == 'miss'
    assert got.note == FALLBACK_NOTE
    assert got.sources == []
    assert store.latest_brief('acme') is None
    get_brief(store, r, 'Acme', 'dev', now=NOW + 1)
    assert len(r.calls) == 2


def test_stale_fallback_keeps_old_row(store):
    """Leave the stale row untouched when the refresh falls back."""
    store.save_brief('acme', 'Acme', 'dev', 'old brief', [], NOW)
    fb = ResearchResult('gk', [], used_web=False, note=FALLBACK_NOTE)
    got = get_brief(store, FakeResearcher(result=fb), 'Acme', 'dev', now=NOW + 40 * DAY)
    assert (got.origin, got.reason, got.brief) == ('fallback', 'stale', 'gk')
    assert store.latest_brief('acme')['brief'] == 'old brief'


def test_on_research_start_called_before_research(store):
    """Call on_research_start once, before research, when researching."""
    events = []
    r = FakeResearcher(events=events)
    get_brief(store, r, 'Acme', 'dev', now=NOW,
              on_research_start=lambda: events.append('start'))
    assert events == ['start', 'research']


def test_slow_notice_fires_once_when_research_is_slow(store):
    """Call on_slow exactly once when research outlasts slow_notice_s."""
    slow = []
    lock = threading.Lock()

    def on_slow():
        with lock:
            slow.append(time.monotonic())

    r = FakeResearcher(delay=0.2)
    got = get_brief(store, r, 'Acme', 'dev', now=NOW, slow_notice_s=0.05, on_slow=on_slow)
    assert got.origin == 'web'
    time.sleep(0.1)
    assert len(slow) == 1


def test_slow_notice_does_not_fire_when_fast(store):
    """Never call on_slow when research finishes before slow_notice_s."""
    slow = []
    r = FakeResearcher(delay=0.0)
    get_brief(store, r, 'Acme', 'dev', now=NOW, slow_notice_s=0.2,
              on_slow=lambda: slow.append(1))
    time.sleep(0.35)
    assert slow == []


def test_slow_notice_not_fired_on_cache_hit(store):
    """Never call on_slow on a cache hit, even with a tiny threshold."""
    store.save_brief('acme', 'Acme', 'dev', 'cached', [], NOW)
    slow = []
    get_brief(store, FakeResearcher(delay=0.2), 'Acme', 'dev', now=NOW + 1,
              slow_notice_s=0.0, on_slow=lambda: slow.append(1))
    time.sleep(0.1)
    assert slow == []


def test_slow_timer_cancelled_when_research_raises(store):
    """Cancel the slow timer even if the researcher raises."""
    slow = []

    class Boom:
        def research(self, company, role):
            raise RuntimeError('boom')

    with pytest.raises(RuntimeError):
        get_brief(store, Boom(), 'Acme', 'dev', now=NOW, slow_notice_s=0.1,
                  on_slow=lambda: slow.append(1))
    time.sleep(0.25)
    assert slow == []


# CompanyResearcher


def test_research_success_sources_and_text():
    """Use web results and citations as deduped sources and mark used_web."""
    content = [
        server_tool_use(),
        search_ok('https://a', 'https://b'),
        text('Acme makes rockets.', citations=[cite('https://b'), cite('https://c')]),
    ]
    client = FakeClient(resp(content))
    result = researcher(client).research('Acme', 'dev')
    assert result.used_web is True
    assert result.note == ''
    assert result.brief == 'Acme makes rockets.'
    assert [s['url'] for s in result.sources] == ['https://a', 'https://b', 'https://c']
    assert result.sources[0] == {'url': 'https://a', 'title': 'title https://a'}
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call['model'] == 'claude-sonnet-5'
    assert call['tools'] == [web_search_tool_for('claude-sonnet-5')]
    assert call['tools'][0]['max_uses'] == 5
    assert 'Acme' in call['messages'][0]['content']
    assert 'dev' in call['messages'][0]['content']
    assert 'stream' not in call or call['stream'] is False


def test_research_sources_capped_at_ten():
    """Keep at most MAX_SOURCES sources."""
    urls = ['https://s%d' % i for i in range(15)]
    content = [search_ok(*urls), text('Brief.', citations=[cite('https://x')])]
    result = researcher(FakeClient(resp(content))).research('Acme', 'dev')
    assert MAX_SOURCES == 10
    assert len(result.sources) == 10
    assert [s['url'] for s in result.sources] == urls[:10]


def test_research_concatenates_all_text_blocks():
    """Join every text block in order; citations split one answer into many."""
    content = [
        search_ok('https://a'),
        text('Acme builds ', citations=[cite('https://a')]),
        text('robots'),
        text('. Values...', citations=None),
    ]
    result = researcher(FakeClient(resp(content))).research('Acme', 'dev')
    assert result.brief == 'Acme builds robots. Values...'


def test_research_concatenates_across_pause_turn():
    """Join text blocks from a paused turn and its continuation, in order."""
    first = resp([text('Acme builds '), server_tool_use(), search_ok('https://a')],
                 stop='pause_turn')
    second = resp([text('robots'), text('. Values...')])
    client = FakeClient(first, second)
    result = researcher(client).research('Acme', 'dev')
    assert result.brief == 'Acme builds robots. Values...'
    assert result.used_web is True
    assert len(client.calls) == 2


def test_pause_turn_continuation_resends_assistant_content():
    """Resend the conversation ending with the paused assistant content."""
    paused_content = [server_tool_use(), search_ok('https://a')]
    client = FakeClient(resp(paused_content, stop='pause_turn'),
                        resp([text('Done brief.')]))
    result = researcher(client).research('Acme', 'dev')
    assert len(client.calls) == 2
    first_msgs = client.calls[0]['messages']
    second_msgs = client.calls[1]['messages']
    assert len(first_msgs) == 1
    assert second_msgs[0] == first_msgs[0]
    assert second_msgs[-1] == {'role': 'assistant', 'content': paused_content}
    assert 'tools' in client.calls[1]
    assert result.brief == 'Done brief.'
    assert result.used_web is True


def test_endless_pause_turn_is_capped():
    """Stop after max_continuations + 1 calls and use what arrived."""
    pauses = [resp([search_ok('https://a'), text('part%d ' % i)], stop='pause_turn')
              for i in range(10)]
    client = FakeClient(*pauses)
    r = researcher(client, max_continuations=2)
    result = r.research('Acme', 'dev')
    assert len(client.calls) == 3
    assert all('tools' in c for c in client.calls)
    assert result.brief == 'part0 part1 part2'
    assert result.used_web is True
    assert any('pause_turn' in line for line in r.logs)


class BlockingSearchClient:
    """
    Fake client whose search calls block until released; fallback calls return at once.

    Search calls are the ones with a tools kwarg. Each waits on an Event, so a
    test can hold the search past the deadline without sleeping for real.
    """

    def __init__(self, search_response, fallback_response, max_block_s=5.0):
        self.release = threading.Event()
        self.search_response = search_response
        self.fallback_response = fallback_response
        self.max_block_s = max_block_s
        self.calls = []
        self.lock = threading.Lock()
        self.messages = self

    def create(self, **kwargs):
        """Block search calls until released; answer fallback calls immediately."""
        with self.lock:
            self.calls.append(kwargs)
        if 'tools' in kwargs:
            self.release.wait(self.max_block_s)
            return self.search_response
        return self.fallback_response

    def search_calls(self):
        """Return the recorded calls that carried tools."""
        with self.lock:
            return [c for c in self.calls if 'tools' in c]

    def fallback_calls(self):
        """Return the recorded calls without tools."""
        with self.lock:
            return [c for c in self.calls if 'tools' not in c]


@pytest.fixture
def blocking_client():
    """Return a BlockingSearchClient and release its search calls after the test."""
    client = BlockingSearchClient(
        resp([search_ok('https://late'), text('Late web brief.')]),
        resp([text('GK brief.')]),
    )
    yield client
    client.release.set()


def test_deadline_times_out_slow_search(blocking_client):
    """Fall back without tools once the search step outlasts deadline_s."""
    r = researcher(blocking_client, deadline_s=0.1)
    started = time.monotonic()
    result = r.research('Acme', 'dev')
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert elapsed >= 0.09
    assert result.brief == 'GK brief.'
    assert result.used_web is False
    assert result.note == FALLBACK_NOTE
    assert result.sources == []
    assert len(blocking_client.search_calls()) == 1
    fallback = blocking_client.fallback_calls()
    assert len(fallback) == 1
    assert 'tools' not in fallback[0]
    assert any('timed out' in line for line in r.logs)


def test_deadline_timeout_discards_late_search_result(blocking_client):
    """Keep the fallback result even if the search finishes after the deadline."""
    r = researcher(blocking_client, deadline_s=0.1)
    result = r.research('Acme', 'dev')
    blocking_client.release.set()
    time.sleep(0.1)
    assert result.brief == 'GK brief.'
    assert len(blocking_client.fallback_calls()) == 1


def test_deadline_timeout_through_get_brief(store, blocking_client):
    """Return origin fallback for a timed out search and save nothing."""
    r = researcher(blocking_client, deadline_s=0.1)
    got = get_brief(store, r, 'Acme', 'dev', now=NOW)
    assert (got.origin, got.reason, got.brief) == ('fallback', 'miss', 'GK brief.')
    assert got.note == FALLBACK_NOTE
    assert store.latest_brief('acme') is None


def test_deadline_longer_than_search_gives_web_result(blocking_client):
    """Return the normal web result when the search ends inside deadline_s."""
    blocking_client.release.set()
    r = researcher(blocking_client, deadline_s=5.0)
    result = r.research('Acme', 'dev')
    assert result.brief == 'Late web brief.'
    assert result.used_web is True
    assert result.sources == [{'url': 'https://late', 'title': 'title https://late'}]
    assert blocking_client.fallback_calls() == []
    assert not any('timed out' in line for line in r.logs)


def test_deadline_with_pause_turn_continuations_inside_limit():
    """Run pause_turn continuations in the worker when they finish in time."""
    client = FakeClient(resp([search_ok('https://a'), text('a ')], stop='pause_turn'),
                        resp([text('b')]))
    r = researcher(client, deadline_s=5.0)
    result = r.research('Acme', 'dev')
    assert len(client.calls) == 2
    assert result.brief == 'a b'
    assert result.used_web is True


def test_deadline_none_runs_without_thread(monkeypatch):
    """Search inline, with no worker thread, when deadline_s is None."""
    def no_thread(*args, **kwargs):
        raise AssertionError('no thread expected when deadline_s is None')

    monkeypatch.setattr(cr, 'threading', NS(Thread=no_thread))
    pauses = [resp([search_ok('https://a'), text('p%d ' % i)], stop='pause_turn')
              for i in range(10)]
    client = FakeClient(*pauses)
    r = researcher(client, max_continuations=2)
    assert r.deadline_s is None
    result = r.research('Acme', 'dev')
    assert len(client.calls) == 3
    assert result.brief == 'p0 p1 p2'
    assert result.used_web is True


@pytest.mark.parametrize('first', [
    resp([server_tool_use(), search_err('unavailable'), text('No luck.')]),
    RuntimeError('503'),
    resp([], stop='refusal'),
    resp([search_ok('https://a')], stop='max_tokens'),
], ids=['search_error', 'exception', 'refusal', 'max_tokens_no_text'])
def test_internal_failure_with_deadline_falls_back_once(first):
    """Make exactly one fallback call for internal failures when deadline_s is set."""
    client = FakeClient(first, resp([text('GK brief.')]))
    r = researcher(client, deadline_s=5.0)
    result = r.research('Acme', 'dev')
    assert len(client.calls) == 2
    assert 'tools' in client.calls[0]
    assert 'tools' not in client.calls[1]
    assert result.brief == 'GK brief.'
    assert result.used_web is False
    assert result.note == FALLBACK_NOTE
    assert not any('timed out' in line for line in r.logs)


def test_fallback_timeout_kwarg_passed():
    """Pass fallback_timeout_s as timeout= on the fallback call only."""
    client = FakeClient(RuntimeError('down'), resp([text('GK.')]))
    r = researcher(client, fallback_timeout_s=12.5)
    result = r.research('Acme', 'dev')
    assert result.brief == 'GK.'
    assert 'timeout' not in client.calls[0]
    assert client.calls[1]['timeout'] == 12.5
    assert 'tools' not in client.calls[1]


def test_fallback_timeout_none_omits_kwarg():
    """Omit the timeout kwarg on the fallback call when fallback_timeout_s is None."""
    client = FakeClient(RuntimeError('down'), resp([text('GK.')]))
    r = researcher(client)
    assert r.fallback_timeout_s is None
    r.research('Acme', 'dev')
    assert 'timeout' not in client.calls[1]


def test_fallback_timeout_with_deadline_timeout(blocking_client):
    """Pass timeout= to the fallback that follows a deadline timeout."""
    r = researcher(blocking_client, deadline_s=0.1, fallback_timeout_s=7.0)
    r.research('Acme', 'dev')
    assert blocking_client.fallback_calls()[0]['timeout'] == 7.0


def test_search_error_object_falls_back():
    """Fall back without tools when every search returned an error object."""
    content = [server_tool_use(), search_err('unavailable'), text('I could not search.')]
    client = FakeClient(resp(content), resp([text('General knowledge brief.')]))
    r = researcher(client)
    result = r.research('Acme', 'dev')
    assert len(client.calls) == 2
    assert 'tools' not in client.calls[1]
    assert result.used_web is False
    assert result.note == FALLBACK_NOTE
    assert result.brief == 'General knowledge brief.'
    assert result.sources == []
    assert any('unavailable' in line for line in r.logs)


def test_one_good_search_beats_one_error():
    """Treat research as web-backed when at least one search succeeded."""
    content = [search_err('too_many_requests'), search_ok('https://a'), text('Brief.')]
    client = FakeClient(resp(content))
    result = researcher(client).research('Acme', 'dev')
    assert result.used_web is True
    assert len(client.calls) == 1


def test_client_exception_falls_back():
    """Fall back without tools when the first API call raises."""
    client = FakeClient(RuntimeError('503 overloaded'), resp([text('GK brief.')]))
    r = researcher(client)
    result = r.research('Acme', 'dev')
    assert len(client.calls) == 2
    assert 'tools' not in client.calls[1]
    assert result.brief == 'GK brief.'
    assert result.used_web is False
    assert result.note == FALLBACK_NOTE


def test_exception_during_continuation_falls_back():
    """Fall back when a pause_turn continuation call raises."""
    client = FakeClient(resp([search_ok('https://a')], stop='pause_turn'),
                        TimeoutError('timeout'), resp([text('GK.')]))
    result = researcher(client).research('Acme', 'dev')
    assert len(client.calls) == 3
    assert 'tools' not in client.calls[2]
    assert result.brief == 'GK.'
    assert result.used_web is False


def test_refusal_falls_back():
    """Fall back when the research call is refused."""
    client = FakeClient(resp([], stop='refusal'), resp([text('GK brief.')]))
    result = researcher(client).research('Acme', 'dev')
    assert len(client.calls) == 2
    assert 'tools' not in client.calls[1]
    assert result.used_web is False
    assert result.brief == 'GK brief.'


def test_fallback_refusal_gives_empty_brief():
    """Return an empty brief when the fallback is refused too."""
    client = FakeClient(resp([], stop='refusal'), resp([text('ignored')], stop='refusal'))
    result = researcher(client).research('Acme', 'dev')
    assert result.brief == ''
    assert result.used_web is False


def test_max_tokens_with_text_uses_text():
    """Use the partial brief when the answer hit max_tokens but has text."""
    content = [search_ok('https://a'), text('Partial brief')]
    client = FakeClient(resp(content, stop='max_tokens'))
    r = researcher(client)
    result = r.research('Acme', 'dev')
    assert len(client.calls) == 1
    assert result.brief == 'Partial brief'
    assert result.used_web is True
    assert any('max_tokens' in line for line in r.logs)


def test_max_tokens_without_text_falls_back():
    """Fall back when max_tokens hit before any text arrived."""
    content = [server_tool_use(), search_ok('https://a')]
    client = FakeClient(resp(content, stop='max_tokens'), resp([text('GK.')]))
    result = researcher(client).research('Acme', 'dev')
    assert len(client.calls) == 2
    assert 'tools' not in client.calls[1]
    assert result.brief == 'GK.'
    assert result.used_web is False


def test_fallback_failure_returns_empty_brief():
    """Return an empty brief and do not raise when the fallback call fails too."""
    client = FakeClient(RuntimeError('down'), RuntimeError('still down'))
    r = researcher(client)
    result = r.research('Acme', 'dev')
    assert result.brief == ''
    assert result.used_web is False
    assert result.note == FALLBACK_NOTE
    assert len(client.calls) == 2
    assert any('fallback failed' in line for line in r.logs)


def test_no_search_but_text_is_not_web():
    """Mark a text-only answer with no searches as not web-backed."""
    client = FakeClient(resp([text('From memory.')]))
    result = researcher(client).research('Acme', 'dev')
    assert len(client.calls) == 1
    assert result.brief == 'From memory.'
    assert result.used_web is False
    assert result.note == NO_SEARCH_NOTE
    assert NO_SEARCH_NOTE != FALLBACK_NOTE
    assert result.sources == []


def test_no_search_brief_is_fallback_and_not_cached(store):
    """Return a no-search answer as fallback with NO_SEARCH_NOTE and do not cache it."""
    client = FakeClient(resp([text('From memory.')]))
    got = get_brief(store, researcher(client), 'Acme', 'dev', now=NOW)
    assert (got.origin, got.note) == ('fallback', NO_SEARCH_NOTE)
    assert store.latest_brief('acme') is None


def test_empty_answer_with_search_falls_back():
    """Fall back when searches succeeded but no text came back."""
    client = FakeClient(resp([search_ok('https://a'), text('   ')]), resp([text('GK.')]))
    result = researcher(client).research('Acme', 'dev')
    assert len(client.calls) == 2
    assert result.brief == 'GK.'
    assert result.used_web is False


def test_fallback_concatenates_text_blocks():
    """Join every text block in the fallback answer too."""
    client = FakeClient(RuntimeError('x'), resp([text('Part one, '), text('part two.')]))
    result = researcher(client).research('Acme', 'dev')
    assert result.brief == 'Part one, part two.'


def test_end_to_end_get_brief_with_researcher(store):
    """Run get_brief with a real CompanyResearcher on a fake client and cache it."""
    content = [search_ok('https://a'), text('Acme '), text('brief.')]
    client = FakeClient(resp(content))
    got = get_brief(store, researcher(client), 'Acme', 'dev', now=NOW)
    assert (got.origin, got.reason, got.brief) == ('web', 'miss', 'Acme brief.')
    again = get_brief(store, researcher(client), 'acme', 'dev', now=NOW + DAY)
    assert again.origin == 'cache'
    assert len(client.calls) == 1
