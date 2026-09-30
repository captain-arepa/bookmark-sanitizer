#!/usr/bin/env python3
"""Flatten a Vivaldi/Netscape bookmark export and remove duplicate bookmarks.

The script keeps the first occurrence of each unique URL, traverses the input
tree depth-first, and writes a new HTML file with either a flat list of
bookmarks or optional domain-based folders under a single root folder.
"""

from __future__ import annotations

import argparse
import html
from collections import defaultdict
from dataclasses import dataclass, field
from html.entities import name2codepoint
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urlparse
from typing import Iterable


@dataclass(slots=True)
class BookmarkEntry:
    href: str
    title: str
    hostname: str = ""
    domain: str = ""
    attrs: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class OutputGroupSummary:
    name: str
    count: int


@dataclass(slots=True)
class RunSummary:
    original_count: int
    duplicate_count: int
    resulting_count: int
    group_count: int
    groups: list[OutputGroupSummary]
    generated_file_count: int = 0


class BookmarkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.entries: list[BookmarkEntry] = []
        self._in_anchor = False
        self._anchor_attrs: list[tuple[str, str]] = []
        self._anchor_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._in_anchor = True
            self._anchor_attrs = [(name, value or "") for name, value in attrs]
            self._anchor_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_anchor:
            self._anchor_text_parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._in_anchor:
            codepoint = name2codepoint.get(name)
            if codepoint is not None:
                self._anchor_text_parts.append(chr(codepoint))
            else:
                self._anchor_text_parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._in_anchor:
            try:
                codepoint = (
                    int(name[1:], 16) if name.lower().startswith("x") else int(name)
                )
            except ValueError:
                self._anchor_text_parts.append(f"&#{name};")
            else:
                self._anchor_text_parts.append(chr(codepoint))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._in_anchor:
            return

        attrs = dict(self._anchor_attrs)
        href = html.unescape(attrs.get("HREF", attrs.get("href", ""))).strip()
        title = html.unescape("".join(self._anchor_text_parts)).strip()

        if href and title:
            self.entries.append(
                BookmarkEntry(
                    href=href,
                    title=title,
                    hostname=extract_hostname(href),
                    domain=extract_absolute_domain(href),
                    attrs=self._anchor_attrs.copy(),
                )
            )

        self._in_anchor = False
        self._anchor_attrs = []
        self._anchor_text_parts = []


def dedupe_entries(entries: Iterable[BookmarkEntry]) -> list[BookmarkEntry]:
    seen: set[str] = set()
    deduped: list[BookmarkEntry] = []
    for entry in entries:
        key = entry.href
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def extract_hostname(href: str) -> str:
    parsed = urlparse(href)
    hostname = parsed.hostname or ""
    return hostname.casefold().rstrip(".")


def extract_absolute_domain(href: str) -> str:
    hostname = extract_hostname(href)
    if not hostname:
        return ""

    if hostname.replace(".", "").isdigit():
        return hostname

    labels = hostname.split(".")
    if len(labels) <= 2:
        return hostname

    second_level_suffixes = {
        "ac.uk",
        "co.uk",
        "com.au",
        "com.br",
        "com.cn",
        "com.hk",
        "com.mx",
        "com.my",
        "com.ph",
        "com.pl",
        "com.pt",
        "com.sa",
        "com.sg",
        "com.tr",
        "com.tw",
        "co.id",
        "co.il",
        "co.in",
        "co.jp",
        "co.kr",
        "co.nz",
        "co.za",
        "gov.uk",
        "net.au",
        "org.au",
        "org.uk",
    }
    suffix = ".".join(labels[-2:])
    if suffix in second_level_suffixes and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix


def entry_sort_key(entry: BookmarkEntry, sort_mode: str) -> tuple[str, str, str, str]:
    if sort_mode == "domain":
        return (
            entry.domain.casefold(),
            entry.hostname.casefold(),
            entry.title.casefold(),
            entry.href.casefold(),
        )
    return (
        entry.title.casefold(),
        entry.href.casefold(),
        entry.domain.casefold(),
        entry.hostname.casefold(),
    )


def sort_entries(
    entries: Iterable[BookmarkEntry], sort_mode: str
) -> list[BookmarkEntry]:
    return sorted(entries, key=lambda entry: entry_sort_key(entry, sort_mode))


