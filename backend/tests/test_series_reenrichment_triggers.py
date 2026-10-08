"""vod_manager-v30: once a series source's episodes had been fetched, nothing
ever refetched them -- the per-source gate only passed never-fetched sources,
so neither a provider-reported last_modified change nor the TTL fallback could
trigger a refetch and new episodes of ongoing shows were never discovered.
These pin what now triggers (and what must not trigger) an episode refetch."""

import asyncio
import time

import vod_db
import vod_importer

DAY = 86400


class _Provider:
    """Fake XC panel: the episode count it reports can be changed between calls."""

    def __init__(self, episodes=2, info=True):
        self.episodes = episodes
        self.info = info
        self.calls = 0

    def client_class(self):
        outer = self

        class FakeClient:
            def __init__(self, _provider):
                pass

            async def get_series_info(self, series_id):
                outer.calls += 1
                eps = {"1": [{"id": f"e{i}", "episode_num": i, "season": 1, "title": f"Ep {i}",
                              "container_extension": "mp4"} for i in range(1, outer.episodes + 1)]}
                return {"info": {"name": "Some Show", "plot": "p"} if outer.info else {}, "episodes": eps}

        return FakeClient


def _setup(db, monkeypatch, last_modified="100", **kw):
    fake = _Provider(**kw)
    monkeypatch.setattr(vod_importer, "XCProviderClient", fake.client_class())
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    p = db.upsert_provider("prov1", "http://example.com", "u", "p")
    _import(db, p, last_modified)
    return fake, p, next(r["id"] for r in db.list_series(limit=10) if r["name"] == "Some Show")


def _import(db, provider_id, last_modified, series_stream_id="s1"):
    item = {"name": "Some Show", "year": 2020, "provider_series_id": series_stream_id, "raw_name": "Some Show", "_has_detail": True}
    if last_modified is not None:
        item["provider_last_modified"] = last_modified
    db.bulk_import_series(provider_id, [item])


def _enrich(sid, **kw):
    return asyncio.run(vod_importer.enrich_series(sid, **kw))


def _age(sid, days):
    conn = vod_db._connect()
    conn.execute("UPDATE series SET last_enriched_at=? WHERE id=?", (str(time.time() - days * DAY - 60), sid))
    conn.commit()
    conn.close()


def _episodes(db, sid):
    return len(db.list_episodes_for_series_ids([sid])[sid])


def test_changed_last_modified_refetches_and_discovers_the_new_episode(db, monkeypatch):
    fake, p, sid = _setup(db, monkeypatch, last_modified="100")
    assert _enrich(sid)["fetched"] is True and fake.calls == 1 and _episodes(db, sid) == 2
    assert _enrich(sid)["fetched"] is False and fake.calls == 1          # nothing changed: no call
    fake.episodes = 3
    _import(db, p, "200")                                                  # provider reports a change
    assert _enrich(sid)["fetched"] is True and fake.calls == 2            # was: 'already up to date'
    assert _episodes(db, sid) == 3
    assert _enrich(sid)["fetched"] is False and fake.calls == 2           # snapshot caught up


def test_no_info_block_still_records_the_snapshot_so_it_is_not_refetched_every_pass(db, monkeypatch):
    fake, p, sid = _setup(db, monkeypatch, last_modified="100", info=False)
    _enrich(sid)
    assert fake.calls == 1
    _enrich(sid)
    assert fake.calls == 1


def test_no_last_modified_ttl_pass_backs_off_while_nothing_new_appears(db, monkeypatch):
    fake, p, sid = _setup(db, monkeypatch, last_modified=None)
    _enrich(sid)                                                           # first fetch, 2 episodes
    assert fake.calls == 1
    _age(sid, 0.5)
    assert _enrich(sid)["fetched"] is False                                # under the 24h TTL
    _age(sid, 1.5)
    assert _enrich(sid)["fetched"] is True and fake.calls == 2             # TTL due (was: never)
    _age(sid, 1.5)
    assert _enrich(sid)["fetched"] is False and fake.calls == 2            # unchanged once -> 2 day interval
    _age(sid, 2.5)
    assert _enrich(sid)["fetched"] is True and fake.calls == 3             # due again, still unchanged -> 4 days
    fake.episodes = 3                                                      # a new episode finally airs
    _age(sid, 4.5)
    assert _enrich(sid)["fetched"] is True and _episodes(db, sid) == 3
    _age(sid, 1.5)
    assert _enrich(sid)["fetched"] is True                                 # change reset the backoff to the plain TTL


def test_ended_series_with_a_permanently_incomplete_list_settles_to_long_intervals(db, monkeypatch):
    fake, p, sid = _setup(db, monkeypatch, last_modified=None, episodes=8)    # provider only ever has 8
    _enrich(sid)
    for _ in range(6):
        _age(sid, 40)                                                      # always past even the 16-day cap
        _enrich(sid)
    conn = vod_db._connect()
    count = conn.execute("SELECT enrich_stable_count FROM series WHERE id=?", (sid,)).fetchone()[0]
    conn.close()
    assert count >= 5                                                      # nothing ever changed
    _age(sid, 8)
    assert _enrich(sid)["fetched"] is False                                # inside the capped interval


def test_a_source_that_was_never_fetched_is_due_even_when_the_series_is_in_sync(db, monkeypatch):
    fake, p, sid = _setup(db, monkeypatch, last_modified="100")
    _enrich(sid)
    assert fake.calls == 1
    p2 = db.upsert_provider("prov2", "http://example2.com", "u", "p")
    _import(db, p2, "100", series_stream_id="s2")                          # a second provider starts carrying it
    assert db.series_needs_enrichment(sid) is True
    _enrich(sid)
    assert fake.calls == 3                                                 # both sources fetched


def test_a_recently_failed_unfetched_source_does_not_keep_the_series_perpetually_due(db, monkeypatch):
    fake, p, sid = _setup(db, monkeypatch, last_modified="100")
    _enrich(sid)
    p2 = db.upsert_provider("prov2", "http://example2.com", "u", "p")
    _import(db, p2, "100", series_stream_id="s2")
    db.record_series_source_failure(sid, p2, "s2")
    assert db.series_needs_enrichment(sid) is False                        # failed just now: retried after the TTL


def test_force_still_refetches(db, monkeypatch):
    fake, p, sid = _setup(db, monkeypatch, last_modified="100")
    _enrich(sid)
    assert _enrich(sid, force=True)["fetched"] is True and fake.calls == 2
