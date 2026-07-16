"""Binlytic mobile runner: the CLIP classifier on an Android phone.

Runs headless inside Termux's Ubuntu container (proot-distro) and reads
frames from the IP Webcam app on the SAME phone (http://127.0.0.1:8080).
No display, no serial, no PC. See docs/mobile_setup.md for install steps.

    python3 binlytic_mobile.py

Environment overrides:
    BINLYTIC_SNAPSHOT_URL   default http://127.0.0.1:8080/shot.jpg
    BINLYTIC_DASHBOARD_API  optional; POSTs FINAL results if set
    BINLYTIC_INTERVAL       seconds between classifications (default 2)
"""

import io
import json
import os
import time
from collections import Counter, deque
from urllib.request import Request, urlopen

import torch
import torch.nn.functional as F
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

# Reuse the exact visual classes and bin rules from the desktop app so the
# phone and PC always agree. Requires opencv-python-headless + pyserial
# installed (imported by wastevision_ai, unused here).
from wastevision_ai import (
    LOCAL_BIN_RULES,
    MODEL_NAME,
    MIN_COSINE_SIMILARITY,
    MIN_WINNING_MARGIN,
    build_items,
)

SNAPSHOT_URL = os.getenv("BINLYTIC_SNAPSHOT_URL", "http://127.0.0.1:8080/shot.jpg")
DASHBOARD_API = os.getenv("BINLYTIC_DASHBOARD_API", "").rstrip("/")
INTERVAL_SECONDS = float(os.getenv("BINLYTIC_INTERVAL", "2"))
BIN_ID = os.getenv("BINLYTIC_BIN_ID", "WV-001")


def fetch_snapshot():
    with urlopen(SNAPSHOT_URL, timeout=5) as response:
        return Image.open(io.BytesIO(response.read())).convert("RGB")


def crop_pair(image):
    width, height = image.size
    tight = image.crop((
        int(width * 0.22), int(height * 0.16),
        int(width * 0.78), int(height * 0.72),
    ))
    wide = image.crop((
        int(width * 0.12), int(height * 0.08),
        int(width * 0.88), int(height * 0.84),
    ))
    return [tight, wide]


def post_dashboard(path, payload):
    if not DASHBOARD_API:
        return
    request = Request(
        f"{DASHBOARD_API}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urlopen(request, timeout=2).read()
    except OSError as error:
        print(f"  (dashboard update failed: {error})")


def main():
    print("Loading CLIP model (first run downloads ~350 MB)...")
    model = CLIPModel.from_pretrained(MODEL_NAME).eval()
    processor = CLIPProcessor.from_pretrained(MODEL_NAME)
    items = build_items()

    print("Encoding text prompts...")
    prototypes = []
    with torch.inference_mode():
        for item in items:
            prompts = [f"{item.label}: {prompt}" for prompt in item.prompts]
            inputs = processor(text=prompts, return_tensors="pt", padding=True, truncation=True)
            features = F.normalize(model.get_text_features(**inputs), dim=-1)
            prototypes.append(F.normalize(features.mean(dim=0), dim=0))
    text_features = torch.stack(prototypes)

    history = deque(maxlen=3)
    print(f"Ready. Watching {SNAPSHOT_URL} every {INTERVAL_SECONDS:.0f}s. Ctrl+C to stop.\n")

    while True:
        started = time.time()
        try:
            image = fetch_snapshot()
        except OSError as error:
            print(f"Camera unreachable ({error}); is IP Webcam started?")
            time.sleep(3)
            continue

        with torch.inference_mode():
            inputs = processor(images=crop_pair(image), return_tensors="pt")
            image_features = F.normalize(model.get_image_features(**inputs), dim=-1)
            image_feature = F.normalize(image_features.mean(dim=0), dim=0)
            similarities = image_feature @ text_features.T

        scores, indices = torch.topk(similarities, 3)
        ranked = [(items[i], float(s)) for s, i in zip(scores.tolist(), indices.tolist())]
        best_item, best_score = ranked[0]
        margin = best_score - ranked[1][1]

        accepted = best_score >= MIN_COSINE_SIMILARITY and margin >= MIN_WINNING_MARGIN
        if not accepted or best_item.label == "empty background":
            history.clear()
            state = "…" if best_item.label == "empty background" else "UNCERTAIN"
        else:
            history.append(best_item.label)
            votes = Counter(history).most_common(1)[0]
            if votes[0] == best_item.label and votes[1] >= 2:
                state = "FINAL"
                post_dashboard("/api/classifications", {
                    "bin_id": BIN_ID,
                    "label": best_item.label,
                    "destination": best_item.bin_type,
                    "confidence": best_score,
                })
                history.clear()
            else:
                state = "checking"

        elapsed = time.time() - started
        print(
            f"[{state:>9s}] {best_item.label:38s} -> {best_item.bin_type:9s} "
            f"cos {best_score:.3f} margin {margin:.3f} ({elapsed:.1f}s)"
        )
        time.sleep(max(0, INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
