def test_successful_successor_clears_a_recoverable_mid_stream_break(db):
    """recoverable=True is what xc_server passes for a genuine mid-stream
    break (started OK, then broke) -- the one case a later successful range
    request really is the same attempt recovering."""
    provider_id = db.upsert_provider(
        "provider-a", "http://provider-a.example", "user", "pass",
    )
    movie_id = db.upsert_movie("Example Movie", 2024)
    db.add_movie_source(movie_id, provider_id, "movie-1")
    db.log_stream_failure(
        "movie", "Example Movie", "relay-user", [{"provider": "provider-a"}],
        "RemoteProtocolError", movie_id=movie_id, client_ip="192.0.2.10", recoverable=True,
    )

    cleared = db.clear_recovered_stream_failures(
        "movie", "Example Movie", "relay-user", movie_id=movie_id,
        client_ip="192.0.2.10",
    )

    assert cleared == 1
    assert db.list_stream_failures() == []


def test_terminal_failure_is_never_cleared(db):
    """Found live 2026-09-23 reviewing PR #34: a genuine terminal failure
    ("every source failed before playback ever started", recoverable
    defaults to False) must stay visible in Failed Streams even after a
    LATER, unrelated successful stream open for the same title -- that's a
    new attempt succeeding, not this failure resolving."""
    provider_id = db.upsert_provider(
        "provider-a", "http://provider-a.example", "user", "pass",
    )
    movie_id = db.upsert_movie("Example Movie", 2024)
    db.add_movie_source(movie_id, provider_id, "movie-1")
    db.log_stream_failure(
        "movie", "Example Movie", "relay-user", [{"provider": "provider-a"}],
        "all sources failed", movie_id=movie_id, client_ip="192.0.2.10",
    )

    cleared = db.clear_recovered_stream_failures(
        "movie", "Example Movie", "relay-user", movie_id=movie_id,
        client_ip="192.0.2.10",
    )

    assert cleared == 0
    assert len(db.list_stream_failures()) == 1
