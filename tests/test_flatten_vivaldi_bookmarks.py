import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import flatten_vivaldi_bookmarks as bookmarks


class BookmarkParserTests(unittest.TestCase):
    def test_decodes_entities_and_recovers_nested_and_unclosed_anchors(self) -> None:
        parser = bookmarks.BookmarkParser()
        parser.feed(
            '<A HREF="https://one.example" ICON="icon">Cafe &amp; '
            '&#9731; &#x1F40D;</A>'
            '<A HREF="https://two.example">First'
            '<A HREF="https://three.example">Third &#999999999;</A>'
            '<A HREF="https://four.example">終'
        )
        parser.close()

        self.assertEqual(
            [(entry.href, entry.title) for entry in parser.entries],
            [
                ("https://one.example", "Cafe & ☃ 🐍"),
                ("https://two.example", "First"),
                ("https://three.example", "Third &#999999999;"),
                ("https://four.example", "終"),
            ],
        )
        self.assertIn(("icon", "icon"), parser.entries[0].attrs)


class UrlNormalizationTests(unittest.TestCase):
    def test_normalizes_authority_without_merging_distinct_urls(self) -> None:
        self.assertEqual(
            bookmarks.normalize_url_for_dedupe("HTTP://Example.COM:80/"),
            "http://example.com",
        )
        self.assertEqual(
            bookmarks.normalize_url_for_dedupe("https://EXAMPLE.com:443/a/"),
            "https://example.com/a/",
        )
        self.assertEqual(
            bookmarks.normalize_url_for_dedupe("https://example.com/a?q=1#x"),
            "https://example.com/a?q=1#x",
        )
        self.assertNotEqual(
            bookmarks.normalize_url_for_dedupe("http://example.com"),
            bookmarks.normalize_url_for_dedupe("https://example.com"),
        )
        self.assertEqual(
            bookmarks.normalize_url_for_dedupe("http://["),
            "http://[",
        )

    def test_deduplicates_normalized_urls_and_keeps_first_entry(self) -> None:
        entries = [
            bookmarks.BookmarkEntry("HTTP://Example.COM:80/", "first"),
            bookmarks.BookmarkEntry("http://example.com", "duplicate"),
            bookmarks.BookmarkEntry("https://example.com/", "https"),
            bookmarks.BookmarkEntry("https://example.com/#fragment", "fragment"),
        ]

        result = bookmarks.dedupe_entries(entries)

        self.assertEqual(
            [entry.title for entry in result], ["first", "https", "fragment"]
        )


class DomainTests(unittest.TestCase):
    def test_public_suffix_exact_wildcard_exception_and_ip_rules(self) -> None:
        rules = (
            {"com", "co.uk", "uk", "jp"},
            {"github.io", "kawasaki.jp"},
            {"city.kawasaki.jp"},
        )
        with patch.object(bookmarks, "load_public_suffix_rules", return_value=rules):
            self.assertEqual(
                bookmarks.extract_absolute_domain("https://www.example.co.uk"),
                "example.co.uk",
            )
            self.assertEqual(
                bookmarks.extract_absolute_domain("https://foo.github.io"),
                "foo.github.io",
            )
            self.assertEqual(
                bookmarks.extract_absolute_domain("https://www.city.kawasaki.jp"),
                "city.kawasaki.jp",
            )
            self.assertEqual(
                bookmarks.extract_absolute_domain("http://192.168.1.1"),
                "192.168.1.1",
            )
            self.assertEqual(
                bookmarks.extract_absolute_domain("http://[2001:db8::1]"),
                "2001:db8::1",
            )
            self.assertEqual(
                bookmarks.extract_absolute_domain("mailto:test@example.com"), ""
            )
            self.assertEqual(bookmarks.extract_absolute_domain("chrome://settings"), "")


