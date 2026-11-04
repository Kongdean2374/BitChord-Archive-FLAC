# BitChord FLAC Addon — Internet Archive

A small self-hosted HTTP addon that searches Internet Archive for audio items
with FLAC files and returns direct HTTPS FLAC URLs.

## Important

This is a **real FLAC-source project**, not a lossy-to-FLAC converter.
It only selects files whose Internet Archive metadata/file name indicates FLAC.

It does **not** magically turn YouTube Music/AAC/MP3 into true lossless audio.

Use it with public-domain or appropriately licensed audio.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Then test:

- `http://127.0.0.1:8080/manifest.json`
- `http://127.0.0.1:8080/health`
- `http://127.0.0.1:8080/search?q=classical`
- `http://127.0.0.1:8080/stream?id=ITEM_IDENTIFIER`

For Android/BitChord, the server must be reachable from the phone.
A public HTTPS deployment is recommended.

## BitChord

BitChord's current releases describe custom addons as user-added HTTP source
modules and the Sources screen accepts the addon's root URL or `manifest.json`.

Paste either:

```text
https://YOUR-DOMAIN/manifest.json
```

or:

```text
https://YOUR-DOMAIN/
```

## Deployment

This project is intentionally simple so it can be deployed to most Python
web hosts. Use:

```bash
gunicorn --bind 0.0.0.0:$PORT server:app
```

## API

### Search

`GET /search?q=<query>&limit=20`

Returns:

```json
{
  "tracks": [
    {
      "id": "archive_identifier",
      "title": "Example",
      "artist": "Example Artist",
      "audioQuality": "LOSSLESS",
      "format": "FLAC"
    }
  ],
  "total": 1
}
```

### Stream

`GET /stream?id=<archive_identifier>`

Returns a direct `audio/flac` URL.

## Compatibility note

BitChord's public repository documents the custom-addon feature, but its
private/runtime HTTP contract is not separately documented as a stable public
API. Therefore this project exposes both a root manifest and `/manifest.json`,
plus conventional `/search` and `/stream` endpoints. If BitChord's Test button
reports a schema mismatch, the response shown by Test should be used to adjust
the manifest fields to the exact build you're running.

## Search correctness and limits

`/search?q=...&quality=lossless&atmos=auto` accepts BitChord preferences and
unknown extra parameters. This source always offers FLAC; it does not synthesize
Atmos or transcode quality tiers. Unicode is normalized with NFKC/case folding,
and punctuation is treated as spacing. Search words are quoted before IA search.

IA full-text item search discovers at most 16 candidates (including indexed track
lists/descriptions). Each item's file metadata is then searched for the actual
track title, artist and filename; exact genre/subject queries are also supported.
IA does not globally index every file tag: tracks absent from its item search
index may remain undiscoverable. A popular commercial title is not guaranteed to
have an authorized FLAC on IA.

Only unrestricted items with explicit Creative Commons license URLs (including
CC0/public-domain mark) are accepted. Hosting on IA alone is not permission.
License conditions such as attribution, noncommercial use and no derivatives
still apply. Search results retain the IA item artwork link and direct source URL.

Every returned file is checked with a 42-byte FLAC STREAMINFO read. Sample rate,
bit depth and duration come from that file; unavailable duration/bitrate stay null.
Titles come from file tags or filenames, never an unrelated album title. IDs are
`item::filename`; both stream routes resolve that exact file. Old item-only IDs
continue selecting a verified authorized FLAC.

Requests use short connect/read timeouts and at most one retry for transient
failures. Search has a 12-second budget and at most 12 file probes. On Linux's
main thread (the default synchronous Gunicorn worker), a wall-clock timer also
interrupts slow-drip responses. Threaded workers retain socket/budget checks but
cannot use SIGALRM; deploy with the documented default sync worker. No executor
queues remain to wait for after returning results. Upstream errors use a sanitized
502 response for HTTP/network/payload faults and 504 for upstream timeouts or
exhausted search budgets; legitimate empty matches remain HTTP 200. Logs include normalized
query, candidate/result counts, elapsed time and failure count, without headers
or credentials.

Run regression tests with `python -m unittest -v`.

### Diagnosing upstream search failures

`archive_upstream` records the search stage (`search`, `metadata`, `flac_probe`),
HTTP status (null if no response), elapsed milliseconds, error category and attempt
number. Attempt records cover transport/HTTP success or failure; operation failure
records also cover JSON/schema errors and budget exhaustion. Exception messages,
URLs, response bodies and headers are omitted.

`archive_search.upstream_failures` counts failed operations, including the initial
IA search. `upstream_attempt_failures` counts failed HTTP attempts, including a
transient failure recovered by retry. These counters have different units and
should not be added. `failures` lists terminal operation errors, and `outcome`
distinguishes empty/results/partial results, probe limits and error categories.

| Condition with no usable tracks | HTTP | errorType |
| --- | --- | --- |
| Valid response with no matches | 200 | absent |
| IA HTTP error | 502 | upstream_http |
| Connection/transport failure | 502 | upstream_network |
| Invalid JSON | 502 | upstream_parse |
| Unexpected payload structure | 502 | upstream_schema |
| IA error payload, even with HTTP 200 | 502 | upstream_api |
| Upstream socket timeout | 504 | upstream_timeout |
| Search wall-clock budget exhausted | 504 | budget_exhausted |

Error responses retain `tracks` and `error`, adding `errorType` and `stage`.
Verified tracks found before a later failure are returned with HTTP 200 and a
`partial` log outcome. Query filters, licensing, retry count and timeout values
are unchanged by the diagnostics fix.
