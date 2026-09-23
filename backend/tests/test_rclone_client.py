"""Pure-logic pieces of rclone_client: the rclone.conf section built per
backend, and the remote root path. No real rclone process involved -- those
paths are covered by test_library_importer_rclone.py (mocked) and by live
Docker testing against real SMB/SFTP/S3 servers.
"""

import rclone_client as rc


def test_smb_section_has_host_user_pass(monkeypatch):
    monkeypatch.setattr(rc, "_obscure", lambda p: f"OBS({p})")
    lines = rc._remote_section("smb", {"username": "bob", "password": "s3cret"}, {"host": "nas.local", "domain": "WORKGROUP"})
    assert "type = smb" in lines
    assert "host = nas.local" in lines
    assert "user = bob" in lines
    assert "pass = OBS(s3cret)" in lines
    assert "domain = WORKGROUP" in lines


def test_sftp_section_has_port(monkeypatch):
    monkeypatch.setattr(rc, "_obscure", lambda p: "OBS")
    lines = rc._remote_section("sftp", {"username": "u", "password": "p"}, {"host": "h", "port": 2222})
    assert "port = 2222" in lines


def test_s3_section_uses_username_password_as_keys():
    lines = rc._remote_section("s3", {"username": "AKIA...", "password": "secret-key"},
                                {"region": "us-east-1", "endpoint": "http://minio:9000", "s3_provider": "Minio"})
    assert "access_key_id = AKIA..." in lines
    assert "secret_access_key = secret-key" in lines
    assert "region = us-east-1" in lines
    assert "endpoint = http://minio:9000" in lines
    assert "provider = Minio" in lines


def test_s3_defaults_provider_to_other_when_unset():
    lines = rc._remote_section("s3", {}, {})
    assert "provider = Other" in lines


def test_oauth_backends_carry_only_the_token(monkeypatch):
    for backend, rclone_type in (("gdrive", "drive"), ("dropbox", "dropbox"), ("box", "box")):
        lines = rc._remote_section(backend, {}, {"token": '{"access_token":"x"}'})
        assert f"type = {rclone_type}" in lines
        assert '{"access_token":"x"}' in "\n".join(lines)
        assert not any(l.startswith("user =") or l.startswith("pass =") for l in lines)


def test_no_secrets_written_when_none_provided():
    lines = rc._remote_section("smb", {}, {"host": "nas"})
    assert not any(l.startswith("user =") or l.startswith("pass =") for l in lines)


def test_remote_path_joins_share_and_subpath():
    assert rc._remote_path({"share": "media", "path": "TV Shows"}) == "media/TV Shows"
    assert rc._remote_path({"bucket": "my-bucket"}) == "my-bucket"
    assert rc._remote_path({"share": "/media/", "path": "/Movies/"}) == "media/Movies"
    assert rc._remote_path({}) == ""


def test_is_remote_backend():
    assert rc.is_remote_backend({"provider_type": "library", "library_backend": "smb"})
    assert rc.is_remote_backend({"provider_type": "library", "library_backend": "s3"})
    assert not rc.is_remote_backend({"provider_type": "library", "library_backend": "local"})
    assert not rc.is_remote_backend({"provider_type": "xc", "library_backend": "smb"})


def test_gdrive_maps_to_rclone_drive_type():
    lines = rc._remote_section("gdrive", {}, {"token": "t"})
    assert "type = drive" in lines


def test_ensure_daemon_is_bounded_even_if_the_readiness_loop_hangs(monkeypatch):
    """Found live 2026-09-22: a readiness GET against a TCP-reachable but
    non-responding backend blocked past its own per-request timeout, so the
    inner loop's bookkeeping alone wasn't sufficient -- ensure_daemon must
    cap the WHOLE operation from outside too."""
    import asyncio
    import time

    async def hung_locked(provider, remote_config, budget):
        await asyncio.sleep(30)
        return 1
    monkeypatch.setattr(rc, "_ensure_daemon_locked", hung_locked)
    monkeypatch.setattr(rc, "_rclone_available", lambda: True)

    async def fake_stop(provider_id):
        pass
    monkeypatch.setattr(rc, "stop_daemon", fake_stop)

    t0 = time.monotonic()
    try:
        asyncio.run(rc.ensure_daemon({"id": 1, "library_backend": "smb"}, {}))
        assert False, "expected RcloneError"
    except rc.RcloneError as exc:
        assert "didn't become ready" in str(exc)
    elapsed = time.monotonic() - t0
    budget = rc._DAEMON_START_TIMEOUT_SECONDS["smb"]
    assert elapsed < budget + 10, f"took {elapsed:.1f}s -- not actually bounded"


def test_cloud_backends_get_a_more_generous_daemon_start_budget():
    """Found live 2026-09-23: a real Google Drive cold daemon start took
    7-10s+, well past the LAN-tuned budget -- cloud backends need real
    headroom for their OAuth token exchange over the actual internet."""
    for backend in ("smb", "sftp"):
        assert rc._DAEMON_START_TIMEOUT_SECONDS[backend] <= 10
    for backend in ("gdrive", "dropbox", "box"):
        assert rc._DAEMON_START_TIMEOUT_SECONDS[backend] >= 15
