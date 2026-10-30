"""On-demand "Check quality": what resolution and size is this source really?

A person presses the button on one movie / episode source; we fetch just the first few MB with a Range request,
run ffprobe on that scrap and keep the answer on the source row. Nothing is downloaded in full.

Providers are protected on every side, because a probe is a real connection to someone else's server:
  * one probe at a time per provider, and a small global cap
  * a probe only starts when the same connection gate downloads and playback use has a free slot
    (never while a download or a viewer holds the provider's connection)
  * a pause between probes on one provider, plus a ceiling per provider per few minutes
  * a provider that pushes back (403/429/458/509, "max connections") puts the whole provider on the downloader's
    cooldown, so downloads and further probes both back off
  * a hard time limit, and a byte cap whatever the server sends
  * a result already on file is returned without touching the provider
There is deliberately no season or series-wide probe: each episode is its own click, and the limits above apply
to every one of them.

Language auditing reuses the same small, paced request path but is kept
separate from the quality result. It reads embedded audio-track tags only; it
never changes the provider language or playback eligibility automatically.
"""

import asyncio
import collections
import json
import logging
import os
import random
import shutil
import tempfile
import time

import httpx

import vod_db
import vod_downloader
import xc_server

logger = logging.getLogger(__name__)

PROBE_BYTES = 6 * 1024 * 1024
TAIL_BYTES = 16 * 1024 * 1024        # most we will read from the end of a file to find its index
PROBE_TIMEOUT = 25.0
_SPACING_SECONDS = 8.0                # quiet time after a probe before the next one on the same provider
_WINDOW_SECONDS = 600.0
_WINDOW_MAX = 8                       # probes per provider per window
_GLOBAL_MAX = 2

# Language trickle defaults: deliberately quieter than normal import traffic.
LANGUAGE_TRICKLE_INTERVAL = 60.0
LANGUAGE_TRICKLE_DAILY_MAX = 500
LANGUAGE_GLOBAL_MAX = 15
LANGUAGE_WINDOW_SECONDS = 900.0
LANGUAGE_WINDOW_MAX: int | None = None
LANGUAGE_CAPACITY_HOLD_SECONDS = 2 * 60 * 60

_in_flight: set[int] = set()          # provider ids with a probe running right now
_running_total = 0
_last_end: dict[int, float] = {}
_recent: dict[int, collections.deque] = {}
_language_trickle_day = ""
_language_trickle_count = 0
_language_provider_blocks: dict[int, str] = {}
_language_claimed_sources: set[int] = set()


class ProbeRefused(Exception):
    """The probe was not started (or didn't finish); the message is plain and safe to show."""

    def __init__(self, message: str, retry_after: int = 0, kind: str = "other"):
        super().__init__(message)
        self.retry_after = retry_after
        # inflight / cooldown / busy / spacing / window / capacity: just wait.  pushback: the provider said slow down.
        self.kind = kind


def quality_label(width: int | None, height: int | None) -> str | None:
    """'4K', '1080p', '720p'... from the real frame size (width counts too: a wide 1080p picture can be 800 rows tall)."""
    if not height:
        return None
    w = width or 0
    if w >= 3200 or height >= 2000:
        return "4K"
    if w >= 1800 or height >= 1000:
        return "1080p"
    if w >= 1200 or height >= 700:
        return "720p"
    if w >= 700 or height >= 460:
        return "480p"
    return f"{height}p"


def result_of(src: dict) -> dict:
    return {
        "source_id": src.get("source_id"),
        "checked": bool(src.get("probed_at")),
        "file_size_bytes": src.get("file_size_bytes"),
        "width": src.get("probe_width"), "height": src.get("probe_height"),
        "resolution": quality_label(src.get("probe_width"), src.get("probe_height")),
        "codec": src.get("probe_codec"), "probed_at": src.get("probed_at"),
    }


