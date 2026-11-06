# DSH token accounting — native schema, 2026-11-05 night baseline, tonight's protocol

Written 2026-11-06 ~23:00 (+08:00) by `usage-eng` for the dual-mode overnight experiment.
Everything below is read from files DSH itself wrote; no number here was estimated, and
each section names the command and the file path that reproduce it.

Tool: [`tools/usage_meter.py`](../../tools/usage_meter.py) (stdlib only, read-only on `~/.dsh`).
Tests: [`tests/test_usage_meter.py`](../../tests/test_usage_meter.py) — **29 passed**.

---

## 0. TL;DR

| Question | Answer |
|---|---|
| Where is DSH's authoritative token count? | `~/.dsh/storages/session_projcache/sessions/<session>.json` → `record.rows.tokenUsage.val.totals` |
| Does a session file expose per-request *and* hourly buckets? | **Per-request: yes** (`assistant/message` events carry `data.usage` + `time`). **Hourly: only derivable from the raw log, not stored in the procache** — see §4. The disk-derived hourly table reproduces the 2026-11-05 session exactly (sums match the procache to the token). |
| Prior night cost | **202,265,396 tokens over 656 requests = 308,331 tokens/request**, 98.46 % cache-read |
| Is "tokens/request" comparable across a night? | **No, not naively.** A fresh session always looks cheap because the prompt grows with every request. Compare like-for-like at the same request index (§6) or per-mode windows within one session. |
| Tonight's per-mode table | Only from labeled snapshots; see the one-liner in §5. |

---

## 1. Native schema (verified against live files)

### 1a. Procache projection — authoritative running totals

`~/.dsh/storages/session_projcache/sessions/<session-id>.json` (`version: 7`, `record.formatVersion: 4`):

```text
record.identity.cwd                    # working directory; this is how a session is attributed
record.identity.createdAt              # epoch ms
record.rows.tokenUsage.val.totals      # {uncachedInputTokens, outputTokens, cacheReadTokens, cacheWriteTokens}
record.rows.tokenUsage.val.last        # {turn, step, buckets:{...}} — last request only
record.rows.sessionStats.val           # {turns, steps, llmMs, toolMs, ttftMs, decodeTokens}
record.rows.contextPressure.val        # {surfaceTokens, contextWindow, pressureTokens, sampledSurfaceTokens}
record.rows.contextBreakdown.val       # {surfaceTokens, breakdown:{systemTokens, toolsTokens, messageTokens}}
```

`steps` is the number of LLM requests (one per step). `pressureTokens` equals the last
request's prompt size including cache-read tokens; `surfaceTokens` is DSH's sampled
"conversation being resent" estimate. Session ids appear both with and without the
`session-` prefix depending on DSH version, so both forms are accepted by the tool.

### 1b. Raw event log — per-request usage with timestamps

`~/.dsh/sessions/<cwd-slug>/<session-id>/session.v4.jsonl.zstd` (zstd-compressed; the
`zstd` binary at `/opt/homebrew/bin/zstd` decodes it — Python `zstandard` is not installed).

```
{"type":"session", ..., "delegationDepth":0, "parentSession": "…" (subagents only)}
{"type":"assistant/message", "time": 1793878682902, "data":{"turn":1,"step":1,
  "usage":{"inputTokens":19792,"outputTokens":215,"totalTokens":20007,"cacheReadTokens":19712}}}
```

* `usage.inputTokens` = **uncached** input; `usage.cacheReadTokens` = cache-read input;
  `usage.totalTokens = inputTokens + outputTokens + cacheReadTokens` (verified: raw-log sums
  equal the procache totals **to the token**, all four fields).
* `time` is the request *completion* timestamp in epoch ms — this is what enables hourly buckets.
* The first line's `delegationDepth` / `parentSession` distinguish root sessions (`depth 0`)
  from teammate/subagent sessions (`depth 1`, `parentSession` set). Subagent spend is **not**
  included in the parent's totals; `usage_meter.py snapshot` records it separately.

---

## 2. Reproduce every number

