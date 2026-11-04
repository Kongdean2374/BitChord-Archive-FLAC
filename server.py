#!/usr/bin/env python3
import os
import re
import time
import json
import unicodedata
import signal
import threading
from functools import wraps
from urllib.parse import quote

import requests
from urllib3.exceptions import HTTPError as TransportError, ReadTimeoutError
from flask import Flask, g, has_request_context, jsonify, request

app = Flask(__name__)
app.logger.setLevel("INFO")

MANIFEST = {
    "id": "bitchord-internet-archive-flac",
    "name": "Layz Add On",
    "version": "1.6.0",
    "resources": ["search", "stream"],
    "settings": [
        {
            "key": "quality",
            "type": "select",
            "default": "lossless",
            "options": [
                {"label": "Lossless", "value": "lossless"},
                {"label": "High", "value": "high"},
                {"label": "Low", "value": "low"},
            ],
        }
    ],
}

IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_METADATA = "https://archive.org/metadata/{}"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Layz-Add-On/1.6 BitChord"})



def num(value):
    if value in (None, ""):
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else number



def first(data, *keys):
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return None



SEARCH_SECONDS = 12
MAX_ITEMS = 16
MAX_PROBES = 12


class SearchBudgetExceeded(requests.Timeout):
    pass


class PayloadError(ValueError):
    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


def error_kind(exc):
    if isinstance(exc, SearchBudgetExceeded):
        return "budget_exhausted"
    if isinstance(exc, requests.Timeout):
        return "upstream_timeout"
    if isinstance(exc, requests.HTTPError):
        return "upstream_http"
    if isinstance(exc, PayloadError):
        return exc.kind
    if isinstance(exc, (ValueError, TypeError)):
        return "upstream_parse"
    return "upstream_network"


def diagnostic_event(stage, started, status=None, exc=None, attempt=None):
    # Never log exception text, URLs, response bodies, or request headers.
    event = dict(event="attempt" if attempt is not None else "failure",
                 stage=stage, elapsed_ms=round((time.monotonic() - started) * 1000),
                 upstream_status=status, error_type=error_kind(exc) if exc else None,
                 attempt=attempt)
    app.logger.info("archive_upstream %s", json.dumps(event))


def search_failure(exc):
    diag = g.search_diagnostics
    kind = error_kind(exc)
    failure = dict(error_type=kind, stage=diag["stage"],
                   upstream_status=diag.get("upstream_status"))
    diag["failures"].append(failure)
    diagnostic_event(diag["stage"], diag["stage_started"],
                     diag.get("upstream_status"), exc)
    return failure


def json_payload(response):
    data = response.json()
    if not isinstance(data, dict):
        raise PayloadError("upstream_schema")
    if "error" in data or (isinstance(data.get("responseHeader"), dict)
                           and data["responseHeader"].get("status", 0) != 0):
        raise PayloadError("upstream_api")
    return data


def request_budget(function):
    """Hard wall-clock cap on Render's default synchronous Gunicorn worker."""
    @wraps(function)
    def bounded(*args, **kwargs):
        if threading.current_thread() is not threading.main_thread():
            return function(*args, **kwargs)
        def expired(signum, frame):
            raise SearchBudgetExceeded("request budget exhausted")
        previous = signal.signal(signal.SIGALRM, expired)
        timer = signal.setitimer(signal.ITIMER_REAL, SEARCH_SECONDS)
        try:
            return function(*args, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, *timer)
            signal.signal(signal.SIGALRM, previous)
    return bounded