def language_result_of(src: dict) -> dict:
    try:
        languages = json.loads(src.get("language_probe_languages") or "[]")
    except (TypeError, ValueError):
        languages = []
    return {
        "source_id": src.get("source_id"),
        "checked": bool(src.get("language_probed_at")),
        "configured_language": src.get("language"),
        "observed_languages": languages,
        "status": src.get("language_probe_status"),
        "confidence": src.get("language_probe_confidence"),
        "method": src.get("language_probe_method"),
        "error": src.get("language_probe_error"),
        "probed_at": src.get("language_probed_at"),
    }


def _gate(pid: int, name: str, spacing: float | None = None,
          window_seconds: float = _WINDOW_SECONDS, window_max: int | None = _WINDOW_MAX) -> None:
    """Raises ProbeRefused unless this provider can take a probe right now."""
    now = time.time()
    if pid in _in_flight:
        raise ProbeRefused(f"{name} is already being checked. Wait for that one to finish.", 10, "inflight")
    cool = vod_downloader._cooldown_until.get(pid, 0)
    if cool > now:
        raise ProbeRefused(f"{name} asked us to slow down. Try again in about {int(cool - now) + 1}s.", int(cool - now) + 1, "cooldown")
    if vod_downloader._provider_active.get(pid, 0) > 0:
        raise ProbeRefused(f"{name} is busy with a download. Try again when it finishes.", 60, "busy")
    gap = (_SPACING_SECONDS if spacing is None else spacing) - (time.monotonic() - _last_end.get(pid, -1e9))
    if gap > 0:
        raise ProbeRefused(f"Easing off {name} between checks. Try again in {int(gap) + 1}s.", int(gap) + 1, "spacing")
    recent = _recent.setdefault(pid, collections.deque())
    while recent and time.monotonic() - recent[0] > window_seconds:
        recent.popleft()
    if window_max is not None and window_max > 0 and len(recent) >= window_max:
        wait = int(window_seconds - (time.monotonic() - recent[0])) + 1
        raise ProbeRefused(f"That's a lot of checks on {name} in a short time. Try again in {wait // 60 + 1} min.", wait, "window")


def _index_offset(head: bytes, total: int) -> int | None:
    """Where the index (moov) of an MP4 starts when it sits right after the media data: walk the top-level atoms
    in the first bytes. None when the layout isn't that simple (the caller then reads the end of the file)."""
    pos = 0
    while pos + 16 <= len(head):
        size, kind = int.from_bytes(head[pos:pos + 4], "big"), head[pos + 4:pos + 8]
        if size == 1:
            size = int.from_bytes(head[pos + 8:pos + 16], "big")
        if size < 8:
            return None
        if kind == b"mdat":
            end = pos + size
            return end if 0 < end < total else None
        pos += size
    return None


async def _ffprobe_video(path: str) -> tuple[int | None, int | None, str | None]:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name,width,height:stream_disposition=attached_pic",
        "-of", "json", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, None, None
    try:
        streams = json.loads(out.decode(errors="replace")).get("streams") or []
    except ValueError:
        return None, None, None
    for st in streams:
        if (st.get("disposition") or {}).get("attached_pic"):
            continue
        return (int(st["width"]) if st.get("width") else None, int(st["height"]) if st.get("height") else None,
                st.get("codec_name"))
    return None, None, None


_LANGUAGE_CODES = {
    "eng": "EN", "hin": "HI", "fra": "FR", "spa": "ES", "deu": "DE",
    "ger": "DE", "ita": "IT", "por": "PT", "jpn": "JA", "zho": "ZH",
    "chi": "ZH", "kor": "KO", "ara": "AR", "rus": "RU", "tam": "TA",
    "tel": "TE", "mal": "ML", "tha": "TH", "dan": "DA", "nld": "NL",
    "dut": "NL", "tur": "TR", "pol": "PL", "swe": "SV", "nor": "NO",
    "fin": "FI", "und": "UNKNOWN",
}


