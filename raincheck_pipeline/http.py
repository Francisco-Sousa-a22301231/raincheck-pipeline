"""Small HTTP helper on the standard library: retries with back-off, byte counting."""

from __future__ import annotations

import http.client
import random
import threading
import time
import urllib.error
import urllib.request

USER_AGENT = "ecmwf-open-data-extractor/1.0"
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}" + (f": {body[:300]}" if body else ""))
        self.status = status
        self.url = url
        self.body = body


class Counter:
    """Bytes and requests so far (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.bytes = 0
        self.requests = 0

    def add(self, n: int) -> None:
        with self._lock:
            self.bytes += n
            self.requests += 1


counter = Counter()


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 60.0,
    tries: int = 4,
    user_agent: str = USER_AGENT,
) -> tuple[int, bytes]:
    """Returns (status, body) of a 2xx answer.

    Raises HttpError on any other status (after retrying the ones worth retrying) and the last network
    error when the server cannot be reached.
    """
    last: Exception | None = None
    for attempt in range(tries):
        if attempt:
            time.sleep(min(30.0, 1.5 * 2 ** (attempt - 1)) + random.random())
        req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": user_agent, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                body = res.read()
                counter.add(len(body))
                return res.status, body
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace") if err.fp else ""
            last = HttpError(err.code, url, body)
            if err.code not in RETRY_STATUS:
                raise last from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            last = err
        except http.client.HTTPException as err:
            # The answer broke off half-way (IncompleteRead) or made no sense: as good as unreachable.
            last = OSError(f"broken answer from {url}: {err!r}")
    assert last is not None
    raise last
