"""A per-client request limit for the public pages, held in memory.

The unsubscribe page answers anyone on the internet without a login, and each
request reads the database and may write a file. Nothing limited how fast one
address could call it. This counts each client's requests over the last
window and says when one is over; what to do then is the caller's to decide,
because a mailbox provider's one-click opt-out must be applied whatever the
count.

The dashboard uses the same table for wrong passwords (counted per address,
locking it out for a while) and for the open-tracking pixel.
"""

import math
import threading
import time
from collections import OrderedDict, deque


class RequestLimiter:
    """At most `limit` requests per client in any `window` seconds.

    Memory is bounded twice: a client keeps at most `limit` timestamps, which
    is all the decision needs, and at most `max_clients` clients are held, the
    least recently seen dropped first. A client whose requests have all left
    the window is dropped when it is next looked past, so the table does not
    keep every address that ever called.
    """

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        max_clients: int = 10000,
        clock=time.monotonic,
    ):
        self.limit = limit
        self.window = window
        self.max_clients = max_clients
        self._clock = clock
        self._hits: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._hits)

    def _drop_expired(self, now: float) -> None:
        """Drop clients whose newest request has left the window. The table is
        in least-recently-seen order, so the scan stops at the first client
        still inside it."""
        while self._hits:
            key, stamps = next(iter(self._hits.items()))
            if now - stamps[-1] < self.window:
                return
            del self._hits[key]

    def retry_after(self, client: str) -> int | None:
        """What `hit` would answer for `client` now, without counting a
        request: for a caller that counts only some requests, such as the
        dashboard's failed logins."""
        now = self._clock()
        with self._lock:
            stamps = self._hits.get(client)
            live = [t for t in stamps or () if now - t < self.window]
            if len(live) < self.limit:
                return None
            return max(1, math.ceil(self.window - (now - live[0])))

    def hit(self, client: str) -> int | None:
        """Count a request from `client`. None while it is within the limit;
        otherwise the whole seconds until its oldest counted request leaves the
        window, for a Retry-After header. A request over the limit is still
        counted, so a client that keeps calling stays limited."""
        now = self._clock()
        with self._lock:
            self._drop_expired(now)
            stamps = self._hits.pop(client, None)
            if stamps is None:
                stamps = deque(maxlen=self.limit)
            while stamps and now - stamps[0] >= self.window:
                stamps.popleft()
            over = len(stamps) >= self.limit
            retry_after = (
                max(1, math.ceil(self.window - (now - stamps[0]))) if over else None
            )
            stamps.append(now)
            self._hits[client] = stamps
            while len(self._hits) > self.max_clients:
                self._hits.popitem(last=False)
            return retry_after
