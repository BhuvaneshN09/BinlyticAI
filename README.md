<p align="center"><img src="assets/binlytic-logo.png" width="110" alt="Binlytic AI logo"></p>

# Binlytic AI

Binlytic AI is a webcam and CLIP prototype for routing common mall waste to:

- `RECYCLING`
- `COMPOST`
- `GARBAGE`
- `HAZARDOUS`
- `UNKNOWN`

Stable results send `R`, `C`, `G`, `H`, or `U` to the controller. The app
sends `N` for no action while checking, when the area is empty, or when the
image is not reliable enough to open a compartment.

## Design changes for accuracy

The model uses broad visual classes instead of many fine-grained labels.
Look-alike objects are merged when a webcam cannot reliably separate them:

- plastic cups, rigid containers, and lids;
- bottles with different colors;
- snack wrappers and flexible plastic film;
- cardboard shipping boxes and thin product boxes;
- related food leftovers.

This improves bin routing while avoiding unsupported exact-item claims.

## Local sorting rules

Recognition descriptions do not contain bin rules. The editable
`LOCAL_BIN_RULES` table maps each visual class to a bin.

The default profile follows current
[Toronto recycling guidance](https://www.toronto.ca/services-payments/recycling-organics-garbage/houses/changes-to-recycling-program/).
Confirm it with the mall's waste contractor before deployment.

For this mall prototype, flexible snack wrappers and plastic film map to
`GARBAGE`. Confirm this local rule with the mall's waste contractor because
some programs accept selected flexible plastic bags.

## Safety behavior

The controller receives `N` and keeps every compartment closed when:

- the best cosine similarity is too low;
- the best two classes are too close;
- clean packaging is too close to a dirty or food-filled class;
- a strict unknown object is recognized;
- the result has not yet won two of three camera checks.

After two matching destination votes, a recognized unknown object receives
`U` and can use the separate unknown compartment. Empty backgrounds and human
hands never open a compartment.

The dedicated hazardous destination includes:

- household cylinder, 9-volt, and button batteries;
- vapes and electronic cigarettes;
- aerosol spray cans;
- syringes, needles, and razor blades;
- chemical cleaner, paint, and solvent containers;
- fluorescent tube and compact fluorescent bulbs.

These classes use specific shape and feature descriptions. If the hazardous
item is not clear enough, the system keeps all compartments closed instead of
guessing.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python wastevision_ai.py
```

Press `Q` to quit.

## ESP32 three-servo sketch

Upload `esp32_wastevision_three_servos.ino` with the Arduino IDE.

- Garbage: `G`, servo GPIO 22
- Recycling: `R`, servo GPIO 12
- Compost: `C`, servo GPIO 14

Garbage and recycling close after a three-second delay. Compost closes only
after the ultrasonic confirmation sees an object between 2 and 9 cm. The ESP32
reports the route and confirmation method to Python, which adds the AI label to
the dashboard history.

## Operations dashboard

Start the local waste-management dashboard:

```powershell
python dashboard_server.py
```

Then open `http://localhost:8000`.

The dashboard includes:

- one configured trashcan on a simple map;
- its name, connection status, and compartment slots;
- one ultrasonic confirmation detector for confirming an object within 40 cm;
- confirmed item labels, counts, confidence, and history;
- red unknown-object cards with camera snapshots;
- recycling, compost, garbage, hazardous, and unknown statistics.

An item is counted only when a final AI result is followed by a matching
ultrasonic event. See [dashboard integration](docs/dashboard_integration.md)
for the exact requests and calibration instructions.

No sensor values, location, or history are invented. Full/empty sensing is not
enabled yet. The dashboard reports `Online` only when the Arduino's `COM5`
port is detected.

Stable unknown objects are photographed from the marked camera area. Unknown
is reserved for electronics, batteries, broken or sharp objects, medical
items, empty backgrounds, and people. Everyday pencils, small tools, reusable
plastic, clothing/accessories, wood, rubber, and mixed packaged food route to
garbage. Empty backgrounds and human hands are excluded from photographs, and
repeated captures are limited by a ten-second cooldown.

## Automatic visual learning

Unknown and low-confidence objects are saved with their screenshot, CLIP
embedding, automatic description, and top-three matches. Three visually similar
captures that independently suggest the same safe existing category create a
learned image prototype. Future classifications combine the original text
prompts with that learned visual prototype.

Electronics, batteries, broken or sharp objects, and medical items are blocked
from automatic learning. Learned references are stored in
`dashboard/data/learning.json`; the Python source code is never rewritten.

## Configuration

```powershell
$env:WASTEVISION_SERIAL_PORT = "COM5"
$env:WASTEVISION_CAMERA_INDEX = "0"
$env:WASTEVISION_MIN_SIMILARITY = "0.18"
$env:WASTEVISION_MIN_MARGIN = "0.004"
$env:WASTEVISION_CONFLICT_MARGIN = "0.008"
python wastevision_ai.py
```

Tune thresholds using many labeled camera examples, not one troublesome item.

## Camera setup

- Fix the camera position.
- Use bright, even lighting.
- Use a plain non-reflective surface.
- Place one item inside the marked box.
- Remove hands before classification.
- Separate food from its wrapper before scanning.

The classifier averages tight and wide crops to reduce position sensitivity.
It still cannot directly measure material chemistry, weight, depth, grease, or
moisture. Treat it as a hackathon prototype, not production equipment.

## Dashboard sign-in

The dashboard has a single demo account, checked client-side only (no
database, no backend auth):

- Username: `MississaugaMall`
- Password: `Malltest123`

Signing in optionally "connects" a bin ID (stored in `localStorage`, no
server-side effect). This is a demo affordance, not a real access-control
system — do not reuse this pattern for anything with real users.

## Live demo

A static, read-only build of the dashboard (sample data, no live
webcam/Arduino) is deployed on Vercel for anyone to browse without running
the Python stack locally. See `vercel.json` for the deployment config.

## Contributing

This is an open, hackathon-stage project. Issues and PRs are welcome —
useful areas: bin-rule accuracy for other cities, additional sensor
integrations, and dashboard features. Keep recognition prompts short and
sorting rules (`LOCAL_BIN_RULES`) separate, per the design notes above.

## License

MIT — see `LICENSE`.
