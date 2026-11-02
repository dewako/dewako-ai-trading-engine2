"""Server-Sent Events delivery for the activity feed, as a framework-free async generator (testable without a web server).

Delivery is at-least-once with client de-duplication by event_id: every batch ends with an `id:` carrying the per-source watermarks, so a
reconnect (Last-Event-ID or ?cursor=) resumes after the last batch the client fully received; a batch cut mid-way is re-sent and the
client drops what it already has. Bounds: <=200 events per source per poll, bounded duration (the client reconnects), a cap on concurrent
streams, a heartbeat that also re-checks authorization, and no queue: the generator only produces when the consumer pulls.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Callable, Optional

from dewako.activity.service import ActivityService, Filters, SRC_KEYS, encode_cursor

MAX_STREAMS = 20
POLL_INTERVAL, HEARTBEAT, MAX_DURATION, MAX_CONSECUTIVE_ERRORS = 1.0, 15.0, 600.0, 5


class StreamLimiter:
    def __init__(self, maximum: int = MAX_STREAMS):
        self.maximum, self.active = maximum, 0

    def acquire(self) -> bool:
        if self.active >= self.maximum:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)


def frame(event: str, data: dict, id_: Optional[str] = None, retry: Optional[int] = None) -> str:
    """One SSE frame. JSON is single-line, so no payload can inject a field or terminate the frame early."""
    out = ""
    if retry is not None:
        out += f"retry: {int(retry)}\n"
    if id_ is not None:
        out += "id: " + "".join(ch for ch in id_ if ch.isalnum() or ch in "-_=") + "\n"
    out += f"event: {event}\n" + "data: " + json.dumps(data, separators=(",", ":"), ensure_ascii=True) + "\n\n"
    return out


async def event_stream(service: ActivityService, *, marks: Optional[dict] = None, flt: Optional[Filters] = None,
                       is_authorized: Callable[[], bool] = lambda: True, sleep: Callable = asyncio.sleep, clock: Callable[[], float] = time.monotonic,
                       poll_interval: Optional[float] = None, heartbeat: Optional[float] = None, max_duration: Optional[float] = None) -> AsyncIterator[str]:
    poll_interval = POLL_INTERVAL if poll_interval is None else poll_interval      # resolved per call so tests/operators can change the module bounds
    heartbeat = HEARTBEAT if heartbeat is None else heartbeat
    max_duration = MAX_DURATION if max_duration is None else max_duration
    start = clock()
    last_beat = last_auth = start
    errors = 0
    try:
        if marks is not None:
            # Clean up the marks to ensure they only contain valid source keys with non-negative integers
            cleaned_marks = {k: int(v) for k, v in marks.items() if k in SRC_KEYS.values() and isinstance(v, (int, str)) and int(v) >= 0}
            # Ensure all required source keys are present
            for k in SRC_KEYS.values():
                cleaned_marks.setdefault(k, 0)
            marks = cleaned_marks
        else:
            marks = service.watermarks_now()
    except Exception:
        yield frame("error", {"code": "BACKEND_ERROR"})
        return
    for k in SRC_KEYS.values():
        marks.setdefault(k, 0)
    yield frame("hello", {"schema_version": 1, "cursor": encode_cursor(marks)}, retry=3000)
    while True:
        now = clock()
        if now - start >= max_duration:
            yield frame("closed", {"reason": "max_duration"})
            return
        try:
            events, new = service.poll(marks, flt)
            errors = 0
        except Exception:                                   # never leak the exception text; the client shows 'backend error' and reconnects
            errors += 1
            yield frame("error", {"code": "BACKEND_ERROR"})
            if errors >= MAX_CONSECUTIVE_ERRORS:
                yield frame("closed", {"reason": "backend_error"})
                return
            await sleep(min(poll_interval * 2 ** errors, 15.0))
            continue
        if events:
            cur = encode_cursor(new)
            for i, ev in enumerate(events):
                yield frame("activity", ev, id_=cur if i == len(events) - 1 else None)
            last_beat = now
        elif new != marks:
            yield frame("cursor", {"cursor": encode_cursor(new)}, id_=encode_cursor(new))   # filtered rows advanced the position: remember it
            last_beat = now
        marks = new
        if now - last_auth >= heartbeat:                   # independent of traffic: a revoked session cannot keep receiving a busy stream
            last_auth = now
            try:
                ok = is_authorized()
            except Exception:
                ok = False
            if not ok:
                yield frame("closed", {"reason": "unauthorized"})
                return
        if now - last_beat >= heartbeat:
            yield ": keepalive\n\n"
            last_beat = now
        await sleep(poll_interval)