def upstream(url, deadline, **kwargs):
    """Two attempts, bounded socket waits and no retry of permanent errors."""
    stage = "search" if url == IA_SEARCH else "metadata" if url.startswith("https://archive.org/metadata/") else "flac_probe"
    diag = getattr(g, "search_diagnostics", None) if has_request_context() else None
    if diag is not None:
        diag.update(stage=stage, stage_started=time.monotonic(), upstream_status=None)
    for attempt in range(2):
        started = time.monotonic()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SearchBudgetExceeded("request budget exhausted")
        response = None
        try:
            response = SESSION.get(url, timeout=(min(1, remaining / 2),
                                   min(1.5, remaining / 2)), **kwargs)
            if diag is not None:
                diag["upstream_status"] = response.status_code
            response.raise_for_status()
            diagnostic_event(stage, started, response.status_code, attempt=attempt + 1)
            return response
        except requests.RequestException as exc:
            # requests may wrap the signal timeout as ConnectionError.
            if time.monotonic() >= deadline:
                exc = SearchBudgetExceeded()
            if response is not None:
                response.close()
            status = exc.response.status_code if exc.response is not None else None
            diagnostic_event(stage, started, status, exc, attempt + 1)
            if diag is not None:
                diag["upstream_attempt_failures"] += 1
                diag["upstream_status"] = status
            if isinstance(exc, SearchBudgetExceeded) or attempt or (status is not None and status != 429 and status < 500):
                raise exc
    raise requests.Timeout("upstream unavailable")


def flac_info(url, deadline):
    with upstream(url, deadline, headers={"Range": "bytes=0-41"}, stream=True) as response:
        try:
            data = response.raw.read(42)
        except ReadTimeoutError as exc:
            raise requests.Timeout() from exc
        except TransportError as exc:
            raise requests.ConnectionError() from exc
    if len(data) != 42 or data[:4] != b"fLaC" or data[4] & 127 != 0:
        return None
    if int.from_bytes(data[5:8], "big") != 34:
        return None
    packed = int.from_bytes(data[18:26], "big")
    sr = packed >> 44
    bd = ((packed >> 36) & 31) + 1
    samples = packed & ((1 << 36) - 1)
    if not 1000 <= sr <= 768000 or not 4 <= bd <= 32:
        return None
    return sr, bd, samples / sr if samples else None



def quality(sr, bd):
    # This addon only returns real FLAC, so its protocol tier is LOSSLESS.
    return "LOSSLESS"



def quality_label(sr, bd):
    details = []
    if bd is not None:
        details.append(f"{bd}-bit")
    if sr is not None:
        details.append(f"{sr / 1000:g} kHz")
    hi_res = (bd is not None and bd > 16) or (sr is not None and sr > 48000)
    label = "Hi-Res Lossless" if hi_res else "Lossless"
    return label + (f" · {' / '.join(details)}" if details else "")



def normalized(value):
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in value).split())


def text(value):
    return "; ".join(map(str, value)) if isinstance(value, list) else str(value or "")


def licensed(metadata):
    # IA hosting alone is not a playback grant. Require an explicit open license.
    licenses = metadata.get("licenseurl", [])
    if not isinstance(licenses, list):
        licenses = [licenses]
    return any(re.fullmatch(
        r"https?://creativecommons\.org/(?:licenses/(?:by|by-sa|by-nc|by-nc-sa|by-nd|by-nc-nd)/[1-4]\.0|publicdomain/(?:zero/1\.0|mark/1\.0))/?",
        str(url).strip()) for url in licenses)


def metadata_for(identifier, deadline):
    with upstream(IA_METADATA.format(quote(identifier, safe="")), deadline) as response:
        data = json_payload(response)
    meta = data.get("metadata")
    files = data.get("files")
    if not isinstance(meta, dict) or not isinstance(files, list) or any(
            not isinstance(f, dict) or not isinstance(f.get("name"), str) for f in files):
        raise PayloadError("upstream_schema")
    if data.get("is_dark") or meta.get("access-restricted-item") in (True, "true", "1") or not licensed(meta):
        return {}, []
    return meta, [f for f in files
                  if str(f.get("name", "")).lower().endswith(".flac")
                  and not f.get("private")]