class GroupingAndExclusionTests(unittest.TestCase):
    def test_sorting_uses_requested_title_or_domain_order(self) -> None:
        entries = [
            bookmarks.BookmarkEntry(
                "https://b.example/z", "Zulu", "b.example", "example"
            ),
            bookmarks.BookmarkEntry(
                "https://a.example/a", "Alpha", "a.example", "example"
            ),
        ]

        self.assertEqual(
            [entry.title for entry in bookmarks.sort_entries(entries, "alphabetic")],
            ["Alpha", "Zulu"],
        )
        self.assertEqual(
            [entry.hostname for entry in bookmarks.sort_entries(entries, "domain")],
            ["a.example", "b.example"],
        )

    def test_exclusion_matches_subdomains_and_report_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.html"
            target = Path(directory) / "nested" / "output.html"
            source.write_text(
                '<A HREF="https://youtube.com/a">Video</A>'
                '<A HREF="https://www.youtube.com/b">Channel</A>'
                '<A HREF="https://example.com/">Site</A>',
                encoding="utf-8",
            )

            summary = bookmarks.flatten_bookmarks(
                source, target, "alphabetic", "none", {"youtube.com"}, False
            )

            self.assertEqual(summary.original_count, 3)
            self.assertEqual(summary.excluded_count, 2)
            self.assertEqual(summary.resulting_count, 1)
            report = bookmarks.build_report(summary, "none")
            self.assertIn(
                "3 original = 0 duplicates + 2 excluded + 1 resulting (matched)",
                report,
            )
            self.assertIn("youtube.com: 2 (100.00%)", report)
            self.assertIn("https://example.com/", target.read_text(encoding="utf-8"))
            self.assertNotIn("youtube.com", target.read_text(encoding="utf-8"))

    def test_atomic_writer_creates_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "output.html"

            bookmarks.write_text_atomic(target, "complete output")

            self.assertEqual(target.read_text(encoding="utf-8"), "complete output")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_cli_rejects_source_and_output_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bookmarks.html"
            source.write_text("<DL></DL>", encoding="utf-8")
            stderr = io.StringIO()
            with patch.object(
                sys,
                "argv",
                ["flatten_vivaldi_bookmarks.py", str(source), str(source)],
            ):
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as result:
                    bookmarks.main()

            self.assertEqual(result.exception.code, 2)
            self.assertIn(
                "source and output paths must be different", stderr.getvalue()
            )

    def test_split_output_names_do_not_collide_or_overwrite_unsorted(self) -> None:
        entries = [
            bookmarks.BookmarkEntry("https://one.example/1", "one", "", "foo bar"),
            bookmarks.BookmarkEntry("https://one.example/2", "two", "", "foo bar"),
            bookmarks.BookmarkEntry("https://two.example/1", "three", "", "foo@bar"),
            bookmarks.BookmarkEntry("https://two.example/2", "four", "", "foo@bar"),
            bookmarks.BookmarkEntry("https://unsorted-domain.example/1", "five", "", "unsorted"),
            bookmarks.BookmarkEntry("https://unsorted-domain.example/2", "six", "", "unsorted"),
            bookmarks.BookmarkEntry("https://single.example/", "singleton"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "split.html"
            count = bookmarks.write_grouped_outputs(entries, target, "alphabetic")
            output_dir = target.with_suffix("")
            html_files = sorted(output_dir.glob("*.html"))

            self.assertEqual(count, 4)
            self.assertEqual(len(html_files), 4)
            self.assertEqual(len({path.name for path in html_files}), 4)
            self.assertEqual(
                sum("one</A>" in path.read_text(encoding="utf-8") for path in html_files),
                1,
            )
            self.assertIn(
                "singleton</A>",
                (output_dir / "unsorted.html").read_text(encoding="utf-8"),
            )


class CliTests(unittest.TestCase):
    def test_existing_outputs_require_overwrite_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.html"
            target = Path(directory) / "output.html"
            source.write_text(
                '<A HREF="https://example.com/">Bookmark</A>', encoding="utf-8"
            )
            target.write_text("keep me", encoding="utf-8")
            stderr = io.StringIO()

            with patch.object(
                sys, "argv", ["flatten_vivaldi_bookmarks.py", str(source), str(target)]
            ):
                with redirect_stdout(io.StringIO()), redirect_stderr(
                    stderr
                ), self.assertRaises(SystemExit) as result:
                    bookmarks.main()

            self.assertEqual(result.exception.code, 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep me")
            self.assertIn("--overwrite", stderr.getvalue())

            with patch.object(
                sys,
                "argv",
                [
                    "flatten_vivaldi_bookmarks.py",
                    str(source),
                    str(target),
                    "--overwrite",
                    "--quiet",
                ],
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(bookmarks.main(), 0)
            self.assertIn(
                'HREF="https://example.com/"',
                target.read_text(encoding="utf-8"),
            )

    def test_dry_run_previews_paths_without_creating_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.html"
            target = Path(directory) / "new" / "output.html"
            report = Path(directory) / "new" / "report.txt"
            source.write_text(
                '<A HREF="https://example.com/">Bookmark</A>', encoding="utf-8"
            )
            stdout = io.StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "flatten_vivaldi_bookmarks.py",
                    str(source),
                    str(target),
                    "--dry-run",
                    "--report-file",
                    str(report),
                    "--verbose",
                ],
            ), redirect_stdout(stdout):
                self.assertEqual(bookmarks.main(), 0)

            self.assertFalse(target.exists())
            self.assertFalse(report.exists())
            self.assertIn(str(target), stdout.getvalue())
            self.assertIn(str(report), stdout.getvalue())
            self.assertIn("No files written", stdout.getvalue())
            self.assertIn("Sort: alphabetic", stdout.getvalue())

    def test_version_and_verbosity_option_validation(self) -> None:
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["flatten_vivaldi_bookmarks.py", "--version"]):
            with redirect_stdout(stdout), self.assertRaises(SystemExit) as result:
                bookmarks.main()
        self.assertEqual(result.exception.code, 0)
        self.assertIn(bookmarks.__version__, stdout.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.html"
            source.write_text("", encoding="utf-8")
            stderr = io.StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "flatten_vivaldi_bookmarks.py",
                    str(source),
                    str(Path(directory) / "output.html"),
                    "--verbose",
                    "--quiet",
                ],
            ):
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as result:
                    bookmarks.main()
            self.assertEqual(result.exception.code, 2)
            self.assertIn("not allowed with argument", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
