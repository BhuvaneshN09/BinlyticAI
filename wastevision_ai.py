import base64
import json
import os
import time
from collections import Counter, deque
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
import cv2
import serial
try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None
import torch
import torch.nn.functional as F
from PIL import Image
from transformers import CLIPModel, CLIPProcessor


MODEL_NAME = "openai/clip-vit-base-patch32"
RULE_PROFILE = "Toronto 2026 public/commercial profile"
RULE_SOURCE = (
    "https://www.toronto.ca/services-payments/recycling-organics-garbage/"
    "houses/changes-to-recycling-program/"
)

CAMERA_SOURCE = os.getenv("WASTEVISION_CAMERA_SOURCE", "").strip()
CAMERA_INDEX = int(os.getenv("WASTEVISION_CAMERA_INDEX", "0"))
SERIAL_PORT = os.getenv("WASTEVISION_SERIAL_PORT", "COM5")
DASHBOARD_API = os.getenv(
    "WASTEVISION_DASHBOARD_API",
    "http://127.0.0.1:8000",
).rstrip("/")
TRASHCAN_ID = os.getenv("WASTEVISION_BIN_ID", "WV-001")
LEARNING_FILE = (
    Path(__file__).resolve().parent
    / "dashboard"
    / "data"
    / "learning.json"
)
TEXT_SCORE_WEIGHT = 0.45
LEARNED_IMAGE_WEIGHT = 0.55
LEARNED_MATCH_MIN_SIMILARITY = 0.88
UNKNOWN_CAPTURE_COOLDOWN_SECONDS = 10
UNKNOWN_CAPTURE_EXCLUSIONS = {
    "empty background",
    "human hand or person",
}
MIN_COSINE_SIMILARITY = float(
    os.getenv("WASTEVISION_MIN_SIMILARITY", "0.20")
)
MIN_WINNING_MARGIN = float(
    os.getenv("WASTEVISION_MIN_MARGIN", "0.008")
)
CONFLICT_MARGIN = float(
    os.getenv("WASTEVISION_CONFLICT_MARGIN", "0.015")
)


# Recognition and sorting rules are intentionally separate. Edit this table
# after confirming the mall's contract; do not rewrite visual prompts.
LOCAL_BIN_RULES = {
    "plastic beverage bottle": "RECYCLING",
    "metal food or drink can": "RECYCLING",
    "glass bottle or jar": "RECYCLING",
    "clean paper or cardboard package": "RECYCLING",
    "clean paper bag sheet or booklet": "RECYCLING",
    "paper drink cup or carton": "RECYCLING",
    "rigid plastic cup container or lid": "RECYCLING",
    "clean foam food package": "RECYCLING",
    "fruit peel or core": "COMPOST",
    "loose meal leftovers": "COMPOST",
    "bread sandwich pizza or pastry": "COMPOST",
    "coffee grounds or tea bag": "COMPOST",
    "flexible snack wrapper or plastic film": "GARBAGE",
    "used tissue napkin or paper towel": "GARBAGE",
    "plastic straw or disposable cutlery": "GARBAGE",
    "thermal cash register receipt": "GARBAGE",
    "greasy or wet paper package": "GARBAGE",
    "black plastic takeout container": "GARBAGE",
    "disposable mask or glove": "GARBAGE",
    "empty background": "UNKNOWN",
    "human hand or person": "UNKNOWN",
    "phone electronic device or cable": "E-WASTE",
    "battery or hazardous small item": "UNKNOWN",
    "reusable plastic toy or case": "GARBAGE",
    "clothing wallet keys or glasses": "GARBAGE",
    "pen pencil or small tool": "GARBAGE",
    "broken glass ceramic or sharp object": "UNKNOWN",
    "medicine blister pack or medical item": "UNKNOWN",
    "wood rubber or mixed material object": "GARBAGE",
    "food still inside packaging": "GARBAGE",
}