def grouped_items(entries: Iterable[BookmarkEntry], sort_mode: str) -> tuple[
    list[tuple[str, str, list[BookmarkEntry]]],
    list[BookmarkEntry],
]:
    buckets: dict[str, list[BookmarkEntry]] = defaultdict(list)
    for entry in entries:
        group_key = entry.domain or entry.hostname or entry.href
        buckets[group_key].append(entry)

    folders: list[tuple[str, str, list[BookmarkEntry]]] = []
    unsorted_entries: list[BookmarkEntry] = []
    for domain, domain_entries in buckets.items():
        sorted_domain_entries = sort_entries(domain_entries, sort_mode)
        if len(sorted_domain_entries) >= 2:
            folders.append(("folder", domain, sorted_domain_entries))
        else:
            unsorted_entries.extend(sorted_domain_entries)

    def folder_sort_key(
        item: tuple[str, str, list[BookmarkEntry]],
    ) -> tuple[str, str, str]:
        _, domain, domain_entries = item
        if sort_mode == "domain":
            return (
                domain.casefold(),
                min(entry.hostname.casefold() for entry in domain_entries),
                min(entry.title.casefold() for entry in domain_entries),
            )
        return (
            min(entry.title.casefold() for entry in domain_entries),
            domain.casefold(),
            min(entry.hostname.casefold() for entry in domain_entries),
        )

    return sorted(folders, key=folder_sort_key), sort_entries(
        unsorted_entries, sort_mode
    )


def render_entry(entry: BookmarkEntry, indent: str) -> str:
    attr_parts = []
    seen_href = False
    for name, value in entry.attrs:
        if name.upper() == "HREF":
            seen_href = True
            attr_parts.append(f'HREF="{escape_attr(entry.href)}"')
        else:
            attr_parts.append(f'{name.upper()}="{escape_attr(value)}"')
    if not seen_href:
        attr_parts.insert(0, f'HREF="{escape_attr(entry.href)}"')
    attrs = " ".join(attr_parts)
    return f"{indent}<DT><A {attrs}>{html.escape(entry.title)}</A>"


def escape_attr(value: str) -> str:
    return html.escape(value, quote=True)


def sanitize_filename(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip().casefold())
    sanitized = sanitized.strip("._-")
    return sanitized or "unsorted"


def parse_domain_list(value: str) -> set[str]:
    domains = {
        part.strip().casefold().rstrip(".") for part in value.split("|") if part.strip()
    }
    return domains


def matches_excluded_domain(entry: BookmarkEntry, excluded_domains: set[str]) -> bool:
    if not excluded_domains:
        return False

    for candidate in (entry.hostname, entry.domain):
        if candidate and candidate in excluded_domains:
            return True
        if candidate:
            labels = candidate.split(".")
            for index in range(len(labels) - 1):
                suffix = ".".join(labels[index:])
                if suffix in excluded_domains:
                    return True
    return False


def partition_excluded_entries(
    entries: Iterable[BookmarkEntry], excluded_domains: set[str]
) -> tuple[list[BookmarkEntry], dict[str, list[BookmarkEntry]]]:
    kept: list[BookmarkEntry] = []
    excluded_by_domain: dict[str, list[BookmarkEntry]] = defaultdict(list)

    for entry in entries:
        if matches_excluded_domain(entry, excluded_domains):
            excluded_by_domain[entry.domain or entry.hostname or entry.href].append(
                entry
            )
        else:
            kept.append(entry)

    return kept, excluded_by_domain


def collect_group_summaries(
    entries: Iterable[BookmarkEntry], sort_mode: str, group_mode: str
) -> list[OutputGroupSummary]:
    if group_mode == "none":
        return []

    folders, unsorted_entries = grouped_items(
        sort_entries(entries, sort_mode), sort_mode
    )
    summaries = [
        OutputGroupSummary(name=domain, count=len(domain_entries))
        for _, domain, domain_entries in folders
    ]
    if unsorted_entries:
        summaries.append(
            OutputGroupSummary(name="unsorted", count=len(unsorted_entries))
        )
    return summaries


def build_report(summary: RunSummary, output_mode: str) -> str:
    lines = [
        "Bookmark Report",
        f"- original number of bookmarks: {summary.original_count}",
        f"- duplicate bookmarks deleted: {summary.duplicate_count}",
        f"- resulting number of bookmark groups/folders: {summary.group_count}",
        f"- total number of resulting bookmarks: {summary.resulting_count}",
    ]

    if summary.groups:
        lines.append("- generated bookmark groups:")
        total = summary.resulting_count or 0
        for group in sorted(
            summary.groups, key=lambda item: (-item.count, item.name.casefold())
        ):
            percentage = (group.count / total * 100) if total else 0.0
            lines.append(f"  - {group.name}: {group.count} ({percentage:.2f}%)")
    else:
        lines.append("- generated bookmark groups: none")

    lines.append(f"- output mode: {output_mode}")
    if summary.generated_file_count:
        lines.append(f"- generated output files: {summary.generated_file_count}")

    return "\n".join(lines)


