# Binlytic AI Handoff for Claude Code

This document is the working handoff for the current Binlytic AI hackathon project. It is meant to let Claude Code continue without re-discovering the architecture, data flow, hardware wiring, or the current project state.

## What This Project Is

Binlytic AI is a Python + CLIP + ESP32 waste-routing prototype for a mall garbage-management setup.

The current flow is:

1. The Python app reads the webcam.
2. CLIP compares the image against a curated list of short visual prompts.
3. The prediction is stabilized with a small voting window.
4. The result is mapped to a bin type.
5. Python sends the route to the ESP32 over serial.
6. The ESP32 opens the correct flap or waits for ultrasonic confirmation.
7. The local dashboard records the event and shows history.

The project is optimized for a hackathon demo, not production. Accuracy matters more than raw speed.

## Current High-Level Files

- `wastevision_ai.py`: main Python app, CLIP inference, stable prediction logic, ESP32 serial output, dashboard posting, unknown learning snapshots.
- `dashboard_server.py`: local HTTP server and state store for the site.
- `esp32_wastevision_three_servos.ino`: ESP32 servo and ultrasonic controller.
- `dashboard/`: static UI files.
- `dashboard/data/bins.json`: live bin metadata, history, unknowns, and summary state.
- `dashboard/data/learning.json`: learned visual references for unknowns and low-confidence objects.
- `dashboard/data/captures/`: saved unknown-object JPEG snapshots.
- `docs/dashboard_integration.md`: dashboard API and confirmation flow.
- `requirements.txt`: Python dependencies.

## Current Runtime Setup

The project is currently wired to these defaults:

- Camera index: `0`
- Arduino serial port: `COM5`
- Dashboard API: `http://127.0.0.1:8000`
- Local dashboard URL: `http://localhost:8000`

The Python app and dashboard are designed to run on the same machine.

## How To Run It

From `C:\Users\bnall\OneDrive\Documents\Summer\WastevisionAI`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python dashboard_server.py
python wastevision_ai.py
```

Upload `esp32_wastevision_three_servos.ino` to the ESP32 in the Arduino IDE.

## Current Recognition Strategy

The current model uses broad, visually reliable classes instead of overly fine-grained labels.

Important design choice:

- recognition prompts and sorting rules are separate;
- `LOCAL_BIN_RULES` in `wastevision_ai.py` decides the bin;
- the visual prompts should stay short, unique, and not too detailed;
- the code should be edited carefully rather than rewritten wholesale.

The model uses:

- CLIP text/image matching;
- a stable voting window;
- an unknown snapshot path for weak or uncertain items;
- a learning store for recurring unknown objects.

### Current bin classes

The project currently routes into:

- `RECYCLING`
- `COMPOST`
- `GARBAGE`
- `HAZARDOUS`
- `E-WASTE`
- `UNKNOWN`

`E-WASTE` is currently used for phone/electronic-device style detections.

## Current Safety And Stability Rules

The classifier is intentionally conservative.

It should avoid opening a bin when:

- the image is too uncertain;
- the top scores are too close;
- a clean object is competing with a contaminated version;
- the item is empty background;
- the item is a human hand or person;
- the item should remain unknown until there is enough evidence.

Unknown captures are rate-limited by a cooldown.

## Unknown-Object Learning

The project has a learning loop for repeated unknown objects.

Current behavior:

- if an object is low-confidence or unknown, the code can save a JPEG snapshot;
- the snapshot is sent to the dashboard with CLIP embedding data and the top-3 guesses;
- the dashboard stores the image in `dashboard/data/captures/`;
- repeated similar captures can create learned references in `dashboard/data/learning.json`.

This means Claude should preserve the image-capture and learning files unless the user specifically asks to clear them.

## Dashboard API And Data Flow

The dashboard server accepts these endpoints:

- `GET /api/state`
- `POST /api/classifications`
- `POST /api/detections`
- `POST /api/unknowns`
- `POST /api/history/clear`
- `POST /api/learning/clear`

The confirmation logic is:

1. Python sends the final classification.
2. The ESP32 or sensor side sends the matching confirmation.
3. The dashboard records the item only when the two match correctly.

The ultrasonic confirmation is currently used as an entry/object-present check, not as a full/empty sensor.

## Arduino Serial Protocol

The current ESP32 sketch responds to:

- `G` = garbage route
- `R` = recycling route
- `C` = compost route
- `U` = unknown, no flap action
- `X` = emergency close all
- `1` to `6` = manual servo test commands

Behavior:

- garbage and recycling are timer-based routes;
- compost waits for ultrasonic confirmation;
- the ESP32 prints status messages back to Python;
- Python uses those messages to update the dashboard and release the active route lock.

The ESP32 sketch currently detaches the servos after movement to reduce buzzing.

## Hardware Assumptions

Current servo pins:

- garbage servo: GPIO 22
- recycling servo: GPIO 12
- compost servo: GPIO 14

Current ultrasonic sensor for compost entry checking:

- TRIG: GPIO 5
- ECHO: GPIO 18

Current wiring assumption:

- servos use 5V power;
- sensors are currently being tested with external 3.3V power;
- all grounds are shared.

If a 5V ultrasonic supply is used later, the ECHO line must be level shifted before reaching the ESP32.

## Current ESP32 File

The active sketch file in the repo is:

`esp32_wastevision_three_servos.ino`

That file is the one to update if servo timing, sensor thresholds, or route behavior changes.

## Dashboard Behavior

The local web UI is intentionally simple and grounded.

It currently shows:

- one trashcan;
- its status;
- confirmed history;
- unknown-object snapshots;
- learning/status information.

The system should not invent full/empty sensor data yet.

The dashboard is only “online” when Windows detects the configured Arduino COM port.

## Important Commands And Environment Variables

Useful environment variables:

```powershell
$env:WASTEVISION_SERIAL_PORT = "COM5"
$env:WASTEVISION_CAMERA_INDEX = "0"
$env:WASTEVISION_MIN_SIMILARITY = "0.20"
$env:WASTEVISION_MIN_MARGIN = "0.008"
$env:WASTEVISION_CONFLICT_MARGIN = "0.015"
```

The main file uses these values in `wastevision_ai.py`.

## What Claude Should Be Careful Not To Break

- Do not rewrite the whole AI pipeline unless the user asks for it.
- Do not delete `dashboard/data/learning.json` or `dashboard/data/captures/` unless the user explicitly wants a reset.
- Do not remove the serial protocol messages without updating the dashboard and Python side together.
- Do not invent full-bin or empty-bin data; that sensor is not implemented yet.
- Do not make the prompts too long or too fine-grained. The current project works better with short, unique, medium-specific descriptions.
- Do not change the base architecture unless needed: webcam -> CLIP -> stable vote -> route -> ESP32 -> dashboard.

## Useful Current Files To Inspect First

If continuing development, start with these files in order:

1. `wastevision_ai.py`
2. `dashboard_server.py`
3. `esp32_wastevision_three_servos.ino`
4. `docs/dashboard_integration.md`
5. `dashboard/data/bins.json`
6. `dashboard/data/learning.json`

## One-Sentence Summary

Binlytic AI is currently a local webcam-to-dashboard-to-ESP32 waste router with CLIP-based classification, a small stability filter, saved unknown snapshots, and a simple dashboard that records only confirmed events.