# If a visually "clean" class barely beats its contaminated version, do not
# open a bin. CLIP cannot reliably measure grease or food residue.
CONFLICT_LABELS = {
    "clean paper or cardboard package": {
        "greasy or wet paper package",
        "food still inside packaging",
    },
    "clean paper bag sheet or booklet": {
        "used tissue napkin or paper towel",
        "greasy or wet paper package",
    },
    "rigid plastic cup container or lid": {
        "black plastic takeout container",
        "food still inside packaging",
    },
    "clean foam food package": {
        "food still inside packaging",
    },
}


class WasteItem:
    def __init__(self, label, prompts):
        self.label = label
        self.bin_type = LOCAL_BIN_RULES[label]
        self.prompts = prompts


class StablePrediction:
    """Accept one label after it wins two of the last three valid checks."""

    def __init__(self, window_size=3, required_votes=2):
        self.history = deque(maxlen=window_size)
        self.required_votes = required_votes

    def clear(self):
        self.history.clear()

    def add(self, item, score):
        if item.label == "empty background":
            self.clear()
            return item, score, True

        self.history.append((item, score))
        counts = Counter(item.label for item, _ in self.history)
        winning_label, votes = counts.most_common(1)[0]
        matches = [
            (item, score)
            for item, score in self.history
            if item.label == winning_label
        ]
        average_score = sum(score for _, score in matches) / len(matches)
        return matches[-1][0], average_score, votes >= self.required_votes


