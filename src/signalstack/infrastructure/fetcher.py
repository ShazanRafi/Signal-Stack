import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from protego import Protego

from signalstack.infrastructure import config

ROBOTS_AGENT = "SignalStackBot"
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
DEFAULT_DELAY = 1.5  
ALLOWED_TYPES = {
    "text/html", "application/xhtml+xml", "text/plain",
    "application/xml", "text/xml", "application/rss+xml", "application/atom+xml",
    "application/json",
}


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int | None = None
    content_type: str = ""
    text: str = ""
    error: str = ""
    blocked_by_robots: bool = False
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and not self.error


class RobotsPolicy:
    def __init__(self, rules: Protego | None, allow_all: bool, delay: float | None = None):
        self._rules = rules
        self._allow_all = allow_all
        self.delay = delay

    @classmethod
    def from_response(cls, status: int | None, text: str) -> "RobotsPolicy":
        if status is None or status >= 500 or status in (401, 403) or 300 <= status < 400:
            return cls(None, allow_all=False)
        if 200 <= status < 300:
            rules = Protego.parse(text)
            return cls(rules, allow_all=False, delay=rules.crawl_delay(ROBOTS_AGENT))
        return cls(None, allow_all=True)  

    def allowed(self, url: str) -> bool:
        if self._rules is None:
            return self._allow_all
        return self._rules.can_fetch(url, ROBOTS_AGENT)


class PoliteFetcher:
    def __init__(self, client: httpx.Client | None = None, default_delay: float = DEFAULT_DELAY):
        self._client = client or httpx.Client(
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en",
            },
            timeout=httpx.Timeout(15.0),
            follow_redirects=False, 
        )
        self.default_delay = default_delay
        self._robots: dict[str, RobotsPolicy] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def _wait(self, host: str, delay: float) -> None:
        with self._lock:
            now = time.monotonic()
            ready_at = self._last_hit.get(host, 0.0) + delay
            wait = max(0.0, ready_at - now)
            self._last_hit[host] = now + wait
        if wait:
            time.sleep(wait)

    def _policy_for(self, parsed) -> RobotsPolicy:
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            policy = self._robots.get(origin)
        if policy is None:
            policy = self._load_robots(origin)
            with self._lock:
                self._robots[origin] = policy
        return policy

    def _load_robots(self, origin: str) -> RobotsPolicy:
        self._wait(urlparse(origin).netloc, self.default_delay)
        try:
            resp = self._client.get(origin + "/robots.txt", follow_redirects=True)
            return RobotsPolicy.from_response(resp.status_code, resp.text[:500_000])
        except httpx.HTTPError:
            return RobotsPolicy.from_response(None, "")

    def fetch(self, url: str) -> FetchResult:
        result = FetchResult(url=url)
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            result.final_url = current
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                result.error = f"unsupported URL: {current}"
                return result

            policy = self._policy_for(parsed)
            if not policy.allowed(current):
                result.blocked_by_robots = True
                result.error = "blocked by robots.txt"
                return result

            self._wait(parsed.netloc, policy.delay or self.default_delay)
            try:
                with self._client.stream("GET", current) as resp:
                    result.status = resp.status_code
                    if resp.is_redirect and "location" in resp.headers:
                        current = urljoin(current, resp.headers["location"])
                        continue

                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    result.content_type = ctype
                    if ctype and ctype not in ALLOWED_TYPES:
                        result.error = f"unsupported content-type: {ctype}"
                        return result

                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > MAX_BYTES:
                            break
                    body = b"".join(chunks)[:MAX_BYTES]
                    result.text = body.decode(resp.encoding or "utf-8", errors="replace")
                    if resp.status_code >= 400:
                        result.error = f"HTTP {resp.status_code}"
                    return result
            except httpx.HTTPError as e:
                result.error = f"{type(e).__name__}: {e}"
                return result

        result.error = "too many redirects"
        return result