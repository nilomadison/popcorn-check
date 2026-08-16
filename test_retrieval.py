import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import rt
import tmdb as tmdb_module
import ytv


def node(jw_id: str, title: str, year: int = 2000) -> dict:
    return {
        "node": {
            "id": jw_id,
            "objectType": "MOVIE",
            "content": {
                "title": title,
                "originalReleaseYear": year,
                "shortDescription": f"About {title}",
                "fullPath": f"/us/movie/{title.lower()}",
                "posterUrl": f"/poster/{jw_id}.jpg",
                "externalIds": {"imdbId": f"tt{jw_id}", "tmdbId": jw_id},
                "scoring": {"tomatoMeter": 81, "certifiedFresh": True},
                "genres": [{"technicalName": "drama"}],
            },
        }
    }


class CatalogSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "yttv.db")
        self.db_patch = patch.object(ytv, "YTTV_DB", self.db)
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp.cleanup()

    def test_complete_snapshot_deactivates_absent_rows_and_saves_jw_data(self) -> None:
        con = ytv._db()
        con.execute(
            "INSERT INTO catalog (jw_id,title,active) VALUES ('old','Old',1)"
        )
        con.execute(
            "INSERT INTO catalog_providers (jw_id,provider_key,active) "
            "VALUES ('old','youtube_tv',1)"
        )
        con.commit()
        con.close()
        page = {
            "totalCount": 1,
            "edges": [node("1", "New")],
            "pageInfo": {"hasNextPage": False, "endCursor": "MQ=="},
        }
        with patch.object(ytv, "_fetch_page", return_value=page):
            self.assertEqual(ytv.fetch_catalog(), 1)
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT active FROM catalog WHERE jw_id='old'").fetchone()[0], 0)
        self.assertEqual(
            con.execute(
                "SELECT active FROM catalog_providers "
                "WHERE jw_id='old' AND provider_key='youtube_tv'"
            ).fetchone()[0],
            0,
        )
        saved = con.execute(
            "SELECT active,jw_tomatometer,jw_synopsis,jw_genres,jw_poster "
            "FROM catalog WHERE jw_id='1'"
        ).fetchone()
        con.close()
        self.assertEqual(saved[:4], (1, 81, "About New", '["drama"]'))
        self.assertTrue(saved[4].startswith("https://images.justwatch.com/"))

    def test_existing_catalog_is_migrated_to_youtube_tv_associations(self) -> None:
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE catalog (jw_id TEXT PRIMARY KEY, title TEXT, "
            "popularity INTEGER, first_seen TEXT, last_seen TEXT, "
            "active INTEGER NOT NULL DEFAULT 1)"
        )
        con.executemany(
            "INSERT INTO catalog (jw_id,title,popularity,active) VALUES (?,?,?,?)",
            [("active", "Active", 1, 1), ("inactive", "Inactive", 2, 0)],
        )
        con.commit()
        con.close()
        con = ytv._db()
        migrated = con.execute(
            "SELECT jw_id,provider_key,active,popularity FROM catalog_providers "
            "ORDER BY jw_id"
        ).fetchall()
        con.close()
        self.assertEqual(
            migrated,
            [("active", "youtube_tv", 1, 1),
             ("inactive", "youtube_tv", 0, 2)],
        )

    def test_partial_snapshot_rolls_back_and_keeps_active_catalog(self) -> None:
        con = ytv._db()
        con.execute("INSERT INTO catalog (jw_id,title,active) VALUES ('old','Old',1)")
        con.execute(
            "INSERT INTO catalog_providers (jw_id,provider_key,active) "
            "VALUES ('old','youtube_tv',1)"
        )
        con.commit()
        con.close()
        page = {
            "totalCount": 2,
            "edges": [node("1", "New")],
            "pageInfo": {"hasNextPage": False, "endCursor": "MQ=="},
        }
        with patch.object(ytv, "_fetch_page", return_value=page):
            with self.assertRaisesRegex(RuntimeError, "Incomplete"):
                ytv.fetch_catalog()
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT active FROM catalog WHERE jw_id='old'").fetchone()[0], 1)
        self.assertIsNone(con.execute("SELECT active FROM catalog WHERE jw_id='1'").fetchone())
        con.close()

    def test_recent_unmatched_title_does_not_starve_queue(self) -> None:
        con = ytv._db()
        con.executemany(
            "INSERT INTO catalog (jw_id,title,year,popularity,active,rt_last_attempt_at) "
            "VALUES (?,?,?,?,1,?)",
            [("a", "Retry Later", 2000, 0, ytv._now()),
             ("b", "Try Now", 2001, 1, None)],
        )
        con.executemany(
            "INSERT INTO catalog_providers "
            "(jw_id,provider_key,active,popularity) VALUES (?,'youtube_tv',1,?)",
            [("a", 0), ("b", 1)],
        )
        con.commit()
        con.close()
        self.assertEqual(ytv.pending_rating_ids(limit=1), [("b", "Try Now", 2001)])

    def test_provider_snapshot_does_not_deactivate_other_provider(self) -> None:
        con = ytv._db()
        con.execute("INSERT INTO catalog (jw_id,title) VALUES ('shared','Shared')")
        con.executemany(
            "INSERT INTO catalog_providers (jw_id,provider_key,active) VALUES (?,?,1)",
            [("shared", "youtube_tv"), ("shared", "netflix")],
        )
        con.commit()
        con.close()
        page = {
            "totalCount": 1,
            "edges": [node("netflix-new", "Netflix New")],
            "pageInfo": {"hasNextPage": False, "endCursor": "MQ=="},
        }
        with patch.object(ytv, "_fetch_page", return_value=page):
            self.assertEqual(ytv.fetch_catalog("netflix"), 1)
        con = sqlite3.connect(self.db)
        statuses = con.execute(
            "SELECT provider_key,active FROM catalog_providers "
            "WHERE jw_id='shared' ORDER BY provider_key"
        ).fetchall()
        legacy_active = con.execute(
            "SELECT active FROM catalog WHERE jw_id='netflix-new'"
        ).fetchone()[0]
        con.close()
        self.assertEqual(statuses, [("netflix", 0), ("youtube_tv", 1)])
        self.assertEqual(legacy_active, 0)

    def test_tmdb_enrichment_stores_validated_id_and_genres(self) -> None:
        con = ytv._db()
        con.execute(
            "INSERT INTO catalog (jw_id,title,year,imdb_id,tmdb_id) "
            "VALUES ('animal','Animal',2014,'tt2996684','wrong')"
        )
        con.execute(
            "INSERT INTO catalog_providers (jw_id,provider_key,active) "
            "VALUES ('animal','youtube_tv',1)"
        )
        con.commit()
        con.close()
        result = {
            "id": 274626,
            "title": "Animal",
            "release_date": "2014-06-17",
            "imdb_id": "tt2996684",
            "genres": [{"id": 53, "name": "Thriller"},
                       {"id": 27, "name": "Horror"}],
        }
        with patch.object(tmdb_module, "is_configured", return_value=True), \
             patch.object(tmdb_module, "lookup", return_value=result):
            self.assertEqual(
                ytv.enrich_tmdb(limit=1),
                {"attempted": 1, "matched": 1, "unmatched": 0, "errors": 0},
            )
        con = sqlite3.connect(self.db)
        saved = con.execute(
            "SELECT tmdb_validated_id,tmdb_genres,tmdb_status FROM catalog "
            "WHERE jw_id='animal'"
        ).fetchone()
        con.close()
        self.assertEqual(saved, ("274626", '["Thriller", "Horror"]', "matched"))


