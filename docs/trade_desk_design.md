# Trade Desk Ritual: Design

Status: designed, not built. Department 3 in plan.md.
An accountability layer, not a trading bot.

## Problem
Akama trades XAUUSD on a $100k FTMO challenge (MT5, manual execution). Strategy: H1/H4/Daily
alignment via TradingView indicators, pre-drawn zones, break > close > retest > rejection entry,
fixed 0.2 lots, SL and TP set at entry. The account is intact; the process is not. He spots
setups in the morning, the wait is long, he gets absorbed driving or in other projects, and
forgets to come back.

## Guardrails (enforced in code)
1. **No capability:** no broker credentials on the Jetson, no order code, v2 hub receive-only.
2. **Neutral data:** hints and event context carry only Akama's inputs and checklist facts.
3. **Output filter:** during trade desk turns each sentence is checked before TTS. Patterns
   like "you should buy/sell", "I'd take", "price will", "good entry", "likely to" are replaced
   with: "That's your call. I can run the checklist or log your decision."
4. **Driving:** at any step, driving intent logs the alert as skipped (tag `driving`), closes
   the flow, and speaks one line: "Logged as skipped. Drive safe." Bridge fast path, no Claude call.

---

## 1. Architecture

Principle: **the node owns the flow; Claude owns the phrasing.**

```
  stt_node ──► claude_bridge_node (+ tools, guardrail filter) ──► tts_node
                   │ services          ▲ /proactive_events
                   ▼                   │
               trade_desk_node (flows, rules, scheduler, trade_desk.db)
                   ▲ /trade_desk/alerts
               webhook hub (v2)
```

- `trade_desk_node`: flow engine (persisted, resumable), scheduler with injectable clock,
  scoring, own DB at `~/companion_ws/data/trade_desk.db`.
- Bridge changes: tools mapped to services; subscribes `/proactive_events` (generic bus,
  presence uses it too); injects flow context when a flow is active; driving fast path;
  per-sentence guardrail filter.

### Topics
| Topic | Type | From → To | QoS |
|---|---|---|---|
| `/trade_desk/alerts` | TradeAlert | hub or bridge → trade_desk | reliable, depth 50 |
| `/proactive_events` | ProactiveEvent | trade_desk, presence → bridge | reliable, depth 20 |
| `/trade_desk/state` | String (JSON) | trade_desk → debug/UI | transient_local |

### Services (`/trade_desk/`)
| Service | Purpose | Claude tool |
|---|---|---|
| `get_context` | Today's plan, active flow, step, hint | `trade_desk_context` |
| `start_flow` | Begin morning / alert / journal | `trade_desk_start` |
| `submit_step` | Answer current step | `trade_desk_answer` |
| `report_alert` | Voice-reported zone hit | `trade_desk_zone_hit` |
| `get_scores` | Day or week summary | `trade_desk_scores` |
| `abort_driving` | Log skip, close flow | none (bridge fast path) |

### Interfaces (`tales_interfaces` package)
```
# msg/TradeAlert.msg
string alert_id              # uuid4
string source                # "voice" | "tradingview"
string symbol                # "XAUUSD"
string zone_label            # matches a plan zone, "" if unknown
string direction             # "bull" | "bear" | ""
float64 price                # 0.0 if unknown
string timeframe
builtin_interfaces/Time triggered_at
string raw_json

# msg/ProactiveEvent.msg
string event_id
string source                # "trade_desk" | "presence"
string kind                  # plan_nudge | alert | alert_reminder | journal_prompt | summary
uint8 priority               # 0 low .. 3 urgent
string context_json          # facts to phrase; never directional
builtin_interfaces/Time created_at
builtin_interfaces/Time expires_at

# srv/StartFlow.srv
string flow
string alert_id
---
bool ok
string flow_id
string step
string hint
string error

# srv/SubmitStep.srv
string flow_id
string step
string answer_json
string raw_text
---
bool ok
string step                  # next step or "done"
string hint
string readback
string[] errors
bool done

# srv/GetContext.srv
---
string context_json

# srv/GetScores.srv
string period                # "day" | "week"
string date
---
string summary_json
```

---

## 2. SQLite schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- plan_deadline '08:30', journal_time '20:00', response_window_min '15',
-- alert_reminder_min '10', timezone 'America/New_York', schema_version

