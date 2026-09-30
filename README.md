# USBCam — Use your Android phone as a USB webcam for PC / OBS

> **100% free & open source** (GPL-3.0). No ads, no account, no internet required — everything goes over the USB cable.

## ⬇️ Download (Windows & Android)

One click — these links always point to the newest release:

| Platform | Download | Requirements |
|---|---|---|
| 🪟 **Windows app** | **[Download USBCam.exe](https://github.com/studiospike/USBCam/releases/download/v1.0.0/USBCam.exe)** | Windows 10 or 11 (64-bit) — run it, no install |
| 📱 **Android app** | **[Download usbcam.apk](https://github.com/studiospike/USBCam/releases/latest/download/usbcam.apk)** | Android 8+ — install directly (allow "unknown apps") |

All releases: [Releases page](https://github.com/studiospike/USBCam/releases)

## What is this?

Two tiny apps that turn your Android phone into a webcam source for your PC:

- The **Android app** captures the camera and sends JPEG frames over the USB cable.
- The **Windows app** shows the live image in a clean, frameless "video only" window — perfect for **OBS Window Capture** (like Windowed Projector, but with full control over size and position).

- The window is *just* the camera image: no menu bar, no icons, no borders.
- Frameless, freely resizable (down to 160×90, up to fullscreen).
- Drag to move, drag edges/corners to resize.
- All controls on **right-click**: Always on top, Mirror, Flip vertical, Fullscreen, vcam, Close.
- Image always centered with aspect ratio preserved.
- USB only (`adb reverse`) — no Wi-Fi, no audio, no virtual driver needed.
- **Optional virtual camera:** send the image straight into Meet / Zoom / Discord as a webcam (right-click → vcam; requires the OBS Virtual Camera driver).
- **Update check:** the Android app tells you when a new version is out (with a Download button), if the phone has internet.

## Quick start

1. Connect the phone by USB (file transfer mode is fine; USB debugging must be enabled).
2. Run `USBCam.exe` — it finds adb itself and keeps `adb reverse` alive.
3. Open **USBCam** on the phone — streaming starts automatically.
4. In OBS: Sources → Window Capture → pick the `USBCam` window.

> OBS tip: the window is frameless, so choose the "Window" mode and enable **Client area** to capture exactly the video.

## Controls

| Action | How |
|---|---|
| Control menu | Right-click the window |
| Move | Left-click drag |
| Resize | Drag edges/corners (min 160×90) |
| Fullscreen | Menu or `F11`, exit with `Esc` |
| Mirror (selfie) | Menu → Mirror |
| Virtual camera | Menu → vcam |
| Close | Menu → Close |

The window title (taskbar / Alt-Tab) shows the status:
`USBCam — phone connected — 1920x1080 @ 30 fps`

## Android app

- The phone screen is only a control panel: front/back camera, resolution (480p up to **4K**), start/stop.
- Opening the app starts streaming automatically.
- **Switching resolution or camera applies instantly** — the stream never stops (the service reconfigures in place). Actual 4K depends on the phone's sensor.
- A dedicated **"Switch camera"** button toggles front/back in one tap.
- Streams in a Foreground Service + PARTIAL_WAKE_LOCK, so it keeps running with the screen off or in background.
- Bottom of the app shows the developer contact (email + Telegram).

Remote control (no touching the phone):

```bash
adb shell am start -n com.usb.cam/.MainActivity --ei facing 1      # front camera
adb shell am start -n com.usb.cam/.MainActivity --ei facing 0      # back camera
adb shell am start -n com.usb.cam/.MainActivity --ei size_index 2  # 1080p (0..4 = 480p..4K)
```

## Building from source

### Android (no Gradle needed)

Requirements: Android SDK (platform-tools + build-tools 36 + platforms;android-36) and a JDK.

```bat
cd android
build_apk.bat        →  android\app\build\usbcam.apk
adb install -r android\app\build\usbcam.apk
```

### Windows

```bat
python -m venv .venv
.venv\Scripts\pip install PySide6 pyvirtualcam numpy pyinstaller
.venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name USBCam --distpath pc/dist --workpath build --hidden-import pyvirtualcam --hidden-import numpy pc/receiver.py
```

## Architecture (short version)

- **Transport:** TCP over USB with `adb reverse tcp:8420 tcp:8420`. The phone is the TCP client; the PC listens on `127.0.0.1:8420`. The Windows app re-establishes `adb reverse` every 5 seconds, so the link self-heals after cable reconnects or adb restarts.
- **Protocol:** every frame = 4-byte little-endian header (payload length) + payload; payload = 1 byte camera id (0=back, 1=front) + JPEG bytes.
- **Low latency:** the phone only ever sends the newest frame (stale frames are dropped, `TCP_NODELAY`); the PC re-syncs to the stream head on every reconnect, so latency never accumulates.
- **Android:** camera2 delivers JPEG directly (no re-encode), ImageReader with `acquireLatestImage`.
- **Windows:** PySide6; frameless window with manual drag/resize hit-testing; optional pyvirtualcam output (capped at 1920×1080 by the OBS driver).

## Performance notes

- **The sensor is pinned to a constant 30 fps** (exact `[30,30]` range when the sensor offers it), so the frame rate stays stable instead of throttling in low light.
- **JPEG quality scales with resolution** (full quality up to 1080p, tuned down above it), so 1440p/4K keeps a usable frame rate over USB2.
- Frame rate ultimately depends on your phone's sensor and its USB chip — budget phones do less, flagships do more. USB2 links cap around 20–30 MB/s, which is the practical limit for 4K.
- Front/back and resolution switching without app restart ✓ (instant, tested with rapid consecutive switches)
- Client/server kill + reconnect → auto-recovery ✓
- Killing adb server → Windows app re-establishes reverse ✓

## Troubleshooting

- **Window says "no phone"**: check the cable; `adb devices` must list the phone; accept the USB debugging prompt.
- **Connected but no image**: open the app on the phone (streaming starts when the app opens) or use the `am start` command above.
- **MIUI**: set USBCam to "No restrictions" in Battery settings so long streams are not killed.
- **Latency grew**: close and reopen the window (it re-syncs to the stream head) — in practice this should not happen.

## Author

- Email: workspikestudio@gmail.com
- Telegram: [@spike_c](https://t.me/spike_c)

## License

Copyright © 2026 workspikestudio.

This program is free software: you can redistribute it and/or modify it under the terms of the **GNU General Public License v3.0** — see the [LICENSE](LICENSE) file.

In short: you may use, study, share and modify this code, but any fork or derivative (including repackaged builds) must also be released under GPL-3.0 with full source, and must keep the original copyright notice. Selling a closed-source copy of this app is not permitted.
