#!/usr/bin/env python3
"""Find WHEN you are stressed from WHOOP data, and what to change.

Inputs (any combination - more inputs give a sharper answer):
  --whoop PATH        normalized WHOOP data (whoop_official.py fetch / import_export.py).
                      Gives the daily stress index, weekday pattern, sleep timing,
                      night-time heart rate, journal effects; also sleep and workout
                      windows that are excluded from the intraday analysis.
  --hr PATH           minute-level heart rate -> time-of-day stress map. A whoops
                      data/hr_cache folder (or one of its day files), or a CSV with a
                      timestamp column and a bpm column. Repeatable.
  --stress-log PATH   CSV of start,end,level (0-3 or low/medium/high), e.g. read off
                      WHOOP's Stress Monitor graphs. Used instead of --hr when given.
  --calendar PATH     events JSON - [{title,start,end,attendees}] or a Google
                      Calendar events.list dump - to see what was happening.
  --since-change DATE the day you started a change (e.g. a breathing break);
                      adds a before/after comparison.

  python3 stress_analysis.py --whoop data/whoop.json --hr ../whoops/data/hr_cache
      --calendar data/calendar.json --tz Asia/Kolkata --out out/analysis.json
  python3 stress_analysis.py --demo --out out/analysis.json      # synthetic data

Then: python3 build_report.py out/analysis.json
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Optional

from common import (
    WEEKDAYS,
    WEEKDAYS_LONG,
    clamp,
    epoch_minute,
    hhmm,
    iso,
    mean,
    median,
    parse_offset,
    parse_ts,
    quantile,
    r1,
    read_json,
    resolve_tz,
    robust_sd,
    spearman,
    to_float,
    write_json,
)

HIGH = 2.0  # score >= 2 is "high" (WHOOP's Stress Monitor uses the same 0-3 bands)
POST_WORKOUT_MIN = 20  # heart rate stays up after exercise; skip this tail too


# ============================================================================
# loading
# ============================================================================


@dataclass
class Event:
    title: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)


def load_whoop(path: Optional[str]) -> dict:
    if not path:
        return {"days": [], "workouts": [], "naps": [], "sleep_hr": {}, "journal": []}
    ds = read_json(path)
    if not isinstance(ds, dict) or "days" not in ds:
        raise SystemExit(f"{path} is not normalized WHOOP data - run whoop_official.py or import_export.py first")
    for k in ("workouts", "naps", "journal"):
        ds.setdefault(k, [])
    ds.setdefault("sleep_hr", {})
    return ds


def _epoch_from_number(v: float) -> int:
    """Epoch minutes (whoops cache), seconds or milliseconds -> epoch minute."""
    if v < 1e8:
        return int(v)
    if v < 1e11:
        return int(v // 60)
    return int(v // 60000)


def load_hr(paths: list[str], tz: tzinfo) -> dict[int, float]:
    """Minute-level heart rate from whoops caches, JSON day files or CSVs."""
    acc: dict[int, list[float]] = defaultdict(list)
    for raw in paths:
        p = Path(raw).expanduser()
        files = sorted(p.rglob("*.json")) + sorted(p.rglob("*.csv")) if p.is_dir() else [p]
        if not files:
            raise SystemExit(f"no heart-rate files under {p}")
        for f in files:
            if f.suffix.lower() == ".json":
                data = read_json(f)
                rows = data.get("samples", []) if isinstance(data, dict) else data
                for row in rows:
                    if isinstance(row, (list, tuple)) and len(row) >= 2:
                        ts, bpm = to_float(row[0]), to_float(row[1])
                        if ts is not None and bpm and 25 <= bpm <= 230:
                            acc[_epoch_from_number(ts)].append(bpm)
                    elif isinstance(row, dict):
                        dt = parse_ts(row.get("timestamp") or row.get("time") or row.get("date"), tz)
                        bpm = to_float(row.get("bpm") or row.get("hr") or row.get("heart_rate") or row.get("value"))
                        if dt and bpm and 25 <= bpm <= 230:
                            acc[epoch_minute(dt)].append(bpm)
            elif f.suffix.lower() == ".csv":
                with f.open(newline="", encoding="utf-8-sig") as fh:
                    reader = csv.DictReader(fh)
                    cols = {c.strip().lower(): c for c in reader.fieldnames or []}
                    tcol = next((cols[c] for c in ("timestamp", "time", "datetime", "date", "start", "startdate") if c in cols), None)
                    bcol = next((cols[c] for c in ("bpm", "hr", "heart_rate", "heart rate", "heartrate", "value") if c in cols), None)
                    if not tcol or not bcol:
                        raise SystemExit(f"{f.name}: need a timestamp column and a bpm column, found {reader.fieldnames}")
                    for row in reader:
                        dt = parse_ts(row.get(tcol), tz)
                        bpm = to_float(row.get(bcol))
                        if dt and bpm and 25 <= bpm <= 230:
                            acc[epoch_minute(dt)].append(bpm)
    return {m: sum(v) / len(v) for m, v in acc.items()}


_LEVEL_WORDS = {"low": 0.5, "medium": 1.5, "med": 1.5, "moderate": 1.5, "high": 2.5, "very high": 3.0}


def load_stress_log(path: str, tz: tzinfo) -> dict[int, float]:
    """Intervals of stress level -> {epoch minute: score 0..3}."""
    scores: dict[int, float] = {}
    with Path(path).expanduser().open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in reader.fieldnames or []}
        need = ("start", "end")
        if not all(c in cols for c in need) or not ({"level", "score", "stress"} & cols.keys()):
            raise SystemExit(f"{path}: expected columns start,end,level (optional date); got {reader.fieldnames}")
        lcol = cols.get("level") or cols.get("score") or cols.get("stress")
        for row in reader:
            day = (row.get(cols["date"]) or "").strip() if "date" in cols else ""
            s_raw, e_raw = row[cols["start"]].strip(), row[cols["end"]].strip()
            if day and len(s_raw) <= 5:
                s_raw, e_raw = f"{day} {s_raw}", f"{day} {e_raw}"
            start, end = parse_ts(s_raw, tz), parse_ts(e_raw, tz)
            if not start or not end:
                continue
            if end <= start:
                end += timedelta(days=1)
            lv_raw = (row.get(lcol) or "").strip().lower()
            level = _LEVEL_WORDS.get(lv_raw, to_float(lv_raw))
            if level is None:
                continue
            for m in range(epoch_minute(start), epoch_minute(end)):
                scores[m] = clamp(level, 0.0, 3.0)
    return scores


def load_calendar(path: Optional[str], tz: tzinfo) -> list[Event]:
    if not path:
        return []
    raw = read_json(path)
    items = (raw.get("items") or raw.get("events") or []) if isinstance(raw, dict) else raw
    events = []
    for ev in items:
        if not isinstance(ev, dict) or ev.get("status") == "cancelled":
            continue
        s, e = ev.get("start"), ev.get("end")
        if isinstance(s, dict):
            s = s.get("dateTime")
        if isinstance(e, dict):
            e = e.get("dateTime")
        if not s or not e or len(str(s)) <= 10:  # all-day events carry no time
            continue
        start, end = parse_ts(s, tz), parse_ts(e, tz)
        if not start or not end or end <= start or (end - start) > timedelta(hours=12):
            continue
        attendees = []
        for a in ev.get("attendees") or []:
            if isinstance(a, str):
                attendees.append(a)
            elif isinstance(a, dict) and not a.get("resource") and a.get("responseStatus") != "declined" and not a.get("self"):
                attendees.append(a.get("email") or a.get("displayName") or "?")
        title = str(ev.get("title") or ev.get("summary") or "(untitled)").strip()
        events.append(Event(title, start, end, attendees))
    events.sort(key=lambda x: x.start)
    return events


def pick_tz(spec: Optional[str], whoop: dict) -> tuple[tzinfo, str]:
    if spec:
        return resolve_tz(spec), spec
    offsets = Counter(d.get("tz_offset") for d in whoop.get("days", []) if d.get("tz_offset"))
    if offsets:
        off = offsets.most_common(1)[0][0]
        return parse_offset(off) or timezone.utc, f"UTC{off} (from WHOOP data)"
    return timezone.utc, "UTC (no --tz given)"


# ============================================================================
# daily: HRV / resting HR / respiratory rate vs your own baseline
# ============================================================================


def _completeness(d: dict) -> float:
    s = d.get("sleep") or {}
    return (2 if d.get("hrv") else 0) + (1 if d.get("rhr") else 0) + (s.get("asleep_min") or 0) / 1000.0


def _bedtime_minutes(sleep_start: Optional[str]) -> Optional[float]:
    """Clock minutes after 12:00 so 23:30 -> 690 and 01:00 -> 780 stay in order."""
    dt = parse_ts(sleep_start) if sleep_start else None
    if not dt:
        return None
    return ((dt.hour * 60 + dt.minute) - 12 * 60) % (24 * 60)


def analyze_daily(days: list[dict]) -> dict:
    by_date: dict[str, dict] = {}
    for d in days:
        cur = by_date.get(d["date"])
        if cur is None or _completeness(d) > _completeness(cur):
            by_date[d["date"]] = d
    rows = [by_date[k] for k in sorted(by_date)]
    valid = [
        (date.fromisoformat(d["date"]), math.log(d["hrv"]), d["rhr"], d.get("resp_rate"))
        for d in rows
        if d.get("hrv") and d["hrv"] > 0 and d.get("rhr") and not d.get("calibrating")
    ]

    out = []
    for d in rows:
        dt = date.fromisoformat(d["date"])
        rec = {
            "date": d["date"],
            "weekday": WEEKDAYS[dt.weekday()],
            "load_day": WEEKDAYS[(dt - timedelta(days=1)).weekday()],
            "recovery": d.get("recovery"),
            "hrv": d.get("hrv"),
            "rhr": d.get("rhr"),
            "resp_rate": d.get("resp_rate"),
            "strain": d.get("strain"),
            "sleep_performance": (d.get("sleep") or {}).get("performance"),
            "asleep_min": (d.get("sleep") or {}).get("asleep_min"),
            "debt_min": (d.get("sleep") or {}).get("debt_min"),
            "bedtime": (d.get("sleep") or {}).get("start"),
            "index": None,
            "level": None,
        }
        ok = d.get("hrv") and d["hrv"] > 0 and d.get("rhr") and not d.get("calibrating")
        if ok:
            base = [v for v in valid if 0 < (dt - v[0]).days <= 28]
            mode = "trailing 28 days"
            if len(base) < 7:
                base = [v for v in valid if v[0] != dt and abs((dt - v[0]).days) <= 28]
                mode = "surrounding 28 days"
            if len(base) >= 7:
                lh = [b[1] for b in base]
                mh = median(lh)
                z_hrv = (math.log(d["hrv"]) - mh) / max(robust_sd(lh, mh), 0.04)
                rr = [b[2] for b in base]
                mr = median(rr)
                z_rhr = (d["rhr"] - mr) / max(robust_sd(rr, mr), 1.0)
                parts, weights = [-z_hrv, z_rhr], [1.0, 1.0]
                rs = [b[3] for b in base if b[3]]
                if d.get("resp_rate") and len(rs) >= 7:
                    mrs = median(rs)
                    parts.append((d["resp_rate"] - mrs) / max(robust_sd(rs, mrs), 0.3))
                    weights.append(0.5)
                idx = sum(p * w for p, w in zip(parts, weights)) / sum(weights)
                idx = clamp(idx, -4.0, 4.0)
                rec.update(
                    index=round(idx, 2),
                    level="high" if idx >= 1 else "elevated" if idx >= 0.5 else "calm" if idx <= -0.5 else "normal",
                    hrv_vs_base_pct=round((d["hrv"] / math.exp(mh) - 1) * 100, 1),
                    rhr_vs_base=round(d["rhr"] - mr, 1),
                    baseline=mode,
                )
        out.append(rec)

    scored = [r for r in out if r["index"] is not None]
    result: dict[str, Any] = {"days": out, "n_days": len(out), "n_scored": len(scored)}
    if not scored:
        return result

    # which day's load shows up the next morning
    by_load = defaultdict(list)
    for r in scored:
        by_load[r["load_day"]].append(r)
    pattern = []
    for wd in WEEKDAYS:
        rs = by_load.get(wd, [])
        pattern.append(
            {
                "load_day": wd,
                "n": len(rs),
                "mean_index": r1(mean(r["index"] for r in rs), 2),
                "hrv_vs_base_pct": r1(mean(r["hrv_vs_base_pct"] for r in rs)),
                "high_share": r1(sum(1 for r in rs if r["index"] >= 1) / len(rs), 2) if rs else None,
            }
        )
    result["weekday_pattern"] = pattern
    enough = [p for p in pattern if p["n"] >= 2 and p["mean_index"] is not None]
    if len(scored) >= 14 and len(enough) >= 5:
        typical = median([p["mean_index"] for p in enough])
        heavy = [
            {**p, "gap": round(p["mean_index"] - typical, 2)}
            for p in enough
            if p["mean_index"] >= 0.4 and p["mean_index"] - typical >= 0.4
        ]
        result["heavy_load_days"] = sorted(heavy, key=lambda p: p["mean_index"], reverse=True)
        result["best_load_day"] = min(enough, key=lambda p: p["mean_index"])

    result["high_days"] = [r["date"] for r in scored if r["index"] >= 1]
    result["recent"] = {
        "last7": r1(mean(r["index"] for r in scored[-7:]), 2),
        "prev21": r1(mean(r["index"] for r in scored[-28:-7]), 2) if len(scored) > 10 else None,
    }

    # sleep timing
    beds = [(r, _bedtime_minutes(r["bedtime"])) for r in out if r["bedtime"]]
    beds = [(r, b) for r, b in beds if b is not None and 6 * 60 <= b <= 20 * 60]  # 18:00..08:00
    if len(beds) >= 7:
        mins = [b for _, b in beds]
        wk = [b for r, b in beds if date.fromisoformat(r["date"]).weekday() < 5]
        we = [b for r, b in beds if date.fromisoformat(r["date"]).weekday() >= 5]
        sleep = {
            "median_bedtime": hhmm(12 * 60 + median(mins)),
            "bedtime_spread_min": round(robust_sd(mins)),
            "weekday_bedtime": hhmm(12 * 60 + median(wk)) if wk else None,
            "weekend_bedtime": hhmm(12 * 60 + median(we)) if we else None,
        }
        pairs = [(b, r["index"]) for r, b in beds if r["index"] is not None]
        if len(pairs) >= 14:
            sleep["bedtime_vs_stress_rho"] = r1(spearman([p[0] for p in pairs], [p[1] for p in pairs]), 2)
        perf = [r["sleep_performance"] for r in out if r["sleep_performance"] is not None]
        if perf:
            sleep["mean_performance"] = round(sum(perf) / len(perf))
            sleep["short_nights"] = sum(1 for p in perf if p < 70)
        debt = [r["debt_min"] for r in out if r["debt_min"] is not None]
        if debt:
            sleep["mean_debt_min"] = round(sum(debt) / len(debt))
        result["sleep"] = sleep

    # does yesterday's strain carry into today's stress reading?
    idx_by_date = {r["date"]: r["index"] for r in scored}
    strain_pairs = []
    for r in out:
        nxt = (date.fromisoformat(r["date"]) + timedelta(days=1)).isoformat()
        if r["strain"] is not None and idx_by_date.get(nxt) is not None:
            strain_pairs.append((r["strain"], idx_by_date[nxt]))
    if len(strain_pairs) >= 14:
        result["strain_carryover_rho"] = r1(spearman([p[0] for p in strain_pairs], [p[1] for p in strain_pairs]), 2)
    return result


def analyze_journal(journal: list[dict], days: list[dict], daily: dict) -> list[dict]:
    """Journal answers vs that cycle's morning stress index.

    WHOOP files each morning's answers ("Have any alcoholic drinks?" about the day
    before) under the cycle that starts with that night's sleep, so the answer and
    the recovery it affects share a cycle. (Checked on 3 years of real exports:
    drinks line up with -35% HRV on the same cycle, -9% one cycle later.)
    """
    if not journal or not daily.get("n_scored"):
        return []
    date_by_start = {}
    for d in days:
        st = parse_ts(d.get("cycle_start"))
        if st:
            date_by_start[int(st.timestamp() // 60)] = date.fromisoformat(d["date"])
    starts = sorted(date_by_start)
    idx = {r["date"]: r for r in daily["days"] if r["index"] is not None}

    def day_for(cs: Optional[str]) -> Optional[date]:
        st = parse_ts(cs) if cs else None
        if not st or not starts:
            return None
        m = int(st.timestamp() // 60)
        best = min(starts, key=lambda s: abs(s - m))
        return date_by_start[best] if abs(best - m) <= 90 else None

    groups: dict[str, dict[bool, list]] = defaultdict(lambda: {True: [], False: []})
    for e in journal:
        if e.get("yes") is None:
            continue
        d = day_for(e.get("cycle_start"))
        row = idx.get(d.isoformat()) if d else None
        if row:
            groups[e["question"]][bool(e["yes"])].append(row)
    out = []
    for q, g in groups.items():
        yes, no = g[True], g[False]
        if len(yes) >= 5 and len(no) >= 5:
            ya, na = [r["index"] for r in yes], [r["index"] for r in no]
            diff = mean(ya) - mean(na)
            hrv = mean(r["hrv_vs_base_pct"] for r in yes) - mean(r["hrv_vs_base_pct"] for r in no)

            def var(xs):
                m = sum(xs) / len(xs)
                return sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)

            se = math.sqrt(var(ya) / len(ya) + var(na) / len(na))
            out.append(
                {
                    "question": q,
                    "n_yes": len(yes),
                    "n_no": len(no),
                    "index_diff": round(diff, 2),
                    "hrv_diff_pct": round(hrv, 1),
                    "t": round(diff / se, 1) if se > 0 else None,
                }
            )
    out.sort(key=lambda x: x["index_diff"], reverse=True)
    return out


def analyze_sleep_hr(sleep_hr: dict, days: list[dict]) -> dict:
    """Night-time heart rate: how fast it settles and when it bottoms out."""
    nights = []
    for sid, rec in sleep_hr.items():
        start, end = parse_ts(rec.get("start")), parse_ts(rec.get("end"))
        samples = rec.get("samples") or []
        if not start or not end or len(samples) < 60:
            continue
        per_min: dict[int, list[float]] = defaultdict(list)
        for ts, bpm in samples:
            per_min[int(ts // 60)].append(bpm)
        mins = sorted(per_min)
        series = [(m, sum(per_min[m]) / len(per_min[m])) for m in mins]
        roll = []
        for i in range(len(series)):
            window = [b for m, b in series[max(0, i - 9) : i + 1] if series[i][0] - m < 10]
            if len(window) >= 5:
                roll.append((series[i][0], sum(window) / len(window)))
        if not roll:
            continue
        floor_m, floor = min(roll, key=lambda x: x[1])
        s0 = int(start.timestamp() // 60)
        span = max(1, int(end.timestamp() // 60) - s0)
        first = [b for m, b in series if m - s0 < 60]
        end_local = end.astimezone(start.tzinfo)
        nights.append(
            {
                "sleep_id": sid,
                "date": end_local.date().isoformat(),
                "evening": WEEKDAYS[(end_local.date() - timedelta(days=1)).weekday()],
                "floor_hr": round(floor, 1),
                "nadir_fraction": round((floor_m - s0) / span, 2),
                "first_hour_excess": round((sum(first) / len(first)) - floor, 1) if first else None,
            }
        )
    if not nights:
        return {}
    late = [n for n in nights if n["nadir_fraction"] >= 0.66]
    fhe = [n["first_hour_excess"] for n in nights if n["first_hour_excess"] is not None]
    by_evening = defaultdict(list)
    for n in nights:
        by_evening[n["evening"]].append(n)
    evenings = [
        {
            "evening": wd,
            "n": len(by_evening[wd]),
            "late_share": round(sum(1 for n in by_evening[wd] if n["nadir_fraction"] >= 0.66) / len(by_evening[wd]), 2),
            "first_hour_excess": r1(mean(n["first_hour_excess"] for n in by_evening[wd] if n["first_hour_excess"] is not None)),
        }
        for wd in WEEKDAYS
        if by_evening.get(wd)
    ]
    return {
        "nights": len(nights),
        "late_nadir_share": round(len(late) / len(nights), 2),
        "median_first_hour_excess": r1(median(fhe)) if fhe else None,
        "by_evening": evenings,
        "per_night": sorted(nights, key=lambda n: n["date"]),
    }


# ============================================================================
# intraday: minute-level stress scores and when they happen
# ============================================================================


def _windows(items: list[dict], tail_min: int = 0) -> list[tuple[int, int]]:
    out = []
    for it in items:
        a, b = parse_ts(it.get("start")), parse_ts(it.get("end"))
        if a and b and b > a:
            out.append((epoch_minute(a), epoch_minute(b) + tail_min))
    return out


def score_heart_rate(
    hr: dict[int, float],
    tz: tzinfo,
    sleeps: list[tuple[int, int]],
    workouts: list[tuple[int, int]],
    night: tuple[int, int],
) -> dict:
    """Minute HR -> stress score 0..3 relative to your own recent awake baseline.

    Mirrors what WHOOP's Stress Monitor does with heart rate (it also uses HRV,
    which the minute stream lacks): compare each awake, non-exercise minute with
    your typical awake heart rate over the previous 14 days.
    """
    minutes = sorted(hr)
    # 5-minute centered rolling median smooths single-sample noise
    smooth: dict[int, float] = {}
    for i, m in enumerate(minutes):
        win = [hr[x] for x in minutes[max(0, i - 2) : i + 3] if abs(x - m) <= 2]
        smooth[m] = median(win)

    sleep_set = set()
    for a, b in sleeps:
        sleep_set.update(range(a, b))
    workout_set = set()
    for a, b in workouts:
        workout_set.update(range(a, b + POST_WORKOUT_MIN))
    wake_dates = set()
    for _, b in sleeps:
        wake_dates.add(datetime.fromtimestamp(b * 60, tz).date())
    ns, ne = night

    def is_night(dt: datetime) -> bool:
        h = dt.hour
        in_night = (h >= ns or h < ne) if ns > ne else (ns <= h < ne)
        if not in_night:
            return False
        wake_day = dt.date() + timedelta(days=1) if (ns > ne and h >= ns) else dt.date()
        return wake_day not in wake_dates  # no WHOOP sleep record for this night

    awake: dict[int, float] = {}
    excluded = Counter()
    for m in minutes:
        if m in sleep_set:
            excluded["sleep"] += 1
            continue
        if m in workout_set:
            excluded["workout"] += 1
            continue
        if is_night(datetime.fromtimestamp(m * 60, tz)):
            excluded["assumed sleep"] += 1
            continue
        awake[m] = smooth[m]
    if len(awake) < 240:
        return {"scores": {}, "excess": {}, "smooth": {}, "excluded": dict(excluded), "baseline": None}

    by_date: dict[date, list[int]] = defaultdict(list)
    for m in awake:
        by_date[datetime.fromtimestamp(m * 60, tz).date()].append(m)
    all_vals = list(awake.values())
    g_med = median(all_vals)
    g_sd = max(robust_sd(all_vals, g_med), 3.0)

    base_cache: dict[date, tuple[float, float]] = {}

    def baseline_for(d: date) -> tuple[float, float]:
        if d not in base_cache:
            vals = [awake[m] for k in range(14) for m in by_date.get(d - timedelta(days=k), [])]
            if len(vals) < 600:
                base_cache[d] = (g_med, g_sd)
            else:
                md = median(vals)
                base_cache[d] = (md, max(robust_sd(vals, md), 3.0))
        return base_cache[d]

    # sustained big rises (>= +35 bpm or 4 SD for 10+ min) are exercise, not stress
    exercise = set()
    run: list[int] = []
    for m in sorted(awake):
        md, sd = baseline_for(datetime.fromtimestamp(m * 60, tz).date())
        hot = awake[m] >= md + max(35.0, 4 * sd)
        if hot and (not run or m - run[-1] <= 2):
            run.append(m)
            continue
        if len(run) >= 10:
            exercise.update(range(run[0], run[-1] + POST_WORKOUT_MIN))
        run = [m] if hot else []
    if len(run) >= 10:
        exercise.update(range(run[0], run[-1] + POST_WORKOUT_MIN))

    scores, excess = {}, {}
    daily_base = {}
    for d, ms in by_date.items():
        md, sd = baseline_for(d)
        daily_base[d.isoformat()] = {"median_hr": round(md, 1), "sd": round(sd, 1)}
        for m in ms:
            if m in exercise:
                excluded["detected exercise"] += 1
                continue
            scores[m] = clamp((awake[m] - md) / sd, 0.0, 3.0)
            excess[m] = awake[m] - md
    return {
        "scores": scores,
        "excess": excess,
        "smooth": {m: awake[m] for m in scores},
        "excluded": dict(excluded),
        "baseline": {"median_hr": round(g_med, 1), "sd": round(g_sd, 1), "per_day": daily_base},
    }


def _day_type(d: date) -> str:
    return "weekday" if d.weekday() < 5 else "weekend"


def _matches(group: str, d: date) -> bool:
    """Does date d belong to a window's group: 'weekday', 'weekend' or a weekday name?"""
    if group in ("weekday", "weekend"):
        return _day_type(d) == group
    return WEEKDAYS_LONG[d.weekday()] == group