def build_output(entries: Iterable[BookmarkEntry], title: str) -> str:
    sorted_entries = list(entries)
    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- Flattened and deduplicated from a Vivaldi bookmark export. -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        f"<TITLE>{html.escape(title)}</TITLE>",
        f"<H1>{html.escape(title)}</H1>",
        "<DL><p>",
        f'    <DT><H3 ADD_DATE="0" LAST_MODIFIED="0" PERSONAL_TOOLBAR_FOLDER="true">{html.escape(title)}</H3>',
        "    <DL><p>",
    ]

    for entry in sorted_entries:
        lines.append(render_entry(entry, "        "))

    lines.extend(
        [
            "    </DL><p>",
            "</DL><p>",
            "",
        ]
    )
    return "\n".join(lines)


def build_grouped_output(
    entries: Iterable[BookmarkEntry], sort_mode: str, title: str
) -> str:
    sorted_entries = sort_entries(entries, sort_mode)
    folders, unsorted_entries = grouped_items(sorted_entries, sort_mode)
    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- Flattened and deduplicated from a Vivaldi bookmark export. -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        f"<TITLE>{html.escape(title)}</TITLE>",
        f"<H1>{html.escape(title)}</H1>",
        "<DL><p>",
        f'    <DT><H3 ADD_DATE="0" LAST_MODIFIED="0" PERSONAL_TOOLBAR_FOLDER="true">{html.escape(title)}</H3>',
        "    <DL><p>",
    ]

    for _, domain, domain_entries in folders:
        lines.append(
            f'        <DT><H3 ADD_DATE="0" LAST_MODIFIED="0">{html.escape(domain)}</H3>'
        )
        lines.append("        <DL><p>")
        for entry in domain_entries:
            lines.append(render_entry(entry, "            "))
        lines.append("        </DL><p>")

    if unsorted_entries:
        lines.append('        <DT><H3 ADD_DATE="0" LAST_MODIFIED="0">unsorted</H3>')
        lines.append("        <DL><p>")
        for entry in unsorted_entries:
            lines.append(render_entry(entry, "            "))
        lines.append("        </DL><p>")

    lines.extend(
        [
            "    </DL><p>",
            "</DL><p>",
            "",
        ]
    )
    return "\n".join(lines)


def write_flat_output(
    entries: Iterable[BookmarkEntry], target_path: Path, title: str
) -> None:
    target_path.write_text(build_output(entries, title), encoding="utf-8")


def write_grouped_html_output(
    entries: Iterable[BookmarkEntry], target_path: Path, sort_mode: str, title: str
) -> None:
    target_path.write_text(
        build_grouped_output(entries, sort_mode, title), encoding="utf-8"
    )


def write_grouped_outputs(
    entries: Iterable[BookmarkEntry], target_path: Path, sort_mode: str
) -> int:
    target_directory = target_path
    if target_directory.suffix.lower() == ".html":
        target_directory = target_directory.with_suffix("")
    target_directory.mkdir(parents=True, exist_ok=True)

    folders, unsorted_entries = grouped_items(
        sort_entries(entries, sort_mode), sort_mode
    )
    written_files = 0

    for _, domain, domain_entries in folders:
        file_path = target_directory / f"{sanitize_filename(domain)}.html"
        write_flat_output(domain_entries, file_path, domain)
        written_files += 1

    if unsorted_entries:
        file_path = target_directory / "unsorted.html"
        write_flat_output(unsorted_entries, file_path, "unsorted")
        written_files += 1

    return written_files


def write_excluded_domain_outputs(
    excluded_by_domain: dict[str, list[BookmarkEntry]],
    target_path: Path,
    sort_mode: str,
) -> int:
    target_directory = target_path
    if target_directory.suffix.lower() == ".html":
        target_directory = target_directory.with_suffix("")
    target_directory.mkdir(parents=True, exist_ok=True)

    written_files = 0
    for domain in sorted(excluded_by_domain):
        file_path = target_directory / f"{sanitize_filename(domain)}.html"
        write_flat_output(
            sort_entries(excluded_by_domain[domain], sort_mode), file_path, domain
        )
        written_files += 1

    return written_files


