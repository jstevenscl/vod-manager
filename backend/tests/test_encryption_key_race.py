"""config._update_raw is the only correct way to write any part of
config.json -- every config setter goes through it. Two distinct races it
guards against, both covered here:

1. get_or_create_encryption_key: generating this key is a one-time,
   NOT-safely-repeatable event -- once anything is encrypted under a key,
   silently generating a different one orphans it (decrypt_value's
   InvalidToken fallback returns old ciphertext unchanged, with no error).
2. ANY later, unrelated config write from a process whose in-memory cache
   predates another process's write can silently ERASE that write by
   blindly overwriting the whole file with its stale view -- found live
   2026-09-23: a `docker exec` script created an XC client (encrypting its
   password under a freshly-generated key), and moments later the app's
   own background scheduler called save_last_enrichment_run from a
   process whose cache had no key yet, wiping it out. See
   config._update_raw's own docstring for the full incident.

The real race windows here are microseconds wide -- too narrow for real OS
thread scheduling to reliably hit in a test. _config_write_window_hook() is
a no-op production hook sitting at exactly that point; monkeypatching it to
block on a barrier forces genuine interleaving there deterministically,
instead of leaving these tests flaky.
"""

import threading

import config
import secrets_util


def _force_full_interleave(monkeypatch, n):
    """Every caller reaches _config_write_window_hook (i.e. has already
    read a snapshot of config.json) before any of them is released to
    proceed -- the worst case for a stale-read-then-write race."""
    barrier = threading.Barrier(n)
    monkeypatch.setattr(config, "_config_write_window_hook", barrier.wait)


def test_harness_actually_reproduces_the_race_without_the_lock(db, monkeypatch):
    """Sanity check on the test methodology itself: with the hook forcing
    full interleave and the lock bypassed, callers DO generate and persist
    different keys -- proving this harness can actually detect the bug,
    not just always pass regardless of what's being tested."""
    _force_full_interleave(monkeypatch, 6)
    monkeypatch.setattr(config, "_CONFIG_WRITE_LOCK", threading.Lock())

    def unlocked_get_or_create():
        # Same shape as the pre-fix function: no lock around the
        # hook-guarded check-then-write.
        data = config._read_raw()
        key = data.get("encryption_key")
        if key:
            return key.encode()
        config._config_write_window_hook()
        new_key = config.Fernet.generate_key()
        data["encryption_key"] = new_key.decode()
        config._write_raw(data)
        return new_key

    keys = []
    threads = [threading.Thread(target=lambda: keys.append(unlocked_get_or_create())) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(set(keys)) > 1, "harness failed to reproduce the race -- test methodology is broken"


def test_concurrent_callers_within_one_process_get_the_same_key(db, monkeypatch):
    """The real function, same forced interleave: the fix must converge all
    callers on one key despite it."""
    _force_full_interleave(monkeypatch, 12)
    keys: list[bytes] = []
    threads = [threading.Thread(target=lambda: keys.append(config.get_or_create_encryption_key())) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(keys) == 12
    assert len(set(keys)) == 1, "concurrent callers generated different keys"
    assert config._read_raw()["encryption_key"] == keys[0].decode()


def test_a_credential_encrypted_by_one_racing_caller_stays_decryptable(db, monkeypatch):
    """The actual failure mode: two callers race to encrypt something (a
    provider password, an XC client secret) at the moment the key is first
    created. Without the fix, whichever call generated the key that DIDN'T
    end up persisted leaves its own encrypted value forever undecryptable."""
    _force_full_interleave(monkeypatch, 8)
    results: dict[int, tuple[bytes, str]] = {}

    def worker(i):
        key = config.get_or_create_encryption_key()
        results[i] = (key, secrets_util.encrypt_value(f"secret-{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    for i, (key, ciphertext) in results.items():
        assert key.decode() == config._read_raw()["encryption_key"]
        assert secrets_util.decrypt_value(ciphertext) == f"secret-{i}"


def test_key_already_on_disk_is_reused_not_regenerated(db):
    first = config.get_or_create_encryption_key()
    config._raw_cache = None  # simulate a fresh process re-reading the file
    second = config.get_or_create_encryption_key()
    assert first == second


def test_unrelated_write_from_a_stale_process_does_not_erase_the_key(db):
    """The real live incident, reproduced directly: process A encrypts a
    credential (creating the key as a side effect). Process B's in-memory
    view of config.json predates that -- it never saw the key -- and then
    calls a totally unrelated setter (save_last_enrichment_run). Without
    _update_raw's forced fresh read, B's write blindly overwrites the whole
    file with its stale dict, erasing the key A's credential depends on."""
    # B "already read" config.json before A did anything -- simulate by
    # populating B's cache first, as a real second process's first read
    # would (empty file, no key yet).
    config._raw_cache = None
    config._read_raw()  # B's stale snapshot: {}

    # A (a different process, in this test: after resetting the cache to
    # simulate that separation) creates the key and encrypts a credential.
    config._raw_cache = None
    key = config.get_or_create_encryption_key()
    ciphertext = secrets_util.encrypt_value("s3cret-provider-password")

    # B, STILL holding its pre-A stale cache (never invalidated -- this is
    # the crux of the bug: nothing about A's write touches B's in-process
    # cache), now saves something unrelated.
    config._raw_cache = {}  # restore B's stale, key-less view
    config.save_last_enrichment_run(12345.0)

    # The key must survive, and the earlier ciphertext must still decrypt --
    # neither the credential nor the setting either process wrote is lost.
    assert config._read_raw()["encryption_key"] == key.decode()
    assert config._read_raw()["last_enrichment_run"] == 12345.0
    assert secrets_util.decrypt_value(ciphertext) == "s3cret-provider-password"


def test_two_unrelated_settings_saved_by_different_stale_processes_both_survive():
    """Same bug, no crypto involved: two ordinary settings, saved by two
    processes that never saw each other's write, must both end up on disk
    -- neither save may silently clobber the other."""
    import tempfile
    from pathlib import Path
    orig_file, orig_cache = config.CONFIG_FILE, config._raw_cache
    try:
        tmp_dir = Path(tempfile.mkdtemp())
        config.CONFIG_FILE = tmp_dir / "config.json"
        config._raw_cache = None

        config._read_raw()  # process B's stale empty snapshot
        config.save_tmdb_api_key("tmdb-key-123")  # process A writes and updates ITS OWN cache
        config._raw_cache = {}  # simulate B still holding its pre-A stale cache
        config.save_mdblist_api_key("mdblist-key-456")  # process B writes

        config._raw_cache = None  # a fresh process reads the final state
        assert config.get_tmdb_api_key() == "tmdb-key-123"
        assert config.get_mdblist_api_key() == "mdblist-key-456"
    finally:
        config.CONFIG_FILE, config._raw_cache = orig_file, orig_cache