def plural(group: str) -> str:
    return group + "s"


# A stress window has to sit clearly above your average for that kind of day
# AND keep coming back. Single-weekday windows ("Sundays 20:00") need more proof.
ACCEPT = {
    "group": {"min_mean": 0.75, "min_excess": 0.3, "min_recurrence": 0.25},
    "weekday_name": {"min_mean": 1.0, "min_excess": 0.5, "min_recurrence": 0.5},
}
CALM_SLOTS = range(16, 40)  # 08:00-20:00: calm stretches you can plan around
DAYS_PER_WEEK = {"weekday": 5, "weekend": 2}


def _group_slots(dates: list[date], per_date_slot: dict) -> dict[int, dict]:
    out = {}
    for s in range(48):
        agg = {"sum": 0.0, "n": 0, "high": 0, "dates": 0}
        for d in dates:
            c = per_date_slot.get((d, s))
            if c and c["n"]:
                agg["sum"] += c["sum"]
                agg["n"] += c["n"]
                agg["high"] += c["high"]
                agg["dates"] += 1
        if agg["n"]:
            out[s] = agg
    return out


def _find_runs(slots: dict[int, dict], type_mean: float, n_dates: int, calm: bool = False) -> list[list[int]]:
    """Contiguous 30-minute slots that stand out from your average for these days."""
    need = max(2, math.ceil(0.4 * n_dates))
    elig = {s: v for s, v in slots.items() if v["dates"] >= need and v["n"] >= 30 and (not calm or s in CALM_SLOTS)}
    if len(elig) < 6:
        return []
    means = {s: v["sum"] / v["n"] for s, v in elig.items()}
    if calm:
        thr = min(type_mean - 0.15, quantile(list(means.values()), 0.25))
        hit = {s for s, m in means.items() if m <= thr}
    else:
        thr = max(type_mean + 0.2, quantile(list(means.values()), 0.75))
        hit = {s for s, m in means.items() if m >= thr}
        # bridge a single slightly-lower slot between two hot ones
        for s in list(means):
            if s not in hit and s - 1 in hit and s + 1 in hit and means[s] >= (thr + type_mean) / 2:
                hit.add(s)
    runs, cur = [], []
    for s in sorted(hit):
        if cur and s != cur[-1] + 1:
            runs.append(cur)
            cur = []
        cur.append(s)
    if cur:
        runs.append(cur)
    return runs


