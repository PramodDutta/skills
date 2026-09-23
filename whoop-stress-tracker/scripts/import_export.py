#!/usr/bin/env python3
"""Convert the WHOOP app's data export into the normalized JSON the analysis reads.

Get the export: WHOOP app -> More -> App Settings -> Data Export -> Create export.
WHOOP emails a link to a zip with physiological_cycles.csv, sleeps.csv,
workouts.csv and journal_entries.csv. Pass the zip, the unzipped folder, or a
single CSV:

  python3 import_export.py ~/Downloads/my_whoop_data.zip --out data/whoop.json

The export has daily recovery / HRV / resting HR / sleep / strain / workouts and
your Journal answers. It does not contain Stress Monitor readings or minute-level
heart rate.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import zipfile
from datetime import timezone
from pathlib import Path
from typing import Optional

from common import (
    format_offset,
    iso,
    new_dataset,
    parse_offset,
    parse_ts,
    resolve_tz,
    to_bool,
    to_float,
    wake_date,
    write_json,
)

FILES = {
    "cycles": "physiological_cycles.csv",
    "sleeps": "sleeps.csv",
    "workouts": "workouts.csv",
    "journal": "journal_entries.csv",
}


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower())


class Table:
    """CSV rows plus tolerant column lookup (exact header, then prefix)."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.headers = {_norm(h): h for h in (rows[0].keys() if rows else [])}

    def column(self, *names: str) -> Optional[str]:
        for n in names:
            if _norm(n) in self.headers:
                return self.headers[_norm(n)]
        for n in names:
            stem = _norm(n).split(" (")[0].rstrip(" %")
            for k, v in self.headers.items():
                if k.startswith(stem):
                    return v
        return None

    def getter(self, *names: str):
        c = self.column(*names)
        return (lambda row: row.get(c)) if c else (lambda row: None)


def _read_csv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def load_tables(path: Path) -> dict[str, Table]:
    found: dict[str, list[dict]] = {}
    if path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1].lower()
                for key, fname in FILES.items():
                    if base == fname and key not in found:
                        found[key] = _read_csv(z.read(name).decode("utf-8-sig", "replace"))
    elif path.is_dir():
        for key, fname in FILES.items():
            hits = sorted(q for q in path.rglob("*") if q.name.lower() == fname)
            if hits:
                found[key] = _read_csv(hits[0].read_text(encoding="utf-8-sig", errors="replace"))
    elif path.is_file() and path.suffix.lower() == ".csv":
        for key, fname in FILES.items():
            if path.name.lower() == fname:
                found[key] = _read_csv(path.read_text(encoding="utf-8-sig", errors="replace"))
        if not found:
            raise SystemExit(f"{path.name}: expected one of {', '.join(FILES.values())}")
    else:
        raise SystemExit(f"not a WHOOP export zip, folder or CSV: {path}")
    if "cycles" not in found:
        raise SystemExit(f"physiological_cycles.csv not found in {path}")
    return {k: Table(v) for k, v in found.items()}


def _tz_of(value, fallback):
    tz = parse_offset(value)
    if tz is None and value:
        try:
            tz = resolve_tz(str(value).strip())
        except SystemExit:
            tz = None
    return tz or fallback


