#!/usr/bin/env python3
"""Flatten a Vivaldi/Netscape bookmark export and remove duplicate bookmarks.

The script keeps the first occurrence of each unique URL, traverses the input
tree depth-first, and writes a new HTML file with either a flat list of
bookmarks or optional domain-based folders under a single root folder.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from html.entities import name2codepoint
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit, urlparse
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
    excluded_count: int = 0
    excluded_groups: list[OutputGroupSummary] = field(default_factory=list)


class BookmarkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.entries: list[BookmarkEntry] = []
        self._in_anchor = False
        self._anchor_attrs: list[tuple[str, str]] = []
        self._anchor_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            if self._in_anchor:
                self._finish_anchor()
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
                    int(name[1:], 16)
                    if name.lower().startswith("x")
                    else int(name, 10)
                )
            except (ValueError, OverflowError):
                self._anchor_text_parts.append(f"&#{name};")
            else:
                try:
                    self._anchor_text_parts.append(chr(codepoint))
                except (ValueError, OverflowError):
                    self._anchor_text_parts.append(f"&#{name};")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._in_anchor:
            return
        self._finish_anchor()

    def _finish_anchor(self) -> None:
        attrs = dict(self._anchor_attrs)
        href = html.unescape(attrs.get("HREF", attrs.get("href", ""))).strip()
        title = "".join(self._anchor_text_parts).strip()

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

    def close(self) -> None:
        super().close()
        if self._in_anchor:
            self._finish_anchor()


def normalize_url_for_dedupe(href: str) -> str:
    stripped = href.strip()
    try:
        parsed = urlsplit(stripped)
    except ValueError:
        return stripped.casefold()

    scheme = parsed.scheme.casefold()
    if not scheme:
        return stripped.casefold()

    hostname = parsed.hostname
    if hostname is None:
        return stripped.casefold()

    hostname = hostname.casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return stripped.casefold()

    userinfo = ""
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo += ":" + parsed.password
        userinfo += "@"

    if ":" in hostname and not hostname.startswith("["):
        host = f"[{hostname}]"
    else:
        host = hostname

    default_ports = {
        "ftp": 21,
        "http": 80,
        "https": 443,
        "ws": 80,
        "wss": 443,
    }
    if port is not None and port != default_ports.get(scheme):
        host = f"{host}:{port}"

    netloc = userinfo + host
    path = parsed.path
    if path == "/" and not parsed.query and not parsed.fragment:
        path = ""

    return urlunsplit((scheme, netloc, path, parsed.query, parsed.fragment))


def dedupe_entries(entries: Iterable[BookmarkEntry]) -> list[BookmarkEntry]:
    seen: set[str] = set()
    deduped: list[BookmarkEntry] = []
    for entry in entries:
        key = normalize_url_for_dedupe(entry.href)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def extract_hostname(href: str) -> str:
    parsed = urlparse(href)
    if parsed.scheme.casefold() not in {"ftp", "http", "https", "ws", "wss"}:
        return ""
    hostname = parsed.hostname or ""
    return hostname.casefold().rstrip(".")


@lru_cache(maxsize=1)
def load_public_suffix_rules() -> tuple[set[str], set[str], set[str]]:
    exact_rules: set[str] = set()
    wildcard_rules: set[str] = set()
    exception_rules: set[str] = set()
    data_paths = (
        Path("/usr/share/publicsuffix/public_suffix_list.dat"),
        Path("/usr/local/share/publicsuffix/public_suffix_list.dat"),
    )
    data_path = next((path for path in data_paths if path.is_file()), None)
    if data_path is None:
        return exact_rules, wildcard_rules, exception_rules

    try:
        lines = data_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return exact_rules, wildcard_rules, exception_rules

    for line in lines:
        rule = line.strip()
        if not rule or rule.startswith("//"):
            continue
        if rule.startswith("!"):
            exception_rules.add(rule[1:].casefold())
        elif rule.startswith("*."):
            wildcard_rules.add(rule[2:].casefold())
        else:
            exact_rules.add(rule.casefold())

    return exact_rules, wildcard_rules, exception_rules


def extract_absolute_domain(href: str) -> str:
    hostname = extract_hostname(href)
    if not hostname:
        return ""

    try:
        return str(ipaddress.ip_address(hostname))
    except ValueError:
        pass

    exact_rules, wildcard_rules, exception_rules = load_public_suffix_rules()
    labels = hostname.split(".")

    matching_exception = ""
    for rule in exception_rules:
        if hostname == rule or hostname.endswith("." + rule):
            if len(rule.split(".")) > len(matching_exception.split(".")):
                matching_exception = rule
    if matching_exception:
        public_suffix = ".".join(matching_exception.split(".")[1:])
    else:
        public_suffix = ""
        for rule in exact_rules:
            if hostname == rule or hostname.endswith("." + rule):
                if len(rule.split(".")) > len(public_suffix.split(".")):
                    public_suffix = rule

        for base in wildcard_rules:
            if hostname == base or hostname.endswith("." + base):
                candidate_labels = len(base.split(".")) + 1
                candidate = ".".join(labels[-candidate_labels:])
                if len(candidate.split(".")) > len(public_suffix.split(".")):
                    public_suffix = candidate

    if not public_suffix:
        public_suffix = labels[-1]

    if hostname == public_suffix:
        return hostname

    suffix_labels = public_suffix.split(".")
    registered_label_count = len(suffix_labels) + 1
    if len(labels) >= registered_label_count:
        return ".".join(labels[-registered_label_count:])
    return hostname


def entry_sort_key(entry: BookmarkEntry, sort_mode: str) -> tuple[str, str, str, str]:
    if sort_mode == "domain":
        return (
            entry.domain.casefold(),
            entry.hostname.casefold(),
            entry.title.casefold(),
            normalize_url_for_dedupe(entry.href),
        )
    return (
        entry.title.casefold(),
        normalize_url_for_dedupe(entry.href),
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


def allocate_domain_filename(
    domain: str,
    used_names: dict[str, str],
    reserved_names: set[str] | None = None,
) -> str:
    base_name = sanitize_filename(domain)
    file_name = f"{base_name}.html"
    reserved_names = reserved_names or set()

    existing_domain = used_names.get(file_name)
    if existing_domain is not None:
        if existing_domain == domain:
            return file_name
        digest = hashlib.sha1(domain.encode("utf-8")).hexdigest()[:8]
        file_name = f"{base_name}-{digest}.html"
    collision_index = 2
    while file_name in used_names or file_name in reserved_names:
        existing_domain = used_names.get(file_name)
        if existing_domain == domain:
            break
        digest = hashlib.sha1(domain.encode("utf-8")).hexdigest()[:8]
        file_name = f"{base_name}-{digest}-{collision_index}.html"
        collision_index += 1

    used_names[file_name] = domain
    return file_name


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
        f"- excluded bookmarks deleted: {summary.excluded_count}",
        f"- resulting number of bookmark groups/folders: {summary.group_count}",
        f"- total number of resulting bookmarks: {summary.resulting_count}",
    ]

    reconciliation = (
        summary.duplicate_count
        + summary.excluded_count
        + summary.resulting_count
    )
    lines.append(
        f"- bookmark reconciliation: "
        f"{summary.original_count} original = "
        f"{summary.duplicate_count} duplicates + "
        f"{summary.excluded_count} excluded + "
        f"{summary.resulting_count} resulting "
        f"({'matched' if reconciliation == summary.original_count else 'mismatch'})"
    )

    if summary.excluded_groups:
        lines.append("- excluded bookmark domains:")
        total = summary.excluded_count or 0
        for group in sorted(
            summary.excluded_groups,
            key=lambda item: (-item.count, item.name.casefold()),
        ):
            percentage = (group.count / total * 100) if total else 0.0
            lines.append(f"  - {group.name}: {group.count} ({percentage:.2f}%)")

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


def write_text_atomic(target_path: Path, text: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target_path.parent,
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        temp_path.replace(target_path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def write_flat_output(
    entries: Iterable[BookmarkEntry], target_path: Path, title: str
) -> None:
    write_text_atomic(target_path, build_output(entries, title))


def write_grouped_html_output(
    entries: Iterable[BookmarkEntry], target_path: Path, sort_mode: str, title: str
) -> None:
    write_text_atomic(
        target_path, build_grouped_output(entries, sort_mode, title)
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
    used_names: dict[str, str] = {}
    reserved_names = {"unsorted.html"}

    for _, domain, domain_entries in folders:
        file_name = allocate_domain_filename(
            domain,
            used_names,
            reserved_names,
        )
        file_path = target_directory / file_name
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
    used_names: dict[str, str] = {}
    for domain in sorted(excluded_by_domain):
        file_name = allocate_domain_filename(
            domain,
            used_names,
        )
        file_path = target_directory / file_name
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
    excluded_groups = [
        OutputGroupSummary(name=domain, count=len(domain_entries))
        for domain, domain_entries in excluded_by_domain.items()
    ]
    excluded_count = sum(len(domain_entries) for domain_entries in excluded_by_domain.values())

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
            excluded_count=excluded_count,
            excluded_groups=excluded_groups,
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
            excluded_count=excluded_count,
            excluded_groups=excluded_groups,
        )

    write_flat_output(entries, target_path, "Bookmarks")
    return RunSummary(
        original_count=original_count,
        duplicate_count=duplicate_count,
        resulting_count=len(entries),
        group_count=len(main_groups),
        groups=main_groups,
        generated_file_count=generated_file_count,
        excluded_count=excluded_count,
        excluded_groups=excluded_groups,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flatten and deduplicate a Vivaldi bookmark export."
    )
    parser.add_argument(
        "source",
        help="Input bookmark export HTML",
    )
    parser.add_argument(
        "output",
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
    if not source_path.is_file():
        parser.error(f"source bookmark export does not exist: {source_path}")
    if source_path.resolve() == target_path.resolve():
        parser.error("source and output paths must be different")
    group_mode = (
        "single" if args.group_domains else "split" if args.split_domains else "none"
    )
    excluded_domains = parse_domain_list(args.exclude_domains)
    try:
        summary = flatten_bookmarks(
            source_path,
            target_path,
            args.sort,
            group_mode,
            excluded_domains,
            args.split_excluded_domains,
        )
    except (OSError, UnicodeDecodeError) as error:
        parser.error(f"unable to process bookmarks: {error}")
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
            write_text_atomic(
                Path(args.report_file),
                report_text + "\n",
            )
        else:
            print()
            print(report_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
