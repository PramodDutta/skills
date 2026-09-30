"""
booking_api.py - a tiny local "Booking API" to practise on.

It is a mini clone of restful-booker (the popular API-testing practice app),
with a few bugs PLANTED ON PURPOSE for your agents to find.

Run it in a terminal (then poke it with curl / Postman):
    python lessons/booking_api.py            -> http://127.0.0.1:3001

Or start it from code on a random free port:
    with start_booking_api() as api:
        requests.get(f"{api.base_url}/ping")

API_SPEC below is the requirements document - what the API SHOULD do.
Tests compare the actual behaviour against it.
(The list of planted bugs is at the very bottom of this file. No peeking!)
"""

from __future__ import annotations

import copy
import json
import re
import sys
import threading
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USERNAME, PASSWORD = "admin", "password123"
TOKEN = "qa-token-8f14e45f"

API_SPEC = """\
Booking API - requirements
==========================
Auth
  POST /auth  {"username": "admin", "password": "password123"}  -> 200 {"token": "..."}
  Send the token as a cookie on PUT/DELETE:   Cookie: token=<token>

Health
  GET /ping -> 201 when the service is up (documented quirk, same as restful-booker)

Bookings
  GET    /booking          -> 200, a list of {"bookingid": <id>}
  GET    /booking/{id}     -> 200 + the booking; 404 when the id does not exist or is not a number
  POST   /booking          -> 200 {"bookingid": <id>, "booking": {...}}
  PUT    /booking/{id}     -> 200 + the updated booking; 403 without a valid token; 404 unknown id
  DELETE /booking/{id}     -> 201 (documented quirk); 403 without a valid token; 404 unknown id

Booking body (POST and PUT)
  Required: firstname, lastname, totalprice, depositpaid, bookingdates.checkin, bookingdates.checkout
  Optional: additionalneeds
  Validation - every violation must return 400 Bad Request:
    - every required field must be present
    - firstname / lastname are strings, depositpaid is a boolean
    - totalprice is a number >= 0
    - dates are YYYY-MM-DD and bookingdates.checkout is AFTER bookingdates.checkin
"""

VALID_BOOKING = {
    "firstname": "Jim",
    "lastname": "Brown",
    "totalprice": 111,
    "depositpaid": True,
    "bookingdates": {"checkin": "2026-01-01", "checkout": "2026-01-05"},
    "additionalneeds": "Breakfast",
}

_SEED = [
    {"firstname": "Sally", "lastname": "Brown", "totalprice": 250, "depositpaid": True,
     "bookingdates": {"checkin": "2026-03-01", "checkout": "2026-03-04"},
     "additionalneeds": "Breakfast"},
    {"firstname": "Mark", "lastname": "Jones", "totalprice": 520, "depositpaid": False,
     "bookingdates": {"checkin": "2026-04-10", "checkout": "2026-04-15"},
     "additionalneeds": "Late checkout"},
    {"firstname": "Priya", "lastname": "Sharma", "totalprice": 180, "depositpaid": True,
     "bookingdates": {"checkin": "2026-05-20", "checkout": "2026-05-22"}},
]


class _BadRequest(Exception):
    pass


def _parse_booking(data: dict) -> dict:
    booking = {
        "firstname": data["firstname"],
        "lastname": data["lastname"],
        "totalprice": data["totalprice"],
        "depositpaid": data["depositpaid"],
        "bookingdates": {
            "checkin": data["bookingdates"]["checkin"],
            "checkout": data["bookingdates"]["checkout"],
        },
    }
    if not isinstance(booking["firstname"], str) or not isinstance(booking["lastname"], str):
        raise _BadRequest
    price = booking["totalprice"]
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        raise _BadRequest
    if not isinstance(booking["depositpaid"], bool):
        raise _BadRequest
    for key in ("checkin", "checkout"):
        value = booking["bookingdates"][key]
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise _BadRequest
        try:
            date.fromisoformat(value)
        except ValueError:
            raise _BadRequest from None
    if "additionalneeds" in data:
        booking["additionalneeds"] = data["additionalneeds"]
    return booking


class _Store:
    def __init__(self, flaky_503: int) -> None:
        self.lock = threading.Lock()
        self.bookings = {i: copy.deepcopy(b) for i, b in enumerate(_SEED, start=1)}
        self.next_id = len(_SEED) + 1
        self.flaky_503_left = flaky_503


