# WHOOP Stress Tracker (skill)

"At what time do I feel stress?" answered from your own WHOOP data, then turned into
one change at a time that you actually measure.

What you get:
- **Your stress windows**: e.g. *weekdays 10:00-11:30, on 18 of 20 weekdays, +11 bpm*,
  with what's usually on your calendar at that time.
- **Days that carry over**: which days show up in your HRV and resting heart rate the next morning.
- **Evenings and nights**: whether your heart rate is still up after you fall asleep.
- **Habits** from your WHOOP Journal that line up with worse mornings.
- **Live alerts**: while it's happening, from the strap's Heart Rate Broadcast, with a
  one-minute breathing prompt and a check 3 minutes later on whether it worked.
- **A plan**: specific changes tied to that evidence, plus a before/after comparison
  after 14 days.

## Where the data comes from

| Route | Setup | Tells you |
|---|---|---|
| WHOOP app data export | none: More -> App Settings -> Data Export | days, nights, habits |
| Official WHOOP API | 5-minute developer app + OAuth | days, nights (incl. sleep heart rate); repeatable |
| Stress Monitor screenshots | 7-14 screenshots | **hours** (WHOOP's own stress score) |
| Minute heart rate | [whoops](https://github.com/lucacadalora/whoop-stress-monitoring) cache or Apple Watch / Garmin / Fitbit export | **hours**, most precise |
| Live (Heart Rate Broadcast) | WHOOP app -> Device Settings -> HR Broadcast; `pip install bleak` on your computer | **right now**, plus logs for the hours |
| Calendar | Google Calendar, .ics or JSON | what was happening |

WHOOP's public API does not include the Stress Monitor or daytime minute-level heart
rate, so the hour-level answer needs one of the two routes marked **hours**.

## Quick start

```bash
cd whoop-stress-tracker/scripts

# see it work on synthetic data (patterns are planted, then recovered)
python3 stress_analysis.py --demo --out ../out/demo/analysis.json
python3 build_report.py ../out/demo/analysis.json        # open ../out/demo/stress_report.html

# your data: export or API ...
python3 import_export.py ~/Downloads/my_whoop_data.zip --out ../data/whoop.json
#   or: whoop_official.py auth-url / exchange / fetch  (see the file's header)

# ... plus the hours (either one) and your calendar (optional)
python3 stress_analysis.py --whoop ../data/whoop.json \
    --stress-log ../data/stress_log.csv \
    --calendar ../data/calendar.json --tz Asia/Kolkata --out ../out/analysis.json
python3 build_report.py ../out/analysis.json

# real time, on your own computer near the strap (HR Broadcast ON in the WHOOP app)
pip install bleak
python3 live_monitor.py --coach --windows ../out/analysis.json
python3 live_monitor.py --simulate          # try it first without a strap

# 14 days after starting a change
python3 stress_analysis.py ... --since-change 2026-09-24 --out ../out/analysis.json
```

Python 3.9+ standard library only; the live monitor also needs `bleak` for Bluetooth.

## Privacy

`data/` and `out/` are git-ignored: health data, calendar titles and reports stay on
your machine. API credentials come from environment variables and tokens live in
`~/.config/whoop-stress/` (owner-only). Nothing is sent anywhere except the WHOOP API calls
and, only if you turn on `--ntfy`, alert texts pushed to your phone via ntfy.sh.

## Layout

```
whoop-stress-tracker/
├── SKILL.md                      # trigger + workflow (what agents read)
├── scripts/
│   ├── whoop_official.py         # WHOOP API v2: OAuth, fetch, normalize
│   ├── import_export.py          # WHOOP app export -> normalized JSON
│   ├── stress_analysis.py        # the analysis
│   ├── build_report.py           # HTML report + Markdown summary
│   ├── live_monitor.py           # real-time alerts over Bluetooth (bleak)
│   ├── demo_data.py              # synthetic data with planted patterns
│   └── common.py
├── references/
│   ├── methodology.md            # every calculation, validation, limits
│   └── improvement_playbook.md   # what to try for each pattern + 14-day protocol
└── assets/example_stress_log.csv
```

## Credits

Minute-level heart-rate import and per-person meeting attribution come from
[whoops](https://github.com/lucacadalora/whoop-stress-monitoring) by Luca Lora (MIT).
This skill reads its heart-rate cache and calendar formats; it does not include its code.

Not medical advice.
