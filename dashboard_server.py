"""Local Binlytic dashboard and sensor API.

Run:
    python dashboard_server.py

The dashboard records a disposal only when:
1. the AI posts a classification, and
2. the entry ultrasonic sensor detects an object soon after.
"""

import argparse
import base64
import binascii
import json
import math
import mimetypes
import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None


ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"
DATA_FILE = DASHBOARD_DIR / "data" / "bins.json"
CAPTURE_DIR = DASHBOARD_DIR / "data" / "captures"
LEARNING_FILE = DASHBOARD_DIR / "data" / "learning.json"
SERIAL_PORT = os.getenv("WASTEVISION_SERIAL_PORT", "COM5")
CONFIRMATION_WINDOW_SECONDS = 30
ENTRY_DETECTION_LIMIT_CM = 40
STATE_LOCK = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def load_state():
    with DATA_FILE.open("r", encoding="utf-8") as source:
        state = json.load(source)
    state.setdefault("history", [])
    state.setdefault("pending", {})
    state.setdefault("unknowns", [])
    return state


STATE = load_state()


def load_learning():
    if not LEARNING_FILE.exists():
        return {"references": {}, "candidates": []}
    try:
        data = json.loads(LEARNING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"references": {}, "candidates": []}
    data.setdefault("references", {})
    data.setdefault("candidates", [])
    return data


LEARNING = load_learning()