def inspect(identifier, item, deadline):
    name = str(item["name"])
    url = f"https://archive.org/download/{quote(identifier, safe='')}/{quote(name, safe='')}"
    info = flac_info(url, deadline)
    if info is None:
        return None
    sr, bd, duration = info
    return dict(url=url, filename=name, length=duration, sampleRate=sr,
                bitDepth=bd, bitrate=None, quality="LOSSLESS")


def get_flac(identifier, filename=None):
    deadline = time.monotonic() + SEARCH_SECONDS
    meta, files = metadata_for(identifier, deadline)
    candidates = files if filename is not None else files[:MAX_PROBES]
    for item in candidates:
        if filename is not None and item["name"] != filename:
            continue
        result = inspect(identifier, item, deadline)
        if result:
            return result
    return None



def artist_hint(query, title, creator):
    # IA metadata is inconsistent. Keep the real creator when available.
    if isinstance(creator, list):
        return str(creator[0]) if creator else "Internet Archive"
    if creator:
        return str(creator)
    return "Internet Archive"



def make_track(doc, flac):
    identifier = str(doc["identifier"])
    title = str(doc.get("title") or flac["filename"])
    creator = doc.get("creator")
    tier = flac.get("quality", "LOSSLESS")

    return {
        "id": identifier + "::" + flac["filename"],
        "title": title,
        "artist": artist_hint("", title, creator),
        "album": str(doc.get("album") or ""),
        "duration": flac.get("length"),
        "artworkURL": f"https://archive.org/services/img/{quote(identifier, safe='')}",
        "format": "flac",
        "audioQuality": tier,
        "quality": quality_label(flac.get("sampleRate"), flac.get("bitDepth")),
        "sampleRate": flac.get("sampleRate"),
        "bitDepth": flac.get("bitDepth"),
        "bitrate": flac.get("bitrate"),
        "streamURL": flac["url"],
    }