class GenreNormalizationTests(unittest.TestCase):
    def test_tmdb_genres_take_precedence(self) -> None:
        keys, labels, source = ytv.preferred_genres(
            ["documentation", "drama"], ["Action"], ["Thriller", "Horror"]
        )
        self.assertEqual(keys, ["horror", "thriller"])
        self.assertEqual(labels, ["Horror", "Mystery & Thriller"])
        self.assertEqual(source, "tmdb")

    def test_rt_genres_take_precedence_over_justwatch(self) -> None:
        keys, labels, source = ytv.preferred_genres(
            ["documentation", "drama"], ["Action", "Sci-Fi"], []
        )
        self.assertEqual(keys, ["action", "scifi"])
        self.assertEqual(labels, ["Action & Adventure", "Science-Fiction"])
        self.assertEqual(source, "rt")

    def test_justwatch_genres_are_the_final_fallback(self) -> None:
        keys, labels, source = ytv.preferred_genres(["drama", "horror"], [], [])
        self.assertEqual(keys, ["drama", "horror"])
        self.assertEqual(labels, ["Drama", "Horror"])
        self.assertEqual(source, "justwatch")

    def test_custom_rt_categories_remain_distinct(self) -> None:
        keys, labels = ytv.canonical_genres(
            [], ["Faith & Spirituality", "Holiday", "LGBTQ+"]
        )
        self.assertEqual(keys, ["faith_spirituality", "holiday", "lgbtq"])
        self.assertEqual(labels, ["Faith & Spirituality", "Holiday", "LGBTQ+"])

    def test_unmapped_rt_values_do_not_create_filter_categories(self) -> None:
        self.assertEqual(
            ytv.canonical_genres([], ["Entertainment", "Health & Wellness"]),
            ([], []),
        )

    def test_every_mapping_targets_a_declared_genre(self) -> None:
        self.assertLessEqual(set(ytv.RT_TO_CANONICAL_GENRE.values()),
                             set(ytv.GENRE_LABELS))


