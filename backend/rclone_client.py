"""rclone-mediated remote library sources -- SMB, SFTP, S3-compatible storage,
Google Drive, Dropbox and Box, all through one mechanism: rclone itself
already speaks every one of these protocols identically from this app's point
of view, so there's exactly one code path here, parameterised by backend type.

Two rclone operations cover everything:
  - `rclone lsjson --recursive` for listing (library_importer's remote-scan
    branch; same [(rel_path, size)] shape _scan_root already produces for a
    local/mounted source, so the whole parser/matcher/bulk-import pipeline
    is untouched).
  - `rclone serve http` as a per-provider background daemon, bound to
    127.0.0.1 only (never exposed outside the container), that xc_server's
    playback proxy streams from exactly like it already streams from an
    xc/plex/emby upstream -- Range requests pass straight through to
    whatever the real backend supports (confirmed live against a real SMB
    share: a 3-byte range request came back as a real 206 Partial Content).

No FUSE, no kernel mount, no `--privileged`/CAP_SYS_ADMIN -- both operations
are plain userspace processes.

Credentials never touch argv or an on-disk file under DATA_DIR (which the
app's own Backup & Restore feature can sweep into a downloadable zip): each
rclone invocation gets a fresh, private rclone.conf written under
/tmp/vod_manager_rclone/provider_<id>/, mode 0600, created immediately
before the process starts and removed as soon as it's done (a lsjson call)
or when the daemon it belongs to stops (serve http).

NFS has no rclone backend at all (confirmed against rclone v1.75's own
`config providers` output) -- an NFS library source is, and stays, a
library_backend='local' mount (a Docker named volume with the nfs driver, or
a host bind-mount), same as Phase 1. MediaFire isn't an rclone backend
either -- dropped from scope; a real integration would need a bespoke API
client this project has no account to test against.
"""

import asyncio
import logging
import os
import shutil
import stat
import time

import config

logger = logging.getLogger(__name__)

RCLONE_BACKENDS = frozenset({"smb", "sftp", "s3", "gdrive", "dropbox", "box"})
_RCLONE_TYPE = {"gdrive": "drive"}  # our short name -> rclone's own backend name

_RCLONE_DIR = "/tmp/vod_manager_rclone"
_LSJSON_TIMEOUT_SECONDS = 60
# Same LAN-vs-real-internet reasoning as _DAEMON_START_TIMEOUT_SECONDS above --
# a cold OAuth token exchange against a real cloud API can legitimately take
# longer than a LAN share's first connection.
_PROBE_TIMEOUT_SECONDS = {"smb": 15, "sftp": 15, "s3": 20, "gdrive": 30, "dropbox": 30, "box": 30}
_PROBE_TIMEOUT_DEFAULT = 20
# LAN backends (smb/sftp) connect in well under a second in practice (confirmed
# live against real Samba/SFTP servers); real cloud backends (s3/gdrive/dropbox/
# box) go over the actual internet and their OAuth token exchange / API
# round-trip alone can take several seconds -- found live 2026-09-23 testing
# against a real Google Drive account: a cold daemon start legitimately took
# 7-10s+ even on a successful attempt, so one shared tight budget either makes
# LAN backends wait needlessly long to fail, or makes cloud backends fail
# spuriously on nothing worse than normal network variance.
_DAEMON_START_TIMEOUT_SECONDS = {"smb": 6, "sftp": 6, "s3": 10, "gdrive": 20, "dropbox": 20, "box": 20}
_DAEMON_START_TIMEOUT_DEFAULT = 10
_DAEMON_BASE_PORT = 18300  # provider_id N listens on _DAEMON_BASE_PORT + N -- deterministic, no port registry needed


class RcloneError(Exception):
    pass


def is_remote_backend(provider: dict) -> bool:
    return provider.get("provider_type") == "library" and provider.get("library_backend") in RCLONE_BACKENDS


