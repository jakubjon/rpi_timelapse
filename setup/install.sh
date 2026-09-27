#!/bin/sh
# Install (or re-install) the timelapse web app as a systemd service for the
# current user and checkout directory. Idempotent: safe to re-run after a pull.
set -eu

UNIT=pi-timelapse.service
REPO=$(cd "$(dirname "$0")/.." && pwd)
USER_NAME=$(id -un)
PORT=${PORT:-8080}
GAIN=${GAIN:-3.0}
PERIOD=${PERIOD:-15}
CAMERA=${CAMERA:-v2.1}

for pkg in picamera2 cv2 flask; do
    python3 -c "import $pkg" 2>/dev/null || {
        echo "Missing python3 module '$pkg'. Install the apt packages first:"
        echo "  sudo apt install -y python3-picamera2 python3-opencv python3-flask ffmpeg"
        exit 1
    }
done
command -v ffmpeg >/dev/null || { echo "ffmpeg not installed: sudo apt install ffmpeg"; exit 1; }

echo "Installing $UNIT for user $USER_NAME from $REPO (port $PORT)"
sudo tee "/etc/systemd/system/$UNIT" >/dev/null <<UNITFILE
[Unit]
Description=Timelapse web app (live view, ROI stills, video/GIF)
After=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$REPO
Environment=PYTHONUNBUFFERED=1
# The period and region set in the browser are persisted to data/timelapse.json
# and win over these flags, so changing them needs no unit edit.
ExecStart=/usr/bin/python3 $REPO/app.py --port $PORT --gain $GAIN --period $PERIOD --camera $CAMERA

Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
UNITFILE

sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT"
sleep 3
systemctl --no-pager --lines=0 status "$UNIT" || true
echo
echo "Open http://$(hostname).local:$PORT/  (or http://$(hostname -I | awk '{print $1}'):$PORT/)"
