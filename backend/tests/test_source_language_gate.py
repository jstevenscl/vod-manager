def _make_provider(db, name="prov1"):
    return db.upsert_provider(name, "http://example.com", "user", "pass")


def test_streaming_sources_exclude_non_english_spanish_language(db):
    provider_id = _make_provider(db)
    db.bulk_import_movies(provider_id, [
        {
            "name": "White House Down",
            "year": 2013,
            "provider_stream_id": "en-1",
            "container_extension": "mp4",
            "provider_category_name": "Action",
            "auto_archive": False,
            "raw_name": "White House Down",
        },
        {
            "name": "White House Down",
            "year": 2013,
            "provider_stream_id": "nl-1",
            "container_extension": "mp4",
            "provider_category_name": "Action",
            "auto_archive": False,
            "raw_name": "NL - White House Down",
        },
    ])

    movie = db.get_movie_by_name_year("White House Down", 2013)

    sources = db.list_movie_sources_for_streaming(movie["id"])
    provider_stream_ids = {s["provider_stream_id"] for s in sources}

    assert provider_stream_ids == {"en-1"}


def test_streaming_sources_allow_spanish(db):
    provider_id = _make_provider(db)
    db.bulk_import_movies(provider_id, [
        {
            "name": "Coco",
            "year": 2017,
            "provider_stream_id": "es-1",
            "container_extension": "mp4",
            "provider_category_name": "Animation",
            "auto_archive": False,
            "raw_name": "ES - Coco",
        },
    ])

    movie = db.get_movie_by_name_year("Coco", 2017)
    sources = db.list_movie_sources_for_streaming(movie["id"])

    assert {s["provider_stream_id"] for s in sources} == {"es-1"}


def test_streaming_sources_skip_entirely_when_no_english_or_spanish_source(db):
    provider_id = _make_provider(db)
    db.bulk_import_movies(provider_id, [
        {
            "name": "Some Foreign Film",
            "year": 2020,
            "provider_stream_id": "nl-1",
            "container_extension": "mp4",
            "provider_category_name": "Drama",
            "auto_archive": False,
            "raw_name": "NL - Some Foreign Film",
        },
        {
            "name": "Some Foreign Film",
            "year": 2020,
            "provider_stream_id": "gr-1",
            "container_extension": "mp4",
            "provider_category_name": "Drama",
            "auto_archive": False,
            "raw_name": "GR - Some Foreign Film",
        },
    ])

    movie = db.get_movie_by_name_year("Some Foreign Film", 2020)
    sources = db.list_movie_sources_for_streaming(movie["id"])

    assert sources == []