class RottenTomatoesMatchingTests(unittest.TestCase):
    def test_rejects_first_result_fallback(self) -> None:
        with patch.object(rt, "search", return_value=[{"title": "It Follows", "year": 2014, "slug": "it_follows"}]), \
             patch.object(rt, "movie") as movie:
            self.assertIsNone(rt.lookup("It", 2017))
            movie.assert_not_called()

    def test_requires_year_when_search_provides_one(self) -> None:
        candidates = [
            {"title": "Dune", "year": 1984, "slug": "dune_1984"},
            {"title": "Dune", "year": 2021, "slug": "dune_2021"},
        ]
        scorecard = {"slug": "dune_2021", "title": "Dune", "year": "2021"}
        with patch.object(rt, "search", return_value=candidates), \
             patch.object(rt, "movie", return_value=scorecard) as movie:
            self.assertEqual(rt.lookup("Dune", 2021), scorecard)
            movie.assert_called_once_with("dune_2021")

    def test_rejects_scorecard_with_different_title(self) -> None:
        candidate = {"title": "Animal", "year": 2014, "slug": "wrong_slug"}
        scorecard = {"title": "Animal Crackers", "year": "2014"}
        with patch.object(rt, "search", return_value=[candidate]), \
             patch.object(rt, "movie", return_value=scorecard):
            self.assertIsNone(rt.lookup("Animal", 2014))

    def test_rejects_scorecard_with_different_year(self) -> None:
        candidate = {"title": "Animal", "year": 2014, "slug": "animal"}
        scorecard = {"title": "Animal", "year": "2023"}
        with patch.object(rt, "search", return_value=[candidate]), \
             patch.object(rt, "movie", return_value=scorecard):
            self.assertIsNone(rt.lookup("Animal", 2014))

    def test_retries_search_with_title_and_year(self) -> None:
        candidate = {"title": "Animal", "year": 2014, "slug": "animal_2014"}
        scorecard = {"title": "Animal", "year": "2014"}
        with patch.object(rt, "search", side_effect=[[], [candidate]]) as search, \
             patch.object(rt, "movie", return_value=scorecard):
            self.assertEqual(rt.lookup("Animal", 2014), scorecard)
            self.assertEqual(
                [call.args[0] for call in search.call_args_list],
                ["Animal", "Animal 2014"],
            )


class TMDbMatchingTests(unittest.TestCase):
    def test_uses_tmdb_id_and_validates_imdb_identity(self) -> None:
        details = {
            "id": 274626, "title": "Animal", "original_title": "Animal",
            "release_date": "2014-06-17", "imdb_id": "tt2996684",
            "genres": [{"id": 53, "name": "Thriller"}],
        }
        with patch.object(tmdb_module, "movie", return_value=details) as movie:
            self.assertEqual(
                tmdb_module.lookup("Animal", 2014, "tt2996684", "274626"),
                details,
            )
            movie.assert_called_once_with("274626")

    def test_uses_imdb_find_when_tmdb_id_is_unavailable(self) -> None:
        found = {
            "movie_results": [{
                "id": 274626, "title": "Animal", "original_title": "Animal",
                "release_date": "2014-06-17",
            }]
        }
        details = {
            "id": 274626, "title": "Animal", "original_title": "Animal",
            "release_date": "2014-06-17", "imdb_id": "tt2996684",
            "genres": [{"id": 53, "name": "Thriller"}],
        }
        with patch.object(tmdb_module, "_get", side_effect=[found, details]) as get:
            self.assertEqual(
                tmdb_module.lookup("Animal", 2014, "tt2996684"),
                details,
            )
            self.assertEqual(get.call_args_list[0].args[0], "/find/tt2996684")
            self.assertEqual(get.call_args_list[1].args[0], "/movie/274626")

    def test_falls_back_to_imdb_when_tmdb_id_is_gone(self) -> None:
        response = requests.Response()
        response.status_code = 404
        missing = requests.HTTPError(response=response)
        found = {"movie_results": [{
            "id": 274626, "title": "Animal", "original_title": "Animal",
            "release_date": "2014-06-17",
        }]}
        details = {
            "id": 274626, "title": "Animal", "original_title": "Animal",
            "release_date": "2014-06-17", "imdb_id": "tt2996684",
            "genres": [{"id": 53, "name": "Thriller"}],
        }
        with patch.object(tmdb_module, "movie", side_effect=[missing, details]), \
             patch.object(tmdb_module, "_get", return_value=found):
            self.assertEqual(
                tmdb_module.lookup("Animal", 2014, "tt2996684", "bad-id"),
                details,
            )

    def test_rejects_wrong_raw_tmdb_record(self) -> None:
        wrong = {
            "id": 1497978, "title": "animal.", "original_title": "animal.",
            "release_date": "2025-01-01", "genres": [],
        }
        with patch.object(tmdb_module, "movie", return_value=wrong):
            self.assertIsNone(tmdb_module.lookup("Animal", 2014, tmdb_id="1497978"))


if __name__ == "__main__":
    unittest.main()
