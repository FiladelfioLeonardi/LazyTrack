"""Webcam smoking detector.

Frames are analysed in RAM and thrown away. The only images ever saved are small
crops of your face area: when you record training data (`lt collect`) or answer
the popup - that's what makes your personal model better over time.

Cascade, so it stays cheap:
  1. every frame: is someone there? (Haar face detection + motion, ~1 ms)
  2. every Nth frame: score the area around your face/mouth/hands
       - your personal model (after `lt train`)       -> "how likely is this smoking?"
       - otherwise YOLO-World (general, less accurate) -> "is there a cigarette?"
  3. every check adds "evidence" that slowly fades (half-life ~20 s). Near-misses
     count a bit too, so a finger over the cigarette or a few bad frames don't
     reset anything. Evidence over the trigger score -> ask you.
"""
from __future__ import annotations

import logging
import sys
import random
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from . import config, db, system

log = logging.getLogger(__name__)

ANSWERS = {"Cigarette": "cigarette", "Joint": "joint"}
DATASET = config.HOME / "dataset"            # dataset/smoking/*.jpg, dataset/not_smoking/*.jpg
PERSONAL_MODEL = config.HOME / "smoke-personal.pt"
CLASSES = ("smoking", "not_smoking")

Box = Tuple[int, int, int, int]  # x, y, w, h


def pick_device() -> str:
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"  # Apple Silicon GPU
        if torch.cuda.is_available():
            return "cuda"  # NVIDIA graphics card (needs the CUDA version of PyTorch)
    except Exception:
        pass
    return "cpu"


def face_detector():
    """Returns a face detector, or None if this OpenCV build is broken (motion-only fallback)."""
    import cv2
    try:
        det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        return None if det.empty() else det
    except AttributeError:
        log.warning("OpenCV install is broken (two opencv packages installed?). Run ./install.sh again.")
        return None


def fix_light(frame, mode: str = "auto"):
    """Undo colored lighting and darkness before the AI looks.

    RGB lights (say, all blue) tint the whole picture, so a white cigarette looks blue and the
    face finder loses contrast. Gray-world balance scales each color channel so they average
    out the same, which brings the picture back to neutral. Dim, grainy webcams get local
    contrast (CLAHE on the brightness only). Returns (frame, what was fixed or "")."""
    import cv2
    import numpy as np
    if mode == "off":
        return frame, ""
    small = cv2.resize(frame, (64, 36))
    means = small.reshape(-1, 3).astype(np.float32).mean(0) + 1.0      # B, G, R
    cast = float(means.max() / means.min())
    bright = float(means.mean())
    fixed = []
    out = frame
    if cast > 1.2 or mode == "on":
        gains = np.clip(means.mean() / means, 0.4, 3.0)
        if gains.max() > 1.6 or bright < 95:
            # boosting a weak color channel also boosts the webcam's grain: smooth it first
            # (a median filter removes speckles but keeps edges like the cigarette sharp)
            out = cv2.medianBlur(out, 3)
        out = np.clip(out.astype(np.float32) * gains, 0, 255).astype(np.uint8)
        if cast > 1.2:
            fixed.append(("blue", "green", "red")[int(np.argmax(means))] + " light")
    if bright < 95 or cast > 1.2 or mode == "on":
        lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        if bright < 95:   # dim webcam: lift the brightness toward normal (at most 2x)
            l = cv2.convertScaleAbs(l, alpha=min(2.0, 115.0 / max(1.0, float(l.mean()))))
        l = cv2.createCLAHE(clipLimit=2.0 if bright < 95 else 1.6, tileGridSize=(8, 8)).apply(l)
        out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
        if bright < 95:
            fixed.append("dark picture")
    return out, " + ".join(fixed)


