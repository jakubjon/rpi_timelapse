#!/usr/bin/env python3
"""Timelapse web app — live view, ROI stills at full sensor resolution, video/GIF.

Single camera owner: this app runs the interval capture loop itself and serves the
live view from the same open camera, so no other process may hold the sensor while
it runs. AE runs continuously with a fixed analogue gain.

Two streams, for the two jobs:
  main  — full sensor resolution, YUV420. Only touched once per capture: converted
          to BGR, cropped to the ROI and written as JPEG. YUV420 halves the buffer
          cost of RGB888 at 8 MP, which matters on a Zero 2 W.
  lores — small, YUV420. The live view only, so watching costs nearly nothing.

The ROI is in full-res sensor coordinates and stills are that crop at native
pixels — no downscaling. Changing the ROI starts a new capture session (its own
directory), because frames of different sizes cannot go into one video.

Usage:
  python3 app.py                                 # port 8080, 15 min, gain 3
  python3 app.py --port 80 --period 5 --camera hq
"""

import argparse
import json
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
from flask import Flask, Response, jsonify, request, send_from_directory
from libcamera import controls as lc
from picamera2 import Picamera2

ROOT = Path(__file__).resolve().parent
# Native pixel-array size per camera module; the stills are crops of this.
SENSORS = {"v2.1": (3280, 2464),      # IMX219, Camera Module v2.1
           "hq": (4056, 3040)}        # IMX477, HQ Camera

LORES_SIZE = (800, 600)      # live view stream; 4:3 like the sensor
MIN_ROI = 64                 # px, in sensor coordinates
JOB_KEEP = 10                # finished jobs kept in the log


def manual_controls(exposure_us: int | None, gain: float | None) -> dict:
    """Pin only the values given; whatever is left out stays under auto-exposure.

    A fixed gain still lets AE lengthen the exposure time as the light fades. With
    the older `AeEnable: False` both froze, so every frame after dusk came out black.
    Needs libcamera >= 0.5 (ExposureTimeMode / AnalogueGainMode).
    """
    controls: dict = {}
    if exposure_us is not None:
        controls["ExposureTimeMode"] = lc.ExposureTimeModeEnum.Manual
        controls["ExposureTime"] = exposure_us
    if gain is not None:
        controls["AnalogueGainMode"] = lc.AnalogueGainModeEnum.Manual
        controls["AnalogueGain"] = gain
    return controls


def even(n: int) -> int:
    """H.264 needs even dimensions, and YUV420 chroma is subsampled 2x."""
    return int(n) // 2 * 2


class Capture:
    """Owns the camera: interval ROI stills, on-demand stills, preview frames."""

    def __init__(self, base_dir: Path, sensor_size, gain: float,
                 period_min: float, state_path: Path):
        self.base_dir = base_dir
        self.sensor_size = sensor_size
        self.gain = gain
        self.state_path = state_path
        self.period_min = period_min
        self.roi: dict | None = None          # {x, y, w, h} in sensor px; None = full frame
        self.session: str = ""
        self.lock = threading.Lock()          # one camera consumer at a time
        self.wake = threading.Event()         # period change / capture now
        self.last_path: Path | None = None
        self.last_time: float | None = None
        self.last_error: str | None = None
        self.next_time: float = 0.0
        self.metadata: dict = {}

        self.cam = Picamera2()
        self.cam.configure(self.cam.create_video_configuration(
            main={"size": sensor_size, "format": "YUV420"},
            lores={"size": LORES_SIZE, "format": "YUV420"},
            buffer_count=2))
        self.cam.start()
        self.cam.set_controls(manual_controls(None, gain))
        time.sleep(2)                         # let AE settle before the first shot

    # ── state ────────────────────────────────────────────────────────────────
    def load_state(self) -> None:
        """Period, ROI and session survive restarts; --period is only a default."""
        try:
            saved = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            saved = {}
        self.period_min = float(saved.get("period_min", self.period_min))
        self.roi = saved.get("roi")
        self.session = saved.get("session") or self.new_session()

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(
            {"period_min": self.period_min, "roi": self.roi, "session": self.session},
            indent=1))

    def new_session(self) -> str:
        """A directory per ROI: one video cannot mix frame sizes."""
        w, h = self.crop_size()
        self.session = f"{datetime.now():%Y%m%d_%H%M%S}_{w}x{h}"
        (self.base_dir / self.session).mkdir(parents=True, exist_ok=True)
        return self.session

    def crop_size(self) -> tuple[int, int]:
        return (self.roi["w"], self.roi["h"]) if self.roi else self.sensor_size

    def set_period(self, minutes: float) -> None:
        self.period_min = minutes
        self.save_state()
        self.wake.set()                       # re-time the pending sleep

    def set_roi(self, roi: dict | None) -> None:
        self.roi = roi
        self.new_session()
        self.save_state()

    def sessions(self) -> list[dict]:
        out = []
        for d in sorted(self.base_dir.glob("*_*x*"), reverse=True):
            if not d.is_dir():
                continue
            shots = sorted(d.glob("*.jpg"))
            out.append({"name": d.name, "count": len(shots),
                        "mb": round(sum(p.stat().st_size for p in shots) / 1e6, 1),
                        "first": shots[0].name if shots else None,
                        "last": shots[-1].name if shots else None,
                        "current": d.name == self.session})
        return out

    # ── frames ───────────────────────────────────────────────────────────────
    def preview_jpeg(self) -> bytes:
        with self.lock:
            yuv = self.cam.capture_array("lores")
            self.metadata = self.cam.capture_metadata()
        bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            raise RuntimeError("JPEG encode failed")
        return buf.tobytes()

    def capture_still(self) -> Path:
        """Full-res frame, cropped to the ROI, saved at native pixels."""
        with self.lock:
            yuv = self.cam.capture_array("main")
            self.metadata = self.cam.capture_metadata()
        bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
        if self.roi:
            r = self.roi
            bgr = bgr[r["y"]:r["y"] + r["h"], r["x"]:r["x"] + r["w"]]
        out_dir = self.base_dir / self.session
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / datetime.now().strftime("%Y%m%d_%H%M%S.jpg")
        if not cv2.imwrite(str(path), bgr, [cv2.IMWRITE_JPEG_QUALITY, 92]):
            raise RuntimeError(f"could not write {path}")
        self.last_path, self.last_time, self.last_error = path, time.time(), None
        return path

    def capture_loop(self) -> None:
        while True:
            try:
                self.capture_still()
            except Exception as exc:                       # keep the loop alive
                self.last_error = f"{type(exc).__name__}: {exc}"
            self.next_time = time.time() + self.period_min * 60
            while time.time() < self.next_time:
                self.wake.wait(timeout=min(self.next_time - time.time(), 30))
                if self.wake.is_set():                     # period/ROI changed
                    self.wake.clear()
                    break