def flatten_bookmarks(
    source_path: Path,
    target_path: Path,
    sort_mode: str,
    group_mode: str,
    excluded_domains: set[str],
    split_excluded_domains: bool,
) -> RunSummary:
    parser = BookmarkParser()
    parser.feed(source_path.read_text(encoding="utf-8"))
    original_count = len(parser.entries)
    entries = dedupe_entries(parser.entries)
    duplicate_count = original_count - len(entries)
    entries, excluded_by_domain = partition_excluded_entries(entries, excluded_domains)
    main_groups = collect_group_summaries(entries, sort_mode, group_mode)

    generated_file_count = 0
    if split_excluded_domains and excluded_by_domain:
        excluded_target = (
            target_path.with_suffix("")
            if target_path.suffix.lower() == ".html"
            else target_path
        )
        excluded_target = excluded_target.parent / f"{excluded_target.name}_excluded"
        generated_file_count = write_excluded_domain_outputs(
            excluded_by_domain,
            excluded_target,
            sort_mode,
        )

    if group_mode == "split":
        written_files = write_grouped_outputs(entries, target_path, sort_mode)
        return RunSummary(
            original_count=original_count,
            duplicate_count=duplicate_count,
            resulting_count=len(entries),
            group_count=len(main_groups),
            groups=main_groups,
            generated_file_count=written_files + generated_file_count,
        )

    if group_mode == "single":
        write_grouped_html_output(entries, target_path, sort_mode, "Bookmarks")
        return RunSummary(
            original_count=original_count,
            duplicate_count=duplicate_count,
            resulting_count=len(entries),
            group_count=len(main_groups),
            groups=main_groups,
            generated_file_count=1 + generated_file_count,
        )

    write_flat_output(entries, target_path, "Bookmarks")
    return RunSummary(
        original_count=original_count,
        duplicate_count=duplicate_count,
        resulting_count=len(entries),
        group_count=len(main_groups),
        groups=main_groups,
        generated_file_count=generated_file_count,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flatten and deduplicate a Vivaldi bookmark export."
    )
    parser.add_argument(
        "source",
        nargs="?",
        default="bookmarks_9_28_26.html",
        help="Input bookmark export HTML",
    )
    parser.add_argument(
        "output",
        nargs="?",
        default="bookmarks_9_28_26_flattened.html",
        help="Output flattened HTML",
    )
    parser.add_argument(
        "--sort",
        choices=("alphabetic", "domain"),
        default="alphabetic",
        help="Sort bookmarks by title or by domain/subdomain",
    )
    output_modes = parser.add_mutually_exclusive_group()
    output_modes.add_argument(
        "--group-domains",
        action="store_true",
        help="Create a single HTML file with domain folders and an unsorted folder",
    )
    output_modes.add_argument(
        "--split-domains",
        action="store_true",
        help="Create one HTML file per domain group and an unsorted.html file",
    )
    parser.add_argument(
        "--exclude-domains",
        default="",
        help="Pipe-separated domain list to exclude, for example youtube.com|x.com",
    )
    parser.add_argument(
        "--split-excluded-domains",
        action="store_true",
        help="Write one HTML file per excluded domain alongside the main output",
    )
    report_outputs = parser.add_mutually_exclusive_group()
    report_outputs.add_argument(
        "--report",
        action="store_true",
        help="Print a bookmark summary report to the terminal",
    )
    report_outputs.add_argument(
        "--report-file",
        help="Write the bookmark summary report to a file",
    )
    args = parser.parse_args()

    source_path = Path(args.source)
    target_path = Path(args.output)
    group_mode = (
        "single" if args.group_domains else "split" if args.split_domains else "none"
    )
    excluded_domains = parse_domain_list(args.exclude_domains)
    summary = flatten_bookmarks(
        source_path,
        target_path,
        args.sort,
        group_mode,
        excluded_domains,
        args.split_excluded_domains,
    )
    if group_mode == "split":
        output_location = (
            target_path.with_suffix("")
            if target_path.suffix.lower() == ".html"
            else target_path
        )
        print(
            f"Parsed {summary.original_count} bookmarks, wrote {summary.resulting_count} unique bookmarks into {summary.generated_file_count} html files under {output_location}"
        )
    elif group_mode == "single":
        print(
            f"Parsed {summary.original_count} bookmarks, wrote {summary.resulting_count} unique bookmarks into grouped HTML at {target_path}"
        )
    else:
        print(
            f"Parsed {summary.original_count} bookmarks, wrote {summary.resulting_count} unique bookmarks to {target_path}"
        )

    report_requested = args.report or args.report_file is not None
    if report_requested:
        report_text = build_report(summary, group_mode)
        if args.report_file:
            Path(args.report_file).write_text(report_text + "\n", encoding="utf-8")
        else:
            print()
            print(report_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
