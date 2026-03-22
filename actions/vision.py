# actions/vision.py
# JARVIS — Vision Primitive
#
# Captures the full screen (or webcam) and asks Gemini Vision a specific question.
# Returns a text answer. This is the STANDALONE vision tool — distinct from
# screen_processor.py which runs a persistent Gemini Live session for voice.
#
# All vision calls use the new google.genai SDK.
# Screenshots are sent as JPEG at reduced quality to minimize API payload.

import base64
import io
import json
import sys
import threading
from pathlib import Path

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False

IMG_MAX_W = 1280
IMG_MAX_H = 720
JPEG_Q    = 60  # Reduced quality for smaller payload

_camera_lock = threading.Lock()


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _to_jpeg(img_bytes: bytes) -> bytes:
    """Convert any image bytes to JPEG at reduced quality and size."""
    if not _PIL:
        return img_bytes
    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail([IMG_MAX_W, IMG_MAX_H], PIL.Image.Resampling.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_Q, optimize=False)
        return buf.getvalue()
    except Exception:
        return img_bytes


def _capture_screen() -> bytes:
    """Captures the primary monitor as JPEG bytes."""
    if not _MSS:
        raise RuntimeError("mss not installed. Run: pip install mss")
    with mss.mss() as sct:
        shot      = sct.grab(sct.monitors[1])
        png_bytes = mss.tools.to_png(shot.rgb, shot.size)
    return _to_jpeg(png_bytes)


def _get_camera_index() -> int:
    """Reads saved camera index from config, or auto-detects."""
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if "camera_index" in cfg:
            return int(cfg["camera_index"])
    except Exception:
        pass

    best = 0
    if _CV2:
        for idx in range(6):
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap.release()
                continue
            for _ in range(5):
                cap.read()
            ret, frame = cap.read()
            cap.release()
            if ret and frame is not None and frame.mean() > 5:
                best = idx
                print(f"[Vision] ✅ Camera at index {idx}")
                break

    try:
        cfg = {}
        if API_CONFIG_PATH.exists():
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        cfg["camera_index"] = best
        with open(API_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
    except Exception:
        pass

    return best


def _capture_camera() -> bytes:
    """Captures a frame from the webcam as JPEG bytes."""
    if not _CV2:
        raise RuntimeError("opencv-python not installed. Run: pip install opencv-python")

    with _camera_lock:
        idx = _get_camera_index()
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open camera at index {idx}")

        for _ in range(10):
            cap.read()

        ret, frame = cap.read()
        cap.release()

    if not ret or frame is None:
        raise RuntimeError("Could not capture camera frame.")

    if _PIL:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail([IMG_MAX_W, IMG_MAX_H], PIL.Image.Resampling.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_Q)
        return buf.getvalue()

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
    return buf.tobytes()


def _ask_gemini_vision(image_bytes: bytes, question: str) -> str:
    """
    Sends image + question to Gemini Vision using new google.genai SDK.
    Returns Gemini's text answer.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=_get_api_key())

    image_part = types.Part.from_bytes(
        data=image_bytes,
        mime_type="image/jpeg"
    )
    text_part = types.Part.from_text(text=question)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=types.Content(
            role="user",
            parts=[image_part, text_part]
        )
    )

    return response.text.strip() if response.text else "Could not interpret the image."


def vision(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Vision primitive — captures screen or camera and answers a question.

    parameters:
        text   : (required) The specific question to answer about the image.
                 Ask specific, answerable questions. Not "describe the page"
                 but "list all unread emails" or "what temperature is shown?"
        angle  : "screen" (default) or "camera"

    Returns the answer as a string.

    Examples:
        vision({"text": "what is the current temperature shown?", "angle": "screen"})
        vision({"text": "list all unread emails with sender and subject"})
        vision({"text": "is there a pending assignment from Mr Omar? Say NOT FOUND if none."})
    """
    params    = parameters or {}
    question  = params.get("text", "").strip() or params.get("question", "").strip()
    angle     = params.get("angle", "screen").lower().strip()

    if not question:
        return "Please provide a question about what to look for, sir."

    print(f"[Vision] 👁️ angle={angle!r} question={question[:60]!r}")

    if player:
        player.write_log(f"[vision] capturing {angle}...")

    try:
        if angle == "camera":
            image_bytes = _capture_camera()
            print(f"[Vision] 📷 Camera captured ({len(image_bytes)} bytes)")
        else:
            image_bytes = _capture_screen()
            print(f"[Vision] 🖥️ Screen captured ({len(image_bytes)} bytes)")
    except Exception as e:
        return f"Could not capture {angle}: {e}"

    try:
        answer = _ask_gemini_vision(image_bytes, question)
        print(f"[Vision] ✅ Answer: {answer[:100]}")
        if player:
            player.write_log(f"[vision] {answer[:80]}")
        return answer
    except Exception as e:
        print(f"[Vision] ❌ Gemini failed: {e}")
        return f"Vision analysis failed: {e}"
