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
pixels — no downscaling.

A run is explicit: set the period, region and gain, press Start and a session
directory is created for it, press Stop and it is closed. Nothing is captured while
stopped, and the settings are only editable then — a session whose frame size or
cadence changed halfway through would not make one video.

Usage:
  python3 app.py                                 # port 8080, 15 min, gain 3
  python3 app.py --port 80 --period 5 --camera hq
"""

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, send_from_directory
from libcamera import controls as lc
from picamera2 import Picamera2

ROOT = Path(__file__).resolve().parent
# Native pixel-array size per camera module; the stills are crops of this.
SENSORS = {"v2.1": (3280, 2464),      # IMX219, Camera Module v2.1
           "hq": (4056, 3040)}        # IMX477, HQ Camera
TUNING = {"v2.1": "imx219.json", "hq": "imx477.json"}
EXPOSURE_CEILING_US = 10_000_000  # how far the AGC is *allowed* to stretch (sensor limit ~11 s)
MIN_FRAME_US = 100                # lower frame-duration bound; AE picks anything above it

LORES_SIZE = (800, 600)      # live view stream; 4:3 like the sensor
BINNED_SIZE = (1640, 1232)   # 2x2 binned mode: the whole sensor, cheap to stream
MIN_ROI = 64                 # px, in sensor coordinates
LEVELS_FILE = ".brightness.json"   # cached mean brightness per still, per session
VIDEO_SUFFIXES = (".mp4", ".gif")
MIN_PERIOD_S = 1             # a full-res capture takes ~4 s, so this is a floor, not a rate
STALL_LIMIT_S = 45           # a camera call taking longer than this means the ISP wedged
JOB_KEEP = 10                # finished jobs kept in the log


def brightness_of(path: Path) -> float:
    """Mean luma 0-255. Decoded at 1/8 scale — 64x less work, same average."""
    img = cv2.imread(str(path), cv2.IMREAD_REDUCED_GRAYSCALE_8)
    return round(float(img.mean()), 1) if img is not None else 0.0


def read_levels(session_dir: Path) -> dict:
    try:
        return json.loads((session_dir / LEVELS_FILE).read_text())
    except (OSError, ValueError):
        return {}


def write_levels(session_dir: Path, levels: dict) -> None:
    try:
        (session_dir / LEVELS_FILE).write_text(json.dumps(levels))
    except OSError:
        pass                     # a cache miss next time is not worth failing over


def open_camera(tuning_file: str) -> Picamera2:
    """Open the camera with the AGC's shutter ceiling raised.

    Auto-exposure never picks a shutter longer than the last entry of the tuning
    file's `rpi.agc` exposure_modes — 66.7 ms for the IMX219, whatever the light.
    Raising it here only widens what AE *may* choose; what it actually uses is
    bounded by FrameDurationLimits, which is the knob the UI exposes.
    """
    try:
        tuning = Picamera2.load_tuning_file(tuning_file)
        agc = Picamera2.find_tuning_algo(tuning, "rpi.agc")
        for channel in agc.get("channels", [agc]):
            for mode in channel["exposure_modes"].values():
                shutter = [s for s in mode["shutter"] if s < EXPOSURE_CEILING_US]
                mode["shutter"] = shutter + [EXPOSURE_CEILING_US]
                gain = mode["gain"]
                # The two lists index each other and must stay the same length.
                mode["gain"] = (gain + [gain[-1]] * len(shutter))[:len(mode["shutter"])]
        return Picamera2(tuning=tuning)
    except Exception as exc:          # an unknown tuning file must not stop the app
        print(f"could not patch tuning {tuning_file!r} ({exc}); "
              f"exposure stays capped by the stock AGC limits", flush=True)
        return Picamera2()


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
                 period_s: float, state_path: Path, tuning_file: str,
                 max_exposure_us: int):
        self.base_dir = base_dir
        self.sensor_size = sensor_size
        self.gain = gain
        self.state_path = state_path
        self.period_s = period_s
        self.max_exposure_us = max_exposure_us
        self.roi: dict | None = None          # {x, y, w, h} in sensor px; None = full frame
        self.session: str = ""                # the open session; empty when stopped
        self.recording = False
        self.lock = threading.Lock()          # one camera consumer at a time
        self.wake = threading.Event()         # period change / capture now
        self.last_path: Path | None = None
        self.last_time: float | None = None
        self.last_error: str | None = None
        self.next_time: float = 0.0
        self.metadata: dict = {}
        self.busy_since: float | None = None   # set while a camera call is in flight
        self.last_level: float | None = None   # brightness of the newest still
        self.mode = "setup"
        self.preview_cache: bytes | None = None   # last still, shown while recording

        self.cam = open_camera(tuning_file)
        self.configure("setup")

    # ── camera modes ─────────────────────────────────────────────────────────
    def build_config(self, mode: str):
        """Framing and capturing want different cameras, so each gets its own.

        setup  — a small preview stream off the binned full-FOV mode: smooth, cheap,
                 and framing is all it is for.
        record — a still configuration at the full readout, which is what actually
                 improves the pictures: high-quality noise reduction instead of the
                 video pipeline's fast path, and no second stream competing for
                 bandwidth or memory on a 512 MB Pi.

        Both keep the same field of view, so a region dragged while framing crops
        the same part of the scene once recording starts.
        """
        limits = {"FrameDurationLimits": (MIN_FRAME_US, self.max_exposure_us)}
        if mode == "record":
            return self.cam.create_still_configuration(
                main={"size": self.sensor_size, "format": "YUV420"},
                buffer_count=1,
                controls={**limits,
                          "NoiseReductionMode": lc.draft.NoiseReductionModeEnum.HighQuality})
        return self.cam.create_video_configuration(
            main={"size": LORES_SIZE, "format": "YUV420"},
            raw={"size": BINNED_SIZE},        # full-FOV mode, so framing matches
            buffer_count=3, controls=limits)

    def configure(self, mode: str) -> None:
        with self.lock:
            self.busy_since = time.time()
            try:
                if self.cam.started:
                    self.cam.stop()
                self.cam.configure(self.build_config(mode))
                self.cam.start()
                self.cam.set_controls(manual_controls(None, self.gain))
                self.mode = mode
            finally:
                self.busy_since = None
        time.sleep(2)                         # let AE settle in the new mode

    # ── state ────────────────────────────────────────────────────────────────
    def load_state(self) -> None:
        """Settings and any open session survive a restart; the flags are defaults.

        A run interrupted by a power cut resumes into its own session rather than
        starting a second one for the same stretch of time.
        """
        try:
            saved = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            saved = {}
        if "period_s" in saved:
            self.period_s = float(saved["period_s"])
        elif "period_min" in saved:                       # state from before seconds
            self.period_s = float(saved["period_min"]) * 60
        self.roi = saved.get("roi")
        self.gain = float(saved.get("gain", self.gain))
        self.max_exposure_us = int(saved.get("max_exposure_us", self.max_exposure_us))
        self.session = saved.get("session") or ""
        self.recording = bool(saved.get("recording", False)) and bool(self.session)
        if self.recording:
            (self.base_dir / self.session).mkdir(parents=True, exist_ok=True)
            self.configure("record")          # a run resumes in its own mode
        else:
            self.session = ""                             # nothing is open when stopped
        self.apply_camera()

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(
            {"period_s": self.period_s, "roi": self.roi, "gain": self.gain,
             "max_exposure_us": self.max_exposure_us,
             "session": self.session, "recording": self.recording}, indent=1))

    def sensor_limits(self) -> dict:
        """What the sensor mode itself allows, in ms — the real ceiling and floor.

        A full-res frame cannot be shorter than its readout (~47 ms here), so a
        max-exposure below that is raised by libcamera rather than honoured.
        """
        lo, hi, _ = self.cam.camera_controls["ExposureTime"]
        frame_lo = self.cam.camera_controls["FrameDurationLimits"][0]
        return {"exposure_min_ms": round(lo / 1000, 2),
                "exposure_max_ms": round(hi / 1000),
                "frame_min_ms": round(frame_lo / 1000, 1)}

    def crop_size(self) -> tuple[int, int]:
        return (self.roi["w"], self.roi["h"]) if self.roi else self.sensor_size

    def apply_camera(self) -> None:
        """Pin the gain and the exposure ceiling; AE works within both.

        A frame can never take less time than its exposure, so the frame-duration
        limit *is* the longest exposure AE can reach. The stock video configuration
        holds it near 1/30 s, which is why AE stalls around 40 ms however dark it is.
        """
        with self.camera() as cam:
            cam.set_controls({**manual_controls(None, self.gain),
                              "FrameDurationLimits": (MIN_FRAME_US, self.max_exposure_us)})

    def start(self) -> str:
        """Open a session named for its time and frame size, and begin capturing."""
        w, h = self.crop_size()
        self.session = f"{datetime.now():%Y%m%d_%H%M%S}_{w}x{h}"
        (self.base_dir / self.session).mkdir(parents=True, exist_ok=True)
        self.preview_cache = None
        self.configure("record")
        self.recording = True
        self.save_state()
        self.wake.set()                       # shoot now, then on the period
        return self.session

    def stop(self) -> str:
        """Close the session. One that never took a photo leaves nothing behind."""
        closed, self.session, self.recording = self.session, "", False
        if closed and not any((self.base_dir / closed).glob("*.jpg")):
            shutil.rmtree(self.base_dir / closed, ignore_errors=True)
        self.configure("setup")
        self.save_state()
        self.wake.set()
        return closed

    def set_setup(self, period_s: float, roi: dict | None, gain: float,
                  max_exposure_us: int) -> None:
        """Settings for the next run — only while stopped, so a session is uniform."""
        self.period_s = period_s
        self.roi = roi
        if (gain, max_exposure_us) != (self.gain, self.max_exposure_us):
            self.gain, self.max_exposure_us = gain, max_exposure_us
            self.apply_camera()               # visible in the live view straight away
        self.save_state()

    def delete_session(self, name: str) -> str:
        """Remove a session and its stills. Deleting the open one stops recording."""
        if "/" in name or ".." in name:
            raise ValueError(f"bad session name: {name}")
        target = self.base_dir / name
        if not target.is_dir():
            raise FileNotFoundError(f"no such session: {name}")
        shutil.rmtree(target)
        if name == self.session:
            self.session, self.recording = "", False
            self.save_state()
        return self.session

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
                        "current": self.recording and d.name == self.session})
        return out

    # ── frames ───────────────────────────────────────────────────────────────
    @contextmanager
    def camera(self):
        """Exclusive camera access, timed so the watchdog can see a wedged call.

        The vc4 pipeline occasionally never returns a frame ("Camera frontend has
        timed out!"). The call blocks forever holding this lock, which freezes the
        capture loop and the live view alike, and the process stays alive so
        systemd sees nothing wrong — exactly the silent stall that a plain
        Restart=always cannot catch.
        """
        with self.lock:
            self.busy_since = time.time()
            try:
                yield self.cam
            finally:
                self.busy_since = None

    def to_bgr(self, stream: str, yuv: "np.ndarray") -> "np.ndarray":
        """YUV420 -> BGR, dropping the ISP's row padding.

        Every row is padded out to a hardware stride (3280 -> 3328 on the IMX219's
        full readout), and those pad bytes decode as a green band down the right
        edge. Y, U and V each have to be cut back to the real width *before* OpenCV
        sees them, or the planes no longer line up.
        """
        cfg = self.cam.camera_configuration()[stream]
        w, h = cfg["size"]
        stride = cfg["stride"]
        if stride == w:                                   # no padding to undo
            return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
        i420 = np.empty((h * 3 // 2, w), dtype=np.uint8)
        i420[:h] = yuv[:h, :w]                            # luma
        chroma = yuv[h:].reshape(h, stride // 2)          # U rows then V rows
        i420[h:] = np.ascontiguousarray(chroma[:, :w // 2]).reshape(h // 2, w)
        return cv2.cvtColor(i420, cv2.COLOR_YUV2BGR_I420)

    def preview_jpeg(self) -> bytes:
        """Live while framing; while recording, the newest still instead.

        Recording runs the still configuration, which has no preview stream — and
        re-reading 8 MP every two seconds to animate a picture nobody is framing
        would cost more than the timelapse itself.
        """
        if self.mode == "record":
            if self.preview_cache is None:
                raise RuntimeError("no capture yet")
            return self.preview_cache
        with self.camera() as cam:
            yuv = cam.capture_array("main")
            self.metadata = cam.capture_metadata()
        return self.encode_preview(self.to_bgr("main", yuv), 80)

    @staticmethod
    def encode_preview(bgr, quality: int) -> bytes:
        h, w = bgr.shape[:2]
        if w > LORES_SIZE[0]:
            scale = LORES_SIZE[0] / w
            bgr = cv2.resize(bgr, (LORES_SIZE[0], max(1, int(h * scale))),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("JPEG encode failed")
        return buf.tobytes()

    def capture_still(self, session: str) -> Path:
        """Full-res frame, cropped to the ROI, saved at native pixels.

        The session is passed in, not read from self: a capture takes seconds, and
        a Stop or a delete landing in the middle of one used to leave the still
        writing into an empty session name — i.e. loose in the captures directory.
        """
        if not session:
            raise RuntimeError("no open session")
        with self.camera() as cam:
            yuv = cam.capture_array("main")
            self.metadata = cam.capture_metadata()
        bgr = self.to_bgr("main", yuv)
        if self.roi:
            r = self.roi
            bgr = bgr[r["y"]:r["y"] + r["h"], r["x"]:r["x"] + r["w"]]
        out_dir = self.base_dir / session
        if not out_dir.is_dir():              # stopped or deleted while we exposed
            raise RuntimeError(f"session {session} is gone")
        # Second resolution collides when a manual shot lands in the same second as a
        # scheduled one; the suffix keeps both, and still sorts after the plain name.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"{stamp}.jpg"
        for n in range(2, 100):
            if not path.exists():
                break
            path = out_dir / f"{stamp}_{n}.jpg"
        if not cv2.imwrite(str(path), bgr, [cv2.IMWRITE_JPEG_QUALITY, 92]):
            raise RuntimeError(f"could not write {path}")
        # Measure here, off the array already in hand, so generating a video later
        # never has to decode this frame just to find out how dark it is.
        self.last_level = round(float(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).mean()), 1)
        levels = read_levels(out_dir)
        levels[path.name] = self.last_level
        write_levels(out_dir, levels)
        self.preview_cache = self.encode_preview(bgr, 80)
        self.last_path, self.last_time, self.last_error = path, time.time(), None
        return path

    def watchdog(self, limit_s: float) -> None:
        """Bail out of the process if a camera call wedges; systemd restarts us."""
        while True:
            time.sleep(5)
            started = self.busy_since
            if started is not None and time.time() - started > limit_s:
                print(f"camera call stuck for {time.time() - started:.0f}s "
                      f"(limit {limit_s}s) — exiting for a restart", flush=True)
                os._exit(1)

    def capture_loop(self) -> None:
        while True:
            if not self.recording:
                self.next_time = 0.0
                self.wake.wait(timeout=5)
                self.wake.clear()
                continue
            try:
                self.capture_still(self.session)
            except Exception as exc:                       # keep the loop alive
                self.last_error = f"{type(exc).__name__}: {exc}"
            self.next_time = time.time() + self.period_s
            while time.time() < self.next_time:
                self.wake.wait(timeout=min(self.next_time - time.time(), 5))
                if self.wake.is_set():                     # period/ROI changed
                    self.wake.clear()
                    break


class VideoJob:
    """One ffmpeg run over a session's stills: MP4 or GIF, with progress."""

    def __init__(self, src: Path, out_dir: Path, fps: float, height: int, fmt: str,
                 min_level: float = 0.0):
        self.src = src
        self.out_dir = out_dir
        self.fps = fps
        self.height = height          # 0 = native crop size, no scaling
        self.fmt = fmt
        self.min_level = min_level    # 0 = keep every frame
        self.skipped = 0
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
                "min_level": self.min_level, "skipped": self.skipped,
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

    def keep(self, shots: list[Path]) -> list[Path]:
        """Drop frames darker than the threshold, measuring any not already known."""
        if self.min_level <= 0:
            return shots
        self.state = "measuring"
        levels = read_levels(self.src)
        missing = [p for p in shots if p.name not in levels]
        for p in missing:                       # stills from before this cache existed
            levels[p.name] = brightness_of(p)
        if missing:
            write_levels(self.src, levels)
        return [p for p in shots if levels.get(p.name, 0.0) >= self.min_level]

    def run(self) -> None:
        listing = None
        try:
            shots = sorted(self.src.glob("*.jpg"))
            found = len(shots)
            shots = self.keep(shots)
            self.skipped = found - len(shots)
            self.frames = len(shots)
            if self.frames < 2:
                self.state = "failed"
                self.error = (f"only {self.frames} of {found} frames are brighter than "
                              f"{self.min_level} — lower the threshold"
                              if self.skipped else "need at least 2 photos")
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
        try:
            data = cap.preview_jpeg()
        except RuntimeError as exc:           # recording, before the first capture
            return Response(str(exc), status=503)
        return Response(data, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/status")
    def status():
        usage = shutil.disk_usage(cap.base_dir)
        md = cap.metadata
        sessions = cap.sessions()
        current = next((s for s in sessions if s["current"]), None)
        with job_lock:
            job = jobs[-1].as_dict() if jobs else None
        photos = current["count"] if current else 0
        mb_each = (current["mb"] / photos) if photos else 0
        return jsonify({
            "recording": cap.recording,
            "period_s": cap.period_s,
            "max_exposure_ms": round(cap.max_exposure_us / 1000),
            "limits": cap.sensor_limits(),
            "mode": cap.mode,
            "mb_per_day": round(mb_each * 86400 / cap.period_s, 1),
            "gain": cap.gain,
            "sensor_size": list(cap.sensor_size),
            "crop_size": list(cap.crop_size()),
            "roi": cap.roi,
            "session": cap.session or None,
            "sessions": sessions,
            "photo_count": current["count"] if current else 0,
            "photos_mb": current["mb"] if current else 0,
            "last_photo": cap.last_path.name if cap.last_path else None,
            "last_capture_ago_s": round(time.time() - cap.last_time)
                                  if cap.last_time else None,
            "next_capture_in_s": None if not cap.recording
                                 else max(0, round(cap.next_time - time.time())),
            "last_level": cap.last_level,
            "exposure_ms": round(md.get("ExposureTime", 0) / 1000, 1),
            "actual_gain": round(md.get("AnalogueGain", 0), 2),
            "disk_free_mb": round(usage.free / 1e6),
            "error": cap.last_error,
            "job": job,
        })

    @app.post("/setup")
    def setup():
        """Period, region and gain for the next run. Refused while recording."""
        if cap.recording:
            return jsonify({"error": "stop the run before changing its settings"}), 409
        body = request.json or {}
        seconds = float(body.get("period_s", cap.period_s))
        if not MIN_PERIOD_S <= seconds <= 24 * 3600:
            return jsonify({"error": f"period must be {MIN_PERIOD_S}s–24h"}), 400
        gain = float(body.get("gain", cap.gain))
        if not 1.0 <= gain <= 16.0:
            return jsonify({"error": "gain must be 1–16"}), 400
        max_us = int(float(body.get("max_exposure_ms", cap.max_exposure_us / 1000)) * 1000)
        if not 1000 <= max_us <= EXPOSURE_CEILING_US:
            return jsonify({"error": f"max exposure must be 1–{EXPOSURE_CEILING_US // 1000} ms"}), 400
        roi = cap.roi if "roi" not in body else body["roi"]
        if roi:
            sw, sh = cap.sensor_size
            try:
                x, y = even(max(0, roi["x"])), even(max(0, roi["y"]))
                w, h = even(roi["w"]), even(roi["h"])
            except (KeyError, TypeError, ValueError):
                return jsonify({"error": "roi needs numeric x, y, w, h"}), 400
            w, h = min(w, sw - x), min(h, sh - y)
            if w < MIN_ROI or h < MIN_ROI:
                return jsonify({"error": f"region must be at least {MIN_ROI}x{MIN_ROI} px"}), 400
            roi = {"x": x, "y": y, "w": w, "h": h}
        cap.set_setup(seconds, roi or None, gain, max_us)
        return jsonify({"period_s": cap.period_s, "roi": cap.roi, "gain": cap.gain,
                        "max_exposure_ms": round(cap.max_exposure_us / 1000),
                        "crop_size": list(cap.crop_size())})

    @app.post("/start")
    def start():
        if cap.recording:
            return jsonify({"error": "already recording"}), 409
        return jsonify({"session": cap.start(), "recording": True})

    @app.post("/stop")
    def stop():
        if not cap.recording:
            return jsonify({"error": "not recording"}), 409
        return jsonify({"closed": cap.stop(), "recording": False})

    @app.post("/session/delete")
    def delete_session():
        name = (request.json or {}).get("name", "")
        try:
            current = cap.delete_session(name)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        # Its videos are named after it, and outlive it as files nothing refers to.
        removed = 0
        for v in video_dir.glob(f"{name}_*"):
            if v.suffix in VIDEO_SUFFIXES:
                v.unlink(missing_ok=True)
                removed += 1
        return jsonify({"deleted": name, "session": current, "removed_videos": removed})

    @app.post("/timelapse")
    def make_timelapse():
        body = request.json or {}
        session = body.get("session") or cap.session
        if not session:
            return jsonify({"error": "no session selected"}), 400
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
                           height=int(body.get("height", 720)), fmt=fmt,
                           min_level=float(body.get("min_level", 0)))
            jobs.append(job)
            del jobs[:-JOB_KEEP]
        threading.Thread(target=job.run, daemon=True).start()
        return jsonify(job.as_dict())

    @app.get("/videos")
    def list_videos():
        vids = sorted([p for p in video_dir.glob("*") if p.suffix in VIDEO_SUFFIXES],
                      key=lambda p: p.stat().st_mtime, reverse=True)
        names = [s["name"] for s in cap.sessions()]
        out = []
        for v in vids:
            owner = next((n for n in names if v.name.startswith(n + "_")), None)
            out.append({"name": v.name, "session": owner,
                        "mb": round(v.stat().st_size / 1e6, 1),
                        "mtime": v.stat().st_mtime})
        return jsonify(out)

    @app.get("/videos/<name>")
    def get_video(name: str):
        return send_from_directory(video_dir, name)

    @app.post("/videos/delete")
    def delete_video():
        name = (request.json or {}).get("name", "")
        if "/" in name or ".." in name or Path(name).suffix not in VIDEO_SUFFIXES:
            return jsonify({"error": f"bad file name: {name}"}), 400
        target = video_dir / name
        if not target.is_file():
            return jsonify({"error": f"no such file: {name}"}), 404
        target.unlink()
        return jsonify({"deleted": name})

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
                        help="Default MINUTES between stills, for the first run only — "
                             "afterwards the value set in the web UI is remembered")
    parser.add_argument("--stall-limit", type=float, default=STALL_LIMIT_S,
                        help="Exit for a restart if a camera call blocks this long")
    parser.add_argument("--gain", type=float, default=3.0,
                        help="Default fixed analogue gain (the web UI sets it too); "
                             "exposure time always stays automatic")
    parser.add_argument("--camera", default="v2.1", choices=sorted(SENSORS),
                        help="Camera module (sets the full-res sensor size)")
    parser.add_argument("--max-exposure-ms", type=float, default=200.0,
                        help="Longest exposure auto-exposure may use (the web UI sets "
                             "it too). The live view runs at 1/exposure fps in the dark")
    args = parser.parse_args()

    cap = Capture(Path(args.captures), SENSORS[args.camera], args.gain,
                  args.period * 60, Path(args.state), TUNING[args.camera],
                  int(args.max_exposure_ms * 1000))
    cap.load_state()
    threading.Thread(target=cap.capture_loop, daemon=True).start()
    threading.Thread(target=cap.watchdog, args=(args.stall_limit,), daemon=True).start()

    app = create_app(cap, Path(args.video_dir))
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