def _rclone_available() -> bool:
    return shutil.which("rclone") is not None


def _provider_dir(provider_id: int) -> str:
    d = os.path.join(_RCLONE_DIR, f"provider_{provider_id}")
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _obscure(password: str) -> str:
    """rclone config files store passwords "obscured" (a documented, openly
    reversible XOR+AES scramble against a fixed, published key -- NOT real
    encryption, just enough to stop a config file being read at a glance).
    Real secrecy comes from this file's 0600 perms and short lifetime, not
    from obscuring -- same reasoning rclone's own docs give."""
    out = subprocess_run_sync(["rclone", "obscure", password])
    return out.strip()


def subprocess_run_sync(argv: list[str]) -> str:
    """Only ever used for `rclone obscure` (fast, no I/O, no secrets in its
    OWN argv beyond the one password being obscured for a value that's about
    to be written to a private 0600 file anyway) -- every real rclone
    operation against a remote goes through the async paths below instead,
    which are on the hot path and must not block the event loop."""
    import subprocess
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RcloneError(f"{' '.join(argv[:1])} failed: {result.stderr.strip()}")
    return result.stdout


def _remote_section(backend: str, provider: dict, remote_config: dict) -> list[str]:
    """[key = value, ...] lines for the [remote] section of a private
    rclone.conf, built from providers.username/password (reused for the
    user/pass backends, same as every other provider type already reuses
    those two columns) plus library_remote_config's backend-specific fields.
    """
    rclone_type = _RCLONE_TYPE.get(backend, backend)
    lines = [f"type = {rclone_type}"]

    if backend in ("smb", "sftp"):
        lines.append(f"host = {remote_config.get('host', '')}")
        if remote_config.get("port"):
            lines.append(f"port = {remote_config['port']}")
        if provider.get("username"):
            lines.append(f"user = {provider['username']}")
        if provider.get("password"):
            lines.append(f"pass = {_obscure(provider['password'])}")
        if backend == "smb" and remote_config.get("domain"):
            lines.append(f"domain = {remote_config['domain']}")
    elif backend == "s3":
        lines.append(f"provider = {remote_config.get('s3_provider') or 'Other'}")
        if provider.get("username"):
            lines.append(f"access_key_id = {provider['username']}")
        if provider.get("password"):
            lines.append(f"secret_access_key = {provider['password']}")  # s3 keys aren't "obscured", stored as-is in the config
        if remote_config.get("region"):
            lines.append(f"region = {remote_config['region']}")
        if remote_config.get("endpoint"):
            lines.append(f"endpoint = {remote_config['endpoint']}")
    elif backend in ("gdrive", "dropbox", "box"):
        # OAuth: the user ran `rclone authorize <backend>` on their own
        # machine (rclone's own documented device flow -- opens a browser,
        # nothing here ever sees their real login) and pasted the resulting
        # token JSON blob in. Nothing to collect ourselves; a real browser
        # OAuth round-trip inside this backend would need a public redirect
        # URI and registered app credentials per provider, out of scope for
        # a self-hosted container with no fixed public URL.
        if remote_config.get("token"):
            lines.append(f"token = {remote_config['token']}")
        if remote_config.get("client_id"):
            lines.append(f"client_id = {remote_config['client_id']}")
        if remote_config.get("client_secret"):
            lines.append(f"client_secret = {remote_config['client_secret']}")
    return lines