```bash
cd /Users/ceontou/Documents/code/seer-automation-night
export PYTHONPATH=.pydeps

python3 tools/usage_meter.py sessions            # all sessions for this repo: depth, steps, totals
python3 tools/usage_meter.py hours --session session-fb389015-5471-459e-837c-13d1813e9b52
python3 tools/usage_meter.py baseline            # writes baseline JSON + live comparison
python3 tools/usage_meter.py report              # per-mode table from snapshots
python3 tools/usage_meter.py snapshot --label EFFICIENT   # append one measurement point
python3 -m pytest -q tests/test_usage_meter.py   # 29 passed
```

`hours` prints the raw-log sum next to the procache totals and says `OK`/`MISMATCH`. For a
finished session it is `OK`; for a live session it can show `MISMATCH` for the in-flight
request and turns `OK` a few seconds later (observed once tonight, then resolved).

Other paths used:

* baseline JSON: `data/overnight/20261106_dual_mode/baseline_prior_night.json`
* snapshot ledger: `data/overnight/20261106_dual_mode/model_usage.jsonl` (schema
  `usage_snapshot.v1`, `kind:"usage_snapshot"`; the dual-mode ledger writes other `kind`s
  to the same file and neither truncates it)

---

## 3. Prior-night baseline (measured)

