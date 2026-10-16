# bottled-kiwix

[Kiwix](https://kiwix.org/) packaged as a Cloud in a Bottle app: read Wikipedia, Wiktionary, Stack Exchange, Project
Gutenberg and thousands of other offline libraries from your own instance.

The app runs the official `kiwix-serve` and puts a small library manager in front of it:

- Browse the Kiwix catalog by search, language and category, and download ZIM files with one click.
- Downloads run in the background one at a time, resume after interruptions and restarts (HTTP range requests across
  the Kiwix mirrors), and are checked against the SHA-256 published by Kiwix before they reach the library.
- The library is private by default. The owner can make the reader public; the manager is always owner-only.
- Delete books, or drop ZIM files into the archive directory yourself and press "Rescan files".

## Using it

Deploy the repo from the dashboard. Open the app and you are sent to the manager (`/_manage/`, also linked from the
app's dashboard page as "Manage library"). Pick something to download; once it finishes it appears on the library page
at `/`.

Some sizes for English Wikipedia, to plan disk space (they change with each release):

| Book | Size |
|---|---|
| `wikipedia_en_100` (top 100 articles) | ~300 MB |
| `wikipedia_en_all_mini` (intros only) | ~14 GB |
| `wikipedia_en_all_nopic` (full text) | ~53 GB |
| `wikipedia_en_all_maxi` (with images) | ~127 GB |

## Storage

| What | Where |
|---|---|
| ZIM files | `$BOTTLE_APP_ARCHIVE_DIR/zims/` |
| Partial downloads | `$BOTTLE_APP_ARCHIVE_DIR/partial/` |
| Library file, settings, download queue | `$BOTTLE_APP_DATA_DIR/` |

ZIM files are bulky and can always be downloaded again, so they live in the archive tier, which the backup app skips
and which the owner can move to S3. The archive is a JuiceFS mount that always reports 1 PiB of capacity, so the
manager checks free space on the instance's real disk instead. A download that would not fit asks for confirmation
(on an S3-backed archive the local disk is not the limit), and a running download stops if the disk gets within 2 GB
of full, well before the platform's 500 MB storage guard stops apps. Stopped downloads can be resumed.

On an S3-backed archive, the first read of each part of a ZIM file is slower; full-text search touches many parts of
the file, so it is noticeably slower until the JuiceFS cache warms up.

## Security notes

- `public_paths = ["/"]` in the manifest makes the router pass every request through, so the app enforces access
  itself using the router's `X-OpenHost-Is-Owner` header: `/_manage/` is owner-only, and the reader is owner-only
  unless the owner turns on public access.
- State-changing manager requests must come from this app's own origin (`Origin` / `Sec-Fetch-Site`), since the
  owner's session cookie is shared with every app on the zone.
- Only books from the Kiwix catalog can be downloaded, only via metalinks on `download.kiwix.org`, and every file is
  verified against its SHA-256.
- ZIM content is served from the same origin as the manager, so a ZIM file's scripts could use the manager as the
  owner. Only add ZIM files you trust; the Kiwix catalog is published by the openZIM project.

## Development

```bash
uv sync
uv run pytest            # needs kiwix-serve and kiwix-manage, see below
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

The library and app tests use the real `kiwix-serve` and `kiwix-manage` binaries. Put them on `PATH` or point
`KIWIX_SERVE_BIN` and `KIWIX_MANAGE_BIN` at the binaries from a
[kiwix-tools release](https://download.kiwix.org/release/kiwix-tools/). The tests download a 760 KB ZIM file once and
cache it in `~/.cache/bottled-kiwix-tests`.

The Dockerfile installs a pinned kiwix-tools release (x86_64 or aarch64), verified by SHA-256, from
`scripts/install_kiwix_tools.py`.
