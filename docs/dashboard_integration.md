# Dashboard integration

The dashboard records an item only after two events happen in this order:

1. Binlytic AI reports a final classification.
2. The ultrasonic confirmation step confirms that an item passed within 40 cm.

The confirmation event must arrive within 30 seconds. AI guesses without
confirmation expire and are not included in the item count or history.

## Start the dashboard

```powershell
python dashboard_server.py
```

Open `http://localhost:8000`.

The server listens on the computer's network so an ESP32 on the same Wi-Fi can
send events. Windows Firewall may ask for permission the first time.

The website reports `Online` only when Windows detects the Arduino on `COM5`.
Otherwise it reports `Offline`. Change the port with
`WASTEVISION_SERIAL_PORT` only if the Arduino is assigned a different port.

## Step 1: send the final AI result

Send this only when the Python classifier reaches `FINAL`:

```http
POST /api/classifications
Content-Type: application/json
```

```json
{
  "bin_id": "WV-001",
  "label": "plastic beverage bottle",
  "destination": "RECYCLING",
  "confidence": 0.91
}
```

This creates a temporary pending event. It does not increase the item count.

## Step 2: send the ultrasonic confirmation

```http
POST /api/detections
Content-Type: application/json
```

```json
{
  "bin_id": "WV-001",
  "destination": "RECYCLING",
  "distance_cm": 19.2
}
```

The server confirms the pending item when:

- the `bin_id` matches;
- the destination compartment matches;
- the detection arrives within 30 seconds; and
- the measured object distance is between 2 and 40 centimetres.

Only then is the label added to history.

## Current ultrasonic job

The one HC-SR04 is only a confirmation detector. It confirms that an object
passed after a flap opened. It does not decide whether the bin is full or
empty. Fullness sensors can be designed and added later.

## Three-servo routing

Upload `esp32_wastevision_three_servos.ino` to the ESP32.

- `G` routes garbage to GPIO 22 and closes it after three seconds.
- `R` routes recycling to GPIO 12 and closes it after three seconds.
- `C` routes compost to GPIO 14 and waits for ultrasonic confirmation.
- `X` is the manual close-all command.

Garbage and recycling send `TIMER,<DESTINATION>` after their timed cycles.
Compost closes only after the sensor first sees a clear chute and then sees an
object between 2 and 9 centimetres; it sends
`OBJECT,COMPOST,<DISTANCE>`. Python records the correct confirmation method in
the dashboard history.

## Trashcan ID and location

The dashboard contains one bin: `WV-001`, named `Binlytic Bin 01`. Its
location is intentionally unset. Add the real location and map position in
`dashboard/data/bins.json` after the physical bin is placed.
