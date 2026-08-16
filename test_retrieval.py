import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rt
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
        saved = con.execute(
            "SELECT active,jw_tomatometer,jw_synopsis,jw_genres,jw_poster "
            "FROM catalog WHERE jw_id='1'"
        ).fetchone()
        con.close()
        self.assertEqual(saved[:4], (1, 81, "About New", '["drama"]'))
        self.assertTrue(saved[4].startswith("https://images.justwatch.com/"))

    def test_partial_snapshot_rolls_back_and_keeps_active_catalog(self) -> None:
        con = ytv._db()
        con.execute("INSERT INTO catalog (jw_id,title,active) VALUES ('old','Old',1)")
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
        con.commit()
        con.close()
        self.assertEqual(ytv.pending_rating_ids(limit=1), [("b", "Try Now", 2001)])


class GenreNormalizationTests(unittest.TestCase):
    def test_merges_jw_and_mapped_rt_genres_in_canonical_order(self) -> None:
        keys, labels = ytv.canonical_genres(
            ["animation", "drama"], ["Anime", "Biography", "Drama"]
        )
        self.assertEqual(keys, ["animation", "anime", "biography", "drama"])
        self.assertEqual(labels, ["Animation", "Anime", "Biography", "Drama"])

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
        with patch.object(rt, "search", return_value=candidates), \
             patch.object(rt, "movie", return_value={"slug": "dune_2021"}) as movie:
            self.assertEqual(rt.lookup("Dune", 2021), {"slug": "dune_2021"})
            movie.assert_called_once_with("dune_2021")


if __name__ == "__main__":
    unittest.main()