def build_items():
    """Broad visual classes that CLIP can reasonably separate."""

    return [
        # RECYCLING -------------------------------------------------------
        WasteItem("plastic beverage bottle", [
            "a plastic drink bottle with a narrow neck screw cap and rounded shoulders",
            "a clear or colored water bottle with thin hollow sides and label",
            "a crushed beverage bottle still showing its small opening and cap ring",
            "a sideways plastic soda bottle with bottle neck and curved base",
        ]),
        WasteItem("metal food or drink can", [
            "a metal beverage can with round ends straight sides and pull tab",
            "a steel food can with rolled rim paper label and hollow center",
            "a crushed aluminum can showing thin shiny metal and printed graphics",
            "a sideways metal can with circular end and cylinder-shaped body",
        ]),
        WasteItem("glass bottle or jar", [
            "a heavy glass bottle with thick clear walls narrow neck and hard base",
            "a wide glass food jar with screw ridges around its open mouth",
            "a green brown or clear glass container with sharp light reflections",
            "an intact glass package lying sideways with a thick solid bottom",
        ]),
        WasteItem("clean paper or cardboard package", [
            "a dry cardboard shipping box with broad flaps and thick layered edges",
            "a thin cereal or retail box with printed panels and small folding tabs",
            "a clean flattened paper package with straight folds and no food marks",
            "a partly crushed cardboard box that remains dry clean and stiff",
        ]),
        WasteItem("clean paper bag sheet or booklet", [
            "a clean paper shopping bag with folded sides and paper handles",
            "a dry flyer newspaper or office sheet covered with printed text",
            "a magazine or booklet made from several flat joined paper pages",
            "clean folded paper with sharp edges and no food or liquid stains",
        ]),
        WasteItem("paper drink cup or carton", [
            "a paper coffee cup with sloped sides rolled top edge and printed wall",
            "a milk or juice carton with sealed folds and plastic pour cap",
            "a crushed paper drink cup still showing its round rim and coated wall",
            "a small drink carton with square sides and a sealed straw opening",
        ]),
        WasteItem("rigid plastic cup container or lid", [
            "a clear plastic drink cup with wide open rim and sloped hollow sides",
            "a rigid plastic food tub or tray with raised edges and hollow center",
            "a clear food box with a joined lid locking rim and corner lines",
            "a round plastic cup lid with snap edge and straw opening",
        ]),
        WasteItem("clean foam food package", [
            "a white foam takeout box with thick light walls and joined lid",
            "a foam plate cup or tray showing tiny pressed white beads",
            "a clean foam food container with rounded corners and no food residue",
            "a partly crushed white foam package that keeps its thick soft shape",
        ]),

        # COMPOST ---------------------------------------------------------
        WasteItem("fruit peel or core", [
            "a banana peel with long yellow strips pale inside and dark stem",
            "an apple core with seeds stem wet flesh and bitten peel ends",
            "orange or lemon peel with bright bumpy skin and thick white inside",
            "irregular moist fruit skin or core with no wrapper or container",
        ]),
        WasteItem("loose meal leftovers", [
            "a loose pile of cooked rice noodles vegetables meat or beans",
            "mixed wet meal scraps with sauce and several soft food pieces",
            "partly eaten food removed from every wrapper plate and container",
            "irregular cooked leftovers spread as loose pieces in the sorting area",
        ]),
        WasteItem("bread sandwich pizza or pastry", [
            "a bitten sandwich or burger with bread around visible layered filling",
            "a triangle pizza slice with crust sauce melted cheese and toppings",
            "a donut muffin or pastry showing baked crumb glaze or flaky layers",
            "partly eaten bread-based food with bite marks and no packaging",
        ]),
        WasteItem("coffee grounds or tea bag", [
            "a damp pile of tiny dark brown used coffee grounds",
            "a wet tea bag with paper cover string and small tag",
            "a brown-stained paper coffee filter holding used coffee pieces",
            "soft wet drink-making waste removed from cups pods and wrappers",
        ]),

        # GARBAGE ---------------------------------------------------------
        WasteItem("flexible snack wrapper or plastic film", [
            "a crinkled chip candy or granola wrapper with shiny printed layers",
            "a thin plastic shopping bag with handles wrinkles and soft folds",
            "clear product wrap that bends stretches and collapses completely flat",
            "a crushed flexible food packet with sealed edges and no rigid shape",
        ]),
        WasteItem("used tissue napkin or paper towel", [
            "a white facial tissue crushed into a soft wrinkled wad",
            "a food napkin with printed pattern grease sauce or drink stains",
            "a torn paper towel showing soft soaking fibers and uneven edges",
            "used thin absorbent paper that looks softer than office paper",
        ]),
        WasteItem("plastic straw or disposable cutlery", [
            "a long narrow plastic drinking straw with a small hollow opening",
            "a plastic fork with handle and several short pointed teeth",
            "a plastic spoon with long handle and oval eating bowl",
            "a thin plastic knife with handle and rough cutting edge",
        ]),
        WasteItem("thermal cash register receipt", [
            "a long narrow white receipt printed with rows of tiny prices",
            "a cash register slip showing store name date subtotal tax and total",
            "a curled thermal paper strip with barcode numbers and straight cut edges",
            "a crumpled receipt still showing dense black text in narrow columns",
        ]),
        WasteItem("greasy or wet paper package", [
            "a pizza box with dark see-through grease stains and stuck food crumbs",
            "a cardboard food box with sauce marks wet spots and softened paper",
            "an oily paper plate or carton with visible food residue",
            "dirty crushed cardboard that looks stained damp or no longer stiff",
        ]),
        WasteItem("black plastic takeout container", [
            "a black plastic food tray with wide open top and raised edge",
            "a dark takeout box with hard smooth walls and flat bottom",
            "a black plastic meal container with separate food sections",
            "a crushed dark plastic food package that blocks light through its walls",
        ]),
        

        # UNKNOWN / NO SERVO ---------------------------------------------
        WasteItem("empty background", [
            "an empty sorting area showing only the flat fixed table surface",
            "a clear inspection box containing no object hand or waste",
            "a plain background with no item near the center",
            "an unused waste scanner showing only its normal background",
        ]),
        WasteItem("human hand or person", [
            "a close-up human fingertip showing skin ridges and a rounded fingernail",
            "one large human finger close to the camera filling most of the image",
            "a human hand showing palm knuckles fingers skin and fingernails",
            "a human face showing eyes nose mouth hair and skin",
            "a person showing head shoulders arms or upper body",
            "human fingers or a thumb holding an object near the camera lens",
        ]),
        WasteItem("phone electronic device or cable", [
            "a smartphone with flat glass screen side buttons and camera lenses",
            "a charger cable with wire and metal USB connector",
            "earbuds power bank remote or electronic device with ports and buttons",
            "a small powered object containing a screen wire plug or circuit parts",
        ]),
        WasteItem("battery or hazardous small item", [
            "a small cylinder battery with metal ends and plus minus markings",
            "a flat round coin battery made from shiny metal",
            "a rectangular nine volt battery with two top connectors",
            "a small hazardous power cell that must not enter an automatic bin",
        ]),
        WasteItem("reusable plastic toy or case", [
            "a puzzle cube with colored square tiles on several sides",
            "a reusable toy made from thick solid plastic pieces",
            "a hard plastic case with latch handle or moving hinge",
            "a solid plastic product without a bottle neck cup rim or food space",
        ]),
        WasteItem("clothing wallet keys or glasses", [
            "a fabric shirt sock glove or hat with cloth folds and sewn seams",
            "a wallet with card slots stitched edges and center fold",
            "metal keys with cut teeth attached to a round key ring",
            "a pair of glasses with two lenses folding arms and nose bridge",
        ]),
        WasteItem("pen pencil or small tool", [
            "a pen with narrow barrel pocket clip and pointed writing tip",
            "a wooden pencil with sharpened graphite point and painted sides",
            "a small screwdriver tool with handle metal shaft and shaped tip",
            "a reusable writing or hand tool with a long solid body",
        ]),
        WasteItem("broken glass ceramic or sharp object", [
            "jagged transparent broken glass pieces with sharp irregular edges",
            "a broken ceramic cup or plate showing thick white cracked pieces",
            "a loose blade needle or sharp metal object with pointed end",
            "a dangerous broken item that could cut a person or collection bag",
        ]),
        WasteItem("medicine blister pack or medical item", [
            "a medicine blister pack with pills under clear plastic bubbles",
            "a flat foil-backed tablet package with several round empty pockets",
            "a syringe bandage medicine tube or small healthcare item",
            "medical packaging with printed dose text foil plastic or pill shapes",
        ]),
        WasteItem("wood rubber or mixed material object", [
            "a wooden chopstick stick or small wood piece with visible grain",
            "a rubber band rubber toy or flexible dark rubber item",
            "an object made from several joined materials that cannot be separated",
            "a reusable solid object that is not food or disposable packaging",
        ]),
        WasteItem("food still inside packaging", [
            "a takeout container still holding visible food sauce or liquid",
            "a wrapped sandwich pastry or meal with food and packaging together",
            "a cup bottle tray or box that is still partly full",
            "mixed food and wrapper material that must be separated before sorting",
        ]),
    ]


