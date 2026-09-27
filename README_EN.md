# capguard

[中文](README.md) | [English](README_EN.md)

**Disk Capacity Sentinel** — what wakes you at 3 a.m. is usually one of two things: someone changed a config ([cfgdrift](https://github.com/tomlen045/cfgdrift) handles that), or **a disk is about to fill up (capguard handles this one)**.

capguard watches disk capacity: sample → archive → predict days-to-full → alert on growth.
It never looks at a single data point — it watches **level AND trend**: 90% used but growing 1%/month is a false alarm; 75% growing +3%/day is the real emergency.

[![tests](https://img.shields.io/badge/self--tests-18%2F18-green)]() [![deps](https://img.shields.io/badge/deps-zero-yellow)]() [![license](https://img.shields.io/badge/license-MIT-blue)]()

---

## The problem it solves

* A backup volume creeps to 92%. Level-only monitoring says "almost full" — but **how long do you actually have?** capguard fits daily growth (least squares) and answers: *1.3 days*.
* An old volume sits at 90% but hasn't grown in six months. Classic alert fatigue. capguard's **trend axis refuses to extrapolate when growth is non-positive** — no wolf-crying. (The level axis still fires past the F-line. The two axes are independent and never impersonate each other.)
* Samples with no follow-up; postmortems can't reconstruct who knew what. Every sample verdict is appended to a per-object JSONL event file; `accept` stamps human sign-off. The audit chain is append-only.
* Need to replay history? `predict --as-of` recomputes **what capguard would have said at any past moment**, using only data available then.
* Where does data come from? Either `--live` (reads local `df`, macOS/Linux) or `--used/--total` manual reporting — feed it numbers from your monitoring platform / inspection scripts for a consistent cross-machine baseline.
* Will it invent a prediction? **No. With fewer than 3 samples it explicitly says "not enough samples, not extrapolating."**

## Install

```bash
# Option 1: single file, stdlib only (Python 3.8+)
curl -fsSLO https://raw.githubusercontent.com/tomlen045/capguard/main/capguard.py

# Option 2: full repo (README / demo artifacts)
git clone https://github.com/tomlen045/capguard.git
```

## 30-second start

```bash
# 1) One sample (manual reporting: feed df numbers from your monitoring)
python3 capguard.py sample --host web01 --path /data --used 780 --total 1000

# 1') Or read local disks directly
python3 capguard.py sample --host web01 --live

# 2) Inspect: who is over the line? exit 0=safe 1=alert 2=usage error
python3 capguard.py check

# 3) Predict: days until full?
python3 capguard.py predict --host-path "web01|/data"

# 4) Replay history: should it have alerted back then?
python3 capguard.py predict --host-path "web01|/data" --as-of "2026-09-25T10:00:00+08:00"

# 5) Accept the current level as the new normal (e.g. ticket filed) — stamped into the event log
python3 capguard.py accept --host-path "web01|/backup" --memo "expansion ticket filed"

# 6) Audit trail: every sample and accept, append-only
python3 capguard.py log --n 20
```

## Two-axis verdicts

| Axis | Default | Meaning |
|---|---|---|
| Level P-line | `--pct-p 80` | used ≥80% → WARN |
| Level F-line | `--pct-f 90` | used ≥90% → CRITICAL |
| Trend P-line | `--days-p 14` | projected ≤14 days left → WARN |
| Trend F-line | `--days-f 7` | projected ≤7 days left → CRITICAL |

Either axis firing means an alert; CRITICAL outranks WARN. Exit codes are a stable `0/1/2` (I4) — wire it straight into cron/CI:

```cron
# 8 a.m. daily inspection; check never writes to disk (I3)
0 8 * * * /usr/bin/python3 /opt/capguard/capguard.py --root /opt/capguard check || echo "capguard: disk over the line" | mail -s "disk alert" ops@example.com
```

## Six subcommands

| Command | What it does | Writes? |
|---|---|---|
| `sample` | Take a reading (`--live` real disks / `--used --total` manual; `--ts` supports backfill) | archive + events |
| `check` | Re-run verdicts for all (or one) objects | **no writes (I3)** |
| `predict` | Days-until-full; `--as-of` replays history | **no writes (I3)** |
| `accept` | Human sign-off: this level is the accepted new normal | events |
| `log` | Show the append-only event stream (`--n` last N) | no |
| `show` | Archive overview: objects / sample counts / thresholds | no |

## Five invariants (selftest 18/18 green)

| # | Invariant | How it's verified |
|---|---|---|
| I1 | Archive is data-only: `*.cap.json` / `*.cap-events.jsonl` never contain executable semantics | T9 scans archive+events for code markers |
| I2 | Events are append-only: JSONL is never rewritten | T8 reads back line-by-line around a new sample |
| I3 | check/predict write nothing: pure read-only computation | T6 mtime snapshot of the whole root dir |
| I4 | Exit codes: stable 0/1/2 | asserted throughout T1–T18 |
| I5 | Verdicts are reproducible: repeated check output is byte-identical | T7 two consecutive runs diffed |

## Field demo (skewed data, 1 anomaly in 10)

`demo_skewed.py` runs a realistic scenario: 5 hosts, 10 mount points, 6 days of
inspection data. Nine objects are perfectly healthy; only backup01's /backup is
being eaten by backup files:

```text
OK       app01|/            level 30.9%  +1.4KB/day  503.9 days left
... (all 8 other healthy objects: OK, zero noise)
CRITICAL backup01|/backup   level 92.0%  +60.0KB/day  1.3 days left
         · level 92.0% ≥ F-line 90%
         · projected 1.3 days ≤ F-days 7
```

Control group: a volume at 90% with zero growth → level axis still fires CRITICAL,
trend axis honestly reports "not extrapolating". Full transcript in
`scripts/out/demo-output.txt` (real output, not staged).

## File layout

```text
capacity.cap.json               # archive: level history per object (numbers only, never file contents)
<host>_<path>.cap-events.jsonl  # per-object append-only event stream (the audit chain)
```

## Design notes (honest boundaries)

* **capguard is a level recorder, not a df replacement**: `--used/--total` is a fact you assert; it does not verify mount authenticity. `--live` reads real disks.
* **Linear extrapolation has limits**: non-linear growth (e.g. periodic log rotation) gets smoothed by the fit — it answers "at the recent average rate, how long?", not rotation spikes.
* **Platform semantics**: macOS and Linux `df` free-block accounting differs at the kernel level; for cross-machine comparisons, use manual samples with a口径 you trust (single reported numbers).
* Each object keeps its last 400 samples (bounded archive); the event files are unaffected — the audit chain stays complete.

## Toolbox companions

| Tool | Watches |
|---|---|
| [cronguard](https://github.com/tomlen045/cronguard) | cron jobs dying silently |
| [logwhisperer](https://github.com/tomlen045/logwhisperer) | log archaeology |
| [debugkit](https://github.com/tomlen045/debugkit) | incident triage & forensics |
| [porteye](https://github.com/tomlen045/porteye) | exposed port surface |
| [cfgdrift](https://github.com/tomlen045/cfgdrift) | who changed the config |
| **capguard** | **how long until the disk fills** |

## License

MIT