def _handler_for(store: _Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep lesson output clean
            pass

        # ---- plumbing -------------------------------------------------------
        def _send(self, status: int, body: str | dict | list) -> None:
            is_text = isinstance(body, str)
            payload = (body if is_text else json.dumps(body)).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain" if is_text else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"null")

        def _authorized(self) -> bool:
            return f"token={TOKEN}" in (self.headers.get("Cookie") or "")

        def _dispatch(self, endpoint) -> None:
            try:
                endpoint()
            except (_BadRequest, json.JSONDecodeError):
                self._send(400, "Bad Request")
            except Exception:  # like most web frameworks: an unhandled error is a 500
                self._send(500, "Internal Server Error")

        def do_GET(self) -> None:
            self._dispatch(self._get)

        def do_POST(self) -> None:
            self._dispatch(self._post)

        def do_PUT(self) -> None:
            self._dispatch(self._put)

        def do_DELETE(self) -> None:
            self._dispatch(self._delete)

        # ---- endpoints ------------------------------------------------------
        def _get(self) -> None:
            if self.path == "/ping":
                return self._send(201, "Created")
            if self.path == "/booking":
                return self._send(200, [{"bookingid": i} for i in sorted(store.bookings)])
            match = re.fullmatch(r"/booking/([^/]+)", self.path)
            if not match:
                return self._send(404, "Not Found")
            with store.lock:
                if store.flaky_503_left > 0:
                    store.flaky_503_left -= 1
                    return self._send(503, "Service Unavailable")
            booking = store.bookings.get(int(match.group(1)))
            if booking is None:
                return self._send(404, "Not Found")
            return self._send(200, booking)

        def _post(self) -> None:
            if self.path == "/auth":
                creds = self._json_body() or {}
                if creds.get("username") == USERNAME and creds.get("password") == PASSWORD:
                    return self._send(200, {"token": TOKEN})
                return self._send(200, {"reason": "Bad credentials"})
            if self.path != "/booking":
                return self._send(404, "Not Found")
            booking = _parse_booking(self._json_body())
            with store.lock:
                booking_id = store.next_id
                store.next_id += 1
                store.bookings[booking_id] = booking
            return self._send(200, {"bookingid": booking_id, "booking": booking})

        def _put(self) -> None:
            match = re.fullmatch(r"/booking/(\d+)", self.path)
            if not match:
                return self._send(404, "Not Found")
            if not self._authorized():
                return self._send(403, "Forbidden")
            booking_id = int(match.group(1))
            if booking_id not in store.bookings:
                return self._send(404, "Not Found")
            booking = _parse_booking(self._json_body())
            with store.lock:
                store.bookings[booking_id] = booking
            return self._send(200, booking)

        def _delete(self) -> None:
            match = re.fullmatch(r"/booking/(\d+)", self.path)
            if not match:
                return self._send(404, "Not Found")
            if not self._authorized():
                return self._send(403, "Forbidden")
            with store.lock:
                removed = store.bookings.pop(int(match.group(1)), None)
            if removed is None:
                return self._send(404, "Not Found")
            return self._send(201, "Created")

    return Handler


@dataclass
class BookingAPI:
    server: ThreadingHTTPServer
    thread: threading.Thread

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> "BookingAPI":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def start_booking_api(port: int = 0, flaky_503: int = 0) -> BookingAPI:
    """Start the API in a background thread. port=0 picks a free port.

    flaky_503: the first N "GET /booking/{id}" calls return 503 Service
    Unavailable - a flaky environment to practise retries on.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler_for(_Store(flaky_503)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return BookingAPI(server, thread)


if __name__ == "__main__":
    api = start_booking_api(port=int(sys.argv[1]) if len(sys.argv) > 1 else 3001)
    print(f"Booking API running at {api.base_url}   (Ctrl+C to stop)\n")
    print(API_SPEC)
    try:
        api.thread.join()
    except KeyboardInterrupt:
        api.stop()


# ---------------------------------------------------------------------------
# SPOILERS - the bugs planted in this API. Let your agent find them first!
# ---------------------------------------------------------------------------
PLANTED_BUGS = {
    "missing_field_500": "POST/PUT /booking with a required field missing returns 500 instead of 400",
    "negative_price_accepted": "POST/PUT /booking accepts a negative totalprice instead of returning 400",
    "checkout_before_checkin": "POST/PUT /booking accepts checkout before checkin instead of returning 400",
    "non_numeric_id_500": "GET /booking/abc returns 500 instead of 404",
}
