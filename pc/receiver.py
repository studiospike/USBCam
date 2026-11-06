#!/usr/bin/env python3
"""
USBCam receiver — shows the phone camera over USB in a borderless, freely
resizable window (for OBS Window Capture).

- Finds adb automatically: bundled copy first, then the standard SDK folders,
  then PATH. If adb is nowhere to be found it downloads the official Android
  platform-tools once, so a fresh Windows install works with no setup.
- Runs `adb reverse tcp:PORT tcp:PORT` every few seconds (heals cable replugs),
  and listens on 127.0.0.1:PORT.
- Protocol per frame (little-endian): u32 payloadLength, then payloadLength bytes;
  payload = 1 camera byte (0=back, 1=front) + JPEG bytes.
- Never buffers: on every connect it resyncs at the stream head, so latency
  never grows. The window draws ONLY the video — nothing else, ever.
- When there is no video yet, the window shows a readable status/error message
  instead of a silent black rectangle (so "adb missing", "unauthorized",
  "no phone" and "open the phone app" all look different).
- Everything is written to %LOCALAPPDATA%\\USBCam\\usbcam.log for support.

Controls (right-click anywhere):
    Always on top / Mirror / Flip vertical / Fullscreen / vcam /
    Open log file / Close
    Esc leaves fullscreen, F11 toggles it, left-drag moves the window.
"""
import logging
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu

try:
    import numpy as np
    import pyvirtualcam
    VIRTUAL_CAM_AVAILABLE = True
except ImportError:
    VIRTUAL_CAM_AVAILABLE = False

APP_VERSION = "1.2.0"
PORT = 8420
CHUNK = 256 * 1024

# platform-tools zip (adb.exe + its DLLs). Our own GitHub asset is tried first
# because dl.google.com is blocked on some networks (e.g. Iran).
PLATFORM_TOOLS_URLS = (
    "https://github.com/studiospike/USBCam/releases/latest/download/platform-tools-win.zip",
    "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
)

# Prevent console windows from flashing on every adb call in the windowed exe.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# paths / logging
# ---------------------------------------------------------------------------