@request_budget
def do_search():
    started = time.monotonic()
    deadline = started + SEARCH_SECONDS
    query = normalized((request.args.get("q") or "")[:300])
    # quality and atmos are preferences; this source offers verified FLAC only.
    tracks, docs = [], []
    diag = dict(stage="search", stage_started=started, upstream_status=None,
                failures=[], upstream_attempt_failures=0, outcome="empty")
    g.search_diagnostics = diag
    try:
        limit = min(max(int(request.args.get("limit", "20")), 1), 30)
    except ValueError:
        limit = 20
    try:
        if not query:
            return jsonify({"tracks": []})
        # Quote each token: punctuation cannot become Solr operators. Full-text
        # search includes IA description/track lists, rather than title only.
        terms = " AND ".join('"' + token + '"' for token in query.split())
        with upstream(IA_SEARCH, deadline, params={
            "q": f'mediatype:audio AND format:Flac AND licenseurl:* AND ({terms})',
            "fl[]": ["identifier"], "rows": MAX_ITEMS, "output": "json",
        }) as response:
            payload = json_payload(response)
            result = payload.get("response")
            if not isinstance(result, dict) or not isinstance(result.get("docs"), list):
                raise PayloadError("upstream_schema")
            docs = result["docs"]
            if any(not isinstance(doc, dict) or not isinstance(doc.get("identifier"), str)
                   for doc in docs):
                raise PayloadError("upstream_schema")
        probes = 0
        for doc in docs[:MAX_ITEMS]:
            if time.monotonic() >= deadline:
                raise SearchBudgetExceeded()
            if probes >= MAX_PROBES:
                diag["outcome"] = "probe_limit"
                break
            identifier = str(doc.get("identifier") or "")
            if not identifier:
                continue
            try:
                meta, files = metadata_for(identifier, deadline)
                for item in files:
                    title = text(item.get("title")) or re.sub(r"^\d+[ ._-]+", "", item["name"].rsplit(".", 1)[0])
                    artist = text(item.get("artist") or item.get("creator") or meta.get("creator"))
                    haystack = normalized(title + " " + artist + " " + item["name"])
                    # Do not return an unrelated track just because its album matched.
                    subjects = meta.get("subject", [])
                    if not isinstance(subjects, list):
                        subjects = str(subjects).split(";")
                    genre_match = query in {normalized(text(v)) for v in
                                            subjects + [item.get("genre", "")]}
                    if not genre_match and not all(token in haystack for token in query.split()):
                        continue
                    if time.monotonic() >= deadline:
                        raise SearchBudgetExceeded()
                    if probes >= MAX_PROBES:
                        diag["outcome"] = "probe_limit"
                        break
                    probes += 1
                    flac = inspect(identifier, item, deadline)
                    if flac:
                        track_doc = dict(identifier=identifier, title=title,
                                         creator=artist, album=text(item.get("album") or meta.get("title")))
                        tracks.append(make_track(track_doc, flac))
                    if len(tracks) >= limit:
                        break
            except SearchBudgetExceeded:
                raise
            except (requests.RequestException, ValueError, TypeError) as exc:
                search_failure(exc)
            if len(tracks) >= limit:
                break
        if diag["failures"] and not tracks:
            failure = diag["failures"][-1]
            diag["outcome"] = failure["error_type"]
            status = 504 if failure["error_type"] in ("upstream_timeout", "budget_exhausted") else 502
            return jsonify({"tracks": [], "error": "archive upstream unavailable",
                            "errorType": failure["error_type"], "stage": failure["stage"]}), status
        diag["outcome"] = "partial" if diag["failures"] else "results" if tracks else diag["outcome"]
        return jsonify({"tracks": tracks})
    except (requests.RequestException, ValueError, TypeError) as exc:
        failure = search_failure(exc)
        diag["outcome"] = failure["error_type"]
        if tracks:
            diag["outcome"] = "partial"
            return jsonify({"tracks": tracks})
        status = 504 if failure["error_type"] in ("upstream_timeout", "budget_exhausted") else 502
        return jsonify({"tracks": [], "error": "archive upstream unavailable",
                        "errorType": failure["error_type"], "stage": failure["stage"]}), status
    finally:
        app.logger.info("archive_search %s", json.dumps(dict(
            query=query, candidates=len(docs), returned=len(tracks),
            elapsed_ms=round((time.monotonic() - started) * 1000),
            upstream_failures=len(diag["failures"]),
            upstream_attempt_failures=diag["upstream_attempt_failures"],
            outcome=diag["outcome"], failures=diag["failures"]), ensure_ascii=False))



@request_budget
def do_stream(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return jsonify({"error": "missing id"}), 400

    try:
        item_id, separator, filename = identifier.partition("::")
        flac = get_flac(item_id, filename if separator else None)
    except (requests.RequestException, ValueError):
        return jsonify({"error": "archive upstream unavailable"}), 502

    if not flac:
        return jsonify({"error": "No FLAC file found for this item"}), 404

    return jsonify({
        "url": flac["url"],
        "format": "flac",
        "quality": quality_label(flac.get("sampleRate"), flac.get("bitDepth")),
        "audioQuality": flac.get("quality", "LOSSLESS"),
        "codec": "flac",
        "container": "flac",
        "manifest": "none",
        "encrypted": False,
        "sampleRate": flac.get("sampleRate"),
        "bitDepth": flac.get("bitDepth"),
        "bitrate": flac.get("bitrate"),
    })



@app.get("/")
def root():
    return jsonify(MANIFEST)


@app.get("/manifest.json")
def manifest():
    return jsonify(MANIFEST)


@app.get("/health")
def health():
    return jsonify({"ok": True, "name": MANIFEST["name"], "version": MANIFEST["version"]})


@app.get("/search")
def search():
    return do_search()


@app.get("/stream/<path:identifier>")
def stream(identifier):
    return do_stream(identifier)


# Compatibility with older addon examples that used /stream?id=...
@app.get("/stream")
def stream_query():
    return do_stream(request.args.get("id"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
