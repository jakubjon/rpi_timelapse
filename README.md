# pi-timelapse

A small web app for a Raspberry Pi camera that shoots long-interval stills and turns
them into a timelapse video or GIF.

- **Live view** in the browser, from a cheap downscaled stream.
- **Drag a region** on the live view. Stills are stored as that crop at **full sensor
  resolution** — the preview is only small for viewing, the stored pixels are native.
- **Configurable period** between shots — seconds, minutes or hours, changeable from
  the browser while it runs.
- **Sessions**: start a fresh one at any time, and delete old ones with their photos.
- **Generate MP4 or GIF** on request, in the background, with a progress bar, and
  optionally skip frames darker than a threshold so the nights drop out.
- **One service**, restarts on failure, starts at boot, and a watchdog that catches the
  camera stalling without the process dying.

Tested on a Raspberry Pi Zero 2 W (512 MB) with Camera Module v2.1 (IMX219), on
Raspberry Pi OS Lite 64-bit. It also supports the HQ Camera (IMX477) via `--camera hq`.

---

## 1. Prepare the SD card

Use **Raspberry Pi Imager** and pick **Raspberry Pi OS Lite (64-bit)**. Lite, not
Desktop: this app needs no graphical desktop, and on a 512 MB Pi the desktop leaves too
little room for the camera buffers.

Before writing, open the settings (the gear icon) and set:

- **Hostname** — e.g. `timelapse`. You then reach the Pi as `timelapse.local`.
- **Enable SSH** with **public-key authentication**, pasting your public key
  (`~/.ssh/id_ed25519.pub`, or on Windows `C:\Users\<you>\.ssh\id_ed25519.pub`).
- **Username** — e.g. `jakub`. The rest of this README assumes that name.
- **Wi-Fi** SSID, password and country.
- **Locale and time zone** — the time zone matters, because stills are named after the
  local clock.

Write the card, put it in the Pi, connect the camera ribbon (contacts towards the
board, cable latch pressed home) and power it up. The first boot takes a few minutes.

## 2. First login

```sh
ssh jakub@timelapse.local
```

If the name doesn't resolve, find the address in your router's client list and use that.

Bring the system up to date and reboot if the kernel or firmware changed:

