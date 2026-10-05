import vod_db


def _insert(conn, table, name, year, tmdb_id, now):
    conn.execute(f"INSERT INTO {table} (name, year, tmdb_id, created_at) VALUES (?, ?, ?, ?)", (name, year, tmdb_id, now))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_source(conn, row_id, provider_id, language, now):
    conn.execute(
        "INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, raw_name, language, added_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (row_id, provider_id, f"m-{row_id}", "x", language, now, now))


def test_possible_matches_can_be_reviewed_and_approved_in_bulk(db):
    provider_id = db.upsert_provider("Provider", "http://provider.invalid", "u", "p", provider_type="xc")
    now = db._now()
    conn = db._connect()
    conn.execute("INSERT INTO movies (name, year, tmdb_id, created_at) VALUES (?, ?, ?, ?)", ("Jeffrey", 1995, "123", now))
    candidate_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO movies (name, year, tmdb_id, needs_year_review, created_at) VALUES (?, ?, ?, ?, ?)", ("Jeffrey", None, None, 1, now))
    item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, raw_name, language, added_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (candidate_id, provider_id, "candidate", "Jeffrey", "EN", now, now))
    conn.execute("INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, raw_name, language, added_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (item_id, provider_id, "item", "Jeffrey", "EN", now, now))
    conn.commit()
    conn.close()

    page = db.list_possible_metadata_matches("movie")
    assert page["total"] == 1
    assert page["items"][0]["id"] == item_id
    assert page["items"][0]["candidates"][0]["id"] == candidate_id

    result = db.bulk_merge_possible_metadata_matches("movie", [{"item_id": item_id, "candidate_id": candidate_id}])
    assert result == {"merged": 1, "skipped": []}
    assert db.get_movie(item_id) is None
    assert db.get_movie(candidate_id)["year"] == 1995


def test_possible_matches_do_not_list_reciprocal_unresolved_pairs(db):
    # KNM: 2026-10-03 two unresolved same-title cards each listed the other,
    # doubling the count; keep one pair with the lower id as the canonical candidate.
    now = db._now()
    conn = db._connect()
    ids = []
    for _ in range(2):
        conn.execute("INSERT INTO series (name, year, tmdb_id, created_at) VALUES (?, ?, ?, ?)", ("Twin Show", None, None, now))
        ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()

    page = db.list_possible_metadata_matches("series")
    assert page["total"] == 1
    assert page["items"][0]["id"] == max(ids)
    assert [c["id"] for c in page["items"][0]["candidates"]] == [min(ids)]


def test_possible_matches_hide_candidates_with_no_shared_language(db):
    # KNM: 2026-10-03 G2 -- the merge rejects pairs with no shared source
    # language, so listing them made a pair that can never merge reappear forever.
    provider_id = db.upsert_provider("Provider", "http://provider.invalid", "u", "p", provider_type="xc")
    now = db._now()
    conn = db._connect()
    candidate_id = _insert(conn, "movies", "Polyglot", 2001, "55", now)
    item_id = _insert(conn, "movies", "Polyglot", None, None, now)
    _add_source(conn, candidate_id, provider_id, "DE", now)
    _add_source(conn, item_id, provider_id, "EN", now)
    conn.commit()
    conn.close()

    assert db.list_possible_metadata_matches("movie")["total"] == 0


def test_possible_match_candidates_are_ordered_best_first(db):
    # KNM: 2026-10-03 G3 -- the UI pre-selects candidates[0]; a TMDB-resolved
    # card must come before an unresolved or year-only one.
    provider_id = db.upsert_provider("Provider", "http://provider.invalid", "u", "p", provider_type="xc")
    now = db._now()
    conn = db._connect()
    unresolved_id = _insert(conn, "movies", "Ordered", None, None, now)
    year_only_id = _insert(conn, "movies", "Ordered", 1999, None, now)
    item_id = _insert(conn, "movies", "Ordered", None, None, now)
    resolved_id = _insert(conn, "movies", "Ordered", 1999, "77", now)
    for row_id in (unresolved_id, year_only_id, item_id, resolved_id):
        _add_source(conn, row_id, provider_id, "EN", now)
    conn.commit()
    conn.close()

    item = next(i for i in db.list_possible_metadata_matches("movie")["items"] if i["id"] == item_id)
    assert [c["id"] for c in item["candidates"]] == [resolved_id, year_only_id, unresolved_id]


def test_bulk_merge_reports_failing_pair_and_continues(db, monkeypatch):
    # KNM: 2026-10-03 G1 -- one failing merge used to abort the whole batch
    # with a 500 and hide the merges already done.
    provider_id = db.upsert_provider("Provider", "http://provider.invalid", "u", "p", provider_type="xc")
    now = db._now()
    conn = db._connect()
    pairs = []
    for title in ("Broken", "Fine"):
        candidate_id = _insert(conn, "movies", title, 2001, "1" if title == "Broken" else "2", now)
        item_id = _insert(conn, "movies", title, None, None, now)
        _add_source(conn, candidate_id, provider_id, "EN", now)
        _add_source(conn, item_id, provider_id, "EN", now)
        pairs.append({"item_id": item_id, "candidate_id": candidate_id})
    conn.commit()
    conn.close()

    real_merge = vod_db.merge_movie

    def flaky_merge(item_id, candidate_id):
        if item_id == pairs[0]["item_id"]:
            raise RuntimeError("database is locked")
        return real_merge(item_id, candidate_id)

    monkeypatch.setattr(vod_db, "merge_movie", flaky_merge)

    result = db.bulk_merge_possible_metadata_matches("movie", pairs)

    assert result["merged"] == 1
    assert result["skipped"] == [{**pairs[0], "reason": "merge failed: database is locked"}]
    assert db.get_movie(pairs[1]["item_id"]) is None
