"""get_or_create_encryption_key is a one-time, NOT-safely-repeatable event:
once anything is encrypted under a key, silently generating a different one
orphans it (decrypt_value's InvalidToken fallback returns old ciphertext
unchanged, with no error -- see config.get_or_create_encryption_key's
docstring for the live incident this was found from).

The real race window (between "no key yet" and persisting a freshly
generated one) is microseconds wide -- too narrow for real OS thread
scheduling to reliably hit in a test. _race_window_hook() is a no-op
production hook sitting at exactly that point; monkeypatching it to block on
a barrier forces genuine interleaving there deterministically, instead of
leaving this test flaky.
"""

import threading

import config
import secrets_util


def _force_full_interleave(monkeypatch, n):
    """Every caller reaches _race_window_hook (i.e. has already read
    "no key yet") before any of them is released to proceed -- the worst
    case for the check-then-write race."""
    barrier = threading.Barrier(n)
    monkeypatch.setattr(config, "_race_window_hook", barrier.wait)


def test_harness_actually_reproduces_the_race_without_the_lock(db, monkeypatch):
    """Sanity check on the test methodology itself: with the hook forcing
    full interleave and the lock bypassed, callers DO generate and persist
    different keys -- proving this harness can actually detect the bug,
    not just always pass regardless of what's being tested."""
    _force_full_interleave(monkeypatch, 6)
    monkeypatch.setattr(config, "_ENCRYPTION_KEY_LOCK", threading.Lock())

    def unlocked_get_or_create():
        # Same shape as the pre-fix function: no lock around the
        # hook-guarded check-then-write.
        data = config._read_raw()
        key = data.get("encryption_key")
        if key:
            return key.encode()
        config._race_window_hook()
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