def save_state():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(STATE, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(DATA_FILE)


def save_learning():
    temporary = LEARNING_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(LEARNING, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(LEARNING_FILE)


def normalize_vector(vector):
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude <= 0:
        raise ValueError("Embedding has zero length.")
    return [value / magnitude for value in vector]


def cosine_similarity(left, right):
    if len(left) != len(right):
        return -1
    return sum(a * b for a, b in zip(left, right))


def find_bin(bin_id):
    return next(
        (trashcan for trashcan in STATE["bins"] if trashcan["id"] == bin_id),
        None,
    )


def parse_timestamp(value):
    if not value:
        return utc_now()
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def expire_pending():
    now = utc_now()
    expired = []
    for bin_id, event in STATE["pending"].items():
        age = (now - parse_timestamp(event["detected_at"])).total_seconds()
        if age > CONFIRMATION_WINDOW_SECONDS:
            expired.append(bin_id)
    for bin_id in expired:
        del STATE["pending"][bin_id]


def public_state():
    expire_pending()
    snapshot = deepcopy(STATE)
    snapshot["server_time"] = iso_now()
    snapshot["confirmation_window_seconds"] = CONFIRMATION_WINDOW_SECONDS
    snapshot["controller"] = controller_status()
    snapshot["learning_summary"] = {
        "learned_classes": len(LEARNING["references"]),
        "collecting_examples": len(LEARNING["candidates"]),
    }
    return snapshot


def controller_status():
    """Report online only when the configured Arduino COM port exists."""

    if list_ports is None:
        return {"port": SERIAL_PORT, "online": False}
    try:
        port = next(
            (
                candidate
                for candidate in list_ports.comports()
                if candidate.device.upper() == SERIAL_PORT.upper()
            ),
            None,
        )
    except Exception:
        port = None
    return {
        "port": SERIAL_PORT,
        "online": port is not None,
        "description": port.description if port is not None else None,
    }


def add_classification(payload):
    bin_id = str(payload.get("bin_id", "")).strip()
    trashcan = find_bin(bin_id)
    if trashcan is None:
        raise ValueError("Unknown bin_id.")

    label = str(payload.get("label", "")).strip()
    destination = str(payload.get("destination", "")).upper().strip()
    valid_destinations = {
        compartment["type"] for compartment in trashcan["compartments"]
    }
    if not label:
        raise ValueError("label is required.")
    if destination not in valid_destinations:
        raise ValueError("destination must match a trashcan compartment.")

    confidence = float(payload.get("confidence", 0))
    event = {
        "event_id": uuid.uuid4().hex[:10],
        "bin_id": bin_id,
        "label": label,
        "destination": destination,
        "confidence": round(max(0, min(1, confidence)), 3),
        "detected_at": iso_now(),
        "status": "waiting_for_sensor",
    }
    STATE["pending"][bin_id] = event
    trashcan["last_ai_detection"] = deepcopy(event)
    save_state()
    return event


def add_object_detection(payload):
    """Confirm an item using the entry sensor without changing fill data."""

    bin_id = str(payload.get("bin_id", "")).strip()
    destination = str(payload.get("destination", "")).upper().strip()
    distance_cm = float(payload["distance_cm"])
    trashcan = find_bin(bin_id)
    if trashcan is None:
        raise ValueError("Unknown bin_id.")
    if not 2 <= distance_cm <= ENTRY_DETECTION_LIMIT_CM:
        raise ValueError("Entry detection must be between 2 and 40 cm.")

    valid_destinations = {
        compartment["type"] for compartment in trashcan["compartments"]
    }
    if destination not in valid_destinations:
        raise ValueError("destination must match a trashcan compartment.")

    expire_pending()
    pending = STATE["pending"].get(bin_id)
    confirmed_event = None
    if pending and pending["destination"] == destination:
        confirmed_event = {
            **pending,
            "status": "confirmed",
            "confirmed_at": iso_now(),
            "confirmation_method": "ultrasonic",
            "entry_sensor_distance_cm": round(distance_cm, 2),
        }
        STATE["history"].insert(0, confirmed_event)
        STATE["history"] = STATE["history"][:500]
        trashcan["last_disposal"] = deepcopy(confirmed_event)
        del STATE["pending"][bin_id]

    save_state()
    return {
        "object_detected": True,
        "confirmed_event": confirmed_event,
    }


def add_timer_confirmation(payload):
    """Confirm garbage/recycling after the ESP32's timed flap cycle."""

    bin_id = str(payload.get("bin_id", "")).strip()
    destination = str(payload.get("destination", "")).upper().strip()
    if find_bin(bin_id) is None:
        raise ValueError("Unknown bin_id.")
    if destination not in {"GARBAGE", "RECYCLING"}:
        raise ValueError("Timer confirmation is only for garbage or recycling.")

    expire_pending()
    pending = STATE["pending"].get(bin_id)
    confirmed_event = None
    if pending and pending["destination"] == destination:
        confirmed_event = {
            **pending,
            "status": "confirmed",
            "confirmed_at": iso_now(),
            "confirmation_method": "timer",
        }
        STATE["history"].insert(0, confirmed_event)
        STATE["history"] = STATE["history"][:500]
        find_bin(bin_id)["last_disposal"] = deepcopy(confirmed_event)
        del STATE["pending"][bin_id]

    save_state()
    return {
        "timer_completed": True,
        "confirmed_event": confirmed_event,
    }


def add_unknown_capture(payload):
    bin_id = str(payload.get("bin_id", "")).strip()
    if find_bin(bin_id) is None:
        raise ValueError("Unknown bin_id.")

    try:
        image = base64.b64decode(
            str(payload.get("image_base64", "")),
            validate=True,
        )
    except (ValueError, binascii.Error) as error:
        raise ValueError("image_base64 is not valid.") from error
    if not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
        raise ValueError("Only JPEG snapshots are accepted.")
    if len(image) > 1_000_000:
        raise ValueError("Snapshot must be smaller than 1 MB.")

    raw_embedding = payload.get("embedding", [])
    if not isinstance(raw_embedding, list) or not 64 <= len(raw_embedding) <= 2048:
        raise ValueError("A valid CLIP embedding is required.")
    embedding = normalize_vector([float(value) for value in raw_embedding])

    raw_guesses = payload.get("top_guesses", [])
    if not isinstance(raw_guesses, list):
        raise ValueError("top_guesses must be a list.")
    top_guesses = []
    for guess in raw_guesses[:3]:
        if not isinstance(guess, dict):
            continue
        top_guesses.append({
            "label": str(guess.get("label", ""))[:100],
            "bin": str(guess.get("bin", "UNKNOWN")).upper()[:20],
            "score": round(float(guess.get("score", 0)), 4),
        })

    capture_id = uuid.uuid4().hex[:12]
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{capture_id}.jpg"
    (CAPTURE_DIR / filename).write_bytes(image)

    event = {
        "event_id": capture_id,
        "bin_id": bin_id,
        "label": "UNKNOWN OBJECT",
        "confidence": round(
            max(0, min(1, float(payload.get("confidence", 0)))),
            3,
        ),
        "captured_at": iso_now(),
        "image_url": f"/data/captures/{filename}",
        "description": str(payload.get("description", ""))[:300],
        "top_guesses": top_guesses,
    }
    apply_automatic_learning(event, embedding)
    STATE["unknowns"].insert(0, event)
    STATE["unknowns"] = STATE["unknowns"][:50]
    save_state()
    return event


def choose_learning_candidate(top_guesses):
    unsafe = {
        "phone electronic device or cable",
        "battery or hazardous small item",
        "broken glass ceramic or sharp object",
        "medicine blister pack or medical item",
    }
    if any(
        guess["label"] in unsafe and guess["score"] >= 0.18
        for guess in top_guesses
    ):
        return None

    safe = [
        guess
        for guess in top_guesses
        if guess["bin"] in {"RECYCLING", "COMPOST", "GARBAGE"}
    ]
    if not safe or safe[0]["score"] < 0.18:
        return None

    candidate = safe[0]
    competing_scores = [
        guess["score"]
        for guess in safe[1:]
        if guess["bin"] != candidate["bin"]
    ]
    if competing_scores and candidate["score"] - max(competing_scores) < 0.012:
        return None
    return candidate


def apply_automatic_learning(event, embedding):
    candidate = choose_learning_candidate(event["top_guesses"])
    if candidate is None:
        event["learning_status"] = "needs-more-evidence"
        event["learning_message"] = "No safe automatic match yet"
        return

    record = {
        "event_id": event["event_id"],
        "candidate_label": candidate["label"],
        "candidate_bin": candidate["bin"],
        "score": candidate["score"],
        "embedding": embedding,
    }
    LEARNING["candidates"].append(record)
    LEARNING["candidates"] = LEARNING["candidates"][-100:]

    cluster = [
        existing
        for existing in LEARNING["candidates"]
        if (
            existing["candidate_label"] == candidate["label"]
            and cosine_similarity(existing["embedding"], embedding) >= 0.90
        )
    ]

    event["suggested_label"] = candidate["label"]
    event["suggested_bin"] = candidate["bin"]
    event["cluster_count"] = len(cluster)

    if len(cluster) < 3:
        event["learning_status"] = "collecting"
        event["learning_message"] = f"Learning example {len(cluster)} of 3"
        save_learning()
        return

    dimensions = len(embedding)
    centroid = normalize_vector([
        sum(sample["embedding"][index] for sample in cluster) / len(cluster)
        for index in range(dimensions)
    ])
    previous = LEARNING["references"].get(candidate["label"])
    if previous:
        previous_count = int(previous.get("count", 0))
        centroid = normalize_vector([
            (
                previous["prototype"][index] * previous_count
                + centroid[index] * len(cluster)
            )
            / (previous_count + len(cluster))
            for index in range(dimensions)
        ])
    else:
        previous_count = 0

    LEARNING["references"][candidate["label"]] = {
        "bin": candidate["bin"],
        "prototype": centroid,
        "count": previous_count + len(cluster),
        "description": event["description"],
        "updated_at": iso_now(),
    }
    learned_ids = {sample["event_id"] for sample in cluster}
    LEARNING["candidates"] = [
        sample
        for sample in LEARNING["candidates"]
        if sample["event_id"] not in learned_ids
    ]
    for prior_event in STATE.get("unknowns", []):
        if prior_event.get("event_id") in learned_ids:
            prior_event["learning_status"] = "auto-learned"
            prior_event["learning_message"] = (
                f"Learned as {candidate['label']}"
            )

    event["learning_status"] = "auto-learned"
    event["learning_message"] = f"Learned as {candidate['label']}"
    save_learning()


def clear_trashcan_history(destination=None):
    """Clear confirmed and pending disposal history.

    When `destination` is given (e.g. "COMPOST"), only entries routed to
    that compartment are removed; otherwise the whole history is cleared.
    """
    destination = str(destination).upper().strip() if destination else None

    if destination:
        STATE["history"] = [
            event for event in STATE["history"] if event["destination"] != destination
        ]
        STATE["pending"] = {
            bin_id: event
            for bin_id, event in STATE["pending"].items()
            if event["destination"] != destination
        }
        for trashcan in STATE["bins"]:
            if trashcan.get("last_disposal", {}).get("destination") == destination:
                trashcan.pop("last_disposal", None)
            if trashcan.get("last_ai_detection", {}).get("destination") == destination:
                trashcan.pop("last_ai_detection", None)
    else:
        STATE["history"] = []
        STATE["pending"] = {}
        for trashcan in STATE["bins"]:
            trashcan.pop("last_disposal", None)
            trashcan.pop("last_ai_detection", None)

    save_state()
    return {"history_cleared": True, "destination": destination}


def clear_learning_memory():
    """Remove learned image references, candidates, and their source captures."""
    removed_references = len(LEARNING["references"])
    removed_candidates = len(LEARNING["candidates"])
    removed_captures = 0
    LEARNING["references"] = {}
    LEARNING["candidates"] = []
    STATE["unknowns"] = []
    if CAPTURE_DIR.exists():
        for capture in CAPTURE_DIR.glob("*.jpg"):
            capture.unlink()
            removed_captures += 1
    save_state()
    save_learning()
    return {
        "learning_cleared": True,
        "removed_references": removed_references,
        "removed_candidates": removed_candidates,
        "removed_captures": removed_captures,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "BinlyticDashboard/1.0"

    def log_message(self, message_format, *args):
        print(f"{self.address_string()} - {message_format % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        if not path.is_file() or DASHBOARD_DIR not in path.parents:
            self.send_error(404)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_OPTIONS(self):
        self.send_json({})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            with STATE_LOCK:
                self.send_json(public_state())
            return
        if path == "/api/health":
            self.send_json({"status": "ok", "time": iso_now()})
            return

        relative = "index.html" if path == "/" else path.lstrip("/")
        requested = (DASHBOARD_DIR / relative).resolve()
        self.send_file(requested)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
            with STATE_LOCK:
                if path == "/api/classifications":
                    result = add_classification(payload)
                    self.send_json(result, 202)
                    return
                if path == "/api/detections":
                    result = add_object_detection(payload)
                    self.send_json(result)
                    return
                if path == "/api/timer-confirmations":
                    result = add_timer_confirmation(payload)
                    self.send_json(result)
                    return
                if path == "/api/unknowns":
                    self.send_json(add_unknown_capture(payload), 201)
                    return
                if path in {"/api/history/clear", "/api/reset"}:
                    self.send_json(clear_trashcan_history(payload.get("destination")))
                    return
                if path == "/api/learning/clear":
                    self.send_json(clear_learning_memory())
                    return
            self.send_json({"error": "Unknown endpoint."}, 404)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, 400)


def main():
    parser = argparse.ArgumentParser(description="Binlytic live dashboard")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()

    server = ThreadingHTTPServer(
        (arguments.host, arguments.port),
        DashboardHandler,
    )
    print(f"Binlytic dashboard: http://localhost:{arguments.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
