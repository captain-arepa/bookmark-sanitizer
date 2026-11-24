# Vivaldi bookmark flattening process

## Usage

The source export and output path are both required:

```console
python3 flatten_vivaldi_bookmarks.py vivaldi_bookmarks_11_23_26.html bookmarks_flattened.html --report
```

Run the regression tests with `python3 -m unittest discover -s tests -v`.

Existing output files are not replaced unless `--overwrite` is passed. Use `--dry-run` to parse the export and preview output paths without writing files. `--verbose` adds configuration details to the preflight summary, while `--quiet` suppresses routine status messages; these options are mutually exclusive. `--version` prints the script version.

The script exits with an error if the source file is missing, if the source and output refer to the same file, or if multiple output options resolve to the same path.
It creates missing parent directories for flat HTML and report outputs. HTML and report files are written atomically through temporary files so interrupted writes do not leave partial bookmark files.
When split output filenames collide after sanitization, one domain receives a stable short-hash suffix instead of overwriting the other domain's file.
Domain grouping uses the system Public Suffix List when available, with a conservative TLD fallback otherwise. IP addresses are grouped by address and non-network URL schemes are collected in `unsorted`.
Reports include duplicate counts, excluded counts, per-domain excluded counts, and a reconciliation check showing that original bookmarks equal duplicates plus exclusions plus remaining results.

## Goal

Take a nested Vivaldi bookmark export, flatten bookmarks, remove duplicates, and optionally group repeated domains into folders.

## Step-by-step process

1. Read the exported HTML bookmark file as UTF-8 text.
2. Parse the Netscape bookmark structure and collect every `<A>` bookmark entry in document order.
3. Ignore folder nodes (`<H3>`) and any nested structure.
4. Deduplicate bookmarks by URL, keeping the first occurrence of each unique `HREF`.
5. Choose a sort mode:
	- `alphabetic` sorts bookmarks by title.
	- `domain` sorts bookmarks by absolute domain and then subdomain.
6. Choose one output mode:
	- `--group-domains` writes a single HTML file containing folders for grouped domains plus an `unsorted` folder.
	- `--split-domains` writes a directory containing one HTML file per grouped domain plus `unsorted.html`.
7. Optionally pass `--exclude-domains` with a pipe-separated list such as `youtube.com|x.com` to remove matching bookmarks from the main output.
8. Optionally pass `--split-excluded-domains` to write one HTML file per excluded domain.
9. Optionally pass `--report` to print a summary to the terminal or `--report-file` to write the same summary to a log file.
10. The report includes the original bookmark count, deleted duplicates, resulting groups/folders, total resulting bookmarks, and per-group counts plus percentages.
11. Place bookmarks into domain groups only when a domain has 2 or more bookmarks.
12. Write either one flat Netscape-format HTML file or the selected grouped output mode.
13. Verify the output opens as bookmark files and that the resulting layout matches the selected options.

## Notes

- The script keeps the original bookmark attributes where possible, including icons and descriptions.
- The parser recovers closed, malformed, nested, and unclosed anchor elements and decodes Unicode, named entities, and numeric character references safely.
- Deduplication is URL-based, so bookmarks pointing to the same link are treated as duplicates even if their folder placement or title differs.
- Deduplication normalizes scheme and hostname case, removes default HTTP/HTTPS/FTP/WebSocket ports, and treats an empty root path as equivalent to `/`. It preserves HTTP versus HTTPS, query strings, fragments, and non-root trailing slashes.
- The domain grouping uses the Public Suffix List when available and a TLD fallback otherwise, so bookmarks from the same registered site can be collected together.
- `--group-domains` and `--split-domains` are mutually exclusive.
- When domain grouping is enabled, singletons and entries without a shared domain are collected into `unsorted` or `unsorted.html` depending on the chosen mode.
- `--exclude-domains` is pipe-separated and matches both hostnames and absolute domains.
- `--split-excluded-domains` writes one HTML file per excluded domain next to the main output location.
- `--report` and `--report-file` are mutually exclusive.
- The output stays in standard Netscape bookmark HTML so it can be imported back into Vivaldi or other browsers.