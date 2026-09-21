#!/usr/bin/env python3
"""Aggregate a HAR locally; never emit URLs, headers, cookies, or bodies."""
import argparse
from collections import defaultdict
from datetime import datetime
import json
from urllib.parse import urlsplit


def summarize(entries):
    totals = defaultdict(lambda: {"requests": 0, "responseBytes": 0, "uploadBytes": 0, "failures": 0})
    put_events = []
    put_keys = defaultdict(int)
    first, last = None, None
    for entry in entries:
        request, response = entry.get("request", {}), entry.get("response", {})
        url = urlsplit(request.get("url", ""))
        method = request.get("method", "GET")
        status = response.get("status", 0)
        path = url.path
        if method == "PUT":
            group = "upload"
            put_keys[(url.netloc, path)] += 1
        elif "/_derived/thumb/" in path:
            group = "thumb"
        elif "/_derived/preview/" in path:
            group = "preview"
        elif path == "/api/media":
            group = "mediaRedirect"  # Its bytes are not RustFS final-response bytes.
        elif path.endswith("/archive") or any(h.get("name", "").lower() == "content-disposition" and "attachment" in h.get("value", "") for h in response.get("headers", [])):
            group = "download"
        elif path.startswith("/api/"):
            group = "api"
        elif response.get("content", {}).get("mimeType", "").startswith("image/"):
            group = "original"
        else:
            group = "other"
        row = totals[group]
        row["requests"] += 1
        row["failures"] += int(status == 0 or status >= 400)
        transfer = response.get("_transferSize", entry.get("_transferSize", response.get("bodySize", 0)))
        row["responseBytes"] += max(0, transfer or 0)
        upload_size = request.get("bodySize", 0)
        if upload_size is None or upload_size < 0:
            upload_size = next((int(h["value"]) for h in request.get("headers", []) if h.get("name", "").lower() == "content-length"), 0)
        row["uploadBytes"] += max(0, upload_size)
        if entry.get("startedDateTime"):
            start = datetime.fromisoformat(entry["startedDateTime"].replace("Z", "+00:00")).timestamp()
            end = start + max(0, entry.get("time", 0)) / 1000
            first = start if first is None else min(first, start)
            last = end if last is None else max(last, end)
            if method == "PUT":
                put_events.extend(((start, 1), (end, -1)))
    active = peak = 0
    for _, delta in sorted(put_events):
        active += delta
        peak = max(peak, active)
    return {"durationSeconds": round(last - first, 3) if first is not None else 0,
            "groups": dict(totals), "peakConcurrentPUT": peak,
            "repeatedPUTs": sum(max(0, count - 1) for count in put_keys.values()),
            "byteBasis": "HAR transferSize when available, otherwise bodySize; unknown bytes counted as 0. Capture must include RustFS final responses."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("har")
    args = parser.parse_args()
    with open(args.har, encoding="utf-8") as source:
        report = summarize(json.load(source)["log"]["entries"])
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