class VideoJob:
    """One ffmpeg run over a session's stills: MP4 or GIF, with progress."""

    def __init__(self, src: Path, out_dir: Path, fps: float, height: int, fmt: str):
        self.src = src
        self.out_dir = out_dir
        self.fps = fps
        self.height = height          # 0 = native crop size, no scaling
        self.fmt = fmt
        self.frames = 0
        self.done_frames = 0
        self.state = "starting"
        self.error: str | None = None
        self.name: str | None = None
        self.started = time.time()
        self.finished: float | None = None

    def as_dict(self) -> dict:
        return {"state": self.state, "frames": self.frames, "name": self.name,
                "done_frames": self.done_frames, "error": self.error,
                "fps": self.fps, "height": self.height, "format": self.fmt,
                "session": self.src.name, "started": self.started,
                "finished": self.finished,
                "percent": round(100 * self.done_frames / self.frames, 1)
                           if self.frames else 0.0}

    def _filters(self) -> list[str]:
        scale = f"scale=-2:{self.height}:flags=lanczos" if self.height else None
        if self.fmt == "gif":
            chain = (scale + "," if scale else "") + \
                    "split[a][b];[a]palettegen[p];[b][p]paletteuse"
            return ["-vf", chain, "-loop", "0"]
        return (["-vf", scale] if scale else []) + \
               ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p"]

    def run(self) -> None:
        listing = None
        try:
            shots = sorted(self.src.glob("*.jpg"))
            self.frames = len(shots)
            if self.frames < 2:
                self.state, self.error = "failed", "need at least 2 photos"
                return
            self.out_dir.mkdir(parents=True, exist_ok=True)
            name = f"{self.src.name}_{datetime.now():%H%M%S}.{self.fmt}"
            out = self.out_dir / name
            # concat demuxer: the stills are timestamps, not a 0001.. sequence
            listing = self.out_dir / f".frames_{int(self.started)}.txt"
            listing.write_text("".join(f"file '{p}'\n" for p in shots))
            self.state = "encoding"
            proc = subprocess.Popen(
                ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-progress", "pipe:1",
                 "-r", str(self.fps), "-f", "concat", "-safe", "0", "-i", str(listing),
                 *self._filters(), str(out)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for line in proc.stdout:                        # -progress key=value
                if line.startswith("frame="):
                    self.done_frames = int(line.split("=", 1)[1] or 0)
            stderr = proc.stderr.read()
            proc.wait()
            if proc.returncode != 0:
                self.state = "failed"
                self.error = (stderr.strip() or f"ffmpeg exit {proc.returncode}")[-400:]
                out.unlink(missing_ok=True)
            else:
                self.state, self.name, self.done_frames = "done", name, self.frames
        except Exception as exc:
            self.state, self.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            if listing:
                listing.unlink(missing_ok=True)
            self.finished = time.time()


def create_app(cap: Capture, video_dir: Path) -> Flask:
    app = Flask(__name__)
    jobs: list[VideoJob] = []
    job_lock = threading.Lock()

    @app.get("/")
    def index():
        return send_from_directory(ROOT, "index.html")

    @app.get("/preview.jpg")
    def preview():
        return Response(cap.preview_jpeg(), mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/status")
    def status():
        usage = shutil.disk_usage(cap.base_dir)
        md = cap.metadata
        sessions = cap.sessions()
        current = next((s for s in sessions if s["current"]), None)
        with job_lock:
            job = jobs[-1].as_dict() if jobs else None
        return jsonify({
            "period_min": cap.period_min,
            "gain": cap.gain,
            "sensor_size": list(cap.sensor_size),
            "crop_size": list(cap.crop_size()),
            "roi": cap.roi,
            "session": cap.session,
            "sessions": sessions,
            "photo_count": current["count"] if current else 0,
            "photos_mb": current["mb"] if current else 0,
            "last_photo": cap.last_path.name if cap.last_path else None,
            "last_capture_ago_s": round(time.time() - cap.last_time)
                                  if cap.last_time else None,
            "next_capture_in_s": max(0, round(cap.next_time - time.time())),
            "exposure_ms": round(md.get("ExposureTime", 0) / 1000, 1),
            "actual_gain": round(md.get("AnalogueGain", 0), 2),
            "disk_free_mb": round(usage.free / 1e6),
            "error": cap.last_error,
            "job": job,
        })

    @app.post("/period")
    def set_period():
        minutes = float((request.json or {}).get("minutes", 0))
        if not 0.25 <= minutes <= 24 * 60:
            return jsonify({"error": "minutes must be 0.25–1440"}), 400
        cap.set_period(minutes)
        return jsonify({"period_min": cap.period_min})

    @app.post("/roi")
    def set_roi():
        body = request.json or {}
        sw, sh = cap.sensor_size
        if not body.get("roi"):                             # clear = full frame
            cap.set_roi(None)
            return jsonify({"roi": None, "session": cap.session})
        r = body["roi"]
        try:
            x, y = even(max(0, r["x"])), even(max(0, r["y"]))
            w, h = even(r["w"]), even(r["h"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "roi needs numeric x, y, w, h"}), 400
        w, h = min(w, sw - x), min(h, sh - y)
        if w < MIN_ROI or h < MIN_ROI:
            return jsonify({"error": f"ROI must be at least {MIN_ROI}x{MIN_ROI} px"}), 400
        cap.set_roi({"x": x, "y": y, "w": w, "h": h})
        return jsonify({"roi": cap.roi, "session": cap.session})

    @app.post("/capture")
    def capture_now():
        try:
            return jsonify({"saved": cap.capture_still().name})
        except Exception as exc:
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/timelapse")
    def make_timelapse():
        body = request.json or {}
        session = body.get("session") or cap.session
        src = cap.base_dir / session
        if ".." in session or "/" in session or not src.is_dir():
            return jsonify({"error": f"no such session: {session}"}), 404
        fmt = body.get("format", "mp4")
        if fmt not in ("mp4", "gif"):
            return jsonify({"error": "format must be mp4 or gif"}), 400
        with job_lock:
            if jobs and jobs[-1].state in ("starting", "encoding"):
                return jsonify({"error": "a video is already being generated"}), 409
            job = VideoJob(src, video_dir, fps=float(body.get("fps", 10)),
                           height=int(body.get("height", 720)), fmt=fmt)
            jobs.append(job)
            del jobs[:-JOB_KEEP]
        threading.Thread(target=job.run, daemon=True).start()
        return jsonify(job.as_dict())

    @app.get("/videos")
    def list_videos():
        vids = sorted([p for p in video_dir.glob("*") if p.suffix in (".mp4", ".gif")],
                      key=lambda p: p.stat().st_mtime, reverse=True)
        return jsonify([{"name": v.name, "mb": round(v.stat().st_size / 1e6, 1),
                         "mtime": v.stat().st_mtime} for v in vids])

    @app.get("/videos/<name>")
    def get_video(name: str):
        return send_from_directory(video_dir, name)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Timelapse web app")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--captures", default=str(ROOT / "captures"),
                        help="Parent directory of the per-ROI session directories")
    parser.add_argument("--video-dir", default=str(ROOT / "videos"))
    parser.add_argument("--state", default=str(ROOT / "data" / "timelapse.json"),
                        help="Where period, ROI and current session are persisted")
    parser.add_argument("--period", type=float, default=15.0,
                        help="Default minutes between stills (web UI overrides)")
    parser.add_argument("--gain", type=float, default=3.0,
                        help="Fixed analogue gain; exposure time stays automatic")
    parser.add_argument("--camera", default="v2.1", choices=sorted(SENSORS),
                        help="Camera module (sets the full-res sensor size)")
    args = parser.parse_args()

    cap = Capture(Path(args.captures), SENSORS[args.camera], args.gain,
                  args.period, Path(args.state))
    cap.load_state()
    threading.Thread(target=cap.capture_loop, daemon=True).start()

    app = create_app(cap, Path(args.video_dir))
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
