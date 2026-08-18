import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import rt
import server as server_module
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

    def test_amazon_prime_uses_amp_with_result_window_limit(self) -> None:
        provider = ytv.PROVIDERS["amazon_prime"]
        self.assertEqual(provider.jw_package, "amp")
        self.assertEqual(provider.snapshot_limit, ytv.JW_RESULT_WINDOW_LIMIT)

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

    def test_catalog_api_remains_readable_while_sync_has_writer_lock(self) -> None:
        con = ytv._db()
        con.execute(
            "INSERT INTO catalog (jw_id,title,year) VALUES ('movie','Movie',2000)"
        )
        con.execute(
            "INSERT INTO catalog_providers "
            "(jw_id,provider_key,active,popularity) "
            "VALUES ('movie','youtube_tv',1,0)"
        )
        con.commit()
        con.close()

        writer = sqlite3.connect(self.db)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE catalog SET title='Uncommitted' WHERE jw_id='movie'"
            )

            response = server_module.api_yttv()
        finally:
            writer.rollback()
            writer.close()

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"title":"Movie"', response.body)
        self.assertNotIn(b"Uncommitted", response.body)

    def test_read_connection_rejects_writes(self) -> None:
        con = ytv._db()
        con.close()

        con = ytv._read_db()
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                con.execute(
                    "INSERT INTO meta (key,value) VALUES ('unexpected','write')"
                )
        finally:
            con.close()

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

    def test_popularity_precedes_attempt_state(self) -> None:
        con = ytv._db()
        old = "2000-01-01 00:00:00"
        recent = ytv._now()
        con.executemany(
            "INSERT INTO catalog (jw_id,title,year,rt_last_attempt_at) "
            "VALUES (?,?,2000,?)",
            [("retry", "Retry", old), ("refresh", "Refresh", old),
             ("never", "Never", None), ("recent", "Recent", recent)],
        )
        con.executemany(
            "INSERT INTO catalog_providers "
            "(jw_id,provider_key,active,popularity) VALUES (?,'youtube_tv',1,?)",
            [("retry", 0), ("refresh", 1), ("never", 99), ("recent", 2)],
        )
        con.executemany(
            "INSERT INTO ratings (jw_id,popcornmeter,updated_at) VALUES (?,?,?)",
            [("refresh", 80, old), ("recent", 90, recent)],
        )
        con.commit()
        con.close()
        self.assertEqual(
            ytv.pending_rating_ids(),
            [("retry", "Retry", 2000), ("refresh", "Refresh", 2000),
             ("never", "Never", 2000)],
        )

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

    def test_limited_provider_stops_at_snapshot_limit(self) -> None:
        provider = ytv.Provider("limited", "Limited", "lim", snapshot_limit=3)
        pages = [
            {
                "totalCount": 10,
                "edges": [node("1", "One"), node("2", "Two")],
                "pageInfo": {"hasNextPage": True, "endCursor": "Mg=="},
            },
            {
                "totalCount": 10,
                "edges": [node("3", "Three"), node("4", "Four")],
                "pageInfo": {"hasNextPage": True, "endCursor": "NA=="},
            },
        ]
        with patch.dict(ytv.PROVIDERS, {provider.key: provider}), \
             patch.object(ytv, "_fetch_page", side_effect=pages) as fetch:
            self.assertEqual(ytv.fetch_catalog(provider.key), 3)
        self.assertEqual(fetch.call_count, 2)

        con = sqlite3.connect(self.db)
        saved = con.execute(
            "SELECT jw_id,popularity FROM catalog_providers "
            "WHERE provider_key='limited' ORDER BY popularity"
        ).fetchall()
        meta = dict(con.execute(
            "SELECT key,value FROM meta WHERE key LIKE 'catalog_%:limited'"
        ).fetchall())
        con.close()
        self.assertEqual(saved, [("1", 0), ("2", 1), ("3", 2)])
        self.assertEqual(meta["catalog_total:limited"], "3")
        self.assertEqual(meta["catalog_reported_total:limited"], "10")
        self.assertEqual(meta["catalog_inaccessible_total:limited"], "7")
        self.assertEqual(meta["catalog_snapshot_limit:limited"], "3")

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

    def test_rt_enrichment_persists_scorecard_details(self) -> None:
        con = ytv._db()
        con.execute(
            "INSERT INTO catalog (jw_id,title,year) VALUES ('movie','Movie',2000)"
        )
        con.execute(
            "INSERT INTO catalog_providers (jw_id,provider_key,active,popularity) "
            "VALUES ('movie','youtube_tv',1,0)"
        )
        con.commit()
        con.close()
        result = {
            "title": "Movie", "year": "2000", "tomatometer": 81,
            "popcornmeter": 72, "tomatometer_certified": True,
            "tomatometer_sentiment": "POSITIVE", "audience_score_type": "VERIFIED",
            "critic_average_rating": "7.10", "audience_average_rating": "4.2",
            "audience_sentiment": "POSITIVE", "audience_certified": True,
            "critic_review_count": 100, "audience_review_count": 200,
            "rt_search_title": "Movie", "rt_search_year": 2000,
            "rt_identity_source": "search",
            "genres": ["Drama"], "url": "https://www.rottentomatoes.com/m/movie",
        }
        with patch.object(rt, "lookup", return_value=result):
            self.assertEqual(
                ytv.enrich(limit=1),
                {"attempted": 1, "matched": 1, "unmatched": 0, "errors": 0},
            )
        con = sqlite3.connect(self.db)
        saved = con.execute(
            "SELECT title,popcornmeter,audience_score_type,critic_avg,audience_avg,"
            "audience_sentiment,audience_certified,critic_review_count,"
            "audience_review_count,rt_search_title,rt_search_year,rt_identity_source "
            "FROM ratings WHERE jw_id='movie'"
        ).fetchone()
        con.close()
        self.assertEqual(
            saved, ("Movie", 72, "VERIFIED", "7.10", "4.2", "POSITIVE", 1,
                    100, 200, "Movie", 2000, "search")
        )

    def test_rt_enrichment_reports_progress_every_50_titles(self) -> None:
        work = [(f"movie-{i}", f"Movie {i}", 2000) for i in range(50)]
        with patch.object(ytv, "pending_rating_ids", return_value=work), \
             patch.object(rt, "lookup", return_value=None), \
             patch.object(rt, "rate_limit_retry_count", return_value=3), \
             patch("builtins.print") as output:
            stats = ytv.enrich(limit=50)
        self.assertEqual(stats["attempted"], 50)
        lines = [call.args[0] for call in output.call_args_list]
        self.assertTrue(any("RT progress: 50/50 (100.0%)" in line for line in lines))
        self.assertTrue(any("429 retries=0" in line for line in lines))
        self.assertTrue(any("elapsed=" in line and "eta=" in line for line in lines))

    def test_revalidation_quarantines_identity_mismatches(self) -> None:
        con = ytv._db()
        con.executemany(
            "INSERT INTO catalog (jw_id,title,year) VALUES (?,?,?)",
            [("valid", "Valid", 2000), ("wrong", "Expected", 2001)],
        )
        con.executemany(
            "INSERT INTO catalog_providers "
            "(jw_id,provider_key,active,popularity) VALUES (?,'youtube_tv',1,?)",
            [("valid", 0), ("wrong", 1)],
        )
        con.executemany(
            "INSERT INTO ratings (jw_id,title,year,popcornmeter,rt_url,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("valid", "Valid", 2000, 80,
                 "https://www.rottentomatoes.com/m/valid", ytv._now()),
                ("wrong", "Expected", 2001, 90,
                 "https://www.rottentomatoes.com/m/wrong", ytv._now()),
            ],
        )
        con.commit()
        con.close()

        scorecards = {
            "valid": {"title": "Valid", "year": "2000", "popcornmeter": 81,
                      "url": "https://www.rottentomatoes.com/m/valid"},
            "wrong": {"title": "Different", "year": "2005", "popcornmeter": 91,
                      "url": "https://www.rottentomatoes.com/m/wrong"},
        }
        with patch.object(rt, "cached_movie", side_effect=scorecards.get), \
             patch.object(rt, "movie") as live_movie:
            self.assertEqual(
                ytv.revalidate_ratings(),
                {"checked": 2, "validated": 1, "invalid": 1,
                 "restored": 0, "errors": 0},
            )
        live_movie.assert_not_called()

        con = sqlite3.connect(self.db)
        remaining = con.execute(
            "SELECT jw_id,popcornmeter FROM ratings ORDER BY jw_id"
        ).fetchall()
        quarantine = con.execute(
            "SELECT jw_id,reason FROM rating_quarantine"
        ).fetchone()
        status = con.execute(
            "SELECT rt_status,rt_last_attempt_at FROM catalog WHERE jw_id='wrong'"
        ).fetchone()
        con.close()
        self.assertEqual(remaining, [("valid", 81)])
        self.assertEqual(quarantine[0], "wrong")
        self.assertIn("Different", quarantine[1])
        self.assertEqual(status, ("invalid", None))

    def test_revalidation_preserves_trusted_search_year_identity(self) -> None:
        con = ytv._db()
        con.execute(
            "INSERT INTO catalog (jw_id,title,year) VALUES ('movie','Movie',2014)"
        )
        con.execute(
            "INSERT INTO catalog_providers (jw_id,provider_key,active,popularity) "
            "VALUES ('movie','youtube_tv',1,0)"
        )
        con.execute(
            """INSERT INTO ratings (
                   jw_id,title,year,rt_url,rt_search_title,rt_search_year,
                   rt_identity_source
               ) VALUES ('movie','Movie',2023,?,'Movie',2014,'search')""",
            ("https://www.rottentomatoes.com/m/movie",),
        )
        con.commit()
        con.close()
        scorecard = {
            "title": "Movie", "year": "2023",
            "url": "https://www.rottentomatoes.com/m/movie",
        }
        with patch.object(rt, "cached_movie", return_value=scorecard):
            stats = ytv.revalidate_ratings()
        self.assertEqual(stats["validated"], 1)
        self.assertEqual(stats["invalid"], 0)
        con = sqlite3.connect(self.db)
        saved = con.execute(
            "SELECT rt_search_year,rt_identity_source FROM ratings "
            "WHERE jw_id='movie'"
        ).fetchone()
        con.close()
        self.assertEqual(saved, (2014, "search"))


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
    def test_counts_retried_rate_limit_responses(self) -> None:
        response = requests.Response()
        response.status_code = 200
        response.raw = type("Raw", (), {
            "retries": type("Retries", (), {
                "history": [type("Retry", (), {"status": 429})(),
                            type("Retry", (), {"status": 503})(),
                            type("Retry", (), {"status": 429})()]
            })()
        })()
        with patch.object(rt, "_rate_limit_retries", 0), \
             patch.object(rt, "_throttle"), \
             patch.object(rt._HTTP, "get", return_value=response):
            rt._get("https://example.test")
            self.assertEqual(rt.rate_limit_retry_count(), 2)

    def test_movie_parses_media_scorecard_json(self) -> None:
        response = requests.Response()
        response.status_code = 200
        response._content = b'''<html><body>
          <script id="media-hero-json" type="application/json">
            {"content":{"title":"Movie","metadataProps":["PG-13","2000","2h 1m"],
             "metadataGenres":["Drama"],"posterSrc":"poster.jpg"}}
          </script>
          <script id="media-scorecard-json" type="application/json">
            {"criticsScore":{"score":"81","averageRating":"7.10",
             "reviewCount":100,"certified":true,"sentiment":"POSITIVE"},
             "audienceScore":{"score":"72","averageRating":"4.2",
             "reviewCount":200,"scoreType":"VERIFIED","certified":true,
             "sentiment":"POSITIVE"}}
          </script>
          <script id="where-to-watch-json" type="application/json">
            {"affiliatesText":"Watch Movie with a subscription."}
          </script>
          <rt-text data-qa="synopsis-value">A synopsis.</rt-text>
        </body></html>'''
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(rt, "CACHE_DB", str(Path(temp) / "cache.db")), \
             patch.object(rt, "_get", return_value=response):
            result = rt.movie("movie")
        self.assertEqual(result["tomatometer"], 81)
        self.assertEqual(result["popcornmeter"], 72)
        self.assertEqual(result["critic_average_rating"], "7.10")
        self.assertEqual(result["audience_average_rating"], "4.2")
        self.assertEqual(result["audience_score_type"], "VERIFIED")
        self.assertTrue(result["audience_certified"])
        self.assertEqual(result["critic_review_count"], 100)
        self.assertEqual(result["audience_review_count"], 200)
        self.assertEqual(
            result["where_to_watch"], "Watch Movie with a subscription."
        )

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
            result = rt.lookup("Dune", 2021)
            self.assertEqual(result["slug"], "dune_2021")
            self.assertEqual(result["rt_identity_source"], "scorecard")
            movie.assert_called_once_with("dune_2021")

    def test_rejects_scorecard_with_different_title(self) -> None:
        candidate = {"title": "Animal", "year": 2014, "slug": "wrong_slug"}
        scorecard = {"title": "Animal Crackers", "year": "2014"}
        with patch.object(rt, "search", return_value=[candidate]), \
             patch.object(rt, "movie", return_value=scorecard):
            self.assertIsNone(rt.lookup("Animal", 2014))

    def test_rejects_when_nearby_search_year_links_to_distant_page_year(self) -> None:
        candidate = {"title": "Animal", "year": 2013, "slug": "animal"}
        scorecard = {"title": "Animal", "year": "2023"}
        with patch.object(rt, "search", return_value=[candidate]), \
             patch.object(rt, "movie", return_value=scorecard):
            self.assertIsNone(rt.lookup("Animal", 2014))

    def test_trusts_exact_search_year_when_page_year_conflicts(self) -> None:
        candidate = {"title": "Animal", "year": 2014, "slug": "animal"}
        scorecard = {"title": "Animal", "year": "2023"}
        with patch.object(rt, "search", return_value=[candidate]), \
             patch.object(rt, "movie", return_value=scorecard):
            result = rt.lookup("Animal", 2014)
            self.assertEqual(result["rt_identity_source"], "search")
            self.assertEqual(result["rt_search_year"], 2014)

    def test_accepts_one_year_release_convention_difference(self) -> None:
        candidate = {"title": "Coherence", "year": 2013, "slug": "coherence"}
        scorecard = {"title": "Coherence", "year": "2013"}
        with patch.object(rt, "search", return_value=[candidate]), \
             patch.object(rt, "movie", return_value=scorecard):
            result = rt.lookup("Coherence", 2014)
            self.assertEqual(result["title"], "Coherence")
            self.assertEqual(result["rt_identity_source"], "scorecard")
            self.assertEqual(result["rt_search_year"], 2013)

    def test_retries_search_with_title_and_year(self) -> None:
        candidate = {"title": "Animal", "year": 2014, "slug": "animal_2014"}
        scorecard = {"title": "Animal", "year": "2014"}
        with patch.object(rt, "search", side_effect=[[], [candidate]]) as search, \
             patch.object(rt, "movie", return_value=scorecard):
            self.assertEqual(rt.lookup("Animal", 2014)["title"], "Animal")
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
