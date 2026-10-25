"""What the managed-mode clients share: one authenticated gateway connection.

Managed mode points every perception capability at the conductor-grounder
gateway instead of a model server (see ``managed_grounder`` for why the client
carries no dialect or topology). ``GatewayClient`` is that connection: the
seat token, the timeouts, and the mapping from every failure mode to a
``GrounderError`` the agent can act on.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .grounder import CoordSpace, GrounderError

log = logging.getLogger(__name__)

# A 429 is retried this many times. Each wait follows the gateway's
# Retry-After when it sends one (capped, so one call can't stall the agent),
# else a short doubling backoff.
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_MAX_WAIT = 5.0
RATE_LIMIT_BACKOFF = 1.0


def retry_after_seconds(r: httpx.Response, attempt: int) -> float:
    """How long to wait before retrying a 429: Retry-After in seconds when the
    gateway sends a usable one, else 1 s, 2 s, ...; never more than the cap."""
    raw = r.headers.get("Retry-After", "").strip()
    try:
        wait = float(raw)
    except ValueError:  # absent, or an HTTP date we don't parse
        wait = RATE_LIMIT_BACKOFF * (2 ** attempt)
    return min(max(wait, 0.0), RATE_LIMIT_MAX_WAIT)


def gateway_bbox(b: object) -> tuple[int, int, int, int] | None:
    """Read a gateway ``Bbox`` into the (x1,y1,x2,y2) tuple the elements use.

    The gateway's Go ``fuzzy.Bbox`` has no JSON tags, so it marshals with
    capitalized keys (``X1``/``Y1``/``X2``/``Y2``). We accept the lower-case
    forms too, defensively, in case the wire shape ever gains tags.

    Returns None when a coordinate is missing or not a number. Defaulting it
    to 0 turned a malformed candidate into a confident click at the top-left
    corner of the screen.
    """
    if not isinstance(b, dict):
        return None
    out: list[int] = []
    for upper, lower in (("X1", "x1"), ("Y1", "y1"), ("X2", "x2"), ("Y2", "y2")):
        v = b.get(upper, b.get(lower))
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        out.append(int(v))
    return out[0], out[1], out[2], out[3]


class GatewayClient:
    """An authenticated connection to the conductor-grounder gateway.

    ``what`` names the caller in error messages ("managed grounder"), and
    ``timeout_env`` names the variable that raises its timeout.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float,
                 *, what: str, timeout_env: str,
                 http: httpx.AsyncClient | None = None) -> None:
        if http is None and not api_key:
            raise ValueError(
                f"{what} needs an API key — set CONDUCTOR_DESKTOP_API_KEY "
                f"to your cgr_live_… seat token."
            )
        self.base = base_url.rstrip("/")
        self.what = what
        self._timeout_env = timeout_env
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=timeout, headers={"Authorization": f"Bearer {api_key}"})
        self._sleep = asyncio.sleep  # swapped out in tests

    def borrow(self) -> GatewayClient:
        """A second view on this connection, for another capability. Closing
        the borrowed one leaves the pool to its owner."""
        view = GatewayClient(self.base, "", 0, what=self.what,
                             timeout_env=self._timeout_env, http=self._http)
        view._sleep = self._sleep
        return view

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def post(self, path: str, payload: dict, *,
                   allow: tuple[int, ...] = ()) -> httpx.Response:
        """POST to a gateway endpoint; every failure becomes a
        ``GrounderError`` naming the gateway and the likely fix, so callers
        that already catch ``RuntimeError`` keep working.

        Statuses in ``allow`` come back unraised: /v1/ground answers 404 for
        an honest no-match. A 429 is waited out and retried a couple of
        times before it becomes an error.
        """
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            r = await self._post_once(path, payload)
            if r.status_code != 429:
                break
            if attempt == RATE_LIMIT_RETRIES:
                raise GrounderError(
                    f"{self.what} returned 429 Too Many Requests — the gateway "
                    f"is rate-limiting this API key (retried "
                    f"{RATE_LIMIT_RETRIES} times). Send grounder calls (locate, "
                    f"crop, read_screen, enumerate_elements, click by "
                    f"description) one at a time, or wait a few seconds and "
                    f"retry."
                )
            wait = retry_after_seconds(r, attempt)
            log.info("%s rate-limited on %s; retrying in %.1fs", self.what, path, wait)
            await self._sleep(wait)
        return self._check(r, path, allow)

    async def _post_once(self, path: str, payload: dict) -> httpx.Response:
        try:
            return await self._http.post(f"{self.base}{path}", json=payload)
        except httpx.TimeoutException as e:
            raise GrounderError(
                f"{self.what} timed out after {self._http.timeout.read}s "
                f"talking to {self.base} ({type(e).__name__}). The gateway is "
                f"slow or unreachable — check it, or raise {self._timeout_env}."
            ) from e
        except httpx.HTTPError as e:
            raise GrounderError(
                f"could not reach the {self.what} at {self.base} "
                f"({type(e).__name__}: {e}). Check the gateway is up and "
                f"CONDUCTOR_DESKTOP_GROUNDER_URL points at it."
            ) from e

    def _check(self, r: httpx.Response, path: str,
               allow: tuple[int, ...]) -> httpx.Response:
        """Turn a non-2xx reply into a ``GrounderError`` naming the fix."""
        if r.status_code == 402:
            raise GrounderError(
                f"{self.what} returned 402 Payment Required — the credit "
                f"wallet is empty. Top up at the gateway's billing page, then "
                f"retry."
            )
        if r.status_code == 401:
            raise GrounderError(
                f"{self.what} returned 401 Unauthorized — the API key is "
                f"missing, wrong, or revoked. Check CONDUCTOR_DESKTOP_API_KEY."
            )
        if r.status_code in allow:
            return r
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise GrounderError(
                f"{self.what} at {self.base} returned HTTP "
                f"{e.response.status_code} on {path}. The gateway is up but "
                f"rejected the request — check its logs."
            ) from e
        return r

    def body(self, r: httpx.Response, path: str) -> dict:
        """The JSON object in a gateway reply, or a GrounderError saying why
        not. A proxy in front of the gateway can answer 200 with an HTML page;
        that must surface as the soft error, not a JSONDecodeError crash."""
        where = f"{self.what} at {self.base} {path}"
        try:
            data = r.json()
        except ValueError as e:
            raise GrounderError(
                f"{where} returned a non-JSON reply (HTTP {r.status_code}, "
                f"{r.headers.get('content-type', 'no content-type')}). Is something "
                f"other than the gateway answering at that URL?"
            ) from e
        if not isinstance(data, dict):
            raise GrounderError(f"{where} returned JSON {type(data).__name__}, "
                                f"expected an object")
        return data

    def coord_space(self, declared: object) -> CoordSpace:
        """The space the gateway says it answered in. Absent means its
        default, norm1000. An unknown space is an error: projecting it as
        either known space would put clicks in the wrong place."""
        if not declared:
            return CoordSpace.NORM1000
        try:
            return CoordSpace(str(declared))
        except ValueError as e:
            raise GrounderError(
                f"{self.what} served coord_system={declared!r}, which this "
                f"client can't project. Upgrade conductor-desktop, or ask the "
                f"gateway for {CoordSpace.NORM1000} coordinates."
            ) from e
