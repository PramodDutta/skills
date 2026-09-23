---
name: whoop-stress-tracker
description: Find WHEN someone gets stressed from their WHOOP data - which hours and weekdays, which meetings, which days carry over into the next morning - alert them in real time while it is happening, and turn it into a measured improvement plan. Use whenever the user mentions WHOOP (or HRV / recovery / Stress Monitor data) together with stress, anxiety, burnout, "when do I get stressed", "what time am I stressed", "what stresses me", "connect my WHOOP", "analyze my WHOOP data", or wants to improve recovery/HRV, or asks for real-time / live stress alerts. Also use to check whether a change worked ("did the breathing breaks help?"). Works with the official WHOOP API, the WHOOP app's CSV export, Stress Monitor screenshots, and minute-level heart rate (e.g. from the whoops tool or an Apple Watch/Garmin export).
metadata:
  version: "1.0"
  authors: "Built in a Claude Code session for Pramod Dutta"
---

# WHOOP Stress Tracker

Answer "at what time do I feel stress?" from WHOOP data, then run the fix as an
experiment: one change, 14 days, re-measure.

## Be upfront about what each data source can answer

| Source | What it holds | Answers "what hour?" |
|---|---|---|
| Official WHOOP API (OAuth, `scripts/whoop_official.py`) | daily recovery, HRV, resting HR, respiratory rate, sleep timing and stages, strain, workouts, heart rate during sleep | no: which **days** and **evenings/nights** |
| WHOOP app data export (CSV, `scripts/import_export.py`) | the same daily metrics + Journal answers (alcohol, caffeine...) | no: days, nights, habits |
| Stress Monitor readings (screenshots, transcribed to a stress log) | WHOOP's own 0-3 stress score through the day | **yes** |
| Minute heart rate (whoops sync, Apple Watch / Garmin / Fitbit exports) | heart rate every minute | **yes**, most precise |
| **Live: WHOOP Heart Rate Broadcast** (`scripts/live_monitor.py`) | heart rate + beat-to-beat intervals, live over Bluetooth | **yes, as it happens**, and logs it |
| Calendar (Google Calendar connector, .ics, JSON) | what was happening | explains the windows |

WHOOP's public API does not expose the Stress Monitor or daytime minute-level heart
rate (checked September 2026), and nothing in the cloud is real time. Never promise
an hour-level answer from API or export data alone; say which days it can show and
how to add the hours. For **real time**, the only route within WHOOP's terms is the
strap's own Heart Rate Broadcast to a computer nearby (below).

## Workflow

### 1. Find the fastest data route

Check what is already available before asking, and ask only for the missing piece:

1. **A WHOOP connector or MCP server in the session** (search tools for "whoop"): pull
   cycles, recovery, sleep and workouts for 60-90 days, save them as
   `{"cycles": [...], "recoveries": [...], "sleeps": [...], "workouts": [...]}` and run
   `python3 scripts/whoop_official.py normalize raw.json --out data/whoop.json`.
2. **WHOOP API credentials** (`WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` in the
   environment): run the OAuth flow in step 2.
3. **An export** the user already has (zip or CSVs in the repo, Google Drive or an
   email attachment): `import_export.py`.
4. Nothing yet: offer the routes below and recommend by goal. For "what time" that is
   Stress Monitor screenshots (today, no setup) plus the export or API for the daily
   picture.

For the hour-level answer, add one of:
- **Stress Monitor screenshots**: in the WHOOP app, open Stress Monitor and screenshot
  each day's graph for 7-14 days (weekdays and weekends). Read each graph and write
  `data/stress_log.csv` (format below), covering the whole awake day as contiguous
  segments, levels 0-3 (or low/medium/high), about ±15 minutes is fine. Skip sleep.
- **Minute heart rate**: import Apple Watch / Garmin / Fitbit exports with
  [whoops](https://github.com/lucacadalora/whoop-stress-monitoring)
  (`python3 -m whoops import --provider apple_health --file export.zip`) and point
  `--hr` at its `data/hr_cache`. whoops can also pull WHOOP's minute heart rate, but
  only through WHOOP's internal API with the account password, which is against
  WHOOP's terms. Mention it only as the user's own choice, to run on their own
  machine, and never handle their password.

```csv
date,start,end,level
2026-09-14,07:00,09:45,low
2026-09-14,09:45,11:30,high
2026-09-14,11:30,16:30,0.8
```

### Real time: live alerts while it's happening

When the user wants to know **as it happens** ("real time", "alert me", "live"):

1. WHOOP app -> Menu -> Device Settings -> **HR Broadcast** ON. It can switch itself
   off after a firmware update and costs some strap battery. Heart-rate sensors
   usually serve one receiver at a time, so disconnect Peloton/Zwift first.
2. On the user's own computer (macOS/Windows/Linux, within Bluetooth range; a cloud
   session has no Bluetooth): `pip install bleak`, then
   `python3 scripts/live_monitor.py --coach [--windows out/analysis.json] [--ntfy <long-random-topic>]`.
   On macOS the terminal app needs Bluetooth permission.
3. The first run calibrates for 5 calm minutes. After that, every 10 s it scores stress
   0-3 from heart rate and HRV (RMSSD from beat-to-beat intervals) against the user's
   baseline. That's the same two signals WHOOP's Stress Monitor uses; after ~5 logged
   hours it switches to a 14-day baseline built from the logs.
4. It alerts when stress stays high (≥ 2 for 80% of 5 minutes), with a 30-minute
   cooldown and quiet hours 22-7. Alerts are held while the signal looks like movement.
   Each alert is followed by a 1-minute breathing prompt (`--coach`), and 3 minutes
   later it reports whether heart rate and HRV came down.
5. `--windows` adds a heads-up 10 minutes before each known stress window. `--ntfy`
   mirrors alerts to the phone via the ntfy app; the topic name is the only protection,
   so make it long and random.
6. Minute logs go to `data/live/`. Feed them back with `--stress-log data/live` for the
   weekly map and the before/after.

Try it without a strap: `python3 scripts/live_monitor.py --simulate` (a sped-up
40-minute session: calibration, a stressful call, the alert, breathing, a walk that
must not alert).

### 2. Get the data

**Official API** (repeatable, within WHOOP's terms):
```bash
# one-time: app at https://developer-dashboard.whoop.com, redirect https://localhost:8765/callback
export WHOOP_CLIENT_ID=... WHOOP_CLIENT_SECRET=...     # env vars / environment secrets only
python3 scripts/whoop_official.py auth-url              # user opens link, approves
python3 scripts/whoop_official.py exchange "<URL the browser landed on>"
python3 scripts/whoop_official.py fetch --days 90 --sleep-hr --out data/whoop.json
```
- The redirect page not loading is expected: the user copies the address bar and
  pastes it back. The code expires in minutes, so exchange straight away.
- Cloud sessions: `api.prod.whoop.com` must be in the environment's allowed domains
  and the credentials must be environment secrets. A proxy 403 means the domain is
  not allowed; say so plainly and point to the environment's network settings.
- Tokens stay in `~/.config/whoop-stress/token.json` (0600). WHOOP rotates refresh
  tokens on every refresh, so never copy the file or commit it.

**App export:** WHOOP app -> More -> App Settings -> Data Export. WHOOP emails a link.
```bash
python3 scripts/import_export.py ~/Downloads/my_whoop_data.zip --out data/whoop.json
```

### 3. Add the calendar (optional, high value)

If a Google Calendar connector is available, list the user's events for the same date
range and save `data/calendar.json` as `[{"title","start","end","attendees"}]`
(ISO times with offsets; a raw Google `events.list` dump also works). Otherwise accept
an `.ics` converted to that JSON, or skip. Calendar titles are private; keep them local.

### 4. Analyze and build the report

```bash
python3 scripts/stress_analysis.py --whoop data/whoop.json \
    [--stress-log data/stress_log.csv | --hr path/to/whoops/data/hr_cache] \
    [--calendar data/calendar.json] --tz Asia/Kolkata --out out/analysis.json
python3 scripts/build_report.py out/analysis.json   # -> out/stress_report.html + out/stress_summary.md
```
Use the user's IANA timezone (their calendar's timezone is a good source). Run
`python3 scripts/stress_analysis.py --demo --out out/demo/analysis.json` to see a
full report on synthetic data with planted patterns.

### 5. Deliver the answer

Lead with the answer, not the method:
1. The headline: top stress windows with day type, time, and how often (e.g.
   "weekdays 10:00-11:30, on 18 of 20 weekdays"), and what's usually on the calendar then.
2. Days that carry over into the next morning and evening/night spillover.
3. The top 2-3 items from the plan, each tied to its evidence.
4. What the data can't show yet and the one step that would add it.

Share `stress_report.html` as the full report. Health data is sensitive: don't
publish the report or post it anywhere without the user's explicit go-ahead.

### 6. Improve: run it as an experiment

- Agree on ONE change, usually plan item #1 (e.g. 5 minutes of cyclic sighing 10
  minutes before the window, or a buffer before the meeting that sits in it). Note
  the start date.
- Offer (ask first, since it writes to their calendar) a daily reminder event 10
  minutes before the window, or run `live_monitor.py --windows` for a live heads-up
  plus in-the-moment alerts.
- After 14 days, re-run with `--since-change YYYY-MM-DD`. The report adds a before/after
  section scored against the pre-change baseline. Keep what worked, then try the next item.
- `references/improvement_playbook.md` has the interventions and how to measure each.

## Guardrails

- Only numbers from the data, never estimates presented as measurements. With fewer
  than ~10 days of intraday data, or windows seen on only 2-3 days, say it is early.
- Heart rate rises with movement too. A window that lines up with a commute, a walk
  or stairs is not stress. Ask about anything that looks like routine movement.
- Everything is correlation. Phrase findings as "your stress runs higher when...", not
  "X causes your stress".
- Never ask for passwords, client secrets or tokens in chat; use environment
  variables / environment secrets. Keep `data/` and `out/` out of git.
- Not medical advice. For persistent anxiety, panic, chest pain or palpitations, point
  to a professional.

## Files

- `scripts/whoop_official.py`: official WHOOP API v2 (OAuth with rotating refresh
  tokens, paginated fetch, optional sleep heart-rate streams, `normalize` for raw v2 records).
- `scripts/import_export.py`: WHOOP app export (zip/folder/CSV) -> normalized JSON.
- `scripts/live_monitor.py`: real-time monitor over WHOOP Heart Rate Broadcast (Bluetooth,
  needs `bleak`): live heart rate + HRV stress score, alerts, breathing coach, 3-minute
  check, heads-up before known windows, minute logs; `--simulate` to try without a strap.
- `scripts/stress_analysis.py`: daily stress index, weekday carry-over, sleep and
  night heart rate, journal effects, intraday stress map / windows / episodes,
  calendar overlap, before/after, recommendations.
- `scripts/build_report.py`: self-contained HTML report (light/dark, mobile) + Markdown summary.
- `scripts/demo_data.py`: synthetic data with planted patterns (demo and self-test).
- `scripts/common.py`: shared parsing and statistics helpers.
- `references/methodology.md`: how every number is computed, validation, limits.
- `references/improvement_playbook.md`: interventions by pattern, evidence, 14-day protocol.
- `assets/example_stress_log.csv`: stress-log template.