def app_dir() -> Path:
    """Folder the exe (or receiver.py) lives in."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Writable per-user folder (never Program Files / the extracted temp dir)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "USBCam"


def _meipass_dir():
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


LOG = logging.getLogger("usbcam")


def setup_logging():
    LOG.setLevel(logging.INFO)
    if LOG.handlers:
        return
    try:
        folder = data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            folder / "usbcam.log", maxBytes=2_000_000, backupCount=2,
            encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(threadName)s %(message)s"))
        LOG.addHandler(handler)
    except Exception:
        LOG.addHandler(logging.NullHandler())

    def _excepthook(exc_type, exc, tb):
        LOG.exception("uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook


# ---------------------------------------------------------------------------
# adb discovery / bootstrap
# ---------------------------------------------------------------------------

def run_quiet(cmd, **kw):
    """subprocess.run without any visible console window (Windows)."""
    kw.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.run(cmd, **kw)


def _candidate_adb_paths():
    """Every place an adb.exe could reasonably live, best first."""
    dirs = []
    meipass = _meipass_dir()
    if meipass:
        dirs.append(meipass)          # PyInstaller onefile bundle
    dirs.append(app_dir())            # next to the exe / source tree
    dirs.append(data_dir())           # our own downloaded copy
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk:
        dirs.append(Path(sdk))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Android" / "Sdk")

    seen = set()
    for d in dirs:
        try:
            p = (Path(d) / "platform-tools" / "adb.exe").resolve()
        except OSError:
            continue
        if p in seen:
            continue
        seen.add(p)
        yield p

    which = shutil.which("adb")
    if which:
        yield Path(which)


def _adb_works(path) -> bool:
    try:
        run_quiet([str(path), "version"], stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, timeout=10, check=True)
        return True
    except Exception:
        return False


def find_adb():
    """Return a working adb path, or None."""
    for path in _candidate_adb_paths():
        if path.is_file() and _adb_works(path):
            return str(path)
    return None


def _download_platform_tools(progress=None) -> str:
    """Download + unzip platform-tools into %LOCALAPPDATA%\\USBCam.

    `progress(done, total)` is called while downloading (total may be 0).
    Returns the adb path, or raises on failure.
    """
    dest = data_dir()
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / "platform-tools.zip"

    def _fetch(url, context):
        with urllib.request.urlopen(url, timeout=60,
                                    context=context) as resp, \
                open(archive, "wb") as out:
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)

    last_error = None
    for url in PLATFORM_TOOLS_URLS:
        archive.unlink(missing_ok=True)
        try:
            _fetch(url, ssl.create_default_context())
            break
        except ssl.SSLError:
            # Some frozen builds have no CA bundle. The URLs are fixed and
            # HTTPS, so fall back rather than leaving the user with no adb.
            LOG.warning("SSL verification failed for %s; retrying unverified", url)
            archive.unlink(missing_ok=True)
            try:
                _fetch(url, ssl._create_unverified_context())
                break
            except Exception as exc:
                last_error = exc
                LOG.warning("download failed from %s: %s", url, exc)
        except Exception as exc:
            last_error = exc
            LOG.warning("download failed from %s: %s", url, exc)
    else:
        raise RuntimeError(f"all download mirrors failed (last: {last_error})")

    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"unsafe path in zip: {info.filename}")
        zf.extractall(dest)
    archive.unlink(missing_ok=True)

    adb = dest / "platform-tools" / "adb.exe"
    if not adb.is_file():
        raise RuntimeError("platform-tools.zip did not contain adb.exe")
    return str(adb)


# ---------------------------------------------------------------------------
# adb keep-alive thread
# ---------------------------------------------------------------------------

def parse_device_states(output: str):
    """States from `adb devices` output: attached / unauthorized / offline …"""
    states = []
    for line in (output or "").splitlines():
        line = line.strip()
        if (not line or line.startswith("List of devices")
                or line.startswith("*") or line.startswith("adb ")):
            continue
        parts = line.split()
        if len(parts) >= 2:
            states.append(parts[1].lower())
    return states


class AdbSetupThread(QThread):
    """Finds/bootstraps adb, runs `adb reverse`, and keeps it alive."""
    status = Signal(str)

    def __init__(self, port=PORT):
        super().__init__()
        self.port = port
        self._stop = False

    def _progress(self, done, total):
        if total:
            pct = min(99, int(done * 100 / total))
            self.status.emit(
                f"Downloading adb (Android platform-tools)... {pct}%\n"
                "This happens only once.")
        else:
            self.status.emit(
                f"Downloading adb (Android platform-tools)... {done // 1024} KB\n"
                "This happens only once.")

    def run(self):
        self.status.emit("Looking for adb...")
        adb = find_adb()
        if not adb:
            LOG.info("adb not found locally; downloading platform-tools")
            self.status.emit(
                "adb not found - downloading Android platform-tools...\n"
                "This happens only once; keep the PC online.")
            try:
                adb = _download_platform_tools(self._progress)
            except Exception as exc:
                LOG.exception("platform-tools download failed")
                self.status.emit(
                    "Could not find or download adb.\n"
                    "Install Android platform-tools, then restart USBCam.\n"
                    f"({exc})")
                return
            if not _adb_works(adb):
                self.status.emit(
                    "Downloaded adb, but it does not run on this PC.\n"
                    "Install Android platform-tools manually.")
                return
            LOG.info("downloaded adb to %s", adb)

        LOG.info("using adb: %s", adb)
        while not self._stop:
            try:
                run_quiet([adb, "start-server"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=15)
                raw = run_quiet([adb, "devices"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=10
                                ).stdout.decode(errors="replace")
                states = parse_device_states(raw)

                if "device" in states:
                    run_quiet([adb, "reverse", f"tcp:{self.port}", f"tcp:{self.port}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=10)
                    LOG.info("phone connected (adb reverse refreshed)")
                    self.status.emit(
                        "Phone connected - waiting for video.\n"
                        "Open the USBCam app on your phone (it starts streaming).")
                elif "unauthorized" in states:
                    LOG.info("phone is unauthorized")
                    self.status.emit(
                        "Phone found, but USB debugging is not authorized.\n"
                        'Unlock the phone, tap "Allow" on the USB debugging prompt,\n'
                        "then reconnect the cable.")
                elif "offline" in states:
                    LOG.info("phone is offline")
                    self.status.emit(
                        "Phone is offline in adb.\n"
                        "Reconnect the cable, or run: adb kill-server")
                else:
                    LOG.info("no adb device attached")
                    self.status.emit(
                        "No phone detected.\n"
                        "1) Connect the USB cable (file transfer mode is fine)\n"
                        "2) Enable Developer options → USB debugging\n"
                        '3) Accept the "Allow USB debugging" prompt on the phone')
            except Exception as exc:
                LOG.exception("adb loop error")
                self.status.emit(f"adb error: {exc}")

            for _ in range(100):  # sleep 5 s in small steps so stop stays responsive
                if self._stop:
                    return
                time.sleep(0.05)

    def stop(self):
        self._stop = True


# ---------------------------------------------------------------------------
# frame server
# ---------------------------------------------------------------------------

class FrameReader(threading.Thread):
    """TCP server on 127.0.0.1:PORT (the phone connects via `adb reverse`).
    Keeps only the newest complete JPEG for the GUI thread."""

    def __init__(self, host="127.0.0.1", port=PORT):
        super().__init__(daemon=True)
        self.server = None
        self.sock = None          # current phone connection (for status display)
        self.lock = threading.Lock()
        self.current = None       # latest full frame (bytes, JPEG)
        self.counter = 0
        self.host = host
        self.port = port
        self.stop_flag = False
        self.error = None         # set if the listening socket could not open

    def stop(self):
        self.stop_flag = True
        for s in (self.sock, self.server):
            try:
                if s:
                    s.shutdown(socket.SHUT_RDWR)
                    s.close()
            except OSError:
                pass

    def run(self):
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((self.host, self.port))
            self.server.listen(2)
        except OSError as exc:
            self.error = (
                f"Cannot listen on port {self.port}: {exc}\n"
                "Another program is using that port. Close it and restart USBCam.")
            LOG.exception("failed to open TCP listener")
            return

        while not self.stop_flag:
            try:
                conn, _ = self.server.accept()
            except OSError:
                break
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(3)
                self.sock = conn
                with self.lock:           # fresh start: throw away stale frames
                    self.current = None
                    self.counter = 0
                while not self.stop_flag:
                    self.read_frame(conn)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                self.sock = None

    def read_frame(self, s):
        header = self.recv_exact(s, 4)          # any error here -> reconnect+resync
        (length,) = struct.unpack("<I", header)
        if length <= 1 or length > 20 * 1024 * 1024:
            raise OSError("bad frame length")
        payload = self.recv_exact(s, length)
        with self.lock:
            self.current = payload[1:]          # strip camera byte; payload = JPEG
            self.counter += 1

    @staticmethod
    def recv_exact(s, n):
        chunks = []
        while n > 0:
            chunk = s.recv(min(n, CHUNK))       # timeout/close raise OSError
            if not chunk:
                raise OSError("closed")
            chunks.append(chunk)
            n -= len(chunk)
        return b"".join(chunks)

    def get_frame(self):
        with self.lock:
            return self.current, self.counter


# ---------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------

class VideoWindow(QMainWindow):
    """Borderless window showing ONLY the video (plus status while waiting)."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setMinimumSize(160, 90)
        self.setMouseTracking(True)   # cursor feedback on edges without buttons held

        self.reader = FrameReader()
        self.reader.start()

        self.adb_thread = AdbSetupThread()
        self.adb_thread.status.connect(self._on_adb_status)
        self.adb_thread.start()

        self.mirrored = False
        self.flip_v = False
        self._last_counter = -1
        self._pixmap = None
        self._drag_offset = None
        self._resize_mode = None
        self._resize_from = None
        self._move_offset = None
        self._fps_frames = 0
        self._fps_t0 = time.time()
        self._fps = 0.0
        self._status = "Starting..."
        self._last_frame_at = 0.0

        # Virtual camera (OBS Virtual Camera / Meet / Zoom …) — off by default.
        self._vcam = None
        self._vcam_error = None
        if not VIRTUAL_CAM_AVAILABLE:
            self._vcam_error = "pyvirtualcam/numpy not installed"

        self._set_status(self._status)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(15)

    # ---------- status / title ----------
    def _set_status(self, msg):
        self._status = msg
        if self._pixmap is None:
            first = msg.splitlines()[0] if msg else ""
            self.setWindowTitle(f"USBCam {APP_VERSION} - {first}")
            self.update()

    def _on_adb_status(self, msg):
        LOG.info("status: %s", msg.replace("\n", " | "))
        self._set_status(self._with_vcam(msg))

    def _with_vcam(self, msg):
        if self._vcam is not None:
            return msg + "\n(virtual camera ON)"
        if self._vcam_error:
            return msg + "\n(virtual camera unavailable)"
        return msg

    # ---------- drawing ----------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)

        if self._pixmap is None:
            self._paint_message(p)
            return

        scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        sx = -1.0 if self.mirrored else 1.0
        sy = -1.0 if self.flip_v else 1.0
        if sx != 1.0 or sy != 1.0:
            p.save()
            p.translate(x + (scaled.width() if sx < 0 else 0),
                        y + (scaled.height() if sy < 0 else 0))
            p.scale(sx, sy)
            p.drawPixmap(0, 0, scaled)
            p.restore()
        else:
            p.drawPixmap(x, y, scaled)

    def _paint_message(self, p):
        """Draw a readable status/error while there is no video yet."""
        w, h = self.width(), self.height()
        margin = max(16, min(48, w // 12))

        p.setPen(QColor(255, 255, 255))
        title_font = QFont()
        title_font.setPointSize(max(13, min(30, w // 34)))
        title_font.setBold(True)
        p.setFont(title_font)
        p.drawText(QRect(margin, margin, w - 2 * margin, 60),
                   Qt.AlignHCenter | Qt.AlignTop, "USBCam")

        body_font = QFont()
        body_font.setPointSize(max(9, min(16, w // 64)))
        p.setFont(body_font)

        msg = self._status
        if self.reader.error:
            msg = self.reader.error

        body_rect = QRect(margin, margin + 60, w - 2 * margin, h - margin - 120)
        p.setPen(QColor(225, 225, 225))
        p.drawText(body_rect,
                   Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, msg)

        if h > 220:
            hint_font = QFont()
            hint_font.setPointSize(max(8, min(12, w // 80)))
            p.setFont(hint_font)
            p.setPen(QColor(140, 140, 140))
            p.drawText(QRect(margin, h - margin - 40, w - 2 * margin, 40),
                       Qt.AlignHCenter | Qt.AlignBottom,
                       "Right-click for menu  |  F11 fullscreen  |  Esc to exit")

    def tick(self):
        if self.reader.error:
            if self._pixmap is None:
                self.setWindowTitle(f"USBCam {APP_VERSION} - port {self.reader.port} busy")
                self.update()
            return

        frame, counter = self.reader.get_frame()
        if counter == self._last_counter:
            return
        self._last_counter = counter
        img = QImage.fromData(frame, "JPEG")
        if not img.isNull():
            self._pixmap = QPixmap.fromImage(img)
            self._last_frame_at = time.time()
            self._fps_frames += 1
            now = time.time()
            if now - self._fps_t0 >= 1.0:
                self._fps = self._fps_frames / (now - self._fps_t0)
                self._fps_frames = 0
                self._fps_t0 = now
                w, h = self._pixmap.width(), self._pixmap.height()
                self.setWindowTitle(
                    f"USBCam {APP_VERSION} - {self._status.splitlines()[0]} - "
                    f"{w}x{h} @ {self._fps:.0f} fps")
            self._send_to_vcam(img)
            self.update()

    # ---------- virtual camera ----------
    def _send_to_vcam(self, img):
        """Forward the current frame to the virtual camera, if active."""
        if self._vcam is None:
            return
        try:
            w, h = self._vcam.width, self._vcam.height
            scaled = img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            scaled = scaled.convertToFormat(QImage.Format_BGR888)  # returns NEW image!
            buf = scaled.constBits()
            arr = np.frombuffer(buf, dtype=np.uint8)
            arr = arr.reshape(scaled.height(), scaled.bytesPerLine())[:, : w * 3]
            arr = arr.reshape(h, w, 3)   # pyvirtualcam wants (h, w, 3)
            self._vcam.send(np.ascontiguousarray(arr))
        except Exception as e:
            LOG.exception("virtual camera send failed")
            self._vcam_error = str(e)
            self._stop_vcam()

    def _start_vcam(self):
        if self._vcam is not None:
            return
        if not VIRTUAL_CAM_AVAILABLE:
            self._vcam_error = "pyvirtualcam/numpy not installed"
            return
        if self._pixmap is None:
            self._vcam_error = "no frame yet"
            return
        # Native size first; OBS driver caps at 1920x1080 (quirk: needs non-zero size).
        img = self._pixmap.toImage().convertToFormat(QImage.Format_BGR888)
        w, h = img.width(), img.height()
        if w == 0 or h == 0:
            self._vcam_error = "empty frame"
            return
        if w > 1920:
            h = int(h * 1920 / w)
            w = 1920
        if h > 1080:
            w = int(w * 1080 / h)
            h = 1080
        try:
            self._vcam = pyvirtualcam.Camera(width=w, height=h, fps=30)
            self._vcam_error = None
            LOG.info("virtual camera started %dx%d", w, h)
            self._status = self._with_vcam(self._status.split("\n(virtual")[0])
        except Exception as e:
            LOG.exception("virtual camera start failed")
            self._vcam_error = str(e)
            self._vcam = None

    def _stop_vcam(self):
        if self._vcam is not None:
            try:
                self._vcam.close()
            except Exception:
                pass
        self._vcam = None
        self._status = self._with_vcam(self._status.split("\n(virtual")[0])

    def _open_log(self):
        log_path = data_dir() / "usbcam.log"
        try:
            if not log_path.exists():
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.touch()
            os.startfile(str(log_path))  # noqa: S606 (Windows only)
        except Exception:
            LOG.exception("could not open log file")

    # ---------- menu / controls ----------
    def contextMenuEvent(self, ev):
        menu = QMenu(self)
        act_top = menu.addAction("Always on top")
        act_top.setCheckable(True)
        act_top.setChecked(bool(self.windowFlags() & Qt.WindowStaysOnTopHint))
        act_top.triggered.connect(self._toggle_top)
        act_mir = menu.addAction("Mirror")
        act_mir.setCheckable(True)
        act_mir.setChecked(self.mirrored)
        act_mir.triggered.connect(self._toggle_mirror)
        act_fv = menu.addAction("Flip vertical")
        act_fv.setCheckable(True)
        act_fv.setChecked(self.flip_v)
        act_fv.triggered.connect(self._toggle_flip_v)
        act_fs = menu.addAction("Fullscreen")
        act_fs.setCheckable(True)
        act_fs.setChecked(self.isFullScreen())
        act_fs.triggered.connect(self._toggle_fullscreen)
        menu.addSeparator()
        if VIRTUAL_CAM_AVAILABLE:
            act_vc = menu.addAction("Virtual camera (Meet/Zoom …)")
            act_vc.setCheckable(True)
            act_vc.setChecked(self._vcam is not None)
            act_vc.triggered.connect(self._toggle_vcam)
        elif self._vcam_error:
            act_vc = menu.addAction(f"Virtual camera unavailable ({self._vcam_error})")
            act_vc.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Open log file", self._open_log)
        menu.addAction("Close", self.close)
        menu.exec(ev.globalPos())

    def _toggle_top(self):
        on = not (self.windowFlags() & Qt.WindowStaysOnTopHint)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.show()

    def _toggle_mirror(self):
        self.mirrored = not self.mirrored
        self.update()

    def _toggle_flip_v(self):
        self.flip_v = not self.flip_v
        self.update()

    def _toggle_vcam(self):
        if self._vcam is not None:
            self._stop_vcam()
        else:
            self._start_vcam()

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
        elif ev.key() == Qt.Key_F11:
            self._toggle_fullscreen()

    # ---------- drag & resize (frameless: manual hit-test on edges) ----------
    M = 6  # resizable border width in px

    def _hit(self, pos):
        """Return which edges the point is near, e.g. 'L,T,' or '' for the middle."""
        m = self.M
        parts = ""
        if pos.x() <= m: parts += "L,"
        if pos.x() >= self.width() - m: parts += "R,"
        if pos.y() <= m: parts += "T,"
        if pos.y() >= self.height() - m: parts += "B,"
        return parts

    def _update_cursor(self, parts):
        c = {
            "L,": Qt.SizeHorCursor, "R,": Qt.SizeHorCursor,
            "T,": Qt.SizeVerCursor, "B,": Qt.SizeVerCursor,
            "L,T,": Qt.SizeFDiagCursor, "R,B,": Qt.SizeFDiagCursor,
            "R,T,": Qt.SizeBDiagCursor, "L,B,": Qt.SizeBDiagCursor,
        }.get(parts)
        if c:
            self.setCursor(c)
        else:
            self.unsetCursor()

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        self._resize_mode = self._hit(ev.position().toPoint())
        if self._resize_mode:
            self._drag_offset = ev.globalPosition().toPoint()
            self._resize_from = self.frameGeometry()
        else:
            # Manual move: the reliable way for frameless windows.
            self._move_offset = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        self._update_cursor(self._hit(ev.position().toPoint()))
        if not (ev.buttons() & Qt.LeftButton):
            return
        g = ev.globalPosition().toPoint()
        if self._resize_mode:
            base = self._resize_from
            new = QRect(base.topLeft(), base.size())
            if "L" in self._resize_mode:
                new.setLeft(min(g.x(), base.right() - self.minimumWidth()))
            if "T" in self._resize_mode:
                new.setTop(min(g.y(), base.bottom() - self.minimumHeight()))
            if "R" in self._resize_mode:
                new.setRight(max(g.x(), base.left() + self.minimumWidth() - 1))
            if "B" in self._resize_mode:
                new.setBottom(max(g.y(), base.top() + self.minimumHeight() - 1))
            self.setGeometry(new)
        elif self._move_offset is not None:
            self.move(g - self._move_offset)

    def mouseReleaseEvent(self, ev):
        self._resize_mode = None
        self._drag_offset = None
        self._move_offset = None

    def closeEvent(self, ev):
        self._stop_vcam()
        self.adb_thread.stop()
        self.adb_thread.wait(3000)
        self.reader.stop()
        super().closeEvent(ev)


def main():
    setup_logging()
    LOG.info("USBCam %s starting on Python %s", APP_VERSION, sys.version.replace("\n", " "))
    app = QApplication(sys.argv)
    win = VideoWindow()
    win.resize(1280, 720)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