CREATE TABLE plans (
  id INTEGER PRIMARY KEY,
  trade_date TEXT NOT NULL,
  symbol TEXT NOT NULL DEFAULT 'XAUUSD',
  align_daily TEXT CHECK (align_daily IN ('bull','bear','neutral')),
  align_h4    TEXT CHECK (align_h4    IN ('bull','bear','neutral')),
  align_h1    TEXT CHECK (align_h1    IN ('bull','bear','neutral')),
  alignment   TEXT CHECK (alignment   IN ('bull','bear','mixed')),
  bull_trigger TEXT, bear_trigger TEXT,
  windows_json TEXT NOT NULL DEFAULT '[]',
  alerts_confirmed INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','confirmed','superseded')),
  created_at TEXT NOT NULL, confirmed_at TEXT,
  supersedes_id INTEGER REFERENCES plans(id)
);
CREATE UNIQUE INDEX one_confirmed_plan_per_day ON plans(trade_date) WHERE status = 'confirmed';

CREATE TABLE zones (
  id INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES plans(id),
  label TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('bull','bear')),
  price_low REAL NOT NULL,
  price_high REAL NOT NULL CHECK (price_high >= price_low),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','hit','invalidated')),
  UNIQUE (plan_id, label)
);

CREATE TABLE alerts (
  id TEXT PRIMARY KEY,
  plan_id INTEGER REFERENCES plans(id),
  zone_id INTEGER REFERENCES zones(id),
  source TEXT NOT NULL CHECK (source IN ('voice','tradingview')),
  symbol TEXT NOT NULL, direction TEXT, price REAL,
  triggered_at TEXT NOT NULL, received_at TEXT NOT NULL, responded_at TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','in_checklist','decided','skipped_driving','expired')),
  in_window INTEGER,
  raw_json TEXT
);

CREATE TABLE checklist_runs (
  id INTEGER PRIMARY KEY,
  alert_id TEXT NOT NULL REFERENCES alerts(id),
  started_at TEXT NOT NULL, completed_at TEXT,
  capacity_ok INTEGER, zone_valid INTEGER, news_clear INTEGER, not_extended INTEGER,
  result TEXT CHECK (result IN ('pass','fail','aborted')),
  failed_items_json TEXT
);

CREATE TABLE decisions (
  id INTEGER PRIMARY KEY,
  alert_id TEXT REFERENCES alerts(id),
  plan_id INTEGER REFERENCES plans(id),
  checklist_run_id INTEGER REFERENCES checklist_runs(id),
  decided_at TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('took','skipped')),
  reason_text TEXT,
  compliant INTEGER NOT NULL,
  entry REAL, sl REAL, tp REAL, lots REAL,
  outcome TEXT CHECK (outcome IN ('win','loss','be','open')),
  outcome_r REAL
);

CREATE TABLE decision_tags (
  decision_id INTEGER NOT NULL REFERENCES decisions(id),
  tag TEXT NOT NULL CHECK (tag IN ('per_plan','driving','no_rejection','news','not_aligned',
      'extended','zone_invalid','off_window','late_response','no_checklist','other')),
  source TEXT NOT NULL CHECK (source IN ('claude','keyword','derived')),
  PRIMARY KEY (decision_id, tag)
);

CREATE TABLE journal_entries (
  id INTEGER PRIMARY KEY, trade_date TEXT NOT NULL UNIQUE,
  completed_at TEXT NOT NULL, reflection TEXT
);