def _describe(run: list[int], group: str, dates: list[date], per_date_slot: dict, g_mean: float, calm: bool) -> dict:
    covered = hits = 0
    tot_sum = tot_n = 0.0
    ex_sum = ex_n = 0.0
    for d in dates:
        cells = [c for c in (per_date_slot.get((d, s)) for s in run) if c]
        n = sum(c["n"] for c in cells)
        tot_sum += sum(c["sum"] for c in cells)
        tot_n += n
        ex_sum += sum(c["ex_sum"] for c in cells)
        ex_n += sum(c["ex_n"] for c in cells)
        if n < 15 * len(run):
            continue
        covered += 1
        hi = sum(c["high"] for c in cells)
        dm = sum(c["sum"] for c in cells) / n
        if (not calm and (hi >= 10 or dm >= g_mean + 0.5)) or (calm and dm <= g_mean - 0.3):
            hits += 1
    mean_score = tot_sum / tot_n if tot_n else 0.0
    rec = hits / covered if covered else None
    hours = len(run) / 2
    win = {
        "day_type": group,
        "label": plural(group),
        "start": hhmm(run[0] * 30),
        "end": hhmm((run[-1] + 1) * 30),
        "start_min": run[0] * 30,
        "end_min": (run[-1] + 1) * 30,
        "mean_score": round(mean_score, 2),
        "type_mean": round(g_mean, 2),
        "days_hit": hits,
        "days_covered": covered,
        "recurrence": round(rec, 2) if rec is not None else None,
        # excess stress-hours this window adds to an average week
        "weekly_load": round(
            abs(mean_score - g_mean) * hours * (rec if rec is not None else 0.5) * DAYS_PER_WEEK.get(group, 1), 2
        ),
    }
    if ex_n:
        win["excess_bpm"] = round(ex_sum / ex_n, 1)
    return win


