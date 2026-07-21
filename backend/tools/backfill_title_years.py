"""
One-time backfill for movies/series imported before the parse_name_year()
fix (see vod_importer.py) -- re-parses every NULL-year row's name, and for
each one that now yields a real year:

  - if an existing row already has that exact (name, year), merge into it
    (moves sources/episodes/placements over, drops the now-empty old row)
  - otherwise, just corrects this row's own name/year in place

Run with --dry-run first (default) to see counts/examples with no writes.
Pass --apply to actually commit the changes.
"""

import argparse
import sys

sys.path.insert(0, "/app")

import vod_db
from vod_importer import parse_name_year


def backfill_movies(apply: bool) -> dict:
    conn = vod_db._connect()
    rows = conn.execute("SELECT id, name FROM movies WHERE year IS NULL").fetchall()
    fixed_in_place = 0
    merged = 0
    unchanged = 0
    examples = []

    for r in rows:
        name, year = parse_name_year(r["name"])
        if year is None:
            unchanged += 1
            continue

        target = conn.execute(
            "SELECT id FROM movies WHERE name=? AND year=? AND id!=?", (name, year, r["id"])
        ).fetchone()

        if len(examples) < 20:
            examples.append((r["name"], r["id"], name, year, target["id"] if target else None))

        if target:
            if apply:
                # merge_movie opens its own connection and commits -- flush
                # and release any transaction this connection is holding
                # first, or the two writers deadlock against each other.
                vod_db._commit_with_retry(conn)
                vod_db.merge_movie(r["id"], target["id"])
            merged += 1
        else:
            if apply:
                conn.execute(
                    "UPDATE movies SET name=?, year=?, updated_at=? WHERE id=?",
                    (name, year, vod_db._now(), r["id"]),
                )
                vod_db._commit_with_retry(conn)
            fixed_in_place += 1

    conn.close()
    return {
        "total_null_year": len(rows),
        "fixed_in_place": fixed_in_place,
        "merged": merged,
        "left_unchanged": unchanged,
        "examples": examples,
    }


def backfill_series(apply: bool) -> dict:
    conn = vod_db._connect()
    rows = conn.execute("SELECT id, name FROM series WHERE year IS NULL").fetchall()
    fixed_in_place = 0
    merged = 0
    unchanged = 0
    examples = []

    for r in rows:
        name, year = parse_name_year(r["name"])
        if year is None:
            unchanged += 1
            continue

        target = conn.execute(
            "SELECT id FROM series WHERE name=? AND year=? AND id!=?", (name, year, r["id"])
        ).fetchone()

        if len(examples) < 20:
            examples.append((r["name"], r["id"], name, year, target["id"] if target else None))

        if target:
            if apply:
                vod_db._commit_with_retry(conn)
                vod_db.merge_series(r["id"], target["id"])
            merged += 1
        else:
            if apply:
                conn.execute(
                    "UPDATE series SET name=?, year=?, updated_at=? WHERE id=?",
                    (name, year, vod_db._now(), r["id"]),
                )
                vod_db._commit_with_retry(conn)
            fixed_in_place += 1

    conn.close()
    return {
        "total_null_year": len(rows),
        "fixed_in_place": fixed_in_place,
        "merged": merged,
        "left_unchanged": unchanged,
        "examples": examples,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually write changes (default: dry run)")
    args = parser.parse_args()

    print(f"=== movies (apply={args.apply}) ===")
    m = backfill_movies(args.apply)
    for k, v in m.items():
        if k != "examples":
            print(f"  {k}: {v}")
    print("  sample:")
    for old_name, old_id, new_name, new_year, target_id in m["examples"]:
        action = f"MERGE into id={target_id}" if target_id else "update in place"
        print(f"    [{old_id}] {old_name!r} -> {new_name!r} ({new_year}) [{action}]")

    print(f"\n=== series (apply={args.apply}) ===")
    s = backfill_series(args.apply)
    for k, v in s.items():
        if k != "examples":
            print(f"  {k}: {v}")
    print("  sample:")
    for old_name, old_id, new_name, new_year, target_id in s["examples"]:
        action = f"MERGE into id={target_id}" if target_id else "update in place"
        print(f"    [{old_id}] {old_name!r} -> {new_name!r} ({new_year}) [{action}]")