```sh
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

## 3. Check the camera

```sh
rpicam-hello --list-cameras
```

You should see your sensor listed, e.g. `imx219 [3280x2464]`. On Raspberry Pi OS
Bookworm and newer the camera is auto-detected and needs no `config.txt` entry. If the
list is empty, power off and reseat both ends of the ribbon cable; a cable in backwards
or not latched is by far the most common cause.

## 4. Install

```sh
sudo apt install -y git python3-picamera2 python3-opencv python3-flask ffmpeg
```

Install these **from apt, not pip**. The apt packages are pre-built for the Pi;
`pip install opencv-python picamera2` compiles from source and takes hours on a Zero.

```sh
git clone https://github.com/jakubjon/rpi_timelapse.git ~/pi-timelapse
cd ~/pi-timelapse
./setup/install.sh
```

`install.sh` writes a systemd unit for the current user and directory, enables it and
starts it. Then open:

```
http://timelapse.local:8080/
```

## 5. Using it

- **Region**: drag a box on the live view, then press **Save dragged region**. Stored
  stills are that crop at native sensor pixels. **Use full frame** clears it.
  Changing the region starts a new session directory, because one video cannot mix
  frame sizes.
- **Period**: a number plus a unit — seconds, minutes or hours. Takes effect
  immediately and is remembered across restarts. A full-res capture takes about 4
  seconds, so below that the shots simply follow each other as fast as the camera
  manages. Watch the **Fills** figure: at 20-second intervals full frames come to
  roughly 8 GB a day.
- **Take a picture now**: one extra still, outside the schedule.
- **Sessions**: **Start new session** begins a fresh directory without changing the
  region — useful to separate one run from the next. The Sessions panel lists them all
  with their shot count and size; **✕** deletes a session and its photos after a
  confirmation. Deleting the one being recorded is allowed: a new session starts
  immediately in its place.
- **Generate**: pick a session, frames per second, MP4 or GIF, and a height (or
  "Native — no scaling" to keep the crop's true size). Encoding runs in the background;
  results appear under **Results** to download.
- **Skip darker than**: leave out frames whose mean brightness (0 = black, 255 = white)
  falls below the threshold — the usual way to drop the night from a multi-day run. 0
  keeps everything. The **Brightness** reading in the Capture panel shows the level of
  the newest still, so pick a value somewhat under the daylight figure. Each still's
  brightness is measured as it is written and cached in `.brightness.json` inside the
  session, so filtering costs nothing on a second run; older stills without an entry are
  measured once (decoded at 1/8 scale) and then cached too.

### Where things land

| Path | Contents |
|---|---|
| `captures/<time>_<WxH>/` | One directory per region ("session"), holding the stills and a `.brightness.json` cache |
| `videos/` | Generated MP4s and GIFs |
| `data/timelapse.json` | Period, region and current session, restored on start |

All three are git-ignored.

## 6. Configuration

Everything has a sensible default; the unit only sets a few flags:

```
ExecStart=/usr/bin/python3 app.py --port 8080 --gain 3.0 --period 15
```

| Flag | Default | Meaning |
|---|---|---|
| `--port` | 8080 | HTTP port |
| `--period` | 15 | **Minutes** between stills, for the very first run only — once set in the browser, that value wins |
| `--stall-limit` | 45 | Seconds a camera call may block before the process exits for a restart |
| `--gain` | 3.0 | Fixed analogue gain, roughly ISO/100 |
| `--camera` | v2.1 | `v2.1` (IMX219) or `hq` (IMX477) |
| `--captures` `--video-dir` `--state` | in the repo | Override to store elsewhere |

After editing the unit:

```sh
sudo systemctl daemon-reload && sudo systemctl restart pi-timelapse
```

### Exposure and gain

The gain is pinned while **auto-exposure keeps adjusting the exposure time**, so the
picture follows the light through the day. Pinning both (the obvious
`AeEnable: False`) freezes the exposure at whatever value the camera started on, and
every frame after dusk comes out black.

Auto-exposure will not go past the **66.7 ms** ceiling in the sensor's tuning file
(`/usr/share/libcamera/ipa/rpi/vc4/imx219.json`, "normal" exposure mode). A dark room
therefore stays dark no matter how high the gain, because gain multiplies an almost
black frame. For night shots you need a light source, or a custom tuning file allowing
multi-second exposures.

### When the camera stalls

The vc4 pipeline occasionally stops delivering frames (`Camera frontend has timed
out!` in the log). The call never returns, so the capture loop and the live view both
freeze while the process stays alive — which means `Restart=always` alone never
notices. A watchdog thread therefore times every camera call and, past
`--stall-limit`, exits the process so systemd restarts it. Expect a line like:

```
camera call stuck for 46s (limit 45s) — exiting for a restart
```

You lose one frame and about ten seconds. Frequent stalls point at the ribbon cable or
the power supply rather than at software.

## 7. Service management

```sh
systemctl status pi-timelapse          # is it running
journalctl -u pi-timelapse -f          # follow the log
sudo systemctl restart pi-timelapse    # restart
sudo systemctl disable --now pi-timelapse   # stop and don't start at boot
```

## 8. Housekeeping

Full-frame stills are roughly 1–2 MB each; a small region is a fraction of that. At
4 shots an hour a full frame fills about 150 MB a day, so a 8 GB card runs out in a
few weeks. Check and copy off:

The **Fills** figure in the Capture panel estimates the daily rate from the current
period and the average photo size, which is the number to check before choosing a
short interval. Delete whole sessions from the Sessions panel, or from a shell:

```sh
du -sh captures/* videos; df -h /
```

```sh
# from your PC, pull one session and then delete it on the Pi
scp -r jakub@timelapse.local:~/pi-timelapse/captures/20260927_204042_3280x2464 .
```

## 9. Troubleshooting

**The live view stays grey / `Camera __init__ sequence did not complete`**
Something else holds the camera. Only one process can. Check with
`sudo fuser -v /dev/video0` and stop the other user, then `sudo systemctl restart
pi-timelapse`.

**The service restarts in a loop**
`journalctl -u pi-timelapse -n 50`. On a 512 MB Pi, a too-large frame or a second
camera consumer can exhaust CMA memory; a reboot clears a fragmented CMA pool.

**Dark or black pictures**
See "Exposure and gain" above. Check the reported exposure and gain in the UI: if the
exposure sits at 66.7 ms it is already at the ceiling and only more light will help.

**Encoding fails or takes forever**
Check `ffmpeg -version` exists. Encoding hundreds of 8 MP frames on a Zero 2 W is slow;
prefer 720p, and a region rather than the full frame.

**Stills stop appearing but the page works**
Look for an error line under the panels; the capture loop reports failures there and
keeps running. The journal has the traceback.
