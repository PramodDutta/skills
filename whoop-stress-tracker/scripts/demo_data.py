"""Synthetic WHOOP + minute heart-rate + calendar data with planted stress patterns.

Used by `stress_analysis.py --demo` and as a self-test: the analysis should find
what was planted here and nothing else.

Planted:
  * weekdays 10:00-11:30 stress (85% of weekdays, every Monday, +13 bpm; +17 on Mondays)
  * weekdays 16:30-18:00 stress (65% of weekdays, +10 bpm)
  * Sunday 20:00-21:00 "Sunday scaries" (50% of Sundays, +8 bpm)
  * Mondays load: Tuesday-morning HRV -22%, resting HR +3
  * Friday drinks (journal "Have any alcoholic drinks?"): Saturday-morning HRV -18%
  * late night-time heart-rate low after Sunday and Monday evenings
  * workouts Mon/Wed/Fri 07:15-08:00 (must be excluded, not read as stress)
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

from common import iso, new_dataset, resolve_tz, write_json

PLANTED = {
    "weekday_windows": [("10:00", "11:30"), ("16:30", "18:00")],
    "weekend_windows": [("20:00", "21:00")],
    "worst_load_day": "Mon",
    "journal_stressor": "Have any alcoholic drinks?",
}


def _ramp(minute_of_day: int, start: int, end: int, amp: float, ramp: int = 10) -> float:
    if minute_of_day < start or minute_of_day >= end:
        return 0.0
    up = min(1.0, (minute_of_day - start + 1) / ramp)
    down = min(1.0, (end - minute_of_day) / ramp)
    return amp * min(up, down)


def make_demo(out_dir: Path | str, days: int = 28, tz_name: str = "Asia/Kolkata", seed: int = 7, end: date | None = None) -> dict:
    rng = random.Random(seed)
    noise = random.Random(seed + 1000)  # unplanted journal answers: no effect on anything
    tz = resolve_tz(tz_name)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    end = end or (datetime.now(tz).date() - timedelta(days=1))
    first = end - timedelta(days=days - 1)
    dates = [first + timedelta(days=i) for i in range(days)]

    ds = new_dataset("demo")
    events = []
    hr: dict[int, float] = {}
    ar = 0.0

    alcohol = {d: (d.weekday() == 4 and rng.random() < 0.85) or rng.random() < 0.08 for d in dates}
    # sleep onset of the night that ends on each date (and the night after the last one)
    onsets = {
        d: datetime.combine(d - timedelta(days=1), time(23, 15), tz) + timedelta(minutes=rng.randint(-25, 35))
        for d in dates + [end + timedelta(days=1)]
    }
    for d in dates:
        wd = d.weekday()
        # --- the night before d: sleep ------------------------------------
        prev = d - timedelta(days=1)
        onset = onsets[d]
        wake = datetime.combine(d, time(6, 50), tz) + timedelta(minutes=rng.randint(-20, 25))
        evening = prev.weekday()
        late = rng.random() < (0.8 if evening in (6, 0) else 0.12)
        night_len = int((wake - onset).total_seconds() // 60)
        samples = []
        for i in range(night_len):
            f = i / night_len
            nadir_at = 0.85 if late else 0.4
            start_hr = 70 if late else 63
            if f < nadir_at:
                v = start_hr - (start_hr - 52) * (f / nadir_at)
            else:
                v = 52 + 6 * (f - nadir_at) / (1 - nadir_at)
            v += rng.gauss(0, 1.2)
            t = onset + timedelta(minutes=i)
            hr[int(t.timestamp() // 60)] = v
            samples.append([int(t.timestamp()), round(v, 1)])

        # --- daily WHOOP numbers --------------------------------------------
        hrv = 62 * math.exp(rng.gauss(0, 0.07))
        rhr = 58 + rng.gauss(0, 1.2)
        resp = 15.2 + rng.gauss(0, 0.2)
        if wd == 1:  # Tuesday morning carries Monday
            hrv *= 0.78
            rhr += 3
        if alcohol.get(prev):
            hrv *= 0.82
            rhr += 4
            resp += 0.4
        if late:
            hrv *= 0.93
        recovery = max(5, min(99, 55 + 110 * math.log(hrv / 62) - 3 * (rhr - 58) + rng.gauss(0, 4)))
        sleep_id = f"demo-sleep-{d.isoformat()}"
        ds["days"].append(
            {
                "date": d.isoformat(),
                "cycle_start": iso(onset),
                "cycle_end": None,
                "tz_offset": onset.strftime("%z")[:3] + ":" + onset.strftime("%z")[3:],
                "recovery": round(recovery),
                "rhr": round(rhr),
                "hrv": round(hrv, 1),
                "resp_rate": round(resp, 1),
                "spo2": 96.5,
                "skin_temp": 33.8,
                "calibrating": False,
                "strain": round(rng.uniform(8, 11) + (4 if wd in (0, 2, 4) else 0), 1),
                "avg_hr": None,
                "max_hr": None,
                "sleep": {
                    "id": sleep_id,
                    "start": iso(onset),
                    "end": iso(wake),
                    "performance": round(min(100, rng.gauss(86, 7))),
                    "efficiency": round(rng.uniform(88, 95)),
                    "consistency": round(rng.uniform(70, 88)),
                    "resp_rate": round(resp, 1),
                    "asleep_min": night_len - 30,
                    "in_bed_min": night_len,
                    "awake_min": 30,
                    "need_min": 460,
                    "debt_min": max(0, 460 - (night_len - 30)),
                },
            }
        )
        ds["sleep_hr"][sleep_id] = {"start": iso(onset), "end": iso(wake), "samples": samples}
        if ds["days"][-2:-1]:
            ds["days"][-2]["cycle_end"] = iso(onset)
        # answered this morning about yesterday; WHOOP files it under this cycle
        ds["journal"].append({"cycle_start": iso(onset), "question": PLANTED["journal_stressor"], "yes": alcohol.get(prev, False), "notes": ""})
        ds["journal"].append({"cycle_start": iso(onset), "question": "Viewed a screen device in bed?", "yes": noise.random() < 0.5, "notes": ""})

        # --- the waking day d -------------------------------------------------
        workout = None
        if wd in (0, 2, 4):
            ws = datetime.combine(d, time(7, 15), tz)
            workout = (ws, ws + timedelta(minutes=45))
            ds["workouts"].append({"start": iso(workout[0]), "end": iso(workout[1]), "sport": "Running", "strain": 12.5, "avg_hr": 146, "max_hr": 171})
        stress_a = wd < 5 and (wd == 0 or rng.random() < 0.85)
        stress_b = wd < 5 and rng.random() < 0.65
        scaries = wd == 6 and rng.random() < 0.5
        day_start = int(wake.timestamp() // 60)
        for m in range(day_start, int(onsets[d + timedelta(days=1)].timestamp() // 60)):
            t = datetime.fromtimestamp(m * 60, tz)
            mod = t.hour * 60 + t.minute
            ar = 0.9 * ar + rng.gauss(0, 1.2)
            v = 71 + 3 * math.sin((mod - 9 * 60) / (24 * 60) * 2 * math.pi) + ar
            if rng.random() < 0.012:
                v += 8
            if workout and workout[0] <= t < workout[1] + timedelta(minutes=25):
                if t < workout[1]:
                    v = 146 + rng.gauss(0, 6)
                else:
                    v += 70 * math.exp(-(t - workout[1]).total_seconds() / 60 / 6)
            if stress_a:
                v += _ramp(mod, 600, 690, 17 if wd == 0 else 13)
            if stress_b:
                v += _ramp(mod, 990, 1080, 10)
            if scaries:
                v += _ramp(mod, 1200, 1260, 8)
            hr[m] = v
        # --- calendar ---------------------------------------------------------
        def ev(title, hs, ms, he, me, who):
            events.append(
                {
                    "title": title,
                    "start": iso(datetime.combine(d, time(hs, ms), tz)),
                    "end": iso(datetime.combine(d, time(he, me), tz)),
                    "attendees": [f"{w}@example.com" for w in who],
                }
            )

        if wd < 5:
            ev("Daily standup", 10, 0, 10, 15, ["manager", "dev_a", "dev_b", "pm"])
            ev("Lunch", 13, 0, 13, 45, [])
        if wd == 0:
            ev("Sprint planning", 10, 15, 11, 30, ["manager", "pm", "dev_a"])
        if wd == 2:
            ev("Growth review", 10, 30, 11, 30, ["manager", "pm", "cfo"])
        if wd == 1:
            ev("1:1 with mentor", 15, 0, 15, 30, ["mentor"])
        if wd == 3:
            ev("Client call", 16, 30, 17, 30, ["client", "pm"])
        if wd == 4:
            ev("Team retro", 16, 30, 17, 15, ["manager", "dev_a", "dev_b"])

    cache = out / "hr_cache" / "demo"
    cache.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list] = {}
    for m in sorted(hr):
        day = datetime.fromtimestamp(m * 60, tz).date().isoformat()
        by_day.setdefault(day, []).append([m, round(hr[m], 1)])
    for day, rows in by_day.items():
        (cache / f"{day}.json").write_text(json.dumps({"date": day, "provider": "demo", "samples": rows}))
    whoop_path = write_json(out / "demo_whoop.json", ds)
    cal_path = write_json(out / "demo_calendar.json", events)
    return {"whoop": str(whoop_path), "hr": str(cache), "calendar": str(cal_path), "planted": PLANTED}
