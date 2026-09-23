#!/usr/bin/env python3
"""Pull your data from the official WHOOP Developer API (v2). Stdlib only.

What this API gives you: per-day recovery (HRV, resting HR, respiratory rate,
SpO2, skin temp), sleep (timing, stages, performance), strain and workouts,
plus - where WHOOP serves it - the raw heart-rate stream recorded during each
sleep. It does NOT expose the in-app Stress Monitor or daytime minute-by-minute
heart rate; see SKILL.md for how to add those.

One-time setup (about 5 minutes):
  1. https://developer-dashboard.whoop.com -> create an app.
     Redirect URL: https://localhost:8765/callback  (any https URL you register
     works: after approving you only copy the address bar, the page itself
     does not need to load).
     Scopes: read:recovery read:cycles read:sleep read:workout read:profile
             read:body_measurement, and offline (refresh tokens).
  2. Put the credentials in environment variables - never paste them in a chat:
       WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET
       WHOOP_REDIRECT_URI   (optional, default https://localhost:8765/callback)
  3. python3 whoop_official.py auth-url              # open the link, approve
     python3 whoop_official.py exchange "<URL you landed on>"
  4. python3 whoop_official.py fetch --days 90 --out data/whoop.json --sleep-hr

Tokens live in $WHOOP_TOKEN_FILE or ~/.config/whoop-stress/token.json (0600).
WHOOP rotates the refresh token on every refresh, so the file is rewritten
atomically each time; never copy it around or commit it.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from common import (
    format_offset,
    iso,
    new_dataset,
    parse_offset,
    parse_ts,
    read_json,
    to_float,
    wake_date,
    write_json,
)

API = "https://api.prod.whoop.com"
AUTHORIZE_URL = f"{API}/oauth/oauth2/auth"
TOKEN_URL = f"{API}/oauth/oauth2/token"
DATA_URL = f"{API}/developer"
SCOPES = [
    "offline",
    "read:recovery",
    "read:cycles",
    "read:sleep",
    "read:workout",
    "read:profile",
    "read:body_measurement",
]
DEFAULT_REDIRECT = "https://localhost:8765/callback"
USER_AGENT = "whoop-stress-tracker/1.0"
PAGE_LIMIT = 25  # WHOOP's maximum page size


class ApiError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} from {url.split('?')[0]}: {body}")
        self.status = status


# -- config -----------------------------------------------------------------


def home() -> Path:
    return Path(os.environ.get("WHOOP_STRESS_HOME") or Path.home() / ".config" / "whoop-stress")


def token_path() -> Path:
    return Path(os.environ.get("WHOOP_TOKEN_FILE") or home() / "token.json")


def state_path() -> Path:
    return home() / "oauth_state.json"


def credentials() -> tuple[str, str, str]:
    cid = os.environ.get("WHOOP_CLIENT_ID", "").strip()
    secret = os.environ.get("WHOOP_CLIENT_SECRET", "").strip()
    redirect = os.environ.get("WHOOP_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT
    if not cid or not secret:
        raise SystemExit(
            "WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET are not set.\n"
            "Create an app at https://developer-dashboard.whoop.com and provide them as "
            "environment variables (in a cloud session: the environment's settings). "
            "Never paste them into a chat."
        )
    return cid, secret, redirect


# -- HTTP -------------------------------------------------------------------


def http(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    form: Optional[dict] = None,
    params: Optional[dict] = None,
    retries: int = 5,
) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if form is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    hdrs.update(headers or {})

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = resp.read().decode("utf-8", "replace")
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 2 * 2**attempt
                print(f"  WHOOP returned {e.code}; retrying in {wait:.0f}s", file=sys.stderr)
                time.sleep(min(wait, 60))
                attempt += 1
                continue
            raise ApiError(e.code, url, body) from None
        except urllib.error.URLError as e:
            reason = str(e.reason)
            if "Tunnel connection failed: 403" in reason:
                raise SystemExit(
                    "network: this environment's proxy blocks api.prod.whoop.com. Add that host to "
                    "the environment's allowed domains (or run this script on your own computer)."
                ) from None
            if attempt < 2:
                time.sleep(2 + 2 * attempt)
                attempt += 1
                continue
            raise SystemExit(f"network error talking to WHOOP: {reason}") from None


# -- OAuth ------------------------------------------------------------------


def save_token(tok: dict) -> None:
    tok = dict(tok)
    now = int(time.time())
    tok["obtained_at"] = now
    tok["expires_at"] = now + int(tok.get("expires_in") or 3600)
    write_json(token_path(), tok, private=True)


def load_token() -> dict:
    p = token_path()
    if not p.exists():
        raise SystemExit("not authorized yet: run `whoop_official.py auth-url`, then `exchange`.")
    return read_json(p)


def access_token(force_refresh: bool = False) -> str:
    tok = load_token()
    if not force_refresh and tok.get("access_token") and tok.get("expires_at", 0) - 120 > time.time():
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise SystemExit(
            "access token expired and there is no refresh token (was the `offline` scope granted?). "
            "Re-run auth-url / exchange."
        )
    cid, secret, _ = credentials()
    try:
        fresh = http(
            "POST",
            TOKEN_URL,
            form={
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": cid,
                "client_secret": secret,
                "scope": "offline",
            },
            retries=2,
        )
    except ApiError as e:
        raise SystemExit(
            f"token refresh failed ({e.status}). WHOOP refresh tokens are single-use; if another "
            "copy of this token file refreshed first, re-run auth-url / exchange."
        ) from None
    fresh.setdefault("refresh_token", tok["refresh_token"])
    save_token(fresh)
    return fresh["access_token"]


def cmd_auth_url(_args: argparse.Namespace) -> int:
    cid, _secret, redirect = credentials()
    state = secrets.token_urlsafe(18)  # WHOOP requires >= 8 characters
    write_json(
        state_path(),
        {"state": state, "redirect_uri": redirect, "created_at": int(time.time())},
        private=True,
    )
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": redirect,
            "scope": " ".join(SCOPES),
            "state": state,
        }
    )
    print("Open this link, sign in to WHOOP and approve access:\n")
    print(f"{AUTHORIZE_URL}?{query}\n")
    print(
        f"You will be sent to {redirect}?code=...&state=... - the page may fail to load, that is\n"
        "expected. Copy the full address from the browser bar and run:\n"
        '  python3 whoop_official.py exchange "<that URL>"\n'
        "(the code expires within minutes)."
    )
    return 0


def cmd_exchange(args: argparse.Namespace) -> int:
    cid, secret, redirect = credentials()
    saved = read_json(state_path()) if state_path().exists() else {}
    pasted = args.redirect.strip()
    code = ""
    if "://" in pasted or "?" in pasted or "code=" in pasted:
        query = urllib.parse.urlparse(pasted).query or pasted.split("?", 1)[-1]
        q = urllib.parse.parse_qs(query)
        if "error" in q:
            raise SystemExit(f"authorization was denied: {q['error'][0]} {q.get('error_description', [''])[0]}")
        code = (q.get("code") or [""])[0]
        got_state = (q.get("state") or [""])[0]
        if saved.get("state") and not secrets.compare_digest(got_state, saved["state"]):
            raise SystemExit("state mismatch: this URL is not from the latest auth-url run. Start again.")
    else:
        code = pasted
        print("note: bare code given, state could not be verified", file=sys.stderr)
    if not code:
        raise SystemExit("no ?code= found in what you pasted.")

    tok = http(
        "POST",
        TOKEN_URL,
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": saved.get("redirect_uri") or redirect,
            "client_id": cid,
            "client_secret": secret,
        },
        retries=1,
    )
    if "access_token" not in tok:
        raise SystemExit("WHOOP did not return an access token.")
    save_token(tok)
    if state_path().exists():
        state_path().unlink()
    granted = tok.get("scope", "")
    print(f"authorized. token saved to {token_path()} (owner-only).")
    if "offline" not in granted.split():
        print("warning: `offline` scope not granted - you will need to re-authorize in about an hour.")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    p = token_path()
    if not p.exists():
        print("no token yet - run auth-url / exchange")
        return 1
    tok = read_json(p)
    left = int(tok.get("expires_at", 0) - time.time())
    print(f"token file: {p}")
    print(f"scopes: {tok.get('scope', '?')}")
    print(f"access token: {'valid for ' + str(left // 60) + ' min' if left > 0 else 'expired (auto-refreshes)'}")
    print(f"refresh token: {'present' if tok.get('refresh_token') else 'missing'}")
    return 0


def cmd_revoke(_args: argparse.Namespace) -> int:
    token = access_token()
    http("DELETE", f"{DATA_URL}/v2/user/access", headers={"Authorization": f"Bearer {token}"}, retries=1)
    token_path().unlink(missing_ok=True)
    print("access revoked and local token deleted.")
    return 0


# -- data -------------------------------------------------------------------


def _get(path: str, params: Optional[dict] = None) -> Any:
    url = f"{DATA_URL}/{path}"
    try:
        return http("GET", url, headers={"Authorization": f"Bearer {access_token()}"}, params=params)
    except ApiError as e:
        if e.status != 401:
            raise
        return http(
            "GET", url, headers={"Authorization": f"Bearer {access_token(force_refresh=True)}"}, params=params
        )


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def collection(path: str, start: datetime, end: datetime) -> list[dict]:
    params: dict[str, Any] = {"start": _iso_z(start), "end": _iso_z(end), "limit": PAGE_LIMIT}
    records: list[dict] = []
    while True:
        page = _get(path, params)
        records.extend(page.get("records") or [])
        nxt = page.get("next_token") or page.get("nextToken")
        if not nxt:
            return records
        params["nextToken"] = nxt


def parse_stream(resp: Any) -> list[list[float]]:
    rows = []
    if isinstance(resp, dict):
        rows = resp.get("stream") or resp.get("records") or resp.get("data") or resp.get("values") or []
    elif isinstance(resp, list):
        rows = resp
    samples = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ts = parse_ts(r.get("timestamp") or r.get("time"))
        bpm = to_float(r["hr"] if "hr" in r else r.get("heart_rate", r.get("data")))
        if ts and bpm and bpm > 0:
            samples.append([int(ts.timestamp()), round(bpm, 1)])
    samples.sort()
    return samples


def _score(obj: Optional[dict]) -> dict:
    if not obj or obj.get("score_state", "SCORED") != "SCORED":
        return {}
    return obj.get("score") or {}


def _minutes(ms: Any) -> Optional[float]:
    v = to_float(ms)
    return None if v is None else round(v / 60000.0, 1)


def normalize(
    cycles: list[dict],
    recoveries: list[dict],
    sleeps: list[dict],
    workouts: list[dict],
    streams: Optional[dict[str, list]] = None,
) -> dict:
    """Join raw v2 records into the normalized dataset (see common.py)."""
    ds = new_dataset("whoop_api")
    rec_by_cycle = {r.get("cycle_id"): r for r in recoveries}
    sleep_by_id = {s.get("id"): s for s in sleeps}
    main_sleep_by_cycle = {s.get("cycle_id"): s for s in sleeps if not s.get("nap")}

    for c in sorted(cycles, key=lambda c: c.get("start") or ""):
        tz = parse_offset(c.get("timezone_offset")) or timezone.utc
        start = parse_ts(c.get("start"))
        end = parse_ts(c.get("end"))
        start_l = start.astimezone(tz) if start else None
        end_l = end.astimezone(tz) if end else None
        rec = rec_by_cycle.get(c.get("id"))
        sl = sleep_by_id.get(rec.get("sleep_id")) if rec else None
        sl = sl or main_sleep_by_cycle.get(c.get("id"))
        rs, cs, ss = _score(rec), _score(c), _score(sl)

        sleep = None
        if sl:
            s_start = parse_ts(sl.get("start"))
            s_end = parse_ts(sl.get("end"))
            stages = ss.get("stage_summary") or {}
            need = ss.get("sleep_needed") or {}
            light = _minutes(stages.get("total_light_sleep_time_milli"))
            sws = _minutes(stages.get("total_slow_wave_sleep_time_milli"))
            rem = _minutes(stages.get("total_rem_sleep_time_milli"))
            parts = [x for x in (light, sws, rem) if x is not None]
            need_parts = [
                _minutes(need.get(k))
                for k in (
                    "baseline_milli",
                    "need_from_sleep_debt_milli",
                    "need_from_recent_strain_milli",
                    "need_from_recent_nap_milli",
                )
            ]
            sleep = {
                "id": sl.get("id"),
                "start": iso(s_start.astimezone(tz)) if s_start else None,
                "end": iso(s_end.astimezone(tz)) if s_end else None,
                "performance": to_float(ss.get("sleep_performance_percentage")),
                "efficiency": to_float(ss.get("sleep_efficiency_percentage")),
                "consistency": to_float(ss.get("sleep_consistency_percentage")),
                "resp_rate": to_float(ss.get("respiratory_rate")),
                "asleep_min": round(sum(parts), 1) if parts else None,
                "in_bed_min": _minutes(stages.get("total_in_bed_time_milli")),
                "awake_min": _minutes(stages.get("total_awake_time_milli")),
                "light_min": light,
                "sws_min": sws,
                "rem_min": rem,
                "disturbances": to_float(stages.get("disturbance_count")),
                "need_min": round(sum(x for x in need_parts if x is not None), 1)
                if any(x is not None for x in need_parts)
                else None,
                "debt_min": _minutes(need.get("need_from_sleep_debt_milli")),
            }
        s_end_dt = parse_ts(sleep["end"]) if sleep and sleep.get("end") else None
        day = wake_date(start_l, s_end_dt)
        if day is None:
            continue
        ds["days"].append(
            {
                "date": day.isoformat(),
                "cycle_start": iso(start_l),
                "cycle_end": iso(end_l),
                "tz_offset": format_offset(tz, start),
                "recovery": to_float(rs.get("recovery_score")),
                "rhr": to_float(rs.get("resting_heart_rate")),
                "hrv": to_float(rs.get("hrv_rmssd_milli")),
                "resp_rate": sleep.get("resp_rate") if sleep else None,
                "spo2": to_float(rs.get("spo2_percentage")),
                "skin_temp": to_float(rs.get("skin_temp_celsius")),
                "calibrating": bool(rs.get("user_calibrating", False)),
                "strain": to_float(cs.get("strain")),
                "avg_hr": to_float(cs.get("average_heart_rate")),
                "max_hr": to_float(cs.get("max_heart_rate")),
                "sleep": sleep,
            }
        )

    for s in sleeps:
        if s.get("nap"):
            tz = parse_offset(s.get("timezone_offset")) or timezone.utc
            a, b = parse_ts(s.get("start")), parse_ts(s.get("end"))
            if a and b:
                ds["naps"].append({"start": iso(a.astimezone(tz)), "end": iso(b.astimezone(tz))})

    for w in sorted(workouts, key=lambda w: w.get("start") or ""):
        tz = parse_offset(w.get("timezone_offset")) or timezone.utc
        a, b = parse_ts(w.get("start")), parse_ts(w.get("end"))
        if not (a and b):
            continue
        ws = _score(w)
        ds["workouts"].append(
            {
                "start": iso(a.astimezone(tz)),
                "end": iso(b.astimezone(tz)),
                "sport": w.get("sport_name") or str(w.get("sport_id", "activity")),
                "strain": to_float(ws.get("strain")),
                "avg_hr": to_float(ws.get("average_heart_rate")),
                "max_hr": to_float(ws.get("max_heart_rate")),
            }
        )

    for sid, samples in (streams or {}).items():
        sl = sleep_by_id.get(sid)
        a, b = (parse_ts(sl.get("start")), parse_ts(sl.get("end"))) if sl else (None, None)
        if a and b and samples:
            tz = parse_offset(sl.get("timezone_offset")) or timezone.utc
            ds["sleep_hr"][sid] = {"start": iso(a.astimezone(tz)), "end": iso(b.astimezone(tz)), "samples": samples}
    return ds


def _records(raw: dict, *keys: str) -> list[dict]:
    for k in keys:
        v = raw.get(k)
        if isinstance(v, dict):
            v = v.get("records")
        if isinstance(v, list):
            return v
    return []


def cmd_normalize(args: argparse.Namespace) -> int:
    raw = read_json(args.raw)
    ds = normalize(
        _records(raw, "cycles", "cycle"),
        _records(raw, "recoveries", "recovery"),
        _records(raw, "sleeps", "sleep"),
        _records(raw, "workouts", "workout"),
        raw.get("sleep_streams") or {},
    )
    out = write_json(args.out, ds, private=True)
    print(f"wrote {out} ({len(ds['days'])} days)")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    if args.start:
        start = parse_ts(args.start) or start
    if args.end:
        end = parse_ts(args.end) or end
    print(f"fetching WHOOP data {start.date()} .. {end.date()}")

    cycles = collection("v2/cycle", start, end)
    print(f"  cycles:     {len(cycles)}")
    recoveries = collection("v2/recovery", start, end)
    print(f"  recoveries: {len(recoveries)}")
    sleeps = collection("v2/activity/sleep", start, end)
    print(f"  sleeps:     {len(sleeps)}")
    workouts = collection("v2/activity/workout", start, end)
    print(f"  workouts:   {len(workouts)}")

    streams: dict[str, list] = {}
    if args.sleep_hr:
        main = [s for s in sleeps if not s.get("nap") and s.get("score_state") == "SCORED"]
        main = sorted(main, key=lambda s: s.get("start") or "")[-args.max_nights :]
        for i, s in enumerate(main):
            try:
                resp = _get(f"v2/activity/sleep/{s['id']}/stream", {"types": ["hr"]})
            except ApiError as e:
                print(f"  sleep heart-rate stream unavailable ({e.status}); skipping streams", file=sys.stderr)
                break
            samples = parse_stream(resp)
            if samples:
                streams[s["id"]] = samples
            if i % 20 == 19:
                print(f"  sleep streams: {i + 1}/{len(main)}")
            time.sleep(0.7)  # stay well under WHOOP's 100 requests/minute
        print(f"  sleep HR streams: {len(streams)} nights")

    ds = normalize(cycles, recoveries, sleeps, workouts, streams)
    ds["range"] = {"start": iso(start), "end": iso(end)}
    out = write_json(args.out, ds, private=True)
    print(f"wrote {out} ({len(ds['days'])} days)")
    if args.raw:
        write_json(
            args.raw,
            {"cycles": cycles, "recoveries": recoveries, "sleeps": sleeps, "workouts": workouts},
            private=True,
        )
        print(f"wrote raw records to {args.raw}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth-url", help="print the WHOOP consent link")
    p_ex = sub.add_parser("exchange", help="exchange the redirect URL (or code) for tokens")
    p_ex.add_argument("redirect", help="the full URL you were redirected to (or the bare code)")
    p_f = sub.add_parser("fetch", help="download and normalize your data")
    p_f.add_argument("--days", type=int, default=90)
    p_f.add_argument("--start", help="ISO date/time (overrides --days)")
    p_f.add_argument("--end", help="ISO date/time (default: now)")
    p_f.add_argument("--out", default="data/whoop.json")
    p_f.add_argument("--raw", help="also save the raw API records here")
    p_f.add_argument("--sleep-hr", action="store_true", help="also pull heart rate recorded during each sleep")
    p_f.add_argument("--max-nights", type=int, default=60, help="cap on sleep streams to fetch")
    p_n = sub.add_parser("normalize", help="normalize v2 records you already have (e.g. from a WHOOP MCP server)")
    p_n.add_argument("raw", help='JSON with "cycles", "recoveries", "sleeps", "workouts" lists (the --raw format)')
    p_n.add_argument("--out", default="data/whoop.json")
    sub.add_parser("status", help="show token status (no secrets printed)")
    sub.add_parser("revoke", help="revoke this app's access and delete the local token")
    args = ap.parse_args(argv)
    return {
        "auth-url": cmd_auth_url,
        "exchange": cmd_exchange,
        "fetch": cmd_fetch,
        "normalize": cmd_normalize,
        "status": cmd_status,
        "revoke": cmd_revoke,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
