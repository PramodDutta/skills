#!/usr/bin/env python3
"""Real-time stress monitor for a WHOOP strap (or any Bluetooth heart-rate sensor).

Turn on Heart Rate Broadcast in the WHOOP app (Menu -> Device Settings -> HR
Broadcast). The strap then works as a standard Bluetooth heart-rate sensor that
also sends beat-to-beat (RR) intervals. This script listens on your computer,
scores your stress 0-3 every 10 seconds from heart rate AND HRV against your own
baseline (the same two signals WHOOP's Stress Monitor uses), and taps you on the
shoulder when it stays high: a desktop notification (optionally on your phone)
with a one-minute breathing prompt, then a check 3 minutes later on whether your
heart rate and HRV actually came back down.

Every minute is logged to CSV. stress_analysis.py reads those logs
(--stress-log data/live) to map your stress windows over the weeks.

Runs on your own computer within Bluetooth range of the strap (macOS, Windows,
Linux). Needs `pip install bleak`. On macOS allow Bluetooth for your terminal app.

  python3 live_monitor.py                          # find the strap, calibrate once, monitor
  python3 live_monitor.py --windows out/analysis.json   # + heads-up 10 min before your usual windows
  python3 live_monitor.py --coach                  # + guided breathing in the terminal after an alert
  python3 live_monitor.py --ntfy <secret-topic>    # + push alerts to your phone (ntfy app)
  python3 live_monitor.py --simulate               # no strap: a sped-up synthetic session

Broadcasting uses more strap battery. Movement also raises heart rate and lowers
HRV; the monitor holds alerts while the signal looks like movement, but it has no
motion sensor, so alerts are phrased as a check-in, not a verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import random
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from common import clamp, hhmm, mean, median, read_json, robust_sd, write_json

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
EVAL_EVERY = 10  # seconds
LOG_FIELDS = ["timestamp", "bpm", "rmssd_ms", "score", "motion"]


# ============================================================================
# signal
# ============================================================================


def parse_hr_measurement(data: bytes) -> Optional[tuple[int, list[float], Optional[bool]]]:
    """Bluetooth Heart Rate Measurement (0x2A37) -> (bpm, [RR ms], skin contact).

    Flags: bit0 = 16-bit heart rate, bits1-2 = contact status, bit3 = energy
    expended present, bit4 = RR intervals present, in units of 1/1024 s (WHOOP
    follows the standard here).
    """
    if not data:
        return None
    flags = data[0]
    if flags & 0x01:
        if len(data) < 3:
            return None
        bpm, i = data[1] | (data[2] << 8), 3
    else:
        if len(data) < 2:
            return None
        bpm, i = data[1], 2
    contact = bool(flags & 0x02) if flags & 0x04 else None
    if flags & 0x08:
        i += 2
    rr = []
    if flags & 0x10:
        while i + 1 < len(data):
            rr.append((data[i] | (data[i + 1] << 8)) * 1000.0 / 1024.0)
            i += 2
    return bpm, rr, contact


def rr_quality(beats: list[tuple[float, float]]) -> tuple[Optional[float], float]:
    """RMSSD (ms) over clean successive beats, and the share of rejected beats.

    Rejects impossible intervals (outside 300-2000 ms) and beats more than 20%
    off their local median (missed or extra beats), and never pairs beats across
    a Bluetooth gap. Needs 20+ clean successive pairs.
    """
    if not beats:
        return None, 0.0
    vals = [x for _, x in beats]
    clean: list[Optional[float]] = []
    for i, x in enumerate(vals):
        if not 300 <= x <= 2000:
            clean.append(None)
            continue
        near = [y for y in vals[max(0, i - 2) : i + 3] if 300 <= y <= 2000]
        m = median(near)
        clean.append(x if abs(x - m) <= 0.2 * m else None)
    diffs = []
    for k in range(1, len(vals)):
        a, b = clean[k - 1], clean[k]
        if a is not None and b is not None and beats[k][0] - beats[k - 1][0] <= 3:
            diffs.append((b - a) ** 2)
    bad = sum(1 for c in clean if c is None) / len(vals)
    if len(diffs) < 20:
        return None, bad
    return math.sqrt(sum(diffs) / len(diffs)), bad


# ============================================================================
# baseline
# ============================================================================


def baseline_from_logs(log_dir: Path, now: datetime, days: int = 14) -> Optional[dict]:
    """Your typical awake heart rate and HRV from the last 14 days of logs."""
    bpms, lns = [], []
    for k in range(days):
        f = log_dir / f"{(now - timedelta(days=k)).date().isoformat()}.csv"
        if not f.exists():
            continue
        with f.open(newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("motion") not in ("", "0", None):
                    continue
                try:
                    bpms.append(float(row["bpm"]))
                    if row.get("rmssd_ms"):
                        lns.append(math.log(float(row["rmssd_ms"])))
                except (ValueError, KeyError):
                    continue
    if len(bpms) < 300:
        return None
    hm = median(bpms)
    base = {"hr_med": hm, "hr_sd": max(3.0, robust_sd(bpms, hm)), "minutes": len(bpms), "source": f"last {days} days of logs"}
    if len(lns) >= 120:
        lm = median(lns)
        base.update(ln_rmssd_med=lm, ln_rmssd_sd=max(0.15, robust_sd(lns, lm)))
    return base


def baseline_from_calibration(minutes: list[dict]) -> dict:
    bpms = [m["bpm"] for m in minutes]
    hm = median(bpms)
    base = {"hr_med": hm, "hr_sd": max(4.0, robust_sd(bpms, hm)), "minutes": len(minutes), "source": "calibration"}
    lns = [math.log(m["rmssd"]) for m in minutes if m.get("rmssd")]
    if len(lns) >= 3:
        lm = median(lns)
        base.update(ln_rmssd_med=lm, ln_rmssd_sd=max(0.25, robust_sd(lns, lm)))
    return base


# ============================================================================
# alerts
# ============================================================================


def notify(title: str, message: str, ntfy_topic: Optional[str] = None) -> None:
    """Terminal bell + desktop notification, and a phone push via ntfy if asked."""
    sys.stdout.write("\a")
    sys.stdout.flush()
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", f"display notification {json.dumps(message)} with title {json.dumps(title)} sound name \"Submarine\""],
                timeout=5,
                check=False,
                capture_output=True,
            )
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", title, message], timeout=5, check=False, capture_output=True)
        elif sys.platform == "win32":
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                f"$x.GetElementsByTagName('text').Item(0).AppendChild($x.CreateTextNode({json.dumps(title)})) > $null;"
                f"$x.GetElementsByTagName('text').Item(1).AppendChild($x.CreateTextNode({json.dumps(message)})) > $null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
                ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=10, check=False, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass
    if ntfy_topic:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{ntfy_topic}",
                data=message.encode(),
                headers={"Title": title, "Priority": "high", "Tags": "heart"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10).close()
        except OSError as e:
            print(f"  (phone push failed: {e})")


BREATH = [("breathe in through your nose", 2), ("a second short sip of air", 1), ("long, slow exhale through your mouth", 6)]


async def coach(sleep: Callable, cycles: int = 6) -> None:
    """About a minute of cyclic sighing, paced in the terminal."""
    print("\n  Cyclic sighing, 1 minute. Follow along:")
    for c in range(cycles):
        for text, secs in BREATH:
            print(f"    {c + 1}/{cycles}  {text} ({secs}s)")
            await sleep(secs)
    print("  Done. I'll check your heart rate again in 3 minutes.\n")


# ============================================================================
# the monitor
# ============================================================================


class Monitor:
    def __init__(self, args: argparse.Namespace, clock: Callable[[], float], sleep: Callable):
        self.args = args
        self.clock = clock
        self.sleep = sleep
        self.log_dir = Path(args.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.base_path = self.log_dir / "baseline.json"
        self.hr: deque = deque()
        self.beats: deque = deque()
        self.evals: deque = deque()
        self.calibration: list[dict] = []
        self.minute_start: Optional[int] = None
        self.last_eval = 0.0
        self.last_alert = -1e12
        self.hold_until = 0.0
        self.motion_until = 0.0
        self.motion_why = ""
        self.pending: list[dict] = []
        self.events: list[dict] = []
        self.minutes_logged = 0
        self.high_minutes = 0
        self.nudged: set = set()
        self.windows = self._load_windows(args.windows)
        self.quiet = tuple(int(x) for x in args.quiet.split("-")) if args.quiet else None
        now = datetime.fromtimestamp(clock())
        self.base = None if args.recalibrate else baseline_from_logs(self.log_dir, now)
        if self.base is None and not args.recalibrate and self.base_path.exists():
            self.base = read_json(self.base_path)
        if self.base:
            print(f"baseline: {self.base['hr_med']:.0f} bpm" + (f", HRV {math.exp(self.base['ln_rmssd_med']):.0f} ms" if self.base.get("ln_rmssd_med") is not None else "") + f" ({self.base['source']})")
        else:
            print(f"calibrating for {args.calibrate} min: sit still and breathe normally...")

    # -- input -------------------------------------------------------------
    def on_packet(self, data: bytes, t: float) -> None:
        parsed = parse_hr_measurement(data)
        if not parsed:
            return
        bpm, rr, contact = parsed
        if contact is False or not 25 <= bpm <= 230:
            return
        self.hr.append((t, bpm))
        for x in rr:
            self.beats.append((t, x))
        while self.hr and self.hr[0][0] < t - 600:
            self.hr.popleft()
        while self.beats and self.beats[0][0] < t - 600:
            self.beats.popleft()

    # -- stats ---------------------------------------------------------------
    def stats(self, t: float, secs: int = 60) -> dict:
        hrs = [b for ts, b in self.hr if t - secs <= ts <= t]
        beats = [(ts, x) for ts, x in self.beats if t - secs <= ts <= t]
        rmssd, bad = rr_quality(beats)
        return {"bpm": mean(hrs), "rmssd": rmssd, "artifact": bad, "n": len(hrs)}

    def score(self, st: dict) -> Optional[float]:
        if not self.base or st["bpm"] is None:
            return None
        parts = [(st["bpm"] - self.base["hr_med"]) / self.base["hr_sd"]]
        if st["rmssd"] and self.base.get("ln_rmssd_med") is not None:
            parts.append((self.base["ln_rmssd_med"] - math.log(st["rmssd"])) / self.base["ln_rmssd_sd"])
        return clamp(sum(parts) / len(parts), 0.0, 3.0)

    def motion(self, t: float, st: dict) -> Optional[str]:
        """Best guess at movement; there is no motion sensor in the broadcast."""
        if st["bpm"] is None:
            return None
        why = None
        if st["artifact"] > 0.25 and len([1 for ts, _ in self.beats if ts >= t - 60]) >= 20:
            why = "noisy beats (moving?)"
        elif self.base and st["bpm"] >= self.base["hr_med"] + 40:
            why = "exercise-level heart rate"
        else:
            before = [b for ts, b in self.hr if t - 150 <= ts <= t - 90]
            if before and st["bpm"] - sum(before) / len(before) > 25:
                why = "heart rate jumped (moving?)"
        if why:
            self.motion_until = t + 180
            self.motion_why = why
            return why
        # still moving: movement was seen recently and heart rate hasn't settled
        if t < self.motion_until and self.base and st["bpm"] >= self.base["hr_med"] + 20:
            return self.motion_why
        return None

    # -- the loop ------------------------------------------------------------
    def tick(self) -> None:
        t = self.clock()
        minute = int(t // 60)
        if self.minute_start is None:
            self.minute_start = minute
        elif minute > self.minute_start:
            self._close_minute(self.minute_start * 60 + 59.999)
            self.minute_start = minute
        if t - self.last_eval >= EVAL_EVERY:
            self.last_eval = t
            self._evaluate(t)
        self._follow_ups(t)
        self._nudges(t)

    def _close_minute(self, t_end: float) -> None:
        st = self.stats(t_end, 60)
        if st["bpm"] is None or st["n"] < 20:
            return
        mot = self.motion(t_end, st)
        if not self.base:
            if not mot:
                self.calibration.append(st)
            if len(self.calibration) >= self.args.calibrate:
                self.base = baseline_from_calibration(self.calibration)
                write_json(self.base_path, self.base, private=True)
                hrv = f", HRV {math.exp(self.base['ln_rmssd_med']):.0f} ms" if self.base.get("ln_rmssd_med") is not None else " (no beat-to-beat data yet)"
                print(f"calibrated: calm heart rate {self.base['hr_med']:.0f} bpm{hrv}. Monitoring.")
        sc = self.score(st)
        self._status(t_end, st, sc, mot)
        stamp = datetime.fromtimestamp(t_end).astimezone()  # local time with UTC offset
        path = self.log_dir / f"{stamp.date().isoformat()}.csv"
        new = not path.exists()
        with path.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(LOG_FIELDS)
            w.writerow(
                [
                    stamp.replace(second=0, microsecond=0).isoformat(),
                    round(st["bpm"], 1),
                    round(st["rmssd"], 1) if st["rmssd"] else "",
                    round(sc, 2) if sc is not None else "",
                    1 if mot else 0,
                ]
            )
        self.minutes_logged += 1
        if sc is not None and sc >= 2 and not mot:
            self.high_minutes += 1

    def _evaluate(self, t: float) -> None:
        st = self.stats(t, 60)
        sc = self.score(st)
        mot = self.motion(t, st)
        if mot:
            self.hold_until = t + 600  # hold alerts for 10 minutes after movement
        self.evals.append((t, sc))
        while self.evals and self.evals[0][0] < t - 3600:
            self.evals.popleft()

        span = self.args.alert_minutes * 60
        recent = [(ts, s) for ts, s in self.evals if ts >= t - span and s is not None]
        if not recent or recent[-1][0] - recent[0][0] < span - 2 * EVAL_EVERY:
            return
        avg = sum(s for _, s in recent) / len(recent)
        sustained = sum(1 for _, s in recent if s >= self.args.alert_level - 0.3) / len(recent)
        if (
            avg >= self.args.alert_level
            and sustained >= 0.8
            and recent[-1][1] >= self.args.alert_level - 0.3
            and t - self.last_alert >= self.args.cooldown * 60
            and t >= self.hold_until
            and not self._quiet(t)
        ):
            self._alert(t, st, avg)

    def _status(self, t: float, st: dict, sc: Optional[float], mot: Optional[str]) -> None:
        clock = datetime.fromtimestamp(t).strftime("%H:%M")
        if st["bpm"] is None:
            print(f"{clock}  waiting for heart rate...")
            return
        parts = [f"{clock}  HR {st['bpm']:.0f}"]
        if self.base:
            parts[0] += f" ({st['bpm'] - self.base['hr_med']:+.0f})"
        if st["rmssd"] and not mot:
            hrv = f"HRV {st['rmssd']:.0f} ms"
            if self.base and self.base.get("ln_rmssd_med") is not None:
                hrv += f" ({(st['rmssd'] / math.exp(self.base['ln_rmssd_med']) - 1) * 100:+.0f}%)"
            parts.append(hrv)
        if mot:
            parts.append(f"stress paused: {mot}")
        elif sc is None:
            parts.append(f"calibrating {len(self.calibration)}/{self.args.calibrate} min" if not self.base else "")
        else:
            level = "high" if sc >= 2 else "medium" if sc >= 1 else "low"
            parts.append(f"stress {sc:.1f} {'#' * int(round(sc * 4)):<12} {level}")
        print("  ".join(p for p in parts if p))

    def _alert(self, t: float, st: dict, avg: float) -> None:
        self.last_alert = t
        mins = self.args.alert_minutes
        bits = [f"heart rate {st['bpm']:.0f} ({st['bpm'] - self.base['hr_med']:+.0f} vs your calm)"]
        if st["rmssd"] and self.base.get("ln_rmssd_med") is not None:
            bits.append(f"HRV {(st['rmssd'] / math.exp(self.base['ln_rmssd_med']) - 1) * 100:+.0f}%")
        msg = f"Stress has been high for about {mins} minutes: {', '.join(bits)}. If you're sitting still, take 1 minute: double inhale, long exhale."
        print("\n" + "=" * 72 + f"\n  ALERT  {msg}\n" + "=" * 72)
        self._notify("Stress is up", msg)
        event = {
            "type": "alert",
            "at": datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds"),
            "score": round(avg, 2),
            "bpm": round(st["bpm"], 1),
            "rmssd": round(st["rmssd"], 1) if st["rmssd"] else None,
            "check_at": t + 180,
        }
        self.pending.append(event)
        if self.args.coach:
            asyncio.ensure_future(coach(self.sleep))

    def _follow_ups(self, t: float) -> None:
        for ev in [e for e in self.pending if t >= e["check_at"]]:
            self.pending.remove(ev)
            st = self.stats(t, 60)
            ev.pop("check_at")
            ev["after_3min"] = {
                "bpm": round(st["bpm"], 1) if st["bpm"] is not None else None,
                "rmssd": round(st["rmssd"], 1) if st["rmssd"] else None,
                "score": round(self.score(st), 2) if self.score(st) is not None else None,
            }
            a = ev["after_3min"]
            line = f"  3 minutes after the alert: heart rate {ev['bpm']:.0f} -> {a['bpm']:.0f}" if a["bpm"] is not None else "  3 minutes after the alert: no signal"
            if ev["rmssd"] and a["rmssd"]:
                line += f", HRV {ev['rmssd']:.0f} -> {a['rmssd']:.0f} ms"
            print(line)
            self._log_event(ev)

    def _nudges(self, t: float) -> None:
        if not self.windows:
            return
        now = datetime.fromtimestamp(t)
        mod = now.hour * 60 + now.minute
        for w in self.windows:
            if not self._day_matches(w["day_type"], now) or mod != w["start_min"] - 10:
                continue
            key = (now.date(), w["start_min"], w["day_type"])
            if key in self.nudged or self._quiet(t):
                continue
            self.nudged.add(key)
            msg = f"Your usual {w['start']}-{w['end']} stress window starts in 10 minutes. 2 minutes of cyclic sighing now?"
            print(f"\n  HEADS-UP  {msg}\n")
            self._notify("Stress window ahead", msg)
            self._log_event({"type": "nudge", "at": now.astimezone().isoformat(timespec="seconds"), "window": f"{w['label']} {w['start']}-{w['end']}"})

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _day_matches(day_type: str, now: datetime) -> bool:
        if day_type == "weekday":
            return now.weekday() < 5
        if day_type == "weekend":
            return now.weekday() >= 5
        return now.strftime("%A") == day_type

    def _quiet(self, t: float) -> bool:
        if not self.quiet:
            return False
        h = datetime.fromtimestamp(t).hour
        a, b = self.quiet
        return (h >= a or h < b) if a > b else (a <= h < b)

    @staticmethod
    def _load_windows(path: Optional[str]) -> list[dict]:
        if not path:
            return []
        wins = (read_json(path).get("timing") or {}).get("windows") or []
        if wins:
            print("heads-up 10 min before: " + ", ".join(f"{w['label']} {w['start']}" for w in wins))
        return wins

    def _notify(self, title: str, msg: str) -> None:
        try:
            asyncio.get_running_loop().run_in_executor(None, notify, title, msg, self.args.ntfy)
        except RuntimeError:  # no loop running
            notify(title, msg, self.args.ntfy)

    def _log_event(self, ev: dict) -> None:
        self.events.append(ev)
        with (self.log_dir / "events.jsonl").open("a") as fh:
            fh.write(json.dumps(ev) + "\n")

    def summary(self) -> None:
        if self.minute_start is not None:
            self._close_minute(self.clock())
        if not self.minutes_logged and not self.events:
            return
        alerts = [e for e in self.events if e["type"] == "alert" and e.get("after_3min", {}).get("bpm") is not None]
        print("\nsession summary")
        print(f"  minutes logged: {self.minutes_logged}, high-stress minutes: {self.high_minutes}")
        n_alerts = sum(1 for e in self.events if e["type"] == "alert") + len(self.pending)
        print(f"  alerts: {n_alerts}")
        if alerts:
            drop = sum(e["bpm"] - e["after_3min"]["bpm"] for e in alerts) / len(alerts)
            print(f"  heart rate 3 min after an alert: {-drop:+.1f} bpm on average")
        print(f"  logs: {self.log_dir}/  (map your windows: stress_analysis.py --stress-log {self.log_dir})")


# ============================================================================
# sources: Bluetooth or simulation
# ============================================================================


async def run_bluetooth(args: argparse.Namespace) -> None:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        raise SystemExit("needs the bleak package: pip install bleak")
    mon = Monitor(args, time.time, asyncio.sleep)
    try:
        while True:
            print("looking for your strap (WHOOP app -> Device Settings -> HR Broadcast must be ON)...")
            try:
                found = await BleakScanner.discover(timeout=12, service_uuids=[HR_SERVICE])
            except Exception as e:  # BleakError, OSError: no adapter, no permission
                raise SystemExit(
                    f"Bluetooth unavailable ({e}). Is Bluetooth switched on? On macOS, allow Bluetooth for your "
                    "terminal app (System Settings -> Privacy & Security -> Bluetooth); on Linux, BlueZ must be running."
                )
            want = (args.device or "").lower()

            def pick(devices):
                if want:
                    return [d for d in devices if want in (d.name or "").lower() or want == d.address.lower()]
                return [d for d in devices if "whoop" in (d.name or "").lower()]

            cands = pick(found)
            if not cands:  # some straps don't list the heart-rate service in their advertisement
                cands = pick(await BleakScanner.discover(timeout=8))
            if not cands and not want:
                cands = list(found)  # any heart-rate sensor
            if not cands:
                print("  not found yet; retrying in 10s")
                await asyncio.sleep(10)
                continue
            dev = cands[0]
            gone = asyncio.Event()
            try:
                async with BleakClient(dev, disconnected_callback=lambda _c: gone.set()) as client:
                    await client.start_notify(HR_MEASUREMENT, lambda _h, data: mon.on_packet(bytes(data), time.time()))
                    print(f"connected to {dev.name or dev.address}")
                    while not gone.is_set():
                        await asyncio.sleep(1)
                        mon.tick()
            except Exception as e:  # connection dropped mid-way; keep trying
                print(f"  connection lost ({e}); reconnecting")
            await asyncio.sleep(3)
    finally:
        mon.summary()


# (minutes, bpm, rmssd, artifact share, what). The alert should fire about 5
# minutes into the stressful call, the 3-minute check should see the breathing
# work, and the walk must NOT trigger an alert.
SCENARIO = [
    (6, 66, 46, 0.02, "calm (calibration)"),
    (8, 70, 40, 0.02, "focused work"),
    (6, 87, 17, 0.03, "stressful call"),
    (3, 73, 38, 0.02, "you take a minute of cyclic sighing"),
    (6, 71, 39, 0.02, "back to work"),
    (4, 104, 12, 0.35, "walking to a meeting"),
    (6, 68, 42, 0.02, "calm"),
]


async def run_simulation(args: argparse.Namespace) -> None:
    """Replays SCENARIO as Bluetooth packets at --speed x real time."""
    rng = random.Random(7)
    start = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0).timestamp()
    sim = {"t": start}

    async def sim_sleep(secs: float) -> None:
        await asyncio.sleep(secs / args.speed)

    mon = Monitor(args, lambda: sim["t"], sim_sleep)
    next_beat = start
    try:
        for minutes, bpm, rmssd, artifact, what in SCENARIO:
            print(f"--- simulated: {what} ({minutes} min) ---")
            for _ in range(minutes * 60):
                now = sim["t"]
                rrs = []
                while next_beat <= now + 1:
                    rr = 60000.0 / bpm + rng.gauss(0, rmssd / math.sqrt(2))
                    if rng.random() < artifact:
                        rr *= rng.choice([0.55, 1.6])
                    next_beat += rr / 1000.0
                    rrs.append(max(250.0, rr))
                beat_bpm = int(round(bpm + rng.gauss(0, 1.5)))
                raw = [int(round(r * 1024 / 1000)) for r in rrs]
                packet = bytes([0x10 | 0x04 | 0x02, beat_bpm]) + b"".join(x.to_bytes(2, "little") for x in raw)
                mon.on_packet(packet, now)
                sim["t"] += 1
                mon.tick()
                await asyncio.sleep(1 / args.speed)
    finally:
        mon.summary()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", help="strap name or address (default: first 'WHOOP' heart-rate sensor)")
    ap.add_argument("--log-dir", help="where minute logs go (default data/live, or data/live-sim with --simulate)")
    ap.add_argument("--calibrate", type=int, default=5, help="minutes of calm used as a first baseline (default 5)")
    ap.add_argument("--recalibrate", action="store_true", help="ignore the saved baseline")
    ap.add_argument("--alert-level", type=float, default=2.0, help="stress score that counts as high (0-3, default 2)")
    ap.add_argument("--alert-minutes", type=int, default=5, help="minutes it must stay high before alerting (default 5)")
    ap.add_argument("--cooldown", type=int, default=30, help="minutes between alerts (default 30)")
    ap.add_argument("--quiet", default="22-7", help="hours with no alerts, e.g. 22-7 (default); '' to disable")
    ap.add_argument("--windows", help="analysis.json from stress_analysis.py: heads-up 10 min before each window")
    ap.add_argument("--coach", action="store_true", help="guided 1-minute breathing in the terminal after an alert")
    ap.add_argument("--ntfy", help="ntfy.sh topic for phone push (pick a long random name; anyone who knows it can read it)")
    ap.add_argument("--simulate", action="store_true", help="synthetic session instead of Bluetooth")
    ap.add_argument("--speed", type=float, default=120.0, help="simulation speed-up (default 120x)")
    args = ap.parse_args(argv)
    if args.log_dir is None:
        args.log_dir = "data/live-sim" if args.simulate else "data/live"
    if args.simulate:
        args.quiet = ""
        args.recalibrate = True
    try:
        asyncio.run(run_simulation(args) if args.simulate else run_bluetooth(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