def convert(tables: dict[str, Table], default_tz=timezone.utc) -> dict:
    ds = new_dataset("whoop_export")
    t = tables["cycles"]
    g = {
        "start": t.getter("Cycle start time"),
        "end": t.getter("Cycle end time"),
        "tz": t.getter("Cycle timezone"),
        "recovery": t.getter("Recovery score %"),
        "rhr": t.getter("Resting heart rate (bpm)"),
        "hrv": t.getter("Heart rate variability (ms)"),
        "skin": t.getter("Skin temp (celsius)"),
        "spo2": t.getter("Blood oxygen %"),
        "strain": t.getter("Day Strain"),
        "max_hr": t.getter("Max HR (bpm)"),
        "avg_hr": t.getter("Average HR (bpm)"),
        "onset": t.getter("Sleep onset"),
        "wake": t.getter("Wake onset"),
        "perf": t.getter("Sleep performance %"),
        "resp": t.getter("Respiratory rate (rpm)"),
        "asleep": t.getter("Asleep duration (min)"),
        "in_bed": t.getter("In bed duration (min)"),
        "light": t.getter("Light sleep duration (min)"),
        "sws": t.getter("Deep (SWS) duration (min)"),
        "rem": t.getter("REM duration (min)"),
        "awake": t.getter("Awake duration (min)"),
        "need": t.getter("Sleep need (min)"),
        "debt": t.getter("Sleep debt (min)"),
        "eff": t.getter("Sleep efficiency %"),
        "cons": t.getter("Sleep consistency %"),
    }
    seen = set()
    for row in t.rows:
        tz = _tz_of(g["tz"](row), default_tz)
        start = parse_ts(g["start"](row), tz)
        if start is None or start in seen:
            continue
        seen.add(start)
        end = parse_ts(g["end"](row), tz)
        onset = parse_ts(g["onset"](row), tz)
        wake = parse_ts(g["wake"](row), tz)
        sleep = None
        if onset and wake:
            sleep = {
                "id": f"export-{onset.isoformat()}",
                "start": iso(onset),
                "end": iso(wake),
                "performance": to_float(g["perf"](row)),
                "efficiency": to_float(g["eff"](row)),
                "consistency": to_float(g["cons"](row)),
                "resp_rate": to_float(g["resp"](row)),
                "asleep_min": to_float(g["asleep"](row)),
                "in_bed_min": to_float(g["in_bed"](row)),
                "awake_min": to_float(g["awake"](row)),
                "light_min": to_float(g["light"](row)),
                "sws_min": to_float(g["sws"](row)),
                "rem_min": to_float(g["rem"](row)),
                "need_min": to_float(g["need"](row)),
                "debt_min": to_float(g["debt"](row)),
            }
        day = wake_date(start, wake)
        ds["days"].append(
            {
                "date": day.isoformat(),
                "cycle_start": iso(start),
                "cycle_end": iso(end),
                "tz_offset": format_offset(tz, start),
                "recovery": to_float(g["recovery"](row)),
                "rhr": to_float(g["rhr"](row)),
                "hrv": to_float(g["hrv"](row)),
                "resp_rate": to_float(g["resp"](row)),
                "spo2": to_float(g["spo2"](row)),
                "skin_temp": to_float(g["skin"](row)),
                "calibrating": False,
                "strain": to_float(g["strain"](row)),
                "avg_hr": to_float(g["avg_hr"](row)),
                "max_hr": to_float(g["max_hr"](row)),
                "sleep": sleep,
            }
        )
    ds["days"].sort(key=lambda d: d["cycle_start"])

    if "sleeps" in tables:
        s = tables["sleeps"]
        tzg, on, off, nap = (
            s.getter("Cycle timezone"),
            s.getter("Sleep onset"),
            s.getter("Wake onset"),
            s.getter("Nap"),
        )
        for row in s.rows:
            if to_bool(nap(row)):
                tz = _tz_of(tzg(row), default_tz)
                a, b = parse_ts(on(row), tz), parse_ts(off(row), tz)
                if a and b:
                    ds["naps"].append({"start": iso(a), "end": iso(b)})

    if "workouts" in tables:
        w = tables["workouts"]
        tzg = w.getter("Cycle timezone")
        ws, we = w.getter("Workout start time"), w.getter("Workout end time")
        name, strain = w.getter("Activity name"), w.getter("Activity Strain")
        mx, av = w.getter("Max HR (bpm)"), w.getter("Average HR (bpm)")
        for row in w.rows:
            tz = _tz_of(tzg(row), default_tz)
            a, b = parse_ts(ws(row), tz), parse_ts(we(row), tz)
            if a and b:
                ds["workouts"].append(
                    {
                        "start": iso(a),
                        "end": iso(b),
                        "sport": (name(row) or "Activity").strip(),
                        "strain": to_float(strain(row)),
                        "avg_hr": to_float(av(row)),
                        "max_hr": to_float(mx(row)),
                    }
                )
        ds["workouts"].sort(key=lambda x: x["start"])

    if "journal" in tables:
        j = tables["journal"]
        tzg, cs = j.getter("Cycle timezone"), j.getter("Cycle start time")
        q, yes, notes = j.getter("Question text"), j.getter("Answered yes"), j.getter("Notes")
        for row in j.rows:
            question = (q(row) or "").strip()
            if not question:
                continue
            tz = _tz_of(tzg(row), default_tz)
            start = parse_ts(cs(row), tz)
            ds["journal"].append(
                {
                    "cycle_start": iso(start),
                    "question": question,
                    "yes": to_bool(yes(row)),
                    "notes": (notes(row) or "").strip(),
                }
            )
    return ds


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", help="WHOOP export zip, unzipped folder, or one of its CSVs")
    ap.add_argument("--out", default="data/whoop.json")
    ap.add_argument("--tz", help="timezone for rows without 'Cycle timezone' (e.g. Asia/Kolkata)")
    args = ap.parse_args(argv)

    tables = load_tables(Path(args.export).expanduser())
    ds = convert(tables, resolve_tz(args.tz) or timezone.utc)
    if not ds["days"]:
        raise SystemExit("no cycles found in the export")
    out = write_json(args.out, ds, private=True)
    mapped = sum(1 for e in ds["journal"] if e["cycle_start"])
    print(
        f"wrote {out}: {len(ds['days'])} days ({ds['days'][0]['date']} .. {ds['days'][-1]['date']}), "
        f"{len(ds['workouts'])} workouts, {len(ds['naps'])} naps, "
        f"{len(ds['journal'])} journal answers ({mapped} linked to a day)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