class WasteVisionAI:
    def __init__(self, camera_source=None, camera_index=CAMERA_INDEX):
        print("Loading CLIP model...")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = CLIPModel.from_pretrained(MODEL_NAME).to(
            self.device
        ).eval()
        self.processor = CLIPProcessor.from_pretrained(MODEL_NAME)
        self.items = build_items()

        print("Precomputing balanced text features...")
        self.text_features = self.build_text_features()
        self.item_index_by_label = {
            item.label: index for index, item in enumerate(self.items)
        }
        self.learned_prototypes = {}
        self.learning_file_mtime = 0
        self.load_learned_references(force=True)
        print(f"Text features ready for {len(self.items)} broad classes.")
        print(f"Rule profile: {RULE_PROFILE}")
        print(f"Rule source: {RULE_SOURCE}")

        self.stable_prediction = StablePrediction()
        self.current_label = "none"
        self.current_bin = "UNKNOWN"
        self.current_similarity = 0.0
        self.current_margin = 0.0
        self.current_command = "U"
        self.current_state = "READY"
        self.current_top = []
        self.last_sent_command = None
        self.hardware_cycle_active = False
        self.last_unknown_capture_at = 0.0
        self.last_unknown_capture_label = None
        self.uncertain_capture_count = 0
        self.arduino = None

        if SERIAL_PORT:
            self.connect_arduino(SERIAL_PORT)

        self.frame_count = 0
        self.classify_every_n_frames = 6
        source = camera_source if camera_source is not None else self.default_camera_source()
        self.cap = cv2.VideoCapture(source)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        print(f"Ready. Running on {self.device}.")

    @staticmethod
    def default_camera_source():
        if CAMERA_SOURCE:
            if CAMERA_SOURCE.isdigit():
                return int(CAMERA_SOURCE)
            return CAMERA_SOURCE
        return CAMERA_INDEX

    def extract_features(self, output, modality):
        """Support old and new Transformers CLIP return types."""

        if isinstance(output, torch.Tensor):
            return output

        attribute = "text_embeds" if modality == "text" else "image_embeds"
        projected = getattr(output, attribute, None)
        if isinstance(projected, torch.Tensor):
            return projected

        pooled = getattr(output, "pooler_output", None)
        from_hidden_state = False
        if not isinstance(pooled, torch.Tensor):
            hidden = getattr(output, "last_hidden_state", None)
            if isinstance(hidden, torch.Tensor):
                pooled = hidden[:, 0, :]
                from_hidden_state = True

        if not isinstance(pooled, torch.Tensor):
            raise TypeError(
                f"Could not extract CLIP {modality} features from "
                f"{type(output).__name__}."
            )

        projection = (
            self.model.text_projection
            if modality == "text"
            else self.model.visual_projection
        )
        if not from_hidden_state and pooled.shape[-1] == projection.out_features:
            return pooled
        if pooled.shape[-1] != projection.in_features:
            raise ValueError(
                f"Unexpected CLIP {modality} width {pooled.shape[-1]}."
            )
        return projection(pooled)

    def encode_text(self, prompts):
        inputs = self.processor(
            text=prompts,
            return_tensors="pt",
            padding=True,
            truncation=True,
        )
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        with torch.inference_mode():
            output = self.model.get_text_features(**inputs)
            features = self.extract_features(output, "text")
            return F.normalize(features, dim=-1)

    def build_text_features(self):
        prototypes = []
        for item in self.items:
            prompts = [
                f"{item.label}: {prompt}"
                for prompt in item.prompts
            ]
            features = self.encode_text(prompts)
            prototypes.append(F.normalize(features.mean(dim=0), dim=0))
        return torch.stack(prototypes)

    def get_image_features(self, images):
        inputs = self.processor(images=images, return_tensors="pt")
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        with torch.inference_mode():
            output = self.model.get_image_features(**inputs)
            features = self.extract_features(output, "image")
            return F.normalize(features, dim=-1)

    def load_learned_references(self, force=False):
        if not LEARNING_FILE.exists():
            self.learned_prototypes = {}
            return

        modified_at = LEARNING_FILE.stat().st_mtime
        if not force and modified_at == self.learning_file_mtime:
            return

        try:
            data = json.loads(LEARNING_FILE.read_text(encoding="utf-8"))
            loaded = {}
            for label, reference in data.get("references", {}).items():
                if label not in self.item_index_by_label:
                    continue
                prototype = torch.tensor(
                    reference["prototype"],
                    dtype=torch.float32,
                    device=self.device,
                )
                loaded[label] = F.normalize(prototype, dim=0)
            self.learned_prototypes = loaded
            self.learning_file_mtime = modified_at
            print(f"Loaded {len(loaded)} learned visual classes.")
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"Could not load learned references: {error}")

    def crop_regions(self, frame):
        height, width = frame.shape[:2]
        tight = frame[
            int(height * 0.16):int(height * 0.72),
            int(width * 0.22):int(width * 0.78),
        ]
        wide = frame[
            int(height * 0.08):int(height * 0.84),
            int(width * 0.12):int(width * 0.88),
        ]
        return tight, wide

    def rank_frame(self, frame):
        self.load_learned_references()
        images = [
            Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
            for crop in self.crop_regions(frame)
        ]
        image_features = self.get_image_features(images)
        image_feature = F.normalize(image_features.mean(dim=0), dim=0)
        similarities = image_feature @ self.text_features.T

        for label, prototype in self.learned_prototypes.items():
            index = self.item_index_by_label[label]
            learned_score = image_feature @ prototype
            if learned_score >= LEARNED_MATCH_MIN_SIMILARITY:
                similarities[index] = (
                    TEXT_SCORE_WEIGHT * similarities[index]
                    + LEARNED_IMAGE_WEIGHT * learned_score
                )

        scores, indices = torch.topk(similarities, min(5, len(self.items)))
        ranked = [
            (self.items[index], float(score))
            for score, index in zip(scores.tolist(), indices.tolist())
        ]
        return ranked, image_feature

    def contamination_conflict(self, ranked):
        best_item, best_score = ranked[0]
        conflicts = CONFLICT_LABELS.get(best_item.label, set())
        for item, score in ranked[1:]:
            if item.label in conflicts and best_score - score < CONFLICT_MARGIN:
                return item
        return None

    def classify(self, frame):
        ranked, image_feature = self.rank_frame(frame)
        self.current_top = ranked[:3]
        best_item, best_score = ranked[0]
        margin = best_score - ranked[1][1]
        conflict = self.contamination_conflict(ranked)

        print("\nTop guesses:")
        print(f"{'Label':38s} | {'Bin':10s} | {'Cosine':7s}")
        print("-" * 64)
        for item, score in ranked[:5]:
            print(f"{item.label:38s} | {item.bin_type:10s} | {score:7.3f}")

        accepted = (
            best_score >= MIN_COSINE_SIMILARITY
            and margin >= MIN_WINNING_MARGIN
            and conflict is None
        )

        if not accepted:
            self.stable_prediction.clear()
            self.current_label = (
                "possible contamination"
                if conflict is not None
                else "uncertain object"
            )
            self.current_bin = "UNKNOWN"
            self.current_similarity = best_score
            self.current_margin = margin
            self.current_state = "UNKNOWN"
            self.current_command = "U"
            reason = (
                f"conflicts with {conflict.label}"
                if conflict is not None
                else "score or margin below threshold"
            )
            print(f"UNKNOWN: {reason}; Arduino U")
            if (
                conflict is None
                and best_item.label not in UNKNOWN_CAPTURE_EXCLUSIONS
            ):
                self.uncertain_capture_count += 1
                if self.uncertain_capture_count >= 3:
                    self.publish_unknown_snapshot(
                        frame,
                        ranked,
                        image_feature,
                        best_item.label,
                    )
                    self.uncertain_capture_count = 0
            else:
                self.uncertain_capture_count = 0
            self.send_to_arduino("U")
            return

        self.uncertain_capture_count = 0
        stable_item, stable_score, is_stable = self.stable_prediction.add(
            best_item,
            best_score,
        )
        self.current_label = stable_item.label
        self.current_bin = stable_item.bin_type
        self.current_similarity = stable_score
        self.current_margin = margin

        if is_stable:
            self.current_state = "FINAL"
            self.current_command = self.bin_to_command(stable_item.bin_type)
        else:
            self.current_state = "CHECKING"
            self.current_command = "U"

        print(
            f"{self.current_state}: {self.current_label} -> "
            f"{self.current_bin} | cosine {self.current_similarity:.3f} | "
            f"margin {margin:.3f} | Arduino {self.current_command}"
        )
        if (
            self.current_state == "FINAL"
            and self.current_bin == "UNKNOWN"
            and self.current_label not in UNKNOWN_CAPTURE_EXCLUSIONS
        ):
            self.publish_unknown_snapshot(
                frame,
                ranked,
                image_feature,
                self.current_label,
            )
        self.send_to_arduino(self.current_command)

    def run(self):
        if not self.cap.isOpened():
            print("Error: Could not open webcam.")
            return

        while True:
            ret, frame = self.cap.read()
            if not ret:
                print("Warning: webcam frame failed; retrying...")
                recovered = False
                for _ in range(3):
                    time.sleep(0.2)
                    ret, frame = self.cap.read()
                    if ret:
                        recovered = True
                        break
                if not recovered:
                    print("Error: webcam failed after three retries.")
                    break

            self.frame_count += 1
            self.read_arduino_messages()
            if self.frame_count % self.classify_every_n_frames == 0:
                self.classify(frame)

            self.draw_ui(frame)
            cv2.imshow("Binlytic AI", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        self.cap.release()
        cv2.destroyAllWindows()

    def connect_arduino(self, port):
        try:
            chosen_port = self.autodetect_arduino_port(port)
            if chosen_port != port:
                print(f"Arduino port auto-detected: {chosen_port}")
            # Open with DTR/RTS deasserted: pyserial's defaults pull the
            # ESP32's GPIO0 low during the open-triggered reset, booting it
            # into the flash bootloader (silent) instead of the sketch.
            self.arduino = serial.Serial()
            self.arduino.port = chosen_port
            self.arduino.baudrate = 9600
            self.arduino.timeout = 0
            self.arduino.dtr = False
            self.arduino.rts = False
            self.arduino.open()
            # Clean reset pulse with GPIO0 held high -> normal boot.
            self.arduino.rts = True
            time.sleep(0.1)
            self.arduino.rts = False
            # ESP32 keeps servo signals off for 3 seconds, then homes the flaps.
            time.sleep(4.5)
            print(f"Arduino connected on {chosen_port}.")
        except Exception as error:
            self.arduino = None
            print(f"Arduino not connected on {port}: {error}")
            print("Program will still run without Arduino output.")

    def autodetect_arduino_port(self, preferred_port):
        if not list_ports:
            return preferred_port

        ports = list(list_ports.comports())
        preferred_upper = preferred_port.upper()
        for candidate in ports:
            if candidate.device.upper() == preferred_upper:
                return candidate.device

        keywords = ("arduino", "ch340", "cp210", "usb serial", "ftdi", "esp32")
        for candidate in ports:
            haystack = f"{candidate.description} {candidate.manufacturer or ''}".lower()
            if any(keyword in haystack for keyword in keywords):
                return candidate.device

        return preferred_port

    def send_to_arduino(self, command):
        if (
            self.hardware_cycle_active
            and command != self.last_sent_command
        ):
            print("ESP32 route is active; waiting for confirmation.")
            return

        if command == self.last_sent_command:
            return

        print(f"ARDUINO OUTPUT CHANGED: {command}")
        if self.arduino is not None:
            try:
                self.arduino.write(command.encode())
                self.arduino.flush()
                if (
                    command in {"R", "C", "G", "E"}
                    and self.current_state == "FINAL"
                ):
                    self.hardware_cycle_active = command in {"R", "C", "G"}
                    self.post_dashboard(
                        "/api/classifications",
                        {
                            "bin_id": TRASHCAN_ID,
                            "label": self.current_label,
                            "destination": self.current_bin,
                            "confidence": self.current_similarity,
                        },
                    )
            except Exception as error:
                print(f"Could not send to Arduino: {error}")
                self.arduino = None
        self.last_sent_command = command

    def read_arduino_messages(self):
        """Forward confirmed entry-sensor events to the dashboard."""

        if self.arduino is None:
            return

        try:
            while self.arduino.in_waiting:
                message = self.arduino.readline().decode(
                    "utf-8",
                    errors="replace",
                ).strip()
                if not message:
                    continue

                print(f"ESP32: {message}")
                parts = [part.strip() for part in message.split(",")]
                if parts[0].upper() == "OBJECT" and len(parts) >= 3:
                    self.hardware_cycle_active = False
                    self.post_dashboard(
                        "/api/detections",
                        {
                            "bin_id": TRASHCAN_ID,
                            "destination": parts[1].upper(),
                            "distance_cm": float(parts[2]),
                        },
                    )
                elif parts[0].upper() == "TIMER" and len(parts) >= 2:
                    self.hardware_cycle_active = False
                    self.post_dashboard(
                        "/api/timer-confirmations",
                        {
                            "bin_id": TRASHCAN_ID,
                            "destination": parts[1].upper(),
                        },
                    )
                elif parts[0].upper() == "TIMEOUT" and len(parts) >= 2:
                    self.hardware_cycle_active = False
                    print(
                        f"{parts[1].upper()} route closed after "
                        "12 seconds without ultrasonic confirmation."
                    )
        except (OSError, ValueError, serial.SerialException) as error:
            print(f"Could not read ESP32 message: {error}")

    def publish_unknown_snapshot(
        self,
        frame,
        ranked,
        image_feature,
        capture_key,
    ):
        now = time.monotonic()
        same_recent_object = (
            capture_key == self.last_unknown_capture_label
            and now - self.last_unknown_capture_at
            < UNKNOWN_CAPTURE_COOLDOWN_SECONDS
        )
        if same_recent_object:
            return

        crop = self.crop_regions(frame)[0]
        encoded, jpeg = cv2.imencode(
            ".jpg",
            crop,
            [cv2.IMWRITE_JPEG_QUALITY, 82],
        )
        if not encoded:
            print("Could not capture unknown-object image.")
            return

        top_guesses = [
            {
                "label": item.label,
                "bin": item.bin_type,
                "score": round(score, 4),
            }
            for item, score in ranked[:3]
        ]
        description = "Visually closest to " + ", ".join(
            guess["label"] for guess in top_guesses
        )
        result = self.post_dashboard(
            "/api/unknowns",
            {
                "bin_id": TRASHCAN_ID,
                "confidence": ranked[0][1],
                "image_base64": base64.b64encode(jpeg).decode("ascii"),
                "embedding": [
                    round(value, 6)
                    for value in image_feature.detach().cpu().tolist()
                ],
                "top_guesses": top_guesses,
                "description": description,
            },
        )
        self.last_unknown_capture_at = now
        self.last_unknown_capture_label = capture_key
        if result and result.get("learning_status") == "auto-learned":
            self.load_learned_references(force=True)
        print("UNKNOWN OBJECT snapshot sent to dashboard.")

    @staticmethod
    def post_dashboard(path, payload):
        request = Request(
            f"{DASHBOARD_API}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=0.5) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except (OSError, URLError) as error:
            print(f"Dashboard update failed: {error}")
            return None

    @staticmethod
    def bin_to_command(bin_type):
        return {
            "RECYCLING": "R",
            "COMPOST": "C",
            "GARBAGE": "G",
            "E-WASTE": "E",
        }.get(bin_type, "U")

    def draw_ui(self, frame):
        height, width = frame.shape[:2]
        x1, y1 = int(width * 0.22), int(height * 0.16)
        x2, y2 = int(width * 0.78), int(height * 0.72)
        color = self.get_color(self.current_bin)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)

        cv2.putText(
            frame,
            f"{self.current_state}: {self.current_label.upper()}",
            (x1, y1 - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            color,
            2,
        )
        cv2.putText(
            frame,
            "WasteVision AI - Broad Classes",
            (18, 34),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.68,
            (255, 255, 255),
            2,
        )

        panel_height = 150
        cv2.rectangle(
            frame,
            (0, height - panel_height),
            (width, height),
            (0, 0, 0),
            -1,
        )
        cv2.putText(
            frame,
            f"{self.current_label.upper()} -> {self.current_bin}",
            (18, height - 118),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            color,
            2,
        )
        cv2.putText(
            frame,
            f"Cosine {self.current_similarity:.3f} | "
            f"margin {self.current_margin:.3f} | Arduino {self.current_command}",
            (18, height - 92),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (255, 255, 255),
            1,
        )

        for rank, (item, score) in enumerate(self.current_top[:3], start=1):
            text = f"{rank}. {item.label[:27]} [{item.bin_type}] {score:.3f}"
            cv2.putText(
                frame,
                text,
                (18, height - 92 + rank * 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.36,
                (220, 220, 220),
                1,
            )

        cv2.putText(
            frame,
            f"{RULE_PROFILE} | one item only | Q quit",
            (18, height - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.34,
            (220, 220, 220),
            1,
        )

    @staticmethod
    def get_color(bin_type):
        return {
            "RECYCLING": (255, 180, 0),
            "COMPOST": (0, 200, 0),
            "GARBAGE": (0, 0, 255),
            "E-WASTE": (180, 80, 255),
            "UNKNOWN": (0, 255, 255),
        }.get(bin_type, (0, 255, 255))


if __name__ == "__main__":
    WasteVisionAI().run()