class FaceTracker:
    """Finds the biggest face and remembers it for a moment (your hand covers it while smoking)."""

    def __init__(self):
        self.det = face_detector()
        self.box: Optional[Box] = None
        self.seen_at = 0.0

    def update(self, frame) -> Tuple[Optional[Box], "object"]:
        import cv2
        w = 320
        scale = frame.shape[1] / w
        small = cv2.resize(frame, (w, int(frame.shape[0] / scale)))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if self.det is not None:
            faces = self.det.detectMultiScale(gray, 1.2, 5, minSize=(30, 30))
            if len(faces):
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                self.box = (int(x * scale), int(y * scale), int(fw * scale), int(fh * scale))
                self.seen_at = time.time()
        if time.time() - self.seen_at > 2.5:
            self.box = None
        return self.box, gray


def roi(frame, face: Optional[Box]):
    """Crop around face, mouth and hands. Without a face: the whole frame."""
    if face is None:
        h, w = frame.shape[:2]
        return frame, (0, 0, w, h)
    x, y, fw, fh = face
    H, W = frame.shape[:2]
    cx = x + fw / 2
    x0, x1 = int(max(0, cx - 1.5 * fw)), int(min(W, cx + 1.5 * fw))
    y0, y1 = int(max(0, y - 0.3 * fh)), int(min(H, y + 2.2 * fh))
    return frame[y0:y1, x0:x1], (x0, y0, x1, y1)


# ---------------------------------------------------------------- scorers

class WorldScorer:
    """General open-vocabulary detector. Works out of the box, but it has never seen you."""
    name = "YOLO-World (general)"

    def __init__(self, s: dict, device: str):
        from ultralytics import YOLO
        self.m = YOLO(s["model"])
        if hasattr(self.m, "set_classes"):
            self.m.set_classes(s["classes"])
        self.device = device
        self.threshold = float(s["confidence"])

    def score(self, crop) -> float:
        r = self.m.predict(crop, conf=0.05, verbose=False, device=self.device, imgsz=640)[0]
        if r.boxes is None or len(r.boxes) == 0:
            return 0.0
        return float(r.boxes.conf.max().item())


class PersonalScorer:
    """Small classifier trained on your own photos with `lt train`."""
    name = "personal model"

    def __init__(self, s: dict, device: str):
        from ultralytics import YOLO
        self.m = YOLO(str(PERSONAL_MODEL))
        self.idx = next(k for k, v in self.m.names.items() if v == "smoking")
        self.device = device
        self.threshold = float(s["personal_threshold"])

    def score(self, crop) -> float:
        r = self.m.predict(crop, imgsz=224, verbose=False, device=self.device)[0]
        return float(r.probs.data[self.idx].item())


def make_scorer(s: dict):
    device = pick_device()
    mode = s.get("detector", "auto")
    if mode in ("auto", "personal") and PERSONAL_MODEL.exists():
        try:
            return PersonalScorer(s, device)
        except Exception:
            log.exception("could not load personal model, falling back to YOLO-World")
    return WorldScorer(s, device)


def glowing_tip(crop) -> bool:
    """Small, very bright orange/red spot = a cigarette tip glowing while you pull on it.
    Big bright areas (lamps, windows, screens) are ignored."""
    import cv2
    import numpy as np
    h, w = crop.shape[:2]
    if h < 20 or w < 20:
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    # orange-red hue (wraps around 180), strong colour, very bright
    mask = (((hsv[..., 0] <= 22) | (hsv[..., 0] >= 170)) & (hsv[..., 1] >= 110) & (hsv[..., 2] >= 225))
    mask = mask.astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    area = h * w
    for i in range(1, n):
        a = stats[i, cv2.CC_STAT_AREA]
        if 3 <= a <= max(12, area * 0.002):  # tiny blob only
            return True
    return False


