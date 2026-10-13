"""MDBList authenticates with an `apikey=` query param (no underscore), which
the httpx log redaction missed -- the key showed up in plaintext container logs
on every list fetch."""

import xc_server


def test_mdblist_apikey_is_redacted():
    url = "https://api.mdblist.com/lists/1/items?apikey=SECRETKEY123&limit=500&offset=0"
    out = xc_server._redact_upstream_url(url)
    assert "SECRETKEY123" not in out
    assert "apikey=***" in out and "limit=500" in out


def test_existing_api_key_param_still_redacted():
    assert "S3" not in xc_server._redact_upstream_url("https://api.themoviedb.org/3/x?api_key=S3&page=1")