Session `session-fb389015-5471-459e-837c-13d1813e9b52`, cwd `/Users/ceontou/Documents/code`
(a different directory from tonight's, so it never appears in auto-detection).

* Source: `~/.dsh/storages/session_projcache/sessions/session-fb389015-5471-459e-837c-13d1813e9b52.json`
* Raw log: `~/.dsh/sessions/--Users-ceontou-Documents-code--/session-fb389015-5471-459e-837c-13d1813e9b52/session.v4.jsonl.zstd`
* Created 2026-11-05T14:01:33+08:00; first request 2026-11-05T19:38:02; last request 2026-11-06T07:37:21.

| metric | value |
|---|---|
| requests (= steps) / turns | 656 / 3 |
| total tokens | **202,265,396** |
| uncached input | 3,104,715 |
| output | 323,945 |
| cache read | 198,836,736 |
| cache write | 0 |
| **tokens per request** | **308,331.4** (uncached 4,732.8 + cache-read 303,104.8 + output 493.8) |
| cache hit `cacheRead/(cacheRead+uncached)` | **98.46 %** |
| prompt tokens/request: mean / median / p90 / p95 / max | 307,838 / **320,863** / 513,076 / 531,158 / **558,337** |
| requests with prompt <50K / 50–150K / 150–300K / ≥300K | 25 / 106 / 169 / **356 (54 %)** |
| surfaceTokens / contextWindow | 414,213 / 1,000,000 (41.4 %) |
| context breakdown at last sample | system 1,973 + tools 8,544 + messages 412,240 |

### 3a. Hourly buckets, derived from the raw log (not from the GUI)

| local hour (Asia/Shanghai) | requests | uncached | output | cache read | total | cache hit |
|---|---:|---:|---:|---:|---:|---:|
| 2026-11-05 19 | 80 | 101,491 | 29,661 | 4,678,784 | 4,809,936 | 97.9 % |
| 2026-11-05 20 | 0 | — | — | — | — | — |
| 2026-11-05 21 | 207 | 300,657 | 108,345 | 39,687,936 | 40,096,938 | 99.2 % |
| 2026-11-05 22 | 125 | 102,672 | 62,294 | 42,055,936 | 42,220,902 | 99.8 % |
| 2026-11-05 23 | 76 | 60,925 | 37,989 | 31,094,400 | 31,193,314 | 99.8 % |
| 2026-11-06 00 | 40 | 447,497 | 24,177 | 17,666,048 | 18,137,722 | 97.5 % |
| 2026-11-06 01 | 45 | 1,490,145 | 23,733 | 20,379,648 | 21,893,526 | 93.2 % |
| 2026-11-06 02 | 16 | 11,943 | 8,182 | 8,097,152 | 8,117,277 | 99.9 % |
| 2026-11-06 03 | 14 | 11,204 | 7,497 | 7,241,600 | 7,260,301 | 99.8 % |
| 2026-11-06 04 | 9 | 3,776 | 1,892 | 4,714,880 | 4,720,548 | 99.9 % |
| 2026-11-06 05 | 13 | 6,852 | 4,260 | 6,871,680 | 6,882,792 | 99.9 % |
| 2026-11-06 06 | 11 | 7,971 | 5,880 | 5,882,496 | 5,896,347 | 99.9 % |
| 2026-11-06 07 | 20 | 559,582 | 10,035 | 10,466,176 | 11,035,793 | 94.9 % |
| **total** | **656** | **3,104,715** | **323,945** | **198,836,736** | **202,265,396** | **98.5 %** |

The 00–06 subtotal is **72,908,513** tokens over 158 requests. The column sums equal the
procache totals exactly (same four fields, no rounding), which is the proof that the hourly
table is reproducible from disk rather than reconstructed.

Reproduce: `python3 tools/usage_meter.py hours --session session-fb389015-5471-459e-837c-13d1813e9b52 --date 2026-11-06`.

### 3b. Why the prior night cost ~308K tokens per request

Every LLM request resends the whole conversation. DSH's prompt cache served 98.5 % of that
resend from cache, but cache reads are still billed/counted as tokens, so the total is
essentially `Σ(prompt size at each request)`. In this session the prompt started at 19,792
tokens and grew roughly linearly as tool results accumulated, reaching 558,337 tokens by the
end (median 320,863). 656 requests × 307,838 mean prompt ≈ 202M tokens. The cost driver is
therefore **number of requests × size of context they carry**, not the model's visible output
(median output was only 315 tokens/request, 0.16 % of the total). That is the lever the
dual-mode experiment must move: fewer requests and slower context growth, not shorter answers.

---

## 4. What is measurable — and what is NOT

**Measurable from disk (reproducible):**

1. Per-session totals and steps — procache `tokenUsage.val.totals` / `sessionStats.val`.
2. Per-request usage split (uncached / cache-read / output) and prompt size, with completion
   timestamps — raw log `assistant/message` events.
3. Per-hour buckets — by aggregating (2); reconciles exactly with (1) for finished sessions.
4. Context composition at snapshot time (system / tools / messages) — `contextBreakdown.val`.
5. Per-mode deltas — from labeled snapshots (`snapshot`/`report`); this is the *only* mode
   attribution, because the mode an interval ran under is operator-written, not inferable.
6. Subagent spend, separately — teammate sessions have their own procache/raw log files and a
   `parentSession` pointer.

**NOT reliably measurable from disk:**

1. **The GUI's hourly table as the GUI computes it.** Note carefully: the hourly numbers *are*
   reproducible from the raw log (§3a), but the bucket is the *completion* timestamp of the
   request. If the GUI buckets by request *start* time or by its own sampling, a request that
   crosses an hour boundary can move a few hundred K tokens between two cells. The disk-derived
   total is exact; individual cell placement may differ from the GUI by at most the requests
   straddling each boundary. So: the GUI table is not the source to cite; `hours` is.
2. Money/price. No per-token prices or billing records exist on disk. This report stays in tokens.
3. Cache-write cost. This provider reports `cacheWriteTokens = 0` in every session inspected
   here, so cache-write volume cannot be measured (and no cost can be inferred from it).
4. Retried requests that never produced an `assistant/message` (e.g. transport errors; the
   prior night had 8 `assistant/attempt` failures with no usage). Their token cost, if any, is
   invisible.
5. Per-mode attribution without snapshots. If a switch happens between two snapshots, the whole
   interval is attributed by the label on record — the meter cannot split it.
6. "The night's total" from one file: the root session's totals exclude teammate sessions.
   `snapshot` records delegate spend as a separate cumulative block; `sessions` lists every
   session for the repo.

---

## 5. Measurement protocol for tonight's mode windows

**Snapshot convention (important):** a snapshot closes the interval that just ended, so
`--label` names the mode that was active **during that interval**. At a mode switch:
**snapshot first, then flip the mode** — never the other way round.

- First snapshot of the night: its delta is measured from session start (`basis=session_start`);
  early tokens land on that first label. A `PREP` record was already seeded at
  `2026-11-06T22:57:50+08:00` (42 requests, 2,537,528 total tokens), so the first labeled
  `EFFICIENT`/`MAX_RESULTS` snapshot starts a clean window instead of swallowing bring-up.
- Every 30–60 min inside a window: optional extra snapshots with the same label; they only split
  the interval, `report` re-aggregates by label.

**One-liner (paste into the report):**

```bash
cd /Users/ceontou/Documents/code/seer-automation-night && PYTHONPATH=.pydeps python3 tools/usage_meter.py snapshot --label EFFICIENT   # (a) at each mode switch, before flipping; then PYTHONPATH=.pydeps python3 tools/usage_meter.py report   # (b) per-mode table
```

Label values: `EFFICIENT`, `MAX_RESULTS`, plus `PREP`/`RECOVERY` if a window should be excluded
from the mode comparison. The tool accepts any label and reports it as its own row.

What `report` prints: per label — snapshots, requests, total, avg total/request, avg total/hour,
cache hit, surfaceTokens first→last; plus OVERALL, subagent spend, comparison against the prior
night, flags (cache hit <60 %, surface growth >5,000 tokens/request, thin intervals, session
change, delta-chain integrity) and the source paths.

**Don't forget the subagents.** The root session's totals only cover the orchestrator. At the
PREP seed the four teammate sessions had already spent **12.68M tokens** against the root
session's 2.54M — i.e. in this workflow the teammates can dominate spend. `report` prints their
delta as a separate table; `sessions` shows every session the repo has.

---

## 6. First live reading of tonight (2026-11-06 ~22:56, timestamped observation, not a result)

Command: `python3 tools/usage_meter.py baseline`

| | prior night **first 41 requests** | tonight first 41 requests |
|---|---:|---:|
| total tokens | 1,793,069 | 2,421,742 |
| total/request | 43,733.4 | **59,066.9 (+35.1 % at the same index)** |
| first prompt | 19,792 | 23,750 |
| context breakdown (last sample) | system 1,973 + tools 8,544 + messages 412,240 (end of night) | system 1,871 + tools 10,228 + messages 75,410 (request 42) |
| subagent sessions at the same moment | not recorded | 4 sessions, 12.68M tokens cumulative |

One minute later the seeded `PREP` record measured 42 requests / 2,537,528 tokens, and the
like-for-like gap had widened to **+36.8 %** (baseline first 42 requests 1,855,533 tokens vs
tonight 2,537,528). These are timestamped observations, not results.

Interpretation, honestly: comparing the live session's *lifetime* average (59,067/request) to
the prior night's *full-night* average (308,331) would show a fake "80.8 % saving" — that is the
fresh-session artifact. At the same request index tonight is actually ~35 % more expensive,
because tonight's fixed prompt is larger (tools +1.7K tokens) and its tool results accumulate
faster. Tonight's saving must therefore be earned *within* the run: fewer requests, smaller tool
outputs, and slower context growth in the EFFICIENT windows, measured by the per-mode table.
`report`'s like-for-like block and its `surface first→last` columns are the honest instruments.

---

## 7. Tests

`python3 -m pytest -q tests/test_usage_meter.py` → **29 passed** (0 failed). Coverage:
procache schema parsing and missing-row tolerance, total/cache-hit maths, snapshot deltas
(previous snapshot vs session start, regression clamping, foreign-session isolation), JSONL
append/read with truncated tail + foreign `kind` lines, root-vs-subagent auto-detection,
delegate spend summation, raw-log per-request parsing, hourly bucketing + day filter,
like-for-like prefix sums, per-request distribution, per-label aggregation + delta-chain
integrity, anomaly flags (incl. that non-mode labels like `PREP` are excluded from the
mode comparison), baseline JSON provenance/reconciliation, and CLI round-trips
(`snapshot` → `report --json` / table, `hours`, `sessions`, error paths).

## 8. Known gaps / care

- **Auto session** = newest *top-level* session (raw-log `delegationDepth 0`) whose
  `identity.cwd` is this repo. Literal newest would silently measure a teammate session;
  `--session newest` is available if that is what you want. `--session <id|path>` overrides.
- Snapshot records store the measured session's `source` path and `raw_log`, so every later
  number points back to the file it came from.
- Long first run (`hours` on a 12 h session) decompresses ~2.8 MB zstd in about a second; it
  streams, so memory stays bounded.
- Tonight's `model_usage.jsonl` also receives non-metric lines from the dual-mode ledger; they
  carry a different `kind` and are skipped by `report` (counted in the header line).
