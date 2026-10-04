#!/usr/bin/env python3
"""
finisher_html_ui.py - local web UI for OrchPiano Finisher (same idea as MidiCleaner's HTML UI).

    python finisher_html_ui.py                # serves http://127.0.0.1:8766 and opens a browser
    python finisher_html_ui.py --port 9000 --no-browser

Drop a captured .mid on the page, check the auto-detected track / time signature, press Run, then
download the result or DRAG it straight out of the page into Explorer, Dorico or a DAW (Chrome / Edge).
Everything is a thin layer over orchpiano_finisher.process_capture(); no pipeline logic is duplicated.
Standard library only (plus mido and music21, already required). Binds to localhost only.
Uploaded/generated files live in ui_workspace/ (gitignored).
"""

import argparse
import contextlib
import io
import json
import re
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import mido

import orchpiano_finisher as finisher

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "ui_workspace"
INDEX = ROOT / "ui" / "index.html"
MAX_UPLOAD = 50 * 1024 * 1024

_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_names = {}        # file id -> original stem, for nicer download names
_out_ext = {}      # output id -> ".mid" | ".musicxml"
_run_lock = threading.Lock()   # process_capture prints; capture its stdout one run at a time
_MIME = {".mid": "audio/midi", ".musicxml": "application/vnd.recordare.musicxml+xml"}


def _in_path(file_id):
    return WORK / f"in_{file_id}.mid"


def _out_path(out_id):
    return WORK / f"out_{out_id}{_out_ext.get(out_id, '.mid')}"


def describe(mid):
    """Track list, the Finisher's own auto-pick, and detected time signatures for the form."""
    tracks = []
    for i, tr in enumerate(mid.tracks):
        name = next((m.name for m in tr if m.type == "track_name"), None)
        tracks.append({"index": i, "name": name or "",
                       "note_events": sum(1 for m in tr if m.type in ("note_on", "note_off"))})
    auto = None
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            auto = mid.tracks.index(finisher.find_orchpiano_track(mid, None))
        except SystemExit:
            pass
    sigs = [s for _t, s in finisher.extract_time_signatures(mid)]
    channels = sorted({m.channel for tr in mid.tracks for m in tr if m.type == "note_on"})
    return {"tracks": tracks, "auto_track": auto, "time_signatures": sigs, "channels": [c + 1 for c in channels]}


class Handler(BaseHTTPRequestHandler):
    server_version = "FinisherUI/1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, {"error": message})

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return None if length > MAX_UPLOAD else self.rfile.read(length)

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/":
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif route.startswith("/api/download/"):
            out_id = route.rsplit("/", 1)[1]
            if not _ID_RE.match(out_id) or out_id not in _out_ext or not _out_path(out_id).exists():
                return self._error(404, "unknown output")
            ext = _out_ext[out_id]
            name = _names.get(out_id, "finished") + ext
            self._send(200, _out_path(out_id).read_bytes(), _MIME[ext],
                       {"Content-Disposition": f'attachment; filename="{name}"'})
        else:
            self._error(404, "not found")

    def do_POST(self):
        route = urlparse(self.path).path
        data = self._body()
        if data is None:
            return self._error(413, "file too large")
        try:
            if route == "/api/upload":
                self._upload(data)
            elif route == "/api/run":
                self._run(json.loads(data or b"{}"))
            else:
                self._error(404, "not found")
        except Exception as exc:
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _upload(self, data):
        file_id = uuid.uuid4().hex[:12]
        path = _in_path(file_id)
        path.write_bytes(data)
        try:
            info = describe(mido.MidiFile(path))
        except Exception as exc:
            path.unlink(missing_ok=True)
            return self._error(400, f"Not a readable MIDI file: {exc}")
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(self.headers.get("X-Filename", "capture.mid")).stem) or "capture"
        _names[file_id] = stem
        self._send(200, {"file_id": file_id, "name": stem, **info})

    def _run(self, req):
        file_id = req.get("file_id", "")
        if not _ID_RE.match(file_id) or not _in_path(file_id).exists():
            return self._error(400, "unknown input file; upload again")
        fmt = ".musicxml" if req.get("format") == "musicxml" else ".mid"
        out_id = uuid.uuid4().hex[:12]
        _out_ext[out_id] = fmt
        track = req.get("track")
        channel_base = req.get("channel_base")
        sig = (req.get("time_signature") or "").strip() or None
        buffer = io.StringIO()
        ok = True
        with _run_lock, contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                finisher.process_capture(
                    str(_in_path(file_id)), str(_out_path(out_id)),
                    track=None if track in (None, "") else str(track),
                    channel_base=None if channel_base in (None, "") else int(channel_base),
                    time_signature=sig,
                    max_hand_span=int(req.get("max_hand_span", 14)),
                    max_hand_notes=int(req.get("max_hand_notes", 4)),
                    no_dynamics=not req.get("dynamics", True),
                    notation_scale=float(req.get("notation_scale", 1.0)))
            except SystemExit as exc:
                ok = False
                print(f"ERROR: {exc}")
            except Exception as exc:
                ok = False
                print(f"ERROR: {type(exc).__name__}: {exc}")
        log = buffer.getvalue()
        if not ok or not _out_path(out_id).exists():
            _out_ext.pop(out_id, None)
            return self._send(200, {"ok": False, "log": log})
        label = re.sub(r"[^A-Za-z0-9_.-]", "_", req.get("label") or "finished")
        _names[out_id] = f"{_names.get(file_id, 'capture')}__{label}"
        self._send(200, {"ok": True, "log": log, "out_id": out_id, "download_name": _names[out_id] + fmt,
                         "mime": _MIME[fmt], "bytes": _out_path(out_id).stat().st_size})


def main():
    ap = argparse.ArgumentParser(description="OrchPiano Finisher local web UI")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-browser", action="store_true")
    opts = ap.parse_args()

    WORK.mkdir(exist_ok=True)
    for stale in list(WORK.glob("*.mid")) + list(WORK.glob("*.musicxml")):
        stale.unlink()

    server = ThreadingHTTPServer(("127.0.0.1", opts.port), Handler)
    url = f"http://127.0.0.1:{opts.port}/"
    print("OrchPiano Finisher UI:", url, "(Ctrl+C to stop)")
    if not opts.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