def _write_conf(provider: dict, remote_config: dict) -> str:
    backend = provider["library_backend"]
    conf_path = os.path.join(_provider_dir(provider["id"]), "rclone.conf")
    lines = ["[remote]", *_remote_section(backend, provider, remote_config)]
    # Written then chmod'd 0600 immediately -- there's an unavoidable instant
    # between create and chmod on POSIX, same tradeoff every "private temp
    # file" in this codebase already accepts (e.g. dispatcharr_dvr_importer's
    # own downloaded-file handling); the directory itself is already 0700.
    with open(conf_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(conf_path, stat.S_IRUSR | stat.S_IWUSR)
    return conf_path


def _remote_path(remote_config: dict) -> str:
    """The share/bucket/folder the whole library lives under, e.g. an SMB
    share name, an S3 bucket, or a Drive folder path -- everything the
    importer walks is relative to THIS, matching library_importer's existing
    "root" concept for a local/mounted source."""
    share = (remote_config.get("share") or remote_config.get("bucket") or "").strip("/")
    path = (remote_config.get("path") or "").strip("/")
    return "/".join(p for p in (share, path) if p)


async def _run(argv: list[str], *, timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        # A black-holed host (no RST, no FIN) can leave an rclone subprocess
        # blocked past any of its own internal timeouts -- kill it outright
        # rather than leaving a zombie process and an unresolved await
        # around, same "bound every filesystem/network wait" lesson as the
        # earlier hung-NFS-mount fix in xc_server/library_importer.
        proc.kill()
        await proc.wait()
        raise RcloneError(f"rclone timed out after {timeout:.0f}s -- the remote isn't responding") from None
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def probe(provider: dict, remote_config: dict) -> bool:
    """Cheap reachability check (one shallow listing) before a full recursive
    scan -- mirrors library_importer._probe_root's role for a mounted
    source."""
    if not _rclone_available():
        raise RcloneError("rclone is not installed in this image")
    conf_path = _write_conf(provider, remote_config)
    timeout = _PROBE_TIMEOUT_SECONDS.get(provider.get("library_backend"), _PROBE_TIMEOUT_DEFAULT)
    try:
        code, _, stderr = await _run(
            ["rclone", "--config", conf_path, "lsjson", "--max-depth", "1", f"remote:{_remote_path(remote_config)}"],
            timeout=timeout,
        )
        if code != 0:
            raise RcloneError(stderr.strip() or f"rclone exited {code}")
        return True
    finally:
        os.remove(conf_path)


async def list_remote(provider: dict, remote_config: dict) -> list[tuple[str, int]]:
    """[(rel_path, size_bytes)] for every file under the remote root --
    directories are excluded server-side by --files-only, matching
    _scan_root's contract exactly so library_importer's rest-of-pipeline
    (parser/matcher/bulk-import) needs no remote-vs-local branching beyond
    which scan function it calls."""
    if not _rclone_available():
        raise RcloneError("rclone is not installed in this image")
    conf_path = _write_conf(provider, remote_config)
    try:
        code, stdout, stderr = await _run(
            ["rclone", "--config", conf_path, "lsjson", "--recursive", "--files-only",
             f"remote:{_remote_path(remote_config)}"],
            timeout=_LSJSON_TIMEOUT_SECONDS,
        )
        if code != 0:
            raise RcloneError(stderr.strip() or f"rclone exited {code}")
        import json
        entries = json.loads(stdout) if stdout.strip() else []
        return [(e["Path"].replace("\\", "/"), int(e.get("Size") or 0)) for e in entries if not e.get("IsDir")]
    finally:
        os.remove(conf_path)


# ── serve http daemons ───────────────────────────────────────────────────────
# One rclone serve http process per remote provider, started lazily on first
# playback and reused after that -- kept alive for the life of the app
# process (or until explicitly stopped), not per-request, since starting it
# takes a real connection round-trip to the backend.

class _Daemon:
    __slots__ = ("process", "port", "started_at", "conf_path")

    def __init__(self, process, port: int, conf_path: str):
        self.process = process
        self.port = port
        self.started_at = time.monotonic()
        self.conf_path = conf_path


_daemons: dict[int, _Daemon] = {}
_daemon_lock = asyncio.Lock()


def daemon_port(provider_id: int) -> int:
    return _DAEMON_BASE_PORT + provider_id


def _daemon_alive(daemon: "_Daemon | None") -> bool:
    return daemon is not None and daemon.process.returncode is None


async def ensure_daemon(provider: dict, remote_config: dict) -> int:
    """Starts this provider's rclone serve http daemon if it isn't already
    running, and returns the localhost port it's listening on. Never raises
    for "already running" -- only for a genuine start failure, which the
    caller (xc_server) treats exactly like an unreachable upstream (fail
    over to the next source).

    Wraps the whole operation in an outer asyncio.wait_for on top of the
    readiness loop's own internal deadline -- found live 2026-09-22: against
    a backend that's reachable at the TCP level but not actually serving
    (S3 endpoint stopped), a single readiness GET blocked past its own
    per-request httpx timeout regardless (rclone's http server accepts the
    connection instantly but can stall producing a body while it retries
    against the dead backend internally), so the internal loop's "at most N
    attempts of T seconds each" bookkeeping wasn't actually sufficient on
    its own -- the same "every wait on an external resource needs an outer
    hard cap, not just an inner one" lesson as the NFS-hang fixes elsewhere
    in this codebase."""
    if not _rclone_available():
        raise RcloneError("rclone is not installed in this image")
    budget = _DAEMON_START_TIMEOUT_SECONDS.get(provider.get("library_backend"), _DAEMON_START_TIMEOUT_DEFAULT)
    try:
        return await asyncio.wait_for(_ensure_daemon_locked(provider, remote_config, budget), budget + 5)
    except asyncio.TimeoutError:
        await stop_daemon(provider["id"])
        raise RcloneError(f"rclone serve http didn't become ready within {budget + 5}s") from None


async def _ensure_daemon_locked(provider: dict, remote_config: dict, budget: float) -> int:
    provider_id = provider["id"]
    async with _daemon_lock:
        existing = _daemons.get(provider_id)
        if _daemon_alive(existing):
            return existing.port

        port = daemon_port(provider_id)
        conf_path = _write_conf(provider, remote_config)
        argv = [
            "rclone", "--config", conf_path, "serve", "http",
            "--addr", f"127.0.0.1:{port}",
            # No VFS disk cache -- reads stream straight through to the
            # backend with Range passthrough (confirmed live against real
            # SMB: a 3-byte Range request came back as a real 206). Nothing
            # else in this container needs rclone's cache eating disk space.
            "--vfs-cache-mode", "off",
            "--no-modtime",
            f"remote:{_remote_path(remote_config)}",
        ]
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        daemon = _Daemon(process, port, conf_path)
        _daemons[provider_id] = daemon

        # Wait for it to actually be ready to serve, not just spawned --
        # racing a client request against rclone's own startup handshake
        # with the backend would otherwise surface as a spurious first-play
        # failure.
        deadline = time.monotonic() + budget
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if process.returncode is not None:
                    stderr = (await process.stderr.read()).decode(errors="replace") if process.stderr else ""
                    _daemons.pop(provider_id, None)
                    _cleanup_conf(conf_path)
                    raise RcloneError(f"rclone serve http exited immediately: {stderr.strip()[-300:]}")
                try:
                    await client.get(f"http://127.0.0.1:{port}/")
                    return port
                except httpx.HTTPError:
                    await asyncio.sleep(0.2)
        await stop_daemon(provider_id)
        raise RcloneError(f"rclone serve http didn't become ready within {budget:.0f}s")


async def stop_daemon(provider_id: int) -> None:
    async with _daemon_lock:
        daemon = _daemons.pop(provider_id, None)
    if not daemon:
        return
    if daemon.process.returncode is None:
        daemon.process.terminate()
        try:
            await asyncio.wait_for(daemon.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            daemon.process.kill()
            await daemon.process.wait()
    _cleanup_conf(daemon.conf_path)


def _cleanup_conf(conf_path: str) -> None:
    try:
        os.remove(conf_path)
    except OSError:
        pass


async def stop_all_daemons() -> None:
    for provider_id in list(_daemons):
        await stop_daemon(provider_id)