async def _ffprobe_audio_languages(path: str) -> list[str]:
    """Return embedded audio-track language tags; empty means not tagged/unknown."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream_tags=language:stream_disposition=attached_pic",
        "-of", "json", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return []
    try:
        streams = json.loads(out.decode(errors="replace")).get("streams") or []
    except ValueError:
        return []
    found = []
    for stream in streams:
        raw = ((stream.get("tags") or {}).get("language") or "").strip().lower()
        if not raw:
            continue
        found.append(_LANGUAGE_CODES.get(raw, raw[:3].upper()))
    return sorted(set(found))


def _back_off(pid: int) -> int:
    """Same escalating cooldown the downloader applies when a provider says slow down."""
    n = vod_downloader._pushbacks.get(pid, 0) + 1
    vod_downloader._pushbacks[pid] = n
    wait = min(vod_downloader._PUSHBACK_MAX_SECONDS, vod_downloader._PUSHBACK_BASE_SECONDS * 2 ** (n - 1))
    vod_downloader._cooldown_until[pid] = time.time() + wait
    return wait


async def _fetch_head(url: str, user_agent: str | None, provider_name: str, pid: int,
                      tail: tuple[int, int] | None = None) -> tuple[bytes, int | None]:
    """First PROBE_BYTES of the file and its total size (None if the server won't say).
    With tail=(start, length), that later slice instead (b"" if the server won't honour a range)."""
    headers = {"User-Agent": user_agent} if user_agent else dict(xc_server._UPSTREAM_HEADERS)
    cap = tail[1] if tail else PROBE_BYTES
    headers["Range"] = f"bytes={tail[0]}-{tail[0] + cap - 1}" if tail else f"bytes=0-{cap - 1}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=15.0), follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code in vod_downloader._PUSHBACK_STATUSES:
                wait = _back_off(pid)
                raise ProbeRefused(f"{provider_name} pushed back (HTTP {resp.status_code}). Backing off for a while.", wait, "pushback")
            if resp.status_code >= 400:
                raise ProbeRefused(f"{provider_name} answered HTTP {resp.status_code} for this source.", 900, "provider_error")
            ctype = (resp.headers.get("content-type") or "").lower()
            total = None
            cr = resp.headers.get("content-range", "")
            if resp.status_code == 206 and "/" in cr and cr.rsplit("/", 1)[-1].isdigit():
                total = int(cr.rsplit("/", 1)[-1])
            elif resp.status_code == 200 and resp.headers.get("content-length", "").isdigit():
                total = int(resp.headers["content-length"])
            if tail and resp.status_code != 206:
                return b"", total
            buf = bytearray()
            async for chunk in resp.aiter_bytes(64 * 1024):
                buf += chunk
                if len(buf) >= cap:
                    break
            data = bytes(buf[:cap])
            if (ctype.startswith("text/") or "json" in ctype) and not vod_downloader._looks_like_video(data):
                snippet = vod_downloader._safe(data[:300].decode("utf-8", "replace")).lower()
                if any(w in snippet for w in vod_downloader._CAPACITY_WORDS):
                    vod_downloader._cooldown_until[pid] = time.time() + 120
                    raise ProbeRefused(f"{provider_name} is at its connection limit. It will be checked again after the capacity hold.", LANGUAGE_CAPACITY_HOLD_SECONDS, "capacity")
                raise ProbeRefused(f"{provider_name} returned a web page instead of a video for this source.", 900, "provider_error")
            return data, total


async def _probe_remote(kind: str, src: dict, provider: dict, spacing: float | None = None) -> tuple[int | None, int | None, int | None, str | None]:
    """(size, width, height, codec) from a provider's stream, within every limit in the module docstring."""
    global _running_total
    pid = provider["id"]
    if _running_total >= _GLOBAL_MAX:
        raise ProbeRefused("Other quality checks are running. Try again in a moment.", 10, "busy")
    _gate(pid, provider["name"], spacing)
    _in_flight.add(pid)
    _running_total += 1
    _recent.setdefault(pid, collections.deque()).append(time.monotonic())
    reservation = None
    tmp = None
    try:
        if not vod_downloader._is_remote_library(provider):
            reservation = await xc_server._select_upstream_credentials(provider, is_download=True)
            if reservation is None:
                playing = xc_server._active_vod_streams.get(pid, 0) > 0
                raise ProbeRefused(f"{provider['name']} is {'in use by playback' if playing else 'at its connection limit'}. "
                                   "Try again in a minute.", 60, "capacity")
        url = await xc_server._build_upstream_url("movie" if kind == "movie" else "series", provider, src, reservation)
        ua = provider.get("custom_user_agent")
        data, total = await asyncio.wait_for(_fetch_head(url, ua, provider["name"], pid), PROBE_TIMEOUT)
        fd, tmp = tempfile.mkstemp(suffix=".bin", prefix="vodprobe-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        width, height, codec = await _ffprobe_video(tmp)
        if height is None and total and total > PROBE_BYTES * 2:
            # Many MP4s keep their index (moov) at the END of the file, so the start says nothing. Fetch the last
            # few MB too and lay both into a sparse file of the real length: ffprobe seeks straight to the index.
            # Same reserved connection, one more small request, still inside the same time limit.
            begin = _index_offset(data, total)
            if begin is None:
                begin = total - TAIL_BYTES
            length = min(total - begin, TAIL_BYTES)
            tail, _ = await asyncio.wait_for(
                _fetch_head(url, ua, provider["name"], pid, tail=(begin, length)), PROBE_TIMEOUT)
            if tail:
                with open(tmp, "r+b") as fh:
                    fh.truncate(total)
                    fh.seek(begin)
                    fh.write(tail)
                width, height, codec = await _ffprobe_video(tmp)
        vod_downloader._pushbacks.pop(pid, None)
        return total, width, height, codec
    except asyncio.TimeoutError:
        raise ProbeRefused(f"{provider['name']} took too long to answer. Try again later.", 60)
    except (httpx.HTTPError, OSError) as exc:
        raise ProbeRefused(f"Couldn't reach {provider['name']}: {vod_downloader._safe(exc) or type(exc).__name__}.", 30)
    finally:
        _in_flight.discard(pid)
        _running_total -= 1
        _last_end[pid] = time.monotonic()
        if reservation is not None:
            xc_server._release_capacity(provider, reservation["sub_account_id"], is_download=True)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


async def probe_source(kind: str, source_id: int, force: bool = False, spacing: float | None = None) -> dict:
    """Checks one source and stores the result. Raises ProbeRefused when it can't or shouldn't run right now."""
    if kind not in ("movie", "episode"):
        raise ProbeRefused("Unknown kind.")
    src = await asyncio.to_thread(vod_db.get_probe_source, kind, source_id)
    if not src:
        raise ProbeRefused("That source doesn't exist or its provider is turned off.")
    if src.get("probed_at") and not force:
        return result_of(src)
    if not shutil.which("ffprobe"):
        raise ProbeRefused("ffprobe isn't available on this server, so quality can't be checked.")
    provider = await asyncio.to_thread(vod_db.get_provider, src["provider_id"])
    if not provider:
        raise ProbeRefused("The provider no longer exists.")

    if provider.get("provider_type") in ("dispatcharr_dvr", "library") and not vod_downloader._is_remote_library(provider):
        path = src.get("local_file_path") or ""
        if not os.path.isfile(path):
            raise ProbeRefused("The source file isn't there any more.")
        width, height, codec = await _ffprobe_video(path)
        size = os.path.getsize(path)
    else:
        size, width, height, codec = await _probe_remote(kind, src, provider, spacing)

    await asyncio.to_thread(vod_db.set_source_probe, kind, source_id, size, width, height, codec)
    out = result_of(await asyncio.to_thread(vod_db.get_probe_source, kind, source_id))
    out["unreadable"] = height is None      # we reached the provider but found no video to measure
    return out


async def _probe_remote_language(kind: str, src: dict, provider: dict, spacing: float | None = None) -> list[str]:
    """Read only the bounded stream header and inspect embedded audio tags."""
    global _running_total
    pid = provider["id"]
    current_day = time.strftime("%Y-%m-%d", time.localtime())
    blocked = _language_provider_blocks.get(pid)
    if blocked and blocked.startswith(current_day + ":"):
        raise ProbeRefused(f"{provider['name']} is paused for this language run: {blocked.split(':', 1)[1]}", 900, "provider_blocked")
    if blocked:
        _language_provider_blocks.pop(pid, None)
    if _running_total >= LANGUAGE_GLOBAL_MAX:
        raise ProbeRefused("Other checks are running. Try again in a moment.", 10, "busy")
    _gate(pid, provider["name"], spacing, LANGUAGE_WINDOW_SECONDS, LANGUAGE_WINDOW_MAX)
    _in_flight.add(pid)
    _running_total += 1
    _recent.setdefault(pid, collections.deque()).append(time.monotonic())
    reservation = None
    tmp = None
    try:
        if not vod_downloader._is_remote_library(provider):
            reservation = await xc_server._select_upstream_credentials(provider, is_download=True)
            if reservation is None:
                raise ProbeRefused(f"{provider['name']} has no free probe connection.", 60, "capacity")
        url = await xc_server._build_upstream_url("movie" if kind == "movie" else "series", provider, src, reservation)
        try:
            data, _ = await asyncio.wait_for(
                _fetch_head(url, provider.get("custom_user_agent"), provider["name"], pid), PROBE_TIMEOUT)
        except ProbeRefused as exc:
            if reservation is not None and exc.kind != "capacity":
                await asyncio.to_thread(vod_db.clear_language_probe_capacity_hold, pid)
            raise
        except (asyncio.TimeoutError, httpx.HTTPError, OSError):
            if reservation is not None:
                await asyncio.to_thread(vod_db.clear_language_probe_capacity_hold, pid)
            raise
        # The provider accepted a media response, so capacity is available.
        if reservation is not None:
            await asyncio.to_thread(vod_db.clear_language_probe_capacity_hold, pid)
        fd, tmp = tempfile.mkstemp(suffix=".bin", prefix="vodlangprobe-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return await _ffprobe_audio_languages(tmp)
    except ProbeRefused as exc:
        if exc.kind in ("pushback", "capacity", "provider_error", "other"):
            _language_provider_blocks[pid] = f"{current_day}:{exc}"
        raise
    except asyncio.TimeoutError:
        _language_provider_blocks[pid] = f"{current_day}:timeout"
        raise ProbeRefused(f"{provider['name']} took too long to answer. Try again later.", 60)
    except (httpx.HTTPError, OSError) as exc:
        _language_provider_blocks[pid] = f"{current_day}:connection error"
        raise ProbeRefused(f"Couldn't reach {provider['name']}: {vod_downloader._safe(exc) or type(exc).__name__}.", 30)
    finally:
        _in_flight.discard(pid)
        _running_total -= 1
        _last_end[pid] = time.monotonic()
        if reservation is not None:
            xc_server._release_capacity(provider, reservation["sub_account_id"], is_download=True)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


async def probe_language_source(kind: str, source_id: int, force: bool = False,
                                spacing: float | None = None) -> dict:
    """Advisory embedded-audio-language check for one movie or episode source."""
    if kind not in ("movie", "episode"):
        raise ProbeRefused("Unknown kind.")
    src = await asyncio.to_thread(vod_db.get_language_probe_source, kind, source_id)
    if not src:
        raise ProbeRefused("That source doesn't exist or its provider is turned off.")
    if src.get("language_probed_at") and not force:
        return language_result_of(src)
    if not shutil.which("ffprobe"):
        raise ProbeRefused("ffprobe isn't available on this server, so language can't be checked.")
    provider = await asyncio.to_thread(vod_db.get_provider, src["provider_id"])
    if not provider:
        raise ProbeRefused("The provider no longer exists.")
    method = "embedded_audio_tags"
    if provider.get("provider_type") in ("dispatcharr_dvr", "library") and os.path.isfile(src.get("local_file_path") or ""):
        languages = await _ffprobe_audio_languages(src["local_file_path"])
        method = "local_embedded_audio_tags"
    else:
        languages = await _probe_remote_language(kind, src, provider, spacing)
    status = "observed" if languages else "unknown"
    confidence = "high" if languages else "none"
    await asyncio.to_thread(vod_db.set_source_language_probe, kind, source_id, languages, status, method, confidence)
    out = language_result_of(await asyncio.to_thread(vod_db.get_language_probe_source, kind, source_id))
    out["mismatch"] = bool(languages and src.get("language") and src["language"] not in languages)
    return out


async def language_trickle_once(target: dict | None = None) -> dict:
    """Run at most one quiet audit probe; no automatic source/card changes."""
    global _language_trickle_day, _language_trickle_count
    day = time.strftime("%Y-%m-%d", time.localtime())
    if day != _language_trickle_day:
        _language_trickle_day, _language_trickle_count = day, 0
    if _language_trickle_count >= LANGUAGE_TRICKLE_DAILY_MAX:
        return {"status": "daily_limit", "checked_today": _language_trickle_count}
    if target is None:
        target = await asyncio.to_thread(vod_db.next_language_probe_target)
    if not target:
        return {"status": "idle", "checked_today": _language_trickle_count}
    try:
        result = await probe_language_source(target["kind"], target["source_id"], spacing=0)
        await asyncio.to_thread(vod_db.clear_language_probe_capacity_hold, target["provider_id"])
        await asyncio.to_thread(
            vod_db.recover_language_probe_provider,
            target["provider_id"], settings["language_trickle_probes_per_cycle"],
        )
        _language_trickle_count += 1
        if result.get("mismatch"):
            import notices
            if not await asyncio.to_thread(vod_db.has_open_notice_kind, "language_mismatch"):
                await asyncio.to_thread(
                    notices.add, "language_mismatch", "warn", "Language mismatch approval needed",
                    "A trickle probe found one or more source language mismatches. Open Language Mismatch Report to review."
                )
        return {"status": "checked", "checked_today": _language_trickle_count, "result": result}
    except ProbeRefused as exc:
        failure = None
        capacity_hold = None
        if exc.kind == "capacity":
            capacity_hold = await asyncio.to_thread(
                vod_db.hold_language_probe_provider,
                target["provider_id"], target["kind"], target["source_id"],
                str(exc), LANGUAGE_CAPACITY_HOLD_SECONDS,
            )
        elif _is_provider_throttle(exc):
            await asyncio.to_thread(
                vod_db.record_language_probe_throttle,
                target["provider_id"],
                re.search(r"HTTP (\d{3})", str(exc)).group(1) if re.search(r"HTTP (\d{3})", str(exc)) else "PUSHBACK",
                str(exc), settings["language_trickle_probes_per_cycle"],
            )
        elif exc.kind == "provider_error":
            failure = await asyncio.to_thread(
                vod_db.record_language_probe_failure,
                target["kind"], target["source_id"], str(exc),
            )
        await asyncio.to_thread(vod_db.record_language_probe_run,
                                target["kind"], target["source_id"], target.get("provider_id"),
                                "deferred", None, [], config.get_enabled_languages(), False, False, str(exc))
        return {"status": "quarantined" if failure and failure["quarantined"] else "capacity_hold" if capacity_hold else "deferred",
                "checked_today": current_count,
                "retry_after": LANGUAGE_CAPACITY_HOLD_SECONDS if capacity_hold else exc.retry_after,
                "next_retry_at": capacity_hold["next_retry_at"] if capacity_hold else None,
                "message": str(exc), "failures": failure["failures"] if failure else 0}
    except Exception as exc:
        logger.exception("[language-probe] trickle check failed for %s", target)
        return {"status": "error", "checked_today": _language_trickle_count, "message": str(exc)}


def language_trickle_status() -> dict:
    day = time.strftime("%Y-%m-%d", time.localtime())
    return {
        "interval_seconds": LANGUAGE_TRICKLE_INTERVAL,
        "daily_max": LANGUAGE_TRICKLE_DAILY_MAX,
        "checked_today": _language_trickle_count if _language_trickle_day == day else 0,
        "global_concurrency": LANGUAGE_GLOBAL_MAX,
        "language_window_max": LANGUAGE_WINDOW_MAX,
        "language_window_seconds": LANGUAGE_WINDOW_SECONDS,
        "blocked_providers": len(_language_provider_blocks),
    }


async def run_language_trickle_cycle() -> dict:
    """Run one bounded manual/scheduled cycle without bypassing any gates."""
    targets = await asyncio.to_thread(vod_db.next_language_probe_targets, LANGUAGE_GLOBAL_MAX, False)
    provider_queues: dict[int, list[dict]] = {}
    for target in targets:
        provider_queues.setdefault(target["provider_id"], []).append(target)
    claimed = []
    for target in targets:
        if target["source_id"] in _language_claimed_sources:
            continue
        _language_claimed_sources.add(target["source_id"])
        claimed.append(target["source_id"])

    async def run_provider_queue(queue: list[dict]) -> list[dict]:
        results = []
        for target in queue:
            result = await language_trickle_once(target)
            results.append(result)
            if result.get("status") == "capacity_hold":
                # Capacity is provider-scoped. Stop trying sibling sources
                # until the provider's two-hour retry is due.
                break
        return results

    try:
        queues = [
            [target for target in queue if target["source_id"] in claimed]
            for queue in provider_queues.values()
        ]
        batches = await asyncio.gather(
            *(run_provider_queue(queue) for queue in queues if queue),
            return_exceptions=True,
        ) if queues else []
        results = [result for batch in batches for result in (batch if isinstance(batch, list) else [batch])]
        return {"targets": len(targets), "results": [
            {"status": r.get("status"), "source_id": r.get("result", {}).get("source_id"), "message": r.get("message")}
            if isinstance(r, dict) else {"status": "error", "message": str(r)} for r in results
        ], "trickle": language_trickle_status()}
    finally:
        for source_id in claimed:
            _language_claimed_sources.discard(source_id)


async def language_trickle_loop() -> None:
    """Quietly audit export-relevant sources, one bounded probe per interval."""
    await asyncio.sleep(90)
    while True:
        try:
            await run_language_trickle_cycle()
        except Exception:
            logger.exception("[language-probe] trickle loop failed")
        await asyncio.sleep(LANGUAGE_TRICKLE_INTERVAL)


# ── background season / series checks ───────────────────────────────────────
#
# One episode at a time, long and uneven gaps, and the first sign a provider is unhappy stops the whole job until
# a person resumes it. Every probe in it still goes through probe_source, so every limit above applies to each.

_BATCH_SPACING = (30.0, 60.0)         # seconds of quiet on a provider between two episodes of a batch
_BATCH_MAX_EPISODES = 30              # per click: a longer series is done in chunks
_BATCH_MAX_MISSES = 3                 # this many episodes in a row that error out pauses the job
_BATCH_WAIT_CAP = 300                 # longest single wait for a busy provider, seconds
_BATCH_MAX_WAITS = 80                 # waits for a busy provider before the job pauses itself
_batches: dict[int, dict] = {}
_batch_tasks: dict[int, asyncio.Task] = {}
_batch_seq = 0


def _batch_view(b: dict) -> dict:
    return {k: b[k] for k in ("id", "series_id", "season", "state", "message", "total", "done", "failed", "more_not_queued")}


def list_batches(series_id: int | None = None) -> list[dict]:
    return [_batch_view(b) for b in _batches.values() if series_id is None or b["series_id"] == series_id]


async def start_batch(series_id: int, season: int | None = None) -> dict:
    """Queues the unchecked episodes of a series (or one season) as a background job. Raises ProbeRefused."""
    global _batch_seq
    if any(b["series_id"] == series_id and b["state"] in ("running", "paused") for b in _batches.values()):
        raise ProbeRefused("This series is already being checked.")
    targets = await asyncio.to_thread(vod_db.list_series_probe_targets, series_id, season)
    todo = [t for t in targets if not t.get("probed_at")]
    if not todo:
        raise ProbeRefused("Every episode here has already been checked." if targets else "No episodes with a source to check.")
    if not shutil.which("ffprobe"):
        raise ProbeRefused("ffprobe isn't available on this server, so quality can't be checked.")
    _batch_seq += 1
    b = {"id": _batch_seq, "series_id": series_id, "season": season, "state": "running", "message": "Starting",
         "total": min(len(todo), _BATCH_MAX_EPISODES), "done": 0, "failed": 0,
         "more_not_queued": max(0, len(todo) - _BATCH_MAX_EPISODES),
         "items": [t["source_id"] for t in todo[:_BATCH_MAX_EPISODES]]}
    _batches[b["id"]] = b
    finished = [i for i, x in _batches.items() if x["state"] in ("done", "cancelled") and i != b["id"]]
    for old in finished[:-5]:
        _batches.pop(old, None)
    _batch_tasks[b["id"]] = asyncio.create_task(_run_batch(b))
    return _batch_view(b)


def cancel_batch(batch_id: int) -> bool:
    b = _batches.get(batch_id)
    if not b or b["state"] in ("done", "cancelled"):
        return False
    b["state"], b["message"] = "cancelled", "Cancelled"
    return True


def resume_batch(batch_id: int) -> bool:
    b = _batches.get(batch_id)
    if not b or b["state"] != "paused":
        return False
    b["state"], b["message"] = "running", "Resumed"
    return True


async def _wait(b: dict, seconds: float) -> None:
    """Sleeps in small steps so cancel and pause take effect quickly."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and b["state"] == "running":
        await asyncio.sleep(min(0.5, max(0.0, end - time.monotonic())))


async def _run_batch(b: dict) -> None:
    misses = 0
    try:
        for sid in b["items"]:
            spacing = random.uniform(*_BATCH_SPACING)
            waits = 0
            while True:
                if b["state"] == "cancelled":
                    return
                if b["state"] == "paused":
                    await asyncio.sleep(0.5)
                    continue
                try:
                    await probe_source("episode", sid, spacing=spacing)
                    b["done"] += 1
                    misses = 0
                    b["message"] = f"Checked {b['done']} of {b['total']}"
                    break
                except ProbeRefused as exc:
                    if exc.kind == "pushback":
                        b["state"] = "paused"
                        b["message"] = f"Paused: {exc}"
                    elif exc.kind in ("inflight", "cooldown", "busy", "spacing", "window", "capacity"):
                        waits += 1
                        if waits > _BATCH_MAX_WAITS:
                            b["state"], b["message"] = "paused", "Paused: the provider stayed busy. Resume to keep going."
                        else:
                            b["message"] = f"Waiting: {exc}"
                            await _wait(b, min(max(exc.retry_after, 1), _BATCH_WAIT_CAP))
                    else:
                        b["failed"] += 1
                        misses += 1
                        b["message"] = str(exc)
                        if misses >= _BATCH_MAX_MISSES:
                            b["state"], b["message"] = "paused", f"Paused after {misses} failures in a row. Last: {exc}"
                        break
        if b["state"] != "cancelled":
            b["state"] = "done"
            b["message"] = f"Done: {b['done']} checked" + (f", {b['failed']} couldn't be read" if b["failed"] else "")
    except Exception:
        logger.exception("[probe] batch %s crashed", b["id"])
        b["state"], b["message"] = "paused", "Paused after an unexpected error. See the logs."
    finally:
        _batch_tasks.pop(b["id"], None)
