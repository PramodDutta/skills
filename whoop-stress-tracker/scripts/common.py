"""Shared helpers for the whoop-stress-tracker scripts (Python 3.9+, stdlib only).

Normalized WHOOP data (written by whoop_official.py / import_export.py, read by
stress_analysis.py) looks like:

{
  "schema": "whoop-stress/v1",
  "source": "whoop_api" | "whoop_export" | "demo",
  "generated_at": "...",
  "days": [                      # one row per WHOOP physiological cycle
    {"date": "2026-09-16",       # local date you woke up (the day this cycle's waking hours belong to)
     "cycle_start": ISO, "cycle_end": ISO|null, "tz_offset": "+05:30",
     "recovery": 70, "rhr": 54, "hrv": 117.0, "resp_rate": 15.4, "spo2": 94.7,
     "skin_temp": 35.2, "calibrating": false,
     "strain": 4.3, "avg_hr": 67, "max_hr": 111,
     "sleep": {"start": ISO, "end": ISO, "performance": 92, "efficiency": 95,
               "consistency": 80, "asleep_min": 488, "in_bed_min": 513,
               "awake_min": 25, "light_min": 282, "sws_min": 75, "rem_min": 131,
               "need_min": 451, "debt_min": 0, "id": "..."}}
  ],
  "workouts": [{"start": ISO, "end": ISO, "sport": "Running", "strain": 11.1,
                "avg_hr": 151, "max_hr": 177}],
  "naps": [{"start": ISO, "end": ISO}],
  "sleep_hr": {"<sleep id>": {"start": ISO, "end": ISO, "samples": [[epoch_s, bpm], ...]}},
  "journal": [{"cycle_start": ISO|null, "question": "...", "yes": true|false|null, "notes": ""}]
}
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

SCHEMA = "whoop-stress/v1"

_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.I)
_FRACTION_RE = re.compile(r"(\d{2}:\d{2}:\d{2})\.(\d+)")


# -- time ------------------------------------------------------------------


def parse_offset(text: Any) -> Optional[timezone]:
    """'UTC-04:00' / '+05:30' / '-0400' / 'Z' / 'UTC' -> fixed-offset timezone."""
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    if t.upper() in ("Z", "UTC", "GMT"):
        return timezone.utc
    m = _OFFSET_RE.match(t)
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
    return timezone(sign * delta)


def format_offset(tz: tzinfo, at: Optional[datetime] = None) -> str:
    off = (at or datetime.now(timezone.utc)).astimezone(tz).utcoffset() or timedelta(0)
    total = int(off.total_seconds() // 60)
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


def resolve_tz(spec: Optional[str]) -> Optional[tzinfo]:
    """IANA name ('Asia/Kolkata') or offset ('+05:30', 'UTC-04:00') -> tzinfo."""
    if not spec:
        return None
    off = parse_offset(spec)
    if off is not None:
        return off
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(spec)
    except Exception as e:  # ZoneInfoNotFoundError, ValueError
        raise SystemExit(f"unknown timezone {spec!r}: {e}") from e


def parse_ts(value: Any, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """Parse ISO-8601 (Z or offset), naive 'YYYY-MM-DD HH:MM:SS' (read in `tz`,
    default UTC), or epoch seconds / milliseconds. Returns an aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz or timezone.utc)
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:  # milliseconds
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{9,13}(\.\d+)?", s):
        return parse_ts(float(s))
    if s[-1] in "Zz":
        s = s[:-1] + "+00:00"
    # fromisoformat before 3.11 only takes 3- or 6-digit fractions
    s = _FRACTION_RE.sub(lambda m: f"{m.group(1)}.{(m.group(2) + '000000')[:6]}", s)
    s = s.replace("/", "-")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%m-%d-%Y %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or timezone.utc)
    return dt


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def epoch_minute(dt: datetime) -> int:
    return int(dt.timestamp() // 60)


def wake_date(cycle_start: Optional[datetime], sleep_end: Optional[datetime]) -> Optional[date]:
    """The local date a cycle's waking hours belong to.

    WHOOP cycles begin at sleep onset, so the waking day is the date of the
    wake-up. Without a sleep record, a start after noon means you went to bed
    the evening before that day.
    """
    if sleep_end is not None:
        return sleep_end.date()
    if cycle_start is None:
        return None
    return cycle_start.date() + timedelta(days=1 if cycle_start.hour >= 12 else 0)


def hhmm(minutes_of_day: float) -> str:
    m = int(round(minutes_of_day)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# -- numbers ----------------------------------------------------------------


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return None if (isinstance(value, float) and math.isnan(value)) else float(value)
    s = str(value).strip().replace(",", "")
    if not s or s.lower() in ("nan", "null", "none", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("true", "yes", "y", "1"):
        return True
    if s in ("false", "no", "n", "0"):
        return False
    return None


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        raise ValueError("median of empty sequence")
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def quantile(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        raise ValueError("quantile of empty sequence")
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def robust_sd(xs: Sequence[float], center: Optional[float] = None) -> float:
    """1.4826 * MAD — a standard deviation estimate that ignores outliers."""
    if not xs:
        return 0.0
    c = median(xs) if center is None else center
    return 1.4826 * median([abs(x - c) for x in xs])


def mean(xs: Iterable[float]) -> Optional[float]:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Spearman rank correlation with average ranks for ties."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None

    def ranks(v: Sequence[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den > 0 else None


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def r1(x: Optional[float], nd: int = 1) -> Optional[float]:
    return None if x is None else round(x, nd)


# -- files ------------------------------------------------------------------


def read_json(path: Path | str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"file not found: {path}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"not valid JSON ({path}): {e}")


def write_json(path: Path | str, data: Any, private: bool = False) -> Path:
    """Atomic JSON write. private=True keeps the file owner-only (0600)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p


def new_dataset(source: str) -> dict:
    return {
        "schema": SCHEMA,
        "source": source,
        "generated_at": iso(datetime.now(timezone.utc)),
        "days": [],
        "workouts": [],
        "naps": [],
        "sleep_hr": {},
        "journal": [],
    }