def analyze_timing(scores: dict[int, float], tz: tzinfo, excess: Optional[dict[int, float]] = None) -> dict:
    if not scores:
        return {}
    heat = [[[0.0, 0, 0] for _ in range(24)] for _ in range(7)]
    hours = [[0.0, 0, 0] for _ in range(24)]
    per_date = defaultdict(lambda: {"sum": 0.0, "n": 0, "high": 0})
    per_date_slot = defaultdict(lambda: {"sum": 0.0, "n": 0, "high": 0, "ex_sum": 0.0, "ex_n": 0})
    for m, sc in scores.items():
        dt = datetime.fromtimestamp(m * 60, tz)
        d, wd, h = dt.date(), dt.weekday(), dt.hour
        hi = 1 if sc >= HIGH else 0
        for cell in (heat[wd][h], hours[h]):
            cell[0] += sc
            cell[1] += 1
            cell[2] += hi
        pd = per_date[d]
        pd["sum"] += sc
        pd["n"] += 1
        pd["high"] += hi
        pds = per_date_slot[(d, h * 2 + dt.minute // 30)]
        pds["sum"] += sc
        pds["n"] += 1
        pds["high"] += hi
        if excess and m in excess:
            pds["ex_sum"] += excess[m]
            pds["ex_n"] += 1

    total_n = sum(c[1] for c in hours)
    overall = sum(c[0] for c in hours) / total_n
    result: dict[str, Any] = {
        "awake_minutes": total_n,
        "days": len(per_date),
        "overall_mean": round(overall, 2),
        "high_minutes_per_day": round(sum(c[2] for c in hours) / max(1, len(per_date)), 1),
        "hour_profile": [
            {"hour": h, "mean": r1(c[0] / c[1], 2) if c[1] >= 30 else None, "minutes": c[1], "high_pct": r1(100 * c[2] / c[1]) if c[1] else None}
            for h, c in enumerate(hours)
        ],
        "heatmap": [
            [
                {"mean": r1(c[0] / c[1], 2) if c[1] >= 20 else None, "minutes": c[1], "high_pct": r1(100 * c[2] / c[1]) if c[1] else None}
                for c in heat[wd]
            ]
            for wd in range(7)
        ],
    }

    full_days = sorted(d for d, v in per_date.items() if v["n"] >= 180)
    groups = [("weekday", [d for d in full_days if d.weekday() < 5]), ("weekend", [d for d in full_days if d.weekday() >= 5])]
    groups += [(WEEKDAYS_LONG[i], [d for d in full_days if d.weekday() == i]) for i in range(7)]
    windows, calm_windows = [], []
    for group, dates in groups:
        single = group not in DAYS_PER_WEEK
        if not dates or (single and len(dates) < 3):
            continue
        g_n = sum(per_date[d]["n"] for d in dates)
        g_mean = sum(per_date[d]["sum"] for d in dates) / g_n
        slots = _group_slots(dates, per_date_slot)
        rules = ACCEPT["weekday_name" if single else "group"]
        for calm in (False, True):
            if calm and single:
                continue
            for run in _find_runs(slots, g_mean, len(dates), calm=calm):
                w = _describe(run, group, dates, per_date_slot, g_mean, calm)
                if calm:
                    if g_mean - w["mean_score"] >= 0.15:
                        calm_windows.append(w)
                    continue
                if w["mean_score"] < rules["min_mean"] or w["mean_score"] - g_mean < rules["min_excess"]:
                    continue
                if single and w["days_covered"] < 3:
                    continue
                if w["days_covered"] >= 3 and (w["recurrence"] or 0) < rules["min_recurrence"]:
                    continue
                windows.append(w)

    def overlaps(a: dict, b: dict) -> bool:
        return a["start_min"] < b["end_min"] and b["start_min"] < a["end_min"]

    group_wins = [w for w in windows if w["day_type"] in DAYS_PER_WEEK]
    kept = list(group_wins)
    for w in windows:
        if w["day_type"] in DAYS_PER_WEEK:
            continue
        cls = "weekday" if WEEKDAYS_LONG.index(w["day_type"]) < 5 else "weekend"
        if not any(g["day_type"] == cls and overlaps(g, w) for g in group_wins):
            kept.append(w)
    kept.sort(key=lambda w: w["weekly_load"], reverse=True)
    result["windows"] = kept[:4]
    calm_windows.sort(key=lambda w: w["weekly_load"], reverse=True)
    result["calm_windows"] = [w for w in calm_windows if w["day_type"] == "weekday"][:2] + [
        w for w in calm_windows if w["day_type"] == "weekend"
    ][:1]
    result["per_date"] = [
        {"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()], "mean": round(v["sum"] / v["n"], 2), "high_minutes": v["high"], "minutes": v["n"]}
        for d, v in sorted(per_date.items())
    ]
    result["episodes"] = find_episodes(scores, tz, excess)
    return result


def find_episodes(scores: dict[int, float], tz: tzinfo, excess: Optional[dict[int, float]] = None, min_len: int = 10) -> list[dict]:
    """Stretches of >= min_len minutes at high stress (gaps of <= 3 min allowed)."""
    eps, cur, last = [], [], None
    for m in sorted(scores):
        if scores[m] >= HIGH:
            if cur and m - last > 4:
                eps.append(cur)
                cur = []
            cur.append(m)
            last = m
        elif cur and m - last > 3:
            eps.append(cur)
            cur = []
    if cur:
        eps.append(cur)
    out = []
    for ep in eps:
        if ep[-1] - ep[0] + 1 < min_len:
            continue
        a = datetime.fromtimestamp(ep[0] * 60, tz)
        b = datetime.fromtimestamp((ep[-1] + 1) * 60, tz)
        rec = {
            "start": iso(a),
            "end": iso(b),
            "weekday": WEEKDAYS[a.weekday()],
            "minutes": ep[-1] - ep[0] + 1,
            "mean_score": round(sum(scores[m] for m in ep) / len(ep), 2),
            "peak_score": round(max(scores[m] for m in ep), 2),
        }
        if excess:
            vals = [excess[m] for m in ep if m in excess]
            if vals:
                rec["excess_bpm"] = round(sum(vals) / len(vals), 1)
        out.append(rec)
    return out


# ============================================================================
# calendar: what was on your calendar when stress was high
# ============================================================================


_TITLE_NOISE = re.compile(r"(\b\d{1,2}[/.-]\d{1,2}([/.-]\d{2,4})?\b|#\d+|\(\d+\)|\s+\d+$)")


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", _TITLE_NOISE.sub("", t.lower())).strip(" -:|") or t.lower()


def analyze_calendar(events: list[Event], scores: dict[int, float], tz: tzinfo, timing: dict) -> dict:
    if not events or not scores:
        return {}
    date_mean = {r["date"]: r["mean"] for r in timing.get("per_date", [])}
    scored_events = []
    for ev in events:
        mins = range(epoch_minute(ev.start), epoch_minute(ev.end))
        vals = [scores[m] for m in mins if m in scores]
        if not mins or len(vals) < 0.5 * len(mins):
            continue
        dm = date_mean.get(ev.start.astimezone(tz).date().isoformat())
        mean_sc = sum(vals) / len(vals)
        scored_events.append(
            {
                "title": ev.title,
                "key": _norm_title(ev.title),
                "start": iso(ev.start.astimezone(tz)),
                "minutes": len(mins),
                "mean_score": round(mean_sc, 2),
                "lift": round(mean_sc - dm, 2) if dm is not None else None,
                "high_minutes": sum(1 for v in vals if v >= HIGH),
                "attendees": ev.attendees,
            }
        )
    groups = defaultdict(list)
    for e in scored_events:
        groups[e["key"]].append(e)
    recurring = []
    for key, es in groups.items():
        lifts = [e["lift"] for e in es if e["lift"] is not None]
        recurring.append(
            {
                "title": es[-1]["title"],
                "n": len(es),
                "mean_score": round(sum(e["mean_score"] for e in es) / len(es), 2),
                "mean_lift": round(sum(lifts) / len(lifts), 2) if lifts else None,
                "high_share": round(sum(1 for e in es if e["high_minutes"] >= 10) / len(es), 2),
            }
        )
    rec = [r for r in recurring if r["mean_lift"] is not None]
    stressful = sorted([r for r in rec if r["mean_lift"] > 0.15], key=lambda r: r["mean_lift"], reverse=True)
    calming = sorted([r for r in rec if r["mean_lift"] < -0.15 and r["n"] >= 2], key=lambda r: r["mean_lift"])

    # what usually sits inside each stress window
    for w in timing.get("windows", []):
        counts: Counter = Counter()
        n_dates = set()
        for ev in events:
            s = ev.start.astimezone(tz)
            if not _matches(w["day_type"], s.date()):
                continue
            smin = s.hour * 60 + s.minute
            emin = smin + int((ev.end - ev.start).total_seconds() // 60)
            if smin < w["end_min"] and emin > w["start_min"]:
                counts[_norm_title(ev.title)] += 1
                n_dates.add(s.date())
        title_of = {e["key"]: e["title"] for e in scored_events}
        title_of.update({_norm_title(ev.title): ev.title for ev in events})
        w["calendar"] = [{"title": title_of.get(k, k), "count": c} for k, c in counts.most_common(4)]

    # does stress follow meeting load across the weekday?
    load = defaultdict(int)
    weekdays = {datetime.fromtimestamp(m * 60, tz).date() for m in scores if datetime.fromtimestamp(m * 60, tz).weekday() < 5}
    for ev in events:
        for m in range(epoch_minute(ev.start), epoch_minute(ev.end)):
            dt = datetime.fromtimestamp(m * 60, tz)
            if dt.date() in weekdays:
                load[dt.hour * 2 + dt.minute // 30] += 1
    slot_scores = defaultdict(list)
    for m, sc in scores.items():
        dt = datetime.fromtimestamp(m * 60, tz)
        if dt.weekday() < 5:
            slot_scores[dt.hour * 2 + dt.minute // 30].append(sc)
    common = [s for s in slot_scores if len(slot_scores[s]) >= 60]
    rho = None
    if len(common) >= 10 and any(load.get(s) for s in common):
        rho = spearman([load.get(s, 0) for s in common], [sum(slot_scores[s]) / len(slot_scores[s]) for s in common])
    return {
        "events_scored": len(scored_events),
        "stressful": stressful[:6],
        "calming": calming[:4],
        "meeting_load_rho": r1(rho, 2),
        "top_events": sorted(scored_events, key=lambda e: (e["lift"] or 0), reverse=True)[:8],
    }


def attach_episode_events(episodes: list[dict], events: list[Event]) -> None:
    for ep in episodes:
        a, b = parse_ts(ep["start"]), parse_ts(ep["end"])
        ep["events"] = [ev.title for ev in events if ev.start < b and ev.end > a][:3]


# ============================================================================
# before / after a change
# ============================================================================


def compare_change(
    since: date,
    tz: tzinfo,
    daily: dict,
    smooth: dict[int, float],
    scores: dict[int, float],
    absolute: bool,
    windows: list[dict],
) -> dict:
    out: dict[str, Any] = {"since": since.isoformat()}
    rows = [r for r in daily.get("days", []) if r.get("hrv") and r.get("rhr")]
    before = [r for r in rows if date.fromisoformat(r["date"]) < since]
    after = [r for r in rows if date.fromisoformat(r["date"]) >= since]
    if len(before) >= 5 and len(after) >= 5:
        def gm(rs):
            return math.exp(sum(math.log(r["hrv"]) for r in rs) / len(rs))

        out["daily"] = {
            "days_before": len(before),
            "days_after": len(after),
            "hrv_before": round(gm(before), 1),
            "hrv_after": round(gm(after), 1),
            "hrv_change_pct": round((gm(after) / gm(before) - 1) * 100, 1),
            "rhr_before": round(sum(r["rhr"] for r in before) / len(before), 1),
            "rhr_after": round(sum(r["rhr"] for r in after) / len(after), 1),
            "recovery_before": r1(mean(r["recovery"] for r in before if r.get("recovery") is not None)),
            "recovery_after": r1(mean(r["recovery"] for r in after if r.get("recovery") is not None)),
        }
    if not scores:
        return out

    def split(ms):
        b = [m for m in ms if datetime.fromtimestamp(m * 60, tz).date() < since]
        a = [m for m in ms if datetime.fromtimestamp(m * 60, tz).date() >= since]
        return b, a

    if absolute:  # stress-log levels are already on a fixed 0-3 scale
        fixed = scores
    else:  # re-score both periods against the BEFORE period's baseline
        b_ms, _ = split(list(smooth))
        if len(b_ms) < 600:
            return out
        vals = [smooth[m] for m in b_ms]
        md = median(vals)
        sd = max(robust_sd(vals, md), 3.0)
        fixed = {m: clamp((v - md) / sd, 0.0, 3.0) for m, v in smooth.items()}
    b_ms, a_ms = split(list(fixed))
    if len(b_ms) < 600 or len(a_ms) < 600:
        return out

    def high_per_day(ms):
        days = {datetime.fromtimestamp(m * 60, tz).date() for m in ms}
        return sum(1 for m in ms if fixed[m] >= HIGH) / max(1, len(days))

    out["intraday"] = {
        "mean_before": round(sum(fixed[m] for m in b_ms) / len(b_ms), 2),
        "mean_after": round(sum(fixed[m] for m in a_ms) / len(a_ms), 2),
        "high_min_per_day_before": round(high_per_day(b_ms), 1),
        "high_min_per_day_after": round(high_per_day(a_ms), 1),
        "windows": [],
    }
    for w in windows[:3]:
        def in_w(m):
            dt = datetime.fromtimestamp(m * 60, tz)
            mod = dt.hour * 60 + dt.minute
            return _matches(w["day_type"], dt.date()) and w["start_min"] <= mod < w["end_min"]

        wb = [fixed[m] for m in b_ms if in_w(m)]
        wa = [fixed[m] for m in a_ms if in_w(m)]
        if len(wb) >= 60 and len(wa) >= 60:
            out["intraday"]["windows"].append(
                {
                    "window": f"{w['label']} {w['start']}-{w['end']}",
                    "before": round(sum(wb) / len(wb), 2),
                    "after": round(sum(wa) / len(wa), 2),
                }
            )
    return out


# ============================================================================
# what to change
# ============================================================================


def _minus(hm: str, minutes: int) -> str:
    h, m = map(int, hm.split(":"))
    return hhmm(h * 60 + m - minutes)


def recommendations(res: dict) -> list[dict]:
    recs = []
    timing = res.get("timing") or {}
    daily = res.get("daily") or {}
    cal = res.get("calendar") or {}
    today = date.today().isoformat()

    for i, w in enumerate(timing.get("windows", [])[:2]):
        label = w["label"]
        hit = f" and spikes here on {w['days_hit']} of {w['days_covered']} {label}" if w.get("days_covered") else ""
        bpm = f" (about {w['excess_bpm']:+.0f} bpm over your usual awake heart rate)" if w.get("excess_bpm") else ""
        steps = [
            f"At {_minus(w['start'], 10)} on {label}, take 2-5 minutes of cyclic sighing: a double inhale through "
            "the nose, then a long, slow exhale; repeat. In a 2023 Stanford study (Balban et al., Cell Reports "
            "Medicine), 5 minutes a day of this lowered breathing rate and improved mood more than mindfulness "
            "meditation."
        ]
        cal_titles = [c["title"] for c in w.get("calendar", []) if c["count"] >= 2]
        if cal_titles:
            steps.append(
                f"This window usually holds: {', '.join(cal_titles[:3])}. Put a 5-10 minute buffer before them, "
                "or move the heaviest one out of this slot. Microsoft's Human Factors Lab (2021) found stress "
                "builds up across back-to-back meetings and short breaks between them reset it."
            )
        if w["start_min"] < 11 * 60:
            steps.append(
                "Check what comes right before it: commute, the first inbox/Slack sweep, or coffee on an empty "
                "stomach. Change one thing at a time so you can tell what worked."
            )
        elif w["start_min"] < 17 * 60:
            steps.append(
                "If it follows lunch, try a 10-minute walk right after eating, and keep caffeine before 14:00 "
                "(it stays active for 5-6 hours)."
            )
        else:
            steps.append(
                "This is the workday spilling into your evening: set a shutdown time 30 minutes before this "
                "window and mute work notifications after it."
            )
        recs.append(
            {
                "title": f"Get ahead of your {w['start']}-{w['end']} peak on {label}",
                "why": f"Your stress averages {w['mean_score']:.1f}/3 in this window against {w['type_mean']:.1f}/3 "
                f"for your {label} overall{hit}{bpm}.",
                "try": steps,
                "measure": f"Run again with --since-change {today} after 14 days. It worked if this window's "
                "score drops by 0.3 or more, or it spikes on fewer than half of those days.",
                "priority": 1 + i,
            }
        )

    for ev in (cal.get("stressful") or [])[:1]:
        if ev["n"] >= 2 and ev["mean_lift"] >= 0.4:
            recs.append(
                {
                    "title": f"Reshape \"{ev['title']}\"",
                    "why": f"Across {ev['n']} occurrences your stress sat {ev['mean_lift']:+.1f} above that day's "
                    f"average ({int(ev['high_share'] * 100)}% of them had 10+ high-stress minutes).",
                    "try": [
                        "Send an agenda the day before, cut it to the decisions that need everyone, "
                        "and take 2 minutes of slow breathing right before it starts.",
                        "If it's a person rather than the meeting, whoops' per-person model "
                        "(python3 -m whoops score) separates the two.",
                    ],
                    "measure": "Mean score during this meeting over the next 4 occurrences.",
                    "priority": 3,
                }
            )

    heavy = daily.get("heavy_load_days") or []
    if heavy:
        names = [WEEKDAYS_LONG[WEEKDAYS.index(h["load_day"])] + "s" for h in heavy[:2]]
        nxt = [WEEKDAYS_LONG[(WEEKDAYS.index(h["load_day"]) + 1) % 7] for h in heavy[:2]]
        facts = "; ".join(
            f"after {WEEKDAYS_LONG[WEEKDAYS.index(h['load_day'])]}s HRV averages {h['hrv_vs_base_pct']:+.0f}% "
            f"vs your baseline (stress index {h['mean_index']:+.1f})"
            for h in heavy[:2]
        )
        recs.append(
            {
                "title": f"Lighten your {' and '.join(names)}",
                "why": f"Your recovery the next morning shows it: {facts}.",
                "try": [
                    "Move one recurring meeting off that day and protect a 60-minute no-meeting block.",
                    "Plan an easier evening: early dinner, no alcohol, screens down 45 minutes before bed.",
                ],
                "measure": f"HRV on {' and '.join(nxt)} mornings over the next 3 weeks.",
                "priority": 4,
            }
        )

    sl = daily.get("sleep") or {}
    rho = sl.get("bedtime_vs_stress_rho")
    if sl and ((rho is not None and rho >= 0.25) or sl.get("bedtime_spread_min", 0) >= 60):
        why = f"Your bedtime moves by about ±{sl['bedtime_spread_min']} min around {sl['median_bedtime']}"
        if rho is not None and rho >= 0.25:
            why += f", and later nights are followed by higher stress readings (rank correlation {rho:+.2f})"
        recs.append(
            {
                "title": f"Anchor your bedtime near {sl['median_bedtime']}",
                "why": why + ".",
                "try": [
                    f"Pick a lights-out within 30 minutes of {sl['median_bedtime']}, weekends included, and set a "
                    "wind-down alarm 45 minutes before it."
                ],
                "measure": "WHOOP sleep consistency above 80% and fewer high-stress mornings.",
                "priority": 5,
            }
        )

    night = res.get("sleep_hr") or {}
    bad_evenings = [e for e in night.get("by_evening", []) if e["n"] >= 3 and e["late_share"] >= 0.6]
    if night.get("nights", 0) >= 7 and (night.get("late_nadir_share", 0) >= 0.35 or bad_evenings):
        if bad_evenings:
            names = " and ".join(WEEKDAYS_LONG[WEEKDAYS.index(e["evening"])] for e in bad_evenings[:2])
            shares = " and ".join(str(int(e["late_share"] * 100)) + "%" for e in bad_evenings[:2])
            why = (
                f"After {names} evenings your heart rate keeps running high into the night and only bottoms out "
                f"in the last third of your sleep ({shares} of those nights). That is the day's stress, a late "
                "meal or alcohol still working on you."
            )
        else:
            why = (
                f"On {int(night['late_nadir_share'] * 100)}% of nights your heart rate only bottoms out in the last "
                "third of your sleep, a sign your body is still processing the evening."
            )
        recs.append(
            {
                "title": "Let your evenings wind down" + (f" ({names})" if bad_evenings else ""),
                "why": why,
                "try": [
                    "Finish dinner 3 hours before bed, keep weeknights alcohol-free, and leave hard workouts "
                    "for earlier in the day.",
                    "10 minutes of slow breathing or a body-scan in bed.",
                ],
                "measure": "Night-time heart rate bottoming out earlier (first half of sleep).",
                "priority": 6,
            }
        )

    for j in [j for j in (res.get("journal") or []) if j["index_diff"] >= 0.3 and (j.get("t") or 0) >= 2][:2]:
        if j["n_yes"] >= 8:
            recs.append(
                {
                    "title": f"Test life without: \"{j['question']}\"",
                    "why": f"Mornings after 'yes' ({j['n_yes']}x) your stress index is {j['index_diff']:+.1f} higher "
                    f"and HRV {j['hrv_diff_pct']:+.0f}% vs 'no' ({j['n_no']}x).",
                    "try": ["Two weeks without it, keep answering the WHOOP Journal, then compare."],
                    "measure": "Next-morning HRV and stress index.",
                    "priority": 7,
                }
            )

    if sl and ((sl.get("mean_performance") or 100) < 75 or (sl.get("mean_debt_min") or 0) > 45):
        recs.append(
            {
                "title": "Get the sleep your body is asking for",
                "why": f"Sleep performance averages {sl.get('mean_performance')}% with ~{sl.get('mean_debt_min', 0)} "
                "min of sleep debt; stress readings run higher after short nights.",
                "try": ["Go to bed 30 minutes earlier on weeknights until sleep performance is above 85%."],
                "measure": "WHOOP sleep performance and recovery.",
                "priority": 8,
            }
        )

    if not timing:
        recs.insert(
            0,
            {
                "title": "Pinpoint the hour",
                "why": "Daily WHOOP data shows which days tax you, not which hours. That needs intraday readings.",
                "try": [
                    "Quickest: share 7-14 days of WHOOP Stress Monitor graphs (Home -> Stress Monitor, one "
                    "screenshot per day); they get transcribed into a stress log.",
                    "Most precise: minute-level heart rate (whoops sync, or an Apple Watch / Garmin export).",
                ],
                "measure": "--stress-log or --hr on the next run.",
                "priority": 0,
            },
        )
    recs.sort(key=lambda r: r["priority"])
    return recs[:6]


def headline(res: dict) -> str:
    timing = res.get("timing") or {}
    wins = timing.get("windows") or []
    daily = res.get("daily") or {}
    if wins:
        w = wins[0]
        text = f"Your stress peaks on {w['label']} at {w['start']}-{w['end']}"
        if w.get("days_covered"):
            text += f" (on {w['days_hit']} of {w['days_covered']} {w['label']})"
        if len(wins) > 1:
            w2 = wins[1]
            text += f", then {w2['start']}-{w2['end']}" + (f" on {w2['label']}" if w2["day_type"] != w["day_type"] else "")
        return text + "."
    heavy = daily.get("heavy_load_days") or []
    if heavy:
        name = WEEKDAYS_LONG[WEEKDAYS.index(heavy[0]["load_day"])]
        return f"Your body carries the most stress out of {name}s: HRV runs {heavy[0]['hrv_vs_base_pct']:+.0f}% vs baseline the next morning."
    if daily.get("n_scored"):
        return f"{len(daily.get('high_days', []))} high-stress mornings in {daily['n_scored']} days; add intraday data to see which hours."
    return "Not enough data yet - see the data section."


# ============================================================================
# main
# ============================================================================


def run(args: argparse.Namespace) -> dict:
    notes: list[str] = []
    if args.demo:
        from demo_data import make_demo

        paths = make_demo(Path(args.out).parent / "demo_input", tz_name=args.tz or "Asia/Kolkata")
        args.whoop, args.hr, args.calendar = paths["whoop"], [paths["hr"]], paths["calendar"]
        args.tz = args.tz or "Asia/Kolkata"

    whoop = load_whoop(args.whoop)
    tz, tz_label = pick_tz(args.tz, whoop)
    days = whoop.get("days", [])
    if args.start or args.end:
        lo = args.start or "0000"
        hi = args.end or "9999"
        days = [d for d in days if lo <= d["date"] <= hi]

    res: dict[str, Any] = {
        "schema": "whoop-stress-analysis/v1",
        "generated_at": iso(datetime.now(timezone.utc)),
        "demo": bool(args.demo),
        "timezone": tz_label,
        "sources": {},
    }
    if days:
        res["sources"]["whoop"] = {
            "kind": whoop.get("source"),
            "days": len(days),
            "from": days[0]["date"],
            "to": days[-1]["date"],
            "workouts": len(whoop.get("workouts", [])),
            "sleep_hr_nights": len(whoop.get("sleep_hr", {})),
            "journal_answers": len(whoop.get("journal", [])),
        }
        res["daily"] = analyze_daily(days)
        res["journal"] = analyze_journal(whoop.get("journal", []), days, res["daily"])
        res["sleep_hr"] = analyze_sleep_hr(whoop.get("sleep_hr", {}), days)

    sleeps = _windows([d["sleep"] for d in whoop.get("days", []) if d.get("sleep")]) + _windows(whoop.get("naps", []))
    workouts = _windows(whoop.get("workouts", []))
    scores: dict[int, float] = {}
    excess: dict[int, float] = {}
    smooth: dict[int, float] = {}
    absolute = False
    if args.stress_log:
        if args.hr:
            notes.append("--stress-log given, so --hr was ignored for timing.")
        scores = load_stress_log(args.stress_log, tz)
        absolute = True
        res["sources"]["stress_log"] = {"minutes": len(scores)}
        res["intraday_method"] = "stress levels you logged (e.g. WHOOP Stress Monitor)"
    elif args.hr:
        hr = load_hr(args.hr, tz)
        if args.start or args.end:
            lo = datetime.combine(date.fromisoformat(args.start), datetime.min.time(), tz) if args.start else None
            hi = datetime.combine(date.fromisoformat(args.end) + timedelta(days=1), datetime.min.time(), tz) if args.end else None
            hr = {m: v for m, v in hr.items() if (lo is None or m >= epoch_minute(lo)) and (hi is None or m < epoch_minute(hi))}
        ns, ne = (int(x) for x in args.night.split("-"))
        scored = score_heart_rate(hr, tz, sleeps, workouts, (ns, ne))
        scores, excess, smooth = scored["scores"], scored["excess"], scored["smooth"]
        first = datetime.fromtimestamp(min(hr) * 60, tz).date().isoformat() if hr else None
        last = datetime.fromtimestamp(max(hr) * 60, tz).date().isoformat() if hr else None
        res["sources"]["heart_rate"] = {
            "minutes": len(hr),
            "from": first,
            "to": last,
            "excluded_minutes": scored["excluded"],
            "baseline": {k: v for k, v in (scored["baseline"] or {}).items() if k != "per_day"},
        }
        res["intraday_method"] = "heart rate vs your 14-day awake baseline (sleep and exercise removed)"
        if not sleeps:
            notes.append(f"No WHOOP sleep records for this period: {args.night.replace('-', ':00-')}:00 was treated as sleep.")
        if not whoop.get("workouts"):
            notes.append("No WHOOP workouts: exercise was detected from sustained heart-rate jumps instead.")
        notes.append("Heart rate also rises when you walk or climb stairs; a stress window that lines up with a commute or a walk is movement, not stress.")

    events = load_calendar(args.calendar, tz)
    if events:
        res["sources"]["calendar"] = {"events": len(events)}
    if scores:
        res["timing"] = analyze_timing(scores, tz, excess or None)
        if events:
            res["calendar"] = analyze_calendar(events, scores, tz, res["timing"])
            attach_episode_events(res["timing"]["episodes"], events)
        eps = res["timing"]["episodes"]
        res["timing"]["episodes_recent"] = eps[-25:]
        res["timing"]["episodes_count"] = len(eps)
        del res["timing"]["episodes"]
    if args.since_change:
        res["change"] = compare_change(
            date.fromisoformat(args.since_change),
            tz,
            res.get("daily") or {},
            smooth,
            scores,
            absolute,
            (res.get("timing") or {}).get("windows", []),
        )
    if not days and not scores:
        raise SystemExit("nothing to analyze: pass --whoop, --hr or --stress-log (or --demo)")
    res["recommendations"] = recommendations(res)
    res["headline"] = headline(res)
    res["notes"] = notes
    return res


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--whoop", help="normalized WHOOP JSON")
    ap.add_argument("--hr", action="append", help="minute heart rate: whoops hr_cache dir, JSON or CSV (repeatable)")
    ap.add_argument("--stress-log", help="CSV start,end,level (0-3 or low/medium/high)")
    ap.add_argument("--calendar", help="events JSON")
    ap.add_argument("--tz", help="IANA timezone, e.g. Asia/Kolkata (default: from WHOOP data)")
    ap.add_argument("--start", help="first date to include, YYYY-MM-DD")
    ap.add_argument("--end", help="last date to include, YYYY-MM-DD")
    ap.add_argument("--night", default="23-7", help="hours assumed asleep when WHOOP has no sleep record (default 23-7)")
    ap.add_argument("--since-change", help="YYYY-MM-DD you started a change; adds before/after")
    ap.add_argument("--demo", action="store_true", help="run on synthetic demo data")
    ap.add_argument("--out", default="out/analysis.json")
    args = ap.parse_args(argv)
    res = run(args)
    out = write_json(args.out, res, private=not res["demo"])
    print(res["headline"])
    for r in res["recommendations"][:3]:
        print(f"  - {r['title']}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