CREATE TABLE flow_state (
  flow_id TEXT PRIMARY KEY, flow TEXT NOT NULL, step TEXT NOT NULL, alert_id TEXT,
  data_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK (status IN ('active','done','aborted','expired')),
  started_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE nudges (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, trade_date TEXT NOT NULL,
  ref_id TEXT, sent_at TEXT NOT NULL, snoozed_until TEXT
);

CREATE TABLE scores_daily (         -- cache; always recomputable from raw tables
  trade_date TEXT PRIMARY KEY,
  plan_set INTEGER, plan_on_time INTEGER,
  alerts_total INTEGER, alerts_responded INTEGER,
  decisions_total INTEGER, decisions_compliant INTEGER,
  journal_done INTEGER, score REAL, computed_at TEXT NOT NULL
);
```

---

## 3. Conversation flows

**Global:** driving at any step → ABORT_DRIVING. "Cancel" → aborted, partial kept as draft.
No answer 3 min → paused, resumable. Invalid answer → re-ask with the error.

### Morning check-in
```
ASK_ALIGNMENT    Daily, H4, H1: bull/bear/neutral → derive bull | bear | mixed
ASK_ZONES        loop: label, side, range; "done" with >= 1 zone
ASK_BULL_TRIGGER free text ("skip" allowed)
ASK_BEAR_TRIGGER free text (at least one trigger required)
ASK_WINDOWS      [{start,end}], may be empty
READBACK         "correct" → SAVE | "change X" → that step → READBACK
SAVE             status confirmed
REMIND_ALERTS    "set" → alerts_confirmed=1 | "later" → reminder +15 min
DONE
```
Mixed alignment is read back as his own rule ("Your rules require alignment"), not advice.

### Alert response
```
TRIGGER          voice report or hub TradeAlert → alert row, match zone by label or price
IDENTIFY_ZONE    unmatched → "Which zone?"
CAPACITY         driving → ABORT_DRIVING | no → fail(capacity) → AWAIT_DECISION | yes ↓
ZONE_VALID → NEWS → EXTENDED
RESULT           pass: "Checklist passed. Your call." | fail: lists failed items
AWAIT_DECISION   took/skipped + why | "give me a minute" → PENDING (reminders, max 2)
LOG_DECISION     tags: Claude (enum) + keyword fallback + derived; took → optional entry/SL/TP
DONE
```
Capacity is asked first so driving ends the interaction in one exchange.
Derived tags: `off_window`, `late_response`, `no_checklist`.

### Evening journal
```
LIST_OPEN → ASK_DECISION per undecided alert → ASK_OUTCOMES (win/loss/be, R)
→ ASK_REFLECTION (skippable) → SUMMARY (score + weakest component) → DONE
```

### Nudges (scheduler every 30 s)
| Rule | Condition | Action |
|---|---|---|
| Plan | Trading day, no confirmed plan, now ≥ plan_deadline | Nudge; once more +60 min |
| Alerts not set | Confirmed, alerts_confirmed=0, +15 min | Once |
| Alert reminder | Pending/in_checklist for alert_reminder_min | Max 2, then expired |
| Journal | now ≥ journal_time, no journal | Nudge; once more +90 min |
| Weekly summary | Friday after journal | Summary |
Snooze "later" = 30 min. Driving suppresses non-urgent nudges 45 min. After rung 2: hold
non-urgent nudges until Akama is in view.

---

## 4. Scoring

D = trading days (Mon to Fri minus holidays), A = alerts.

| Component | Formula |
|---|---|
| P plan rate | (on-time plans + 0.5 × late plans) / D |
| R response rate | alerts responded within response_window_min / A. "Driving" counts as a response |
| C compliance | compliant decisions / all decisions |
| J journal rate | days journaled / D |

Compliance: took + pass = compliant; took + fail or no checklist = not; skipped (any reason,
including driving) = compliant; unplanned trade = not. **Scores process, not profit.**

```
Score = 100 × Σ(w_i × X_i) / Σ(w_i), over components with nonzero denominators
weights P 0.30, R 0.30, C 0.30, J 0.10
```
Weekly = ratio of sums, not average of daily scores.
Streak = consecutive trading days with on-time plan, no non-compliant decision, full response.

---

## 5. Build phases (v1 about two weekends)

| Phase | Scope | Tests |
|---|---|---|
| 0 | `tales_interfaces`, schema + migrations, repository layer, Clock abstraction | In-memory SQLite: schema, constraint rejections |
| 1 | FlowEngine, morning + alert flows, node + services | Every transition incl. driving/cancel/invalid; restart-resume; scripted `ros2 service call` runs |
| 2 | Bridge: tools, context injection, proactive turn, driving fast path, guardrail filter | Voice E2E; red team ("Should I buy?", "Where's gold going?", "Yes or no?") zero leaks; driving = one line + correct logs |
| 3 | Journal, scoring, scheduler, summaries | Scoring fixtures vs hand math; simulated week with fake clock |
| 4 | One-week live trial | Plan every trading day, zero unlogged alerts, zero leaks, no stuck flows |

v1.5: phone notifier (mirrors urgent ProactiveEvents with one-tap replies), watcher, alert
context agent. This is where the away-from-home problem is actually solved.

---

## 6. v2 hub contract

`trade_desk_node` only consumes `TradeAlert`. Voice already produces it (`source: voice`);
the hub is a second producer, so it drops in without refactoring.

```json
{
  "schema": "tales.trade_alert.v1",
  "secret": "<shared secret>",
  "symbol": "{{ticker}}",
  "price": {{close}},
  "timeframe": "{{interval}}",
  "triggered_at": "{{timenow}}",
  "zone_label": "Z1",
  "direction": "bull",
  "note": "optional"
}
```
- TradingView alert named with the same zone label as the morning plan (join key).
- Hub: authenticate secret (constant-time), validate schema, dedupe (symbol + zone within
  5 min), normalize symbol, generate alert_id, publish TradeAlert, return 200 fast.
  Receive-only, no broker calls ever.
- Matching zone → alert flow (priority 3). No match → logged, mentioned, no checklist.
  No plan today → logged + plan nudge.
- Breaking change → `tales.trade_alert.v2`; hub supports both during migration.
- TradingView webhooks need a paid plan and post only to ports 80/443: public HTTPS via
  Cloudflare Tunnel or a relay VM.
