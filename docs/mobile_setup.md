# Running Binlytic on an Android phone

The full CLIP classifier runs on the phone's CPU inside a Linux container.
The phone's own camera is read through the IP Webcam app on localhost.
No PC involved after setup.

## One-time install (15–25 min, ~2 GB download)

1. Install **Termux from F-Droid** (f-droid.org — the Play Store version is
   abandoned and will not work).
2. Install **IP Webcam** from the Play Store.
3. Open Termux and run:

```bash
pkg update -y && pkg install -y proot-distro
proot-distro install ubuntu
proot-distro login ubuntu
```

4. Inside Ubuntu (the prompt changes to `root@localhost`):

```bash
apt update && apt install -y python3 python3-pip git
pip3 install --break-system-packages torch --index-url https://download.pytorch.org/whl/cpu
pip3 install --break-system-packages transformers pillow opencv-python-headless pyserial
git clone https://github.com/BhuvaneshN09/BinlyticAI
```

## Every run

1. Open **IP Webcam** → scroll down → **Start server**. Leave it running
   (it keeps serving from the background; disable battery optimization for it).
2. Open **Termux**:

```bash
proot-distro login ubuntu
cd BinlyticAI
python3 binlytic_mobile.py
```

First run downloads the CLIP model (~350 MB); after that it starts in
seconds and prints one classification line every couple of seconds:

```
[    FINAL] plastic beverage bottle              -> RECYCLING  cos 0.291 margin 0.021 (1.4s)
```

## Optional: report to a dashboard

If the laptop dashboard is running on the same Wi-Fi:

```bash
BINLYTIC_DASHBOARD_API=http://10.0.0.236:8000 python3 binlytic_mobile.py
```

FINAL results then appear in the dashboard's live feed (they stay
"awaiting sensor" unless the ESP32 confirms, same as on the PC).

## Notes

- Expect 1–3 s per classification on a modern phone (CPU only) — fine for
  the sorting use case.
- The phone does not talk to the ESP32 in this test build. Servo control
  from the phone would need a Wi-Fi or Bluetooth link to the ESP32 instead
  of USB serial (future work).
- To update the code later: `cd BinlyticAI && git pull`.