class Evidence:
    """A score that goes up with each detection and decays exponentially over time."""

    def __init__(self, half_life: float):
        self.half_life = max(1.0, float(half_life))
        self.v = 0.0
        self.t = time.time()

    def value(self) -> float:
        now = time.time()
        self.v *= 0.5 ** ((now - self.t) / self.half_life)
        self.t = now
        return self.v

    def add_score(self, score: float, threshold: float, glow: bool = False) -> float:
        """Full point for a clear hit, partial credit for a near miss (partly hidden cigarette)
        or a glowing tip."""
        if score >= threshold:
            gain = 1.0
        elif score >= 0.6 * threshold:
            gain = 0.4
        else:
            gain = 0.0
        if glow:
            gain += 0.5
        self.v = self.value() + gain
        return gain

    def reset(self) -> None:
        self.v, self.t = 0.0, time.time()


def save_crop(crop, label: str) -> None:
    import cv2
    d = DATASET / label
    d.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(d / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jpg")), crop)


# ---------------------------------------------------------------- what the dashboard can see
# The detector runs in the same program as the dashboard, so it shares its state through this
# dict: status for Settings, and (only while someone looks at the live view) a small JPEG.
LIVE = {"status": "off", "detail": "", "light": "", "score": 0.0, "threshold": 0.0, "evidence": 0.0, "trigger": 0.0,
        "model": "", "face": False, "until": 0.0, "frame": None, "frame_t": 0.0, "want_until": 0.0}


def _set(status: str, detail: str = "", **kw) -> None:
    LIVE.update(status=status, detail=detail, **kw)


def _preview(frame, face, cv2) -> None:
    """Small picture for the live view, with the face box drawn in."""
    h, w = frame.shape[:2]
    scale = 480.0 / max(1, w)
    small = cv2.resize(frame, (480, int(h * scale)))
    if face is not None:
        x, y, fw, fh = [int(v * scale) for v in face]
        cv2.rectangle(small, (x, y), (x + fw, y + fh), (94, 228, 255), 2)
    ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if ok:
        LIVE.update(frame=jpg.tobytes(), frame_t=time.time())


# ---------------------------------------------------------------- background detector

class SmokeDetector(threading.Thread):
    def __init__(self, cfg: dict, stop: threading.Event):
        super().__init__(name="smoke", daemon=True)
        self.scfg = cfg["smoke"]
        self.device_name = cfg["device_name"]
        self.stop_ev = stop
        self.status = "starting"

    def _paused(self) -> bool:
        if system.screen_locked():
            return True
        # watching a video counts as being here, so the camera keeps going
        return system.idle_seconds() > self.scfg["pause_when_idle_seconds"] and not system.video_playing()

    def run(self) -> None:
        _set("loading", "Loading the camera AI. The first time it downloads it (about 400 MB).")
        try:
            import cv2
            self.scorer = make_scorer(self.scfg)
            self.faces = FaceTracker()
        except Exception as e:
            log.exception("smoke detector disabled: could not load model / OpenCV")
            self.status = "disabled (load error)"
            _set("error", f"The camera AI couldn't load: {e.__class__.__name__}: {str(e)[:160]}")
            return
        LIVE.update(model=self.scorer.name, threshold=float(self.scorer.threshold), trigger=float(self.scfg["trigger_score"]))
        log.info("smoke detector running with the %s", self.scorer.name)
        model_mtime = PERSONAL_MODEL.stat().st_mtime if PERSONAL_MODEL.exists() else 0.0

        s = self.scfg
        period = 1.0 / max(0.5, float(s["fps"]))
        cap = None
        prev = None
        last_presence = 0.0
        n = 0
        evidence = Evidence(s["evidence_half_life"])
        trigger = float(s["trigger_score"])
        # without a graphics chip (most Windows PCs: pip installs the CPU-only PyTorch there) the AI
        # runs on the processor: look half as often so the fan stays quiet. Evidence still adds up.
        every = max(1, int(s["yolo_every_n_frames"])) * (2 if pick_device() == "cpu" else 1)
        hit_crop = None
        cooldown_until = 0.0   # after a confirmed cigarette/joint: camera off
        quiet_until = 0.0      # after "Neither": camera stays on, just no new question for a bit

        while not self.stop_ev.is_set():
            t0 = time.time()
            if t0 < cooldown_until or self._paused():
                if cap is not None:  # release camera -> green light off
                    cap.release()
                    cap = None
                    prev = None
                    evidence.reset()
                self.status = "cooldown" if t0 < cooldown_until else "paused"
                if t0 < cooldown_until:
                    _set("cooldown", "Camera off for a while after a cigarette, so it isn't counted twice.", until=cooldown_until)
                else:
                    _set("paused", "Paused while you're away or the screen is locked.")
                # picked up a freshly trained model?
                mtime = PERSONAL_MODEL.stat().st_mtime if PERSONAL_MODEL.exists() else 0.0
                if mtime != model_mtime:
                    model_mtime = mtime
                    self.scorer = make_scorer(s)
                    log.info("smoke detector switched to the %s", self.scorer.name)
                self.stop_ev.wait(5)
                continue

            if cap is None:
                cap = open_camera(int(s["camera_index"]))
                if not cap.isOpened():
                    log.warning("camera %s not available, retrying in 30s", s["camera_index"])
                    cap = None
                    self.status = "no camera"
                    _set("no_camera", "Can't open the camera. Is another app using it, or is camera access turned off "
                         + ("(Settings > Privacy & security > Camera > Let desktop apps access your camera)?" if sys.platform == "win32"
                            else "(System Settings > Privacy & Security > Camera)?"))
                    self.stop_ev.wait(30)
                    continue
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self.status = "watching"
            if LIVE["status"] != "watching":
                _set("watching", "Watching for cigarettes.")

            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = None
                self.stop_ev.wait(2)
                continue
            n += 1
            frame, LIVE["light"] = fix_light(frame, s.get("light_fix", "auto"))

            # --- stage 1: anyone there? ---
            face, gray = self.faces.update(frame)
            moving = prev is not None and prev.shape == gray.shape and \
                float(cv2.absdiff(gray, prev).mean()) > s["motion_threshold"]
            prev = gray
            if face is not None or moving:
                last_presence = t0
            LIVE["face"] = face is not None
            if LIVE["want_until"] > t0:   # someone has the live view open in the dashboard
                _preview(frame, face, cv2)

            # --- stage 2: score the face/hand area ---
            if t0 - last_presence < 5 and n % every == 0:
                crop, _ = roi(frame, face)
                try:
                    score = self.scorer.score(crop)
                except Exception:
                    log.exception("scoring failed")
                    score = 0.0
                glow = face is not None and glowing_tip(crop)
                if evidence.add_score(score, self.scorer.threshold, glow) > 0:
                    hit_crop = crop.copy()
                LIVE.update(score=float(score), evidence=float(evidence.value()))
                # --- stage 3: enough evidence -> ask (the camera stays on while asking) ---
                if evidence.value() >= trigger and t0 >= quiet_until:
                    evidence.reset()
                    label = self._ask(hit_crop)
                    if label in ("cigarette", "joint"):
                        # confirmed: switch the camera off so one cigarette isn't counted twice
                        cap.release()
                        cap = None
                        cooldown_until = time.time() + s["cooldown_minutes"] * 60
                    else:
                        # "Neither" or no answer: keep watching, but don't ask again right away
                        quiet_until = time.time() + s["false_alarm_cooldown_minutes"] * 60
                        for _ in range(5):  # skip frames buffered while the question was open
                            cap.grab()
                    prev = None
                    evidence.reset()
                    continue

            self.stop_ev.wait(max(0.0, period - (time.time() - t0)))

        if cap is not None:
            cap.release()
        _set("off")

    def _ask(self, crop) -> str:
        system.notify("LazyTrack 👀", "Looks like you're smoking")
        ans = system.ask("Caught you 👀\nIs that a cigarette or a joint?",
                        ["Neither", "Joint", "Cigarette"], "Cigarette", timeout=120)
        if ans in ANSWERS:
            label = ANSWERS[ans]
            db.add_event(label, self.device_name, source="camera")
        elif ans == "Neither":
            label = "none"
            db.add_event("smoke_false_alarm", self.device_name, source="camera")
        else:
            label = ""
            db.add_event("smoke_unconfirmed", self.device_name, source="camera")
        log.info("smoke detector: answer=%s", ans)

        # every answer becomes a training example for `lt train`
        if label and crop is not None and self.scfg.get("save_labeled_frames"):
            try:
                save_crop(crop, "not_smoking" if label == "none" else "smoking")
            except Exception:
                log.exception("could not save training crop")
        return label


# ---------------------------------------------------------------- interactive tools (main thread)

def open_camera(index: int):
    """cv2.VideoCapture, using DirectShow on Windows: it opens much faster than the default there."""
    import cv2
    if sys.platform == "win32":
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if cap.isOpened():
            return cap
    return cv2.VideoCapture(index)


def _open_camera(s: dict):
    import cv2
    cap = open_camera(int(s["camera_index"]))
    if not cap.isOpened():
        raise SystemExit("Could not open the camera. Check your privacy settings (camera access).")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    return cap


def _label(img, text: str, y: int, color=(255, 255, 255)) -> None:
    import cv2
    cv2.putText(img, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)


def camtest(cfg: dict) -> None:
    """Live preview: the area being checked and its score. Press q to quit."""
    import cv2
    s = cfg["smoke"]
    scorer = make_scorer(s)
    faces = FaceTracker()
    cap = _open_camera(s)
    print(f"Using the {scorer.name}. Press q in the window to quit.")
    evidence = Evidence(s["evidence_half_life"])
    trigger = float(s["trigger_score"])
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame, light = fix_light(frame, s.get("light_fix", "auto"))
        face, _ = faces.update(frame)
        crop, (x0, y0, x1, y1) = roi(frame, face)
        score = scorer.score(crop)
        glow = face is not None and glowing_tip(crop)
        evidence.add_score(score, scorer.threshold, glow)
        ev = evidence.value()
        hit = score >= scorer.threshold
        color = (0, 0, 255) if hit else (0, 165, 255) if score >= 0.6 * scorer.threshold else (0, 200, 0)
        out = frame.copy()
        cv2.rectangle(out, (x0, y0), (x1, y1), color, 2)
        _label(out, f"{scorer.name}", 32)
        _label(out, f"this frame: {score:.2f}  (hit at {scorer.threshold:.2f})" + ("  + GLOW" if glow else ""), 64, color)
        # evidence bar: fills up while you smoke, popup when full
        bar_w = 300
        filled = int(bar_w * min(1.0, ev / trigger))
        cv2.rectangle(out, (12, 84), (12 + bar_w, 104), (60, 60, 60), -1)
        cv2.rectangle(out, (12, 84), (12 + filled, 104), (0, 0, 255) if ev >= trigger else (0, 165, 255), -1)
        _label(out, "WOULD ASK NOW" if ev >= trigger else f"evidence {ev:.1f} / {trigger:.0f}", 132,
               (0, 0, 255) if ev >= trigger else (255, 255, 255))
        if light:
            _label(out, f"color fix: {light}", 164, (255, 228, 94))
        cv2.imshow("LazyTrack camtest (q to quit)", out)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cap.release()
    cv2.destroyAllWindows()


def collect(cfg: dict, label: str, seconds: int) -> None:
    """Records training crops: `lt collect smoking` / `lt collect not_smoking`."""
    import cv2
    if label not in CLASSES:
        raise SystemExit(f"label must be one of: {', '.join(CLASSES)}")
    s = cfg["smoke"]
    faces = FaceTracker()
    cap = _open_camera(s)
    tips = {
        "smoking": "Smoke / hold the cigarette or joint like you normally do. Move around, turn your head.",
        "not_smoking": "Act normal: type, drink, eat, touch your face, phone at your ear, pen in your mouth...",
    }
    print(tips[label])
    start = time.time() + 3  # 3 s countdown
    saved = 0
    last_save = 0.0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame, _ = fix_light(frame, s.get("light_fix", "auto"))   # train on what the detector will see
        now = time.time()
        face, _ = faces.update(frame)
        crop, (x0, y0, x1, y1) = roi(frame, face)
        out = frame.copy()
        if now < start:
            _label(out, f"{label}: starting in {int(start - now) + 1}...", 40, (0, 200, 255))
        else:
            left = seconds - (now - start)
            if left <= 0:
                break
            if now - last_save > 0.3:
                save_crop(crop, label)
                saved += 1
                last_save = now
            cv2.rectangle(out, (x0, y0), (x1, y1), (0, 0, 255), 2)
            _label(out, f"REC {label}: {saved} photos, {int(left)}s left", 40, (0, 0, 255))
        cv2.imshow("LazyTrack collect (q to stop)", out)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cap.release()
    cv2.destroyAllWindows()
    counts = {c: len(list((DATASET / c).glob("*.jpg"))) for c in CLASSES}
    print(f"Saved {saved}. Dataset now: {counts}")
    if all(v >= 50 for v in counts.values()):
        print("Enough to train: ./lt train")


def train(cfg: dict, epochs: int) -> None:
    """Trains your personal smoking classifier from ~/.lazytrack/dataset."""
    files = {c: sorted((DATASET / c).glob("*.jpg")) for c in CLASSES}
    counts = {c: len(v) for c, v in files.items()}
    print(f"Dataset: {counts}")
    if min(counts.values()) < 30:
        raise SystemExit("Need at least 30 photos of each. Record more with:\n"
                         "  ./lt collect smoking\n  ./lt collect not_smoking")

    split = config.HOME / "training" / "split"
    shutil.rmtree(split, ignore_errors=True)
    rng = random.Random(42)
    for c, paths in files.items():
        paths = paths[:]
        rng.shuffle(paths)
        n_val = max(5, len(paths) // 5)
        for part, chunk in (("val", paths[:n_val]), ("train", paths[n_val:])):
            d = split / part / c
            d.mkdir(parents=True, exist_ok=True)
            for p in chunk:
                (d / p.name).symlink_to(p)

    from ultralytics import YOLO
    device = pick_device()
    base = None
    for name in ("yolo11n-cls.pt", "yolov8n-cls.pt"):
        try:
            base = YOLO(name)
            break
        except Exception:
            continue
    if base is None:
        raise SystemExit("Could not download a base classification model.")
    print(f"Training on {device} for {epochs} epochs (a few minutes)...")
    try:
        base.train(data=str(split), epochs=epochs, imgsz=224, device=device, batch=32,
                   project=str(config.HOME / "training"), name="run", exist_ok=True, plots=False, verbose=False)
    except Exception as e:
        if device == "cpu":
            raise
        print(f"GPU training failed ({e}), retrying on CPU...")
        base = YOLO("yolo11n-cls.pt")
        base.train(data=str(split), epochs=epochs, imgsz=224, device="cpu", batch=32,
                   project=str(config.HOME / "training"), name="run", exist_ok=True, plots=False, verbose=False)

    best = Path(base.trainer.best)
    shutil.copy(best, PERSONAL_MODEL)
    acc = None
    try:
        acc = YOLO(str(PERSONAL_MODEL)).val(data=str(split), imgsz=224, device=device,
                                            verbose=False, plots=False).top1
    except Exception:
        pass
    print(f"\n✅ Personal model saved: {PERSONAL_MODEL}")
    if acc is not None:
        print(f"   Accuracy on photos it didn't train on: {acc * 100:.0f}%")
    print("   LazyTrack picks it up automatically. Try it: ./lt camtest")
