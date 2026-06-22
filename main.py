import hmac
import json
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

if importlib.util.find_spec("static_ffmpeg"):
    import static_ffmpeg
else:
    static_ffmpeg = None

if importlib.util.find_spec("firebase_admin"):
    import firebase_admin
    from firebase_admin import auth as firebase_auth
    from firebase_admin import credentials
else:

    class FirebaseAdminFallback:
        _apps: dict[str, object] = {}

        @staticmethod
        def initialize_app(cred: object) -> None:
            FirebaseAdminFallback._apps["default"] = cred

    class FirebaseAuthFallback:
        @staticmethod
        def verify_id_token(id_token: str) -> dict[str, Any]:
            raise RuntimeError("firebase-admin is not installed.")

    class FirebaseCredentialsFallback:
        @staticmethod
        def Certificate(data: dict[str, str]) -> dict[str, str]:
            return data

    firebase_admin = FirebaseAdminFallback()
    firebase_auth = FirebaseAuthFallback()
    credentials = FirebaseCredentialsFallback()

# Auto-configure ffmpeg. Prefer a system ffmpeg when available, and do not let
# static-ffmpeg download/network failures prevent the API from starting.
if shutil.which("ffmpeg"):
    print("[OK] ffmpeg ready")
else:
    try:
        if static_ffmpeg:
            static_ffmpeg.add_paths()
            print("[OK] ffmpeg ready")
        else:
            print(
                "[WARN] static-ffmpeg is not installed and system ffmpeg was not found"
            )
    except Exception as exc:
        print(f"[WARN] ffmpeg auto-configuration failed: {exc}")

app = FastAPI(title="Stemify API", version="1.5.0")
stripe_logger = logging.getLogger("stemify.stripe")
audio_logger = logging.getLogger("stemify.audio")
logger = stripe_logger


def _split_csv_env(name: str, default: str = "") -> list[str]:
    return [
        item.strip() for item in os.getenv(name, default).split(",") if item.strip()
    ]


# In production, set ALLOWED_ORIGINS to your frontend URL(s), for example:
# https://your-frontend.vercel.app,http://localhost:3000
ALLOWED_ORIGINS = _split_csv_env("ALLOWED_ORIGINS", "*")
ALLOW_ALL_ORIGINS = "*" in ALLOWED_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if ALLOW_ALL_ORIGINS else ALLOWED_ORIGINS,
    # Browsers reject wildcard origins with credentials. The API does not use
    # cookies, so keep credentials disabled when all origins are allowed.
    allow_credentials=not ALLOW_ALL_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "outputs"))
JOBS_DIR = Path(os.getenv("JOBS_DIR", "jobs"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
DEMUCS_TIMEOUT_SECONDS = int(os.getenv("DEMUCS_TIMEOUT_SECONDS", "900"))
DEMUCS_MODEL = os.getenv("DEMUCS_MODEL", os.getenv("DEMUCS_MODEL_2_STEMS", "mdx_q"))
DEMUCS_SHIFTS = int(os.getenv("DEMUCS_SHIFTS", "0"))
DEMUCS_OVERLAP = float(os.getenv("DEMUCS_OVERLAP", "0.1"))
DEMUCS_SEGMENT_SECONDS = int(float(os.getenv("DEMUCS_SEGMENT_SECONDS", "8")))
DEMUCS_JOBS = int(os.getenv("DEMUCS_JOBS", "0"))
DEMUCS_DEVICE = os.getenv("DEMUCS_DEVICE", "")
DEMUCS_HIGH_MODEL = os.getenv("DEMUCS_HIGH_MODEL", "htdemucs")
DEMUCS_HIGH_SHIFTS = int(os.getenv("DEMUCS_HIGH_SHIFTS", "1"))
DEMUCS_HIGH_OVERLAP = float(os.getenv("DEMUCS_HIGH_OVERLAP", "0.25"))
DEMUCS_HIGH_SEGMENT_SECONDS = int(float(os.getenv("DEMUCS_HIGH_SEGMENT_SECONDS", "0")))
DEMUCS_HIGH_JOBS = int(os.getenv("DEMUCS_HIGH_JOBS", str(DEMUCS_JOBS)))
DEMUCS_HIGH_DEVICE = os.getenv("DEMUCS_HIGH_DEVICE", DEMUCS_DEVICE)
DEMUCS_CPU_THREADS = int(os.getenv("DEMUCS_CPU_THREADS", "2"))
DEMUCS_CONCURRENCY = max(1, int(os.getenv("DEMUCS_CONCURRENCY", "1")))
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}
DEFAULT_OUTPUT_FORMAT = "mp3"
DEFAULT_QUALITY = "fast"

OUTPUT_FORMATS = {
    "wav": {
        "extension": ".wav",
        "media_type": "audio/wav",
        "ffmpeg_args": ["-acodec", "pcm_s16le"],
    },
    "mp3": {
        "extension": ".mp3",
        "media_type": "audio/mpeg",
        "ffmpeg_args": ["-codec:a", "libmp3lame", "-b:a", "192k"],
    },
    "flac": {
        "extension": ".flac",
        "media_type": "audio/flac",
        "ffmpeg_args": ["-codec:a", "flac"],
    },
    "ogg": {
        "extension": ".ogg",
        "media_type": "audio/ogg",
        "ffmpeg_args": ["-codec:a", "libvorbis", "-q:a", "5"],
    },
    "m4a": {
        "extension": ".m4a",
        "media_type": "audio/mp4",
        "ffmpeg_args": ["-codec:a", "aac", "-b:a", "192k"],
    },
}
PREVIEW_DURATION_SECONDS = int(os.getenv("PREVIEW_DURATION_SECONDS", "15"))
MIN_STEM_FILE_SIZE_BYTES = int(os.getenv("MIN_STEM_FILE_SIZE_BYTES", "1024"))
SILENCE_RMS_DB_THRESHOLD = float(os.getenv("SILENCE_RMS_DB_THRESHOLD", "-90"))
PRICE_PER_SONG_CENTS = int(
    os.getenv("PRICE_PER_SONG_CENTS", os.getenv("PRICE_PER_STEM_CENTS", "300"))
)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
PAYMENTS_ENABLED = (
    os.getenv("PAYMENTS_ENABLED", "true" if STRIPE_SECRET_KEY else "false").lower()
    == "true"
)
PAYMENT_CURRENCY = os.getenv("PAYMENT_CURRENCY", "aud").lower()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
FRONTEND_RETURN_PATH = os.getenv("FRONTEND_RETURN_PATH", "/").strip() or "/"
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
FIREBASE_CLIENT_EMAIL = os.getenv("FIREBASE_CLIENT_EMAIL", "")
FIREBASE_PRIVATE_KEY = os.getenv("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")

STEM_MODELS = {
    2: DEMUCS_MODEL,
}
QUALITY_SETTINGS = {
    "fast": {
        "models": STEM_MODELS,
        "shifts": DEMUCS_SHIFTS,
        "overlap": DEMUCS_OVERLAP,
        "segment_seconds": DEMUCS_SEGMENT_SECONDS,
        "jobs": DEMUCS_JOBS,
        "device": DEMUCS_DEVICE,
    },
    "high": {
        "models": {2: DEMUCS_HIGH_MODEL},
        "shifts": DEMUCS_HIGH_SHIFTS,
        "overlap": DEMUCS_HIGH_OVERLAP,
        "segment_seconds": DEMUCS_HIGH_SEGMENT_SECONDS,
        "jobs": DEMUCS_HIGH_JOBS,
        "device": DEMUCS_HIGH_DEVICE,
    },
}
DEMUCS_SEMAPHORE = threading.Semaphore(DEMUCS_CONCURRENCY)

# In-memory job store. This is intentionally simple for a single-instance API;
# use Redis/Postgres if you scale to multiple workers or need durable history.
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
SAFE_DOWNLOAD_RE = re.compile(r"^[A-Za-z0-9_. -]+\.(wav|mp3|flac|ogg|m4a)$")

# Simple in-memory user/job stores. Firebase verifies identity; these dicts only
# keep lightweight per-user usage counts for the current single-instance API.
USERS: dict[str, dict[str, Any]] = {}
AUTH_LOCK = threading.Lock()
PAYMENTS: dict[str, dict[str, Any]] = {}
USER_PAYMENT_JOBS: dict[str, set[str]] = {}
PAYMENTS_LOCK = threading.Lock()
PAID_ASSET_EVENTS: dict[str, threading.Event] = {}
PAID_ASSET_LOCK = threading.Lock()


def job_dir_for(job_id: str) -> Path:
    return JOBS_DIR / job_id


def job_json_path(job_id: str) -> Path:
    return job_dir_for(job_id) / "job.json"


def write_job_json(job: dict[str, Any]) -> None:
    job_id = job["job_id"]
    path = job_json_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(job, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def load_job_json(job_id: str) -> dict[str, Any]:
    path = job_json_path(job_id)
    if not path.is_file():
        raise HTTPException(404, "Job not found")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, "Job metadata is corrupted.") from exc


def find_job_by_checkout_session(checkout_session_id: str) -> dict[str, Any] | None:
    for path in JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("checkout_session_id") == checkout_session_id:
            return job
    return None


class UserProfile(BaseModel):
    uid: str
    email: str
    name: str | None = None
    provider: str


class CheckoutRequest(BaseModel):
    job_id: str
    item_type: str = Field(default="song", pattern="^(song|zip|stem)$")
    filename: str | None = Field(default=None, max_length=120)
    return_path: str | None = Field(default=None, max_length=300)


class PaymentStatusRequest(BaseModel):
    checkout_session_id: str
    job_id: str | None = None


def initialize_firebase() -> None:
    if firebase_admin._apps:
        return
    if not (FIREBASE_PROJECT_ID and FIREBASE_CLIENT_EMAIL and FIREBASE_PRIVATE_KEY):
        return

    cred = credentials.Certificate(
        {
            "type": "service_account",
            "project_id": FIREBASE_PROJECT_ID,
            "private_key": FIREBASE_PRIVATE_KEY,
            "client_email": FIREBASE_CLIENT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    firebase_admin.initialize_app(cred)


initialize_firebase()


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": user["uid"],
        "email": user["email"],
        "name": user.get("name"),
        "provider": user["provider"],
    }


def usage_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        **public_user(user),
        "stem_count": user["stem_count"],
        "jobs_created": user["jobs_created"],
    }


def current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authentication required.")
    if not firebase_admin._apps:
        raise HTTPException(503, "Firebase authentication is not configured.")

    id_token = authorization.removeprefix("Bearer ").strip()
    try:
        decoded = firebase_auth.verify_id_token(id_token)
    except Exception as exc:
        raise HTTPException(401, "Invalid or expired Firebase token.") from exc

    uid = decoded.get("uid")
    email = decoded.get("email")
    if not uid or not email:
        raise HTTPException(401, "Firebase token is missing required user details.")

    provider = (decoded.get("firebase") or {}).get("sign_in_provider", "firebase")
    with AUTH_LOCK:
        user = USERS.setdefault(
            uid,
            {
                "uid": uid,
                "email": email,
                "name": decoded.get("name"),
                "provider": provider,
                "stem_count": 0,
                "jobs_created": 0,
            },
        )
        user.update(
            {
                "email": email,
                "name": decoded.get("name"),
                "provider": provider,
            }
        )
        return user


def update_job(job_id: str, **values: Any) -> None:
    job_for_disk = None
    with JOBS_LOCK:
        if job_id in JOBS:
            values.setdefault("updated_at", time.time())
            JOBS[job_id].update(values)
            job_for_disk = JOBS[job_id].copy()
    if job_for_disk is None and job_json_path(job_id).is_file():
        job_for_disk = load_job_json(job_id)
        values.setdefault("updated_at", time.time())
        job_for_disk.update(values)
    if job_for_disk is not None:
        write_job_json(job_for_disk)


def serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    public_job = job.copy()
    started_at = public_job.get("started_at")
    if started_at:
        public_job["elapsed_seconds"] = round(time.time() - float(started_at), 1)
    public_job["timeout_seconds"] = DEMUCS_TIMEOUT_SECONDS
    return public_job


def get_authorized_job(job_id: str, user: dict[str, Any]) -> dict[str, Any]:
    if job_json_path(job_id).is_file():
        job = load_job_json(job_id)
    else:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                raise HTTPException(404, "Job not found")
            job = job.copy()
    if job.get("user_uid") != user["uid"]:
        raise HTTPException(404, "Job not found")
    return serialize_job(job)


def get_public_job(job_id: str) -> dict[str, Any]:
    if job_json_path(job_id).is_file():
        return serialize_job(load_job_json(job_id))
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return serialize_job(job)


def payment_key(job_id: str, item_type: str, filename: str | None = None) -> str:
    return f"{job_id}:song"


def is_payment_required() -> bool:
    return PAYMENTS_ENABLED


def stripe_key_mode() -> str:
    if STRIPE_SECRET_KEY.startswith("sk_live_"):
        return "live"
    if STRIPE_SECRET_KEY.startswith("sk_test_"):
        return "test"
    return "unknown" if STRIPE_SECRET_KEY else "missing"


def validate_checkout_configuration() -> None:
    if not PAYMENTS_ENABLED:
        logger.warning("Checkout rejected because PAYMENTS_ENABLED is false.")
        raise HTTPException(503, "Payments are not enabled on this server.")
    if not STRIPE_SECRET_KEY:
        logger.error("Checkout rejected because STRIPE_SECRET_KEY is missing.")
        raise HTTPException(503, "Stripe is not configured on this server.")
    if not STRIPE_SECRET_KEY.startswith(("sk_live_", "sk_test_")):
        logger.error(
            "Checkout rejected because STRIPE_SECRET_KEY has an invalid prefix."
        )
        raise HTTPException(503, "Stripe secret key is invalid on this server.")
    parsed_frontend = urlparse(FRONTEND_URL)
    if parsed_frontend.scheme not in {"http", "https"} or not parsed_frontend.netloc:
        logger.error(
            "Checkout rejected because FRONTEND_URL is not absolute: %s", FRONTEND_URL
        )
        raise HTTPException(503, "Checkout redirect URL is not configured correctly.")
    if PRICE_PER_SONG_CENTS < 50:
        logger.error(
            "Checkout rejected because PRICE_PER_SONG_CENTS is too low: %s",
            PRICE_PER_SONG_CENTS,
        )
        raise HTTPException(503, "Checkout price is not configured correctly.")
    if not re.fullmatch(r"[a-z]{3}", PAYMENT_CURRENCY):
        logger.error(
            "Checkout rejected because PAYMENT_CURRENCY is invalid: %s",
            PAYMENT_CURRENCY,
        )
        raise HTTPException(503, "Checkout currency is not configured correctly.")


def is_download_paid(
    job_id: str, user: dict[str, Any], item_type: str, filename: str | None = None
) -> bool:
    if not is_payment_required():
        return True

    key = payment_key(job_id, item_type, filename)
    with PAYMENTS_LOCK:
        return any(
            payment
            for payment in PAYMENTS.values()
            if payment.get("user_uid") == user["uid"]
            and payment.get("status") == "paid"
            and (
                payment.get("stripe_event_confirmed") is True
                or payment.get("stripe_api_verified") is True
            )
            and payment.get("payment_key") == key
        )


def enforce_paid_download(
    job: dict[str, Any],
    user: dict[str, Any],
    item_type: str,
    filename: str | None = None,
) -> None:
    if (
        job.get("payment_status") == "paid"
        and job.get("full_processing_status") == "ready"
    ):
        return
    if is_download_paid(job["job_id"], user, item_type, filename):
        return

    raise HTTPException(
        402,
        {
            "message": "Payment required before download.",
            "price_per_song_cents": PRICE_PER_SONG_CENTS,
            "currency": PAYMENT_CURRENCY,
            "checkout_endpoint": "/api/create-checkout-session",
        },
    )


def enforce_job_done(job: dict[str, Any]) -> None:
    if job.get("status") == "done":
        return

    raise HTTPException(
        409,
        {
            "message": "Stems are not ready yet.",
            "status": job.get("status"),
            "status_detail": job.get("status_detail"),
            "elapsed_seconds": job.get("elapsed_seconds"),
            "timeout_seconds": job.get("timeout_seconds"),
        },
    )


def enforce_public_job_done(job: dict[str, Any]) -> None:
    if job.get("status") == "done":
        return

    raise HTTPException(
        409,
        {
            "message": "Preview stems are not ready yet.",
            "status": job.get("status"),
            "status_detail": job.get("status_detail"),
            "elapsed_seconds": job.get("elapsed_seconds"),
            "timeout_seconds": job.get("timeout_seconds"),
        },
    )


def checkout_amount_for_job(job: dict[str, Any], item_type: str) -> tuple[int, str]:
    return PRICE_PER_SONG_CENTS, "Stemify full song download"


def stripe_request(
    method: str, path: str, data: dict[str, str] | None = None
) -> dict[str, Any]:
    if not STRIPE_SECRET_KEY:
        logger.error("Stripe request blocked because STRIPE_SECRET_KEY is missing.")
        raise HTTPException(503, "Stripe is not configured.")

    logger.info(
        "Creating Stripe request method=%s path=%s key_mode=%s",
        method,
        path,
        stripe_key_mode(),
    )
    encoded_data = (
        urllib.parse.urlencode(data or {}).encode("utf-8") if data is not None else None
    )
    request = urllib.request.Request(
        f"https://api.stripe.com/v1{path}",
        data=encoded_data,
        method=method,
        headers={
            "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json_loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.exception(
            "Stripe API HTTP error status=%s response=%s", exc.code, detail
        )
        try:
            stripe_error = json.loads(detail).get("error", {})
            message = stripe_error.get("message") or detail
            code = stripe_error.get("code")
        except json.JSONDecodeError:
            message = detail
            code = None
        public_detail = {"message": message}
        if code:
            public_detail["code"] = code
        raise HTTPException(exc.code, public_detail) from exc
    except urllib.error.URLError as exc:
        logger.exception("Stripe API connection failed: %s", exc.reason)
        raise HTTPException(
            503, {"message": f"Stripe request failed: {exc.reason}"}
        ) from exc


def json_loads(raw: bytes) -> dict[str, Any]:
    return json.loads(raw.decode("utf-8"))


def normalize_frontend_return_path(return_path: str | None = None) -> str:
    path = (return_path or FRONTEND_RETURN_PATH or "/").strip() or "/"
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        raise HTTPException(400, "return_path must be a relative frontend path.")
    if not path.startswith(("/", "#", "?")):
        path = f"/{path}"
    return path


def frontend_return_url(
    payment_status: str, job_id: str, return_path: str | None = None
) -> str:
    parsed_frontend = urlparse(FRONTEND_URL)
    parsed_return = urlparse(normalize_frontend_return_path(return_path))

    query_params = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(
            parsed_return.query, keep_blank_values=True
        )
        if key not in {"payment", "session_id", "job_id"}
    ]
    query_params.append(("payment", payment_status))
    if payment_status == "success":
        query_params.append(("session_id", "{CHECKOUT_SESSION_ID}"))
    query_params.append(("job_id", job_id))

    return urllib.parse.urlunparse(
        (
            parsed_frontend.scheme,
            parsed_frontend.netloc,
            parsed_return.path or "/",
            "",
            urllib.parse.urlencode(query_params, safe="{}"),
            parsed_return.fragment,
        )
    )


def checkout_success_url(job_id: str, return_path: str | None = None) -> str:
    return frontend_return_url("success", job_id, return_path)


def checkout_cancel_url(job_id: str, return_path: str | None = None) -> str:
    return frontend_return_url("cancelled", job_id, return_path)


def retrieve_checkout_session(checkout_session_id: str) -> dict[str, Any]:
    quoted_session_id = urllib.parse.quote(checkout_session_id, safe="")
    return stripe_request("GET", f"/checkout/sessions/{quoted_session_id}")


def create_preview_input(job_id: str, input_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to create the 15-second preview.")

    preview_path = UPLOAD_DIR / f"{job_id}_preview.wav"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-t",
        str(PREVIEW_DURATION_SECONDS),
        "-vn",
        "-acodec",
        "pcm_s16le",
        str(preview_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        err = (proc.stderr or "") + (proc.stdout or "")
        raise RuntimeError(f"Preview trimming failed: {err[-400:]}")
    audio_logger.info(
        "Created preview input job_id=%s input_path=%s preview_path=%s preview_size_bytes=%s",
        job_id,
        input_path,
        preview_path,
        preview_path.stat().st_size if preview_path.exists() else 0,
    )
    return preview_path


def prepare_output_files(
    job_id: str, wavs: list[Path], output_format: str, output_scope: str = "preview"
) -> list[Path]:
    if output_format == "wav":
        return wavs

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            f"FFmpeg is required to export {output_format.upper()} stems."
        )

    format_config = OUTPUT_FORMATS[output_format]
    base_dir = (
        job_dir_for(job_id) if job_dir_for(job_id).exists() else OUTPUT_DIR / job_id
    )
    converted_dir = base_dir / output_scope / output_format
    converted_dir.mkdir(parents=True, exist_ok=True)
    converted_files: list[Path] = []

    for wav in wavs:
        converted = converted_dir / f"{wav.stem}{format_config['extension']}"
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(wav),
            "-vn",
            *format_config["ffmpeg_args"],
            str(converted),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            err = (proc.stderr or "") + (proc.stdout or "")
            raise RuntimeError(f"{output_format.upper()} export failed: {err[-400:]}")
        validate_audio_file(converted, f"{output_format.upper()} {output_scope} stem")
        audio_logger.info(
            "Generated encoded %s stem job_id=%s path=%s size_bytes=%s",
            output_scope,
            job_id,
            converted,
            converted.stat().st_size,
        )
        converted_files.append(converted)

    return converted_files


def convert_preview_wavs_to_mp3(
    job_id: str, wavs: list[Path]
) -> tuple[list[Path], dict[str, dict[str, float | int | str]]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to export MP3 preview stems.")

    base_dir = (
        job_dir_for(job_id) if job_dir_for(job_id).exists() else OUTPUT_DIR / job_id
    )
    converted_dir = base_dir / "preview" / "mp3"
    converted_dir.mkdir(parents=True, exist_ok=True)
    converted_files: list[Path] = []
    preview_metadata: dict[str, dict[str, float | int | str]] = {}

    for wav in wavs:
        converted = converted_dir / f"{wav.stem}.mp3"
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(wav),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(converted),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        size = converted.stat().st_size if converted.exists() else 0
        duration = 0.0
        validation_error = None
        try:
            if proc.returncode != 0:
                err = (proc.stderr or "") + (proc.stdout or "")
                raise RuntimeError(f"MP3 preview export failed: {err[-400:]}")
            if not converted.is_file():
                raise RuntimeError(f"MP3 preview was not created: {converted}")
            if size <= 0:
                raise RuntimeError(f"MP3 preview is empty: {converted}")
            duration = audio_duration_seconds(converted)
            if duration <= 0:
                raise RuntimeError(f"MP3 preview has no playable duration: {converted}")
        except RuntimeError as exc:
            validation_error = str(exc)

        public_name = public_stem_name(converted)
        preview_metadata[public_name] = {
            "path": str(converted),
            "size_bytes": size,
            "duration_seconds": duration,
            "ffmpeg_return_code": proc.returncode,
        }
        audio_logger.info(
            "Preview MP3 export job_id=%s path=%s size_bytes=%s duration_seconds=%.3f ffmpeg_return_code=%s",
            job_id,
            converted,
            size,
            duration,
            proc.returncode,
        )
        if validation_error:
            raise RuntimeError(validation_error)
        converted_files.append(converted)

    return converted_files, preview_metadata


def normalize_quality(quality: str | None) -> str:
    normalized = (quality or DEFAULT_QUALITY).strip().lower()
    if normalized not in QUALITY_SETTINGS:
        raise HTTPException(
            400,
            f"Unsupported quality: {normalized or 'none'}. Choose one of: {', '.join(sorted(QUALITY_SETTINGS))}.",
        )
    return normalized


def demucs_model_for_quality(stems: int, quality: str = "fast") -> str:
    return QUALITY_SETTINGS[normalize_quality(quality)]["models"][stems]


def demucs_output_dir(
    job_dir: Path, input_audio_path: Path, stems: int, quality: str = "fast"
) -> Path:
    return job_dir / demucs_model_for_quality(stems, quality) / input_audio_path.stem


def expected_demucs_stem_files(
    job_dir: Path, input_audio_path: Path, stems: int, quality: str = "fast"
) -> list[Path]:
    output_dir = demucs_output_dir(job_dir, input_audio_path, stems, quality)
    if stems == 2:
        return [output_dir / "vocals.wav", output_dir / "no_vocals.wav"]
    return sorted(output_dir.glob("*.wav"))


def audio_duration_seconds(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("FFprobe is required to validate generated audio.")

    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "") + (proc.stdout or "")
        raise RuntimeError(
            f"Could not read audio duration for {path.name}: {err[-300:]}"
        )
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid audio duration for {path.name}: {proc.stdout!r}"
        ) from exc


def audio_rms_db(path: Path) -> float | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to validate generated audio.")

    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "") + (proc.stdout or "")
        raise RuntimeError(
            f"Could not analyze audio levels for {path.name}: {err[-300:]}"
        )

    output = (proc.stderr or "") + (proc.stdout or "")
    match = re.search(r"mean_volume:\s*(-?inf|-?\d+(?:\.\d+)?) dB", output)
    if not match:
        raise RuntimeError(f"Could not find audio level metadata for {path.name}.")
    if match.group(1) == "-inf":
        return None
    return float(match.group(1))


def validate_audio_file(path: Path, label: str) -> dict[str, float | int | None]:
    if not path.is_file():
        raise RuntimeError(f"{label} was not created: {path}")

    size = path.stat().st_size
    if size < MIN_STEM_FILE_SIZE_BYTES:
        raise RuntimeError(
            f"{label} is too small to be valid audio ({size} bytes): {path}"
        )

    duration = audio_duration_seconds(path)
    if duration <= 0:
        raise RuntimeError(f"{label} has no playable duration: {path}")

    rms_db = audio_rms_db(path)
    if rms_db is None or rms_db <= SILENCE_RMS_DB_THRESHOLD:
        level = "-inf" if rms_db is None else f"{rms_db:.1f} dB"
        raise RuntimeError(
            f"{label} appears to be silent (mean volume {level}): {path}"
        )

    audio_logger.info(
        "Validated %s path=%s size_bytes=%s duration_seconds=%.3f mean_volume_db=%.1f",
        label,
        path,
        size,
        duration,
        rms_db,
    )
    return {"size_bytes": size, "duration_seconds": duration, "mean_volume_db": rms_db}


def output_files_by_public_name(paths: list[Path]) -> dict[str, Path]:
    return {public_stem_name(path): path for path in paths}


def path_map_for_job(paths_by_name: dict[str, Path]) -> dict[str, str]:
    return {name: str(path) for name, path in paths_by_name.items()}


def create_zip(zip_path: Path, stem_files: list[Path]) -> Path:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for stem_file in stem_files:
            zf.write(stem_file, arcname=stem_file.name)
    return zip_path


def get_recorded_path(job: dict[str, Any], key: str, public_name: str) -> Path | None:
    recorded = (job.get(key) or {}).get(public_name)
    return Path(recorded) if recorded else None


def reject_preview_paid_path(path: Path) -> None:
    if "_preview" in path.stem or any(
        part.lower() == "preview" or part.lower().endswith("_preview")
        for part in path.parts
    ):
        raise HTTPException(500, "Paid download resolved to a preview file.")


def validate_paid_download_file(path: Path, job: dict[str, Any], label: str) -> None:
    if not path.is_file():
        raise HTTPException(404, f"{label} not found.")
    reject_preview_paid_path(path)
    source_duration = job.get("source_duration_seconds")
    if source_duration and source_duration > PREVIEW_DURATION_SECONDS:
        duration = audio_duration_seconds(path)
        if duration <= PREVIEW_DURATION_SECONDS:
            raise HTTPException(500, f"{label} resolved to a preview-length file.")


def validate_audio_files(paths: list[Path], label: str) -> None:
    for path in paths:
        validate_audio_file(path, f"{label} {path.name}")


def public_stem_name(path: Path) -> str:
    if path.stem == "no_vocals":
        return "instrumental"
    return path.stem


@app.get("/api")
async def root():
    return {
        "status": "ok",
        "message": "Stemify API is running",
        "version": app.version,
        "allowed_stems": sorted(STEM_MODELS),
        "max_file_size_mb": MAX_FILE_SIZE_MB,
        "preview_duration_seconds": PREVIEW_DURATION_SECONDS,
        "output_formats": sorted(OUTPUT_FORMATS),
        "default_output_format": DEFAULT_OUTPUT_FORMAT,
        "qualities": sorted(QUALITY_SETTINGS),
        "default_quality": DEFAULT_QUALITY,
        "performance": {
            "demucs_model": DEMUCS_MODEL,
            "demucs_shifts": DEMUCS_SHIFTS,
            "demucs_overlap": DEMUCS_OVERLAP,
            "demucs_segment_seconds": DEMUCS_SEGMENT_SECONDS,
            "demucs_jobs": DEMUCS_JOBS,
            "demucs_device": DEMUCS_DEVICE or "auto",
            "demucs_cpu_threads": DEMUCS_CPU_THREADS,
            "demucs_concurrency": DEMUCS_CONCURRENCY,
        },
        "payments": {
            "enabled": PAYMENTS_ENABLED,
            "price_per_song_cents": PRICE_PER_SONG_CENTS,
            "currency": PAYMENT_CURRENCY,
        },
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "upload_dir": str(UPLOAD_DIR),
        "output_dir": str(OUTPUT_DIR),
        "jobs_dir": str(JOBS_DIR),
        "output_formats": sorted(OUTPUT_FORMATS),
        "qualities": sorted(QUALITY_SETTINGS),
    }


@app.get("/api/me", response_model=UserProfile)
async def me(user: Annotated[dict[str, Any], Depends(current_user)]):
    return UserProfile(**public_user(user))


@app.get("/api/payments/config")
async def payments_config():
    return {
        "enabled": PAYMENTS_ENABLED,
        "provider": "stripe" if PAYMENTS_ENABLED else None,
        "checkout_endpoint": "/api/create-checkout-session",
        "price_per_song_cents": PRICE_PER_SONG_CENTS,
        "currency": PAYMENT_CURRENCY,
    }


@app.post("/api/payments/checkout")
@app.post("/api/create-checkout-session")
async def create_checkout_session(
    payload: CheckoutRequest,
    user: Annotated[dict[str, Any], Depends(current_user)],
):
    validate_checkout_configuration()

    job = get_authorized_job(payload.job_id, user)
    if job.get("preview_status") != "ready" and job.get("status") != "done":
        raise HTTPException(
            409,
            {
                "message": "Preview stems are not ready yet.",
                "preview_status": job.get("preview_status"),
                "status_detail": job.get("status_detail"),
            },
        )
    filename = None

    amount_cents, product_name = checkout_amount_for_job(job, payload.item_type)
    success_url = checkout_success_url(payload.job_id, payload.return_path)
    cancel_url = checkout_cancel_url(payload.job_id, payload.return_path)
    if amount_cents < 50:
        logger.error(
            "Checkout rejected for job_id=%s because amount_cents=%s",
            payload.job_id,
            amount_cents,
        )
        raise HTTPException(503, "Checkout price is not configured correctly.")
    logger.info(
        "Creating Stripe Checkout Session job_id=%s user_uid=%s item_type=%s amount_cents=%s currency=%s key_mode=%s success_url=%s cancel_url=%s",
        payload.job_id,
        user["uid"],
        payload.item_type,
        amount_cents,
        PAYMENT_CURRENCY,
        stripe_key_mode(),
        success_url,
        cancel_url,
    )
    session = stripe_request(
        "POST",
        "/checkout/sessions",
        {
            "mode": "payment",
            "customer_email": user["email"],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": PAYMENT_CURRENCY,
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][price_data][product_data][name]": product_name,
            "metadata[job_id]": payload.job_id,
            "metadata[item_type]": payload.item_type,
            "metadata[filename]": filename or "",
            "metadata[user_uid]": user["uid"],
            "metadata[user_email]": user["email"],
        },
    )

    with PAYMENTS_LOCK:
        PAYMENTS[session["id"]] = {
            "checkout_session_id": session["id"],
            "payment_key": payment_key(payload.job_id, payload.item_type, filename),
            "job_id": payload.job_id,
            "item_type": payload.item_type,
            "filename": filename,
            "user_uid": user["uid"],
            "user_email": user["email"],
            "status": "pending",
            "amount_cents": amount_cents,
            "currency": PAYMENT_CURRENCY,
        }
        USER_PAYMENT_JOBS.setdefault(user["uid"], set()).add(payload.job_id)
    update_job(
        payload.job_id,
        checkout_session_id=session["id"],
        payment_status="pending",
    )

    return {"checkout_session_id": session["id"], "checkout_url": session.get("url")}


def verify_stripe_webhook_signature(
    payload: bytes, signature_header: str | None
) -> None:
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "Stripe webhook signing secret is not configured.")
    if not signature_header:
        raise HTTPException(400, "Missing Stripe signature header.")

    parts = dict(
        item.split("=", 1) for item in signature_header.split(",") if "=" in item
    )
    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature:
        raise HTTPException(400, "Invalid Stripe signature header.")

    signed_payload = timestamp.encode("utf-8") + b"." + payload
    expected = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode("utf-8"), signed_payload, sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(400, "Invalid Stripe webhook signature.")


def mark_checkout_session_paid(
    session: dict[str, Any], verification_source: str = "webhook"
) -> dict[str, Any]:
    session_id = session.get("id")
    metadata = session.get("metadata") or {}
    job_id = metadata.get("job_id")
    item_type = metadata.get("item_type", "song")
    filename = metadata.get("filename") or None
    user_uid = metadata.get("user_uid")

    if not session_id or not job_id or not user_uid:
        raise HTTPException(
            400, "Stripe session metadata is missing required payment details."
        )

    with PAYMENTS_LOCK:
        payment = PAYMENTS.setdefault(
            session_id,
            {
                "checkout_session_id": session_id,
                "payment_key": payment_key(job_id, item_type, filename),
                "job_id": job_id,
                "item_type": item_type,
                "filename": filename,
                "user_uid": user_uid,
                "user_email": metadata.get("user_email")
                or session.get("customer_email"),
                "amount_cents": session.get("amount_total"),
                "currency": (session.get("currency") or PAYMENT_CURRENCY).lower(),
            },
        )
        payment.update(
            {
                "status": "paid",
                "stripe_payment_status": session.get("payment_status"),
                "stripe_verification_source": verification_source,
            }
        )
        if verification_source == "webhook":
            payment["stripe_event_confirmed"] = True
        if verification_source == "api":
            payment["stripe_api_verified"] = True
        USER_PAYMENT_JOBS.setdefault(user_uid, set()).add(job_id)
    paid_job_updates = {
        "paid": True,
        "payment_status": "paid",
        "paid_checkout_session_id": session_id,
        "checkout_session_id": session_id,
        "stripe_payment_intent": session.get("payment_intent"),
        "updated_at": time.time(),
    }
    should_start_paid_assets = False
    with JOBS_LOCK:
        if job_id in JOBS and JOBS[job_id].get("user_uid") == user_uid:
            JOBS[job_id].update(paid_job_updates)
            should_start_paid_assets = not JOBS[job_id].get("zip_url")
    if job_json_path(job_id).is_file():
        disk_job = load_job_json(job_id)
        if disk_job.get("user_uid") == user_uid:
            should_start_paid_assets = should_start_paid_assets or not disk_job.get(
                "zip_url"
            )
            update_job(job_id, **paid_job_updates)
    if should_start_paid_assets:
        start_paid_assets_generation(job_id)
    with PAYMENTS_LOCK:
        return payment.copy()


def start_paid_assets_generation(job_id: str) -> None:
    with PAID_ASSET_LOCK:
        if job_id in PAID_ASSET_EVENTS:
            return
        PAID_ASSET_EVENTS[job_id] = threading.Event()

    thread = threading.Thread(
        target=_paid_assets_worker,
        args=(job_id,),
        daemon=True,
    )
    thread.start()


def _paid_assets_worker(job_id: str) -> None:
    try:
        ensure_paid_assets_ready(job_id, owns_processing=True)
    except Exception as exc:
        audio_logger.exception(
            "Paid full stem generation failed job_id=%s error=%s", job_id, exc
        )


def ensure_paid_assets_ready(
    job_id: str, owns_processing: bool = False
) -> dict[str, Any]:
    job = get_public_job(job_id)
    if job.get("zip_url"):
        return job
    if job.get("payment_status") != "paid":
        raise HTTPException(402, "Payment required before full processing.")

    if owns_processing:
        with PAID_ASSET_LOCK:
            event = PAID_ASSET_EVENTS.get(job_id)
            if event is None:
                event = threading.Event()
                PAID_ASSET_EVENTS[job_id] = event
    else:
        with PAID_ASSET_LOCK:
            event = PAID_ASSET_EVENTS.get(job_id)
            if event is None:
                event = threading.Event()
                PAID_ASSET_EVENTS[job_id] = event
                owns_processing = True

    if not owns_processing:
        event.wait(timeout=DEMUCS_TIMEOUT_SECONDS + 120)
        job = get_public_job(job_id)
        if job.get("zip_url"):
            return job
        raise HTTPException(
            409,
            {
                "message": "Full stems are still processing after payment verification.",
                "status": job.get("status"),
                "status_detail": job.get("status_detail"),
                "full_processing_status": job.get("full_processing_status"),
            },
        )

    try:
        lock_path = job_dir_for(job_id) / "full_processing.lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            owns_lock = True
        except FileExistsError:
            owns_lock = False
        if not owns_lock:
            deadline = time.time() + DEMUCS_TIMEOUT_SECONDS
            while time.time() < deadline:
                time.sleep(1)
                latest_job = get_public_job(job_id)
                if latest_job.get(
                    "full_processing_status"
                ) == "ready" and latest_job.get("zip_path"):
                    return latest_job
                if latest_job.get("full_processing_status") == "error":
                    raise HTTPException(
                        500, latest_job.get("error") or "Full processing failed."
                    )
            raise HTTPException(409, "Full stems are still processing.")

        original_input_path = job.get("original_input_path")
        if not original_input_path:
            raise HTTPException(
                409, "Original upload is not available for full processing."
            )

        input_path = Path(original_input_path)
        if not input_path.is_file():
            raise HTTPException(410, "Original upload is no longer available.")

        job_dir = job_dir_for(job_id)
        stems = int(job.get("requested_stems") or 2)
        track_name = job.get("track_name") or "track"
        output_format = job.get("output_format") or DEFAULT_OUTPUT_FORMAT
        quality = normalize_quality(job.get("quality") or DEFAULT_QUALITY)
        source_duration = job.get("source_duration_seconds")

        update_job(
            job_id,
            status_detail=f"Payment verified. Separating the full track with {demucs_model_for_quality(stems, quality)} ({quality} quality).",
            full_processing_status="processing",
        )
        full_cmd = build_demucs_command(job_dir, input_path, stems, quality)
        env = demucs_subprocess_env()
        full_demucs_dir = demucs_output_dir(job_dir, input_path, stems, quality)
        audio_logger.info(
            "Prepared paid full Demucs job job_id=%s command=%s output_dir=%s input_path=%s input_size_bytes=%s",
            job_id,
            " ".join(full_cmd),
            full_demucs_dir,
            input_path,
            input_path.stat().st_size,
        )
        with DEMUCS_SEMAPHORE:
            proc = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=DEMUCS_TIMEOUT_SECONDS,
                env=env,
            )
        print(f"[JOB {job_id[:8]}] FULL STDOUT:\n{proc.stdout[-2000:]}", flush=True)
        print(f"[JOB {job_id[:8]}] FULL STDERR:\n{proc.stderr[-2000:]}", flush=True)
        print(f"[JOB {job_id[:8]}] FULL RETURN CODE: {proc.returncode}", flush=True)

        if proc.returncode != 0:
            err = (proc.stderr or "") + (proc.stdout or "")
            update_job(
                job_id,
                status_detail="Full stem separation failed after payment verification.",
                full_processing_status="error",
                full_processing_error=f"Demucs error: {err[-400:]}",
            )
            raise HTTPException(500, "Full stem separation failed.")

        full_wavs = expected_demucs_stem_files(job_dir, input_path, stems, quality)
        missing_full_wavs = [path for path in full_wavs if not path.is_file()]
        if missing_full_wavs:
            update_job(
                job_id,
                status_detail="Full stem separation finished without the expected stem files.",
                full_processing_status="error",
                full_processing_error=(
                    "Missing Demucs output files: "
                    + ", ".join(str(path) for path in missing_full_wavs)
                ),
            )
            raise HTTPException(500, "Full stem separation did not create all stems.")
        validate_audio_files(full_wavs, "Demucs full stem")

        full_output_files = prepare_output_files(
            job_id, full_wavs, output_format, "full"
        )
        if full_output_files != full_wavs:
            validate_audio_files(full_output_files, "Paid full stem")
        full_dir = job_dir / "full"
        full_dir.mkdir(parents=True, exist_ok=True)
        persisted_full_files: list[Path] = []
        for full_output_file in full_output_files:
            persisted_file = full_dir / full_output_file.name
            if full_output_file != persisted_file:
                shutil.copy2(full_output_file, persisted_file)
            persisted_full_files.append(persisted_file)
        full_output_files = persisted_full_files
        for full_stem_file in full_output_files:
            validate_paid_download_file(
                full_stem_file,
                {"source_duration_seconds": source_duration},
                "Paid full stem",
            )

        zip_path = job_dir / f"stemify_{track_name}_{output_format}.zip"
        create_zip(zip_path, full_output_files)
        reject_preview_paid_path(zip_path)

        download_stem_urls = {
            public_stem_name(stem_file): f"/api/download/{job_id}/stem/{stem_file.name}"
            for stem_file in full_output_files
        }
        full_stem_paths_by_name = output_files_by_public_name(full_output_files)
        update_job(
            job_id,
            status="done",
            status_detail="Payment verified. Full paid stems and ZIP are ready.",
            full_processing_status="ready",
            full_stems=download_stem_urls,
            zip_url=f"/api/download/{job_id}/zip",
            full_stem_paths=path_map_for_job(full_stem_paths_by_name),
            download_stem_urls=download_stem_urls,
            download_urls={
                "zip": f"/api/download/{job_id}/zip",
                "stems": download_stem_urls,
            },
            zip_path=str(zip_path),
        )
        return get_public_job(job_id)
    finally:
        if "owns_lock" in locals() and owns_lock:
            lock_path.unlink(missing_ok=True)
        event.set()
        with PAID_ASSET_LOCK:
            PAID_ASSET_EVENTS.pop(job_id, None)


def payment_verification_response(
    payment: dict[str, Any], user: dict[str, Any]
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "status": payment.get("status", "pending"),
        "stripe_payment_status": payment.get("stripe_payment_status"),
        "job_id": payment["job_id"],
        "item_type": payment.get("item_type", "song"),
        "payment_status": payment.get("status", "pending"),
    }
    if payment.get("status") == "paid":
        job = ensure_paid_assets_ready(payment["job_id"])
        if job.get("user_uid") != user["uid"]:
            raise HTTPException(404, "Job not found")
        response.update(
            {
                "zip_url": job.get("zip_url"),
                "stem_urls": job.get("download_stem_urls", {}),
                "download_urls": job.get(
                    "download_urls",
                    {
                        "zip": job.get("zip_url"),
                        "stems": job.get("download_stem_urls", {}),
                    },
                ),
            }
        )
    return response


def verify_checkout_payment(
    checkout_session_id: str, job_id: str | None, user: dict[str, Any]
) -> dict[str, Any]:
    validate_checkout_configuration()

    with PAYMENTS_LOCK:
        payment = PAYMENTS.get(checkout_session_id)

    if payment and payment.get("user_uid") != user["uid"]:
        raise HTTPException(404, "Payment not found.")
    if payment and job_id and payment.get("job_id") != job_id:
        raise HTTPException(400, "Checkout Session does not belong to this job.")

    session = retrieve_checkout_session(checkout_session_id)
    metadata = session.get("metadata") or {}
    session_job_id = metadata.get("job_id")
    if metadata.get("user_uid") != user["uid"]:
        raise HTTPException(404, "Payment not found.")
    if job_id and session_job_id != job_id:
        raise HTTPException(400, "Checkout Session does not belong to this job.")

    if session.get("payment_status") == "paid":
        payment = mark_checkout_session_paid(session, verification_source="api")
    elif not payment:
        with PAYMENTS_LOCK:
            payment = PAYMENTS.setdefault(
                checkout_session_id,
                {
                    "checkout_session_id": checkout_session_id,
                    "payment_key": payment_key(
                        session_job_id or "",
                        metadata.get("item_type", "song"),
                        metadata.get("filename") or None,
                    ),
                    "job_id": session_job_id,
                    "item_type": metadata.get("item_type", "song"),
                    "filename": metadata.get("filename") or None,
                    "user_uid": user["uid"],
                    "user_email": metadata.get("user_email")
                    or session.get("customer_email"),
                    "status": "pending",
                    "stripe_payment_status": session.get("payment_status"),
                    "amount_cents": session.get("amount_total"),
                    "currency": (session.get("currency") or PAYMENT_CURRENCY).lower(),
                },
            ).copy()
    else:
        with PAYMENTS_LOCK:
            payment.update(
                {
                    "status": "pending",
                    "stripe_payment_status": session.get("payment_status"),
                }
            )
            payment = payment.copy()

    if not payment.get("job_id"):
        raise HTTPException(400, "Stripe session metadata is missing job_id.")
    return payment_verification_response(payment, user)


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    verify_stripe_webhook_signature(payload, request.headers.get("stripe-signature"))

    try:
        event = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid Stripe webhook payload.") from exc

    if event.get("type") == "checkout.session.completed":
        session = (event.get("data") or {}).get("object") or {}
        if session.get("payment_status") == "paid":
            payment = mark_checkout_session_paid(session)
            return {"received": True, "status": "paid", "job_id": payment["job_id"]}

    return {"received": True}


@app.get("/api/payment/verify")
async def verify_payment(
    session_id: str,
    job_id: str,
    user: Annotated[dict[str, Any], Depends(current_user)],
):
    return verify_checkout_payment(session_id, job_id, user)


@app.post("/api/payments/confirm")
async def payment_status(
    payload: PaymentStatusRequest,
    user: Annotated[dict[str, Any], Depends(current_user)],
):
    return verify_checkout_payment(payload.checkout_session_id, payload.job_id, user)


@app.post("/api/split")
async def split_audio(
    user: Annotated[dict[str, Any], Depends(current_user)],
    file: UploadFile = File(...),
    stems: int = Form(2),
    output_format: str = Form(DEFAULT_OUTPUT_FORMAT),
    quality: str = Form(DEFAULT_QUALITY),
):
    if stems not in STEM_MODELS:
        raise HTTPException(
            400, "Stemify only supports 2 stems: vocals and instrumental."
        )

    output_format = output_format.strip().lower()
    quality = normalize_quality(quality)
    if output_format not in OUTPUT_FORMATS:
        raise HTTPException(
            400,
            f"Unsupported output format: {output_format or 'none'}. Choose one of: {', '.join(sorted(OUTPUT_FORMATS))}.",
        )

    original_name = Path(file.filename or "").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {suffix or 'none'}")

    contents = await file.read(MAX_FILE_SIZE_BYTES + 1)
    if len(contents) > MAX_FILE_SIZE_BYTES:
        size_mb = len(contents) / (1024 * 1024)
        raise HTTPException(
            413, f"File too large ({size_mb:.1f} MB). Max {MAX_FILE_SIZE_MB} MB."
        )
    if not contents:
        raise HTTPException(400, "Uploaded file is empty.")

    job_id = str(uuid.uuid4())
    job_dir = job_dir_for(job_id)
    job_dir.mkdir(parents=True, exist_ok=False)
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    safe_stem = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(original_name).stem)[:80] or "track"
    input_path = input_dir / original_name
    input_path.write_bytes(contents)
    now = time.time()

    job_record = {
        "job_id": job_id,
        "original_filename": original_name,
        "uploaded_file_path": str(input_path),
        "preview_status": "processing",
        "preview_stems": [],
        "preview_durations_seconds": {},
        "preview_file_info": {},
        "payment_status": "pending",
        "checkout_session_id": None,
        "stripe_payment_intent": None,
        "full_processing_status": "not_started",
        "full_stems": [],
        "zip_path": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
        "status": "processing",
        "status_detail": f"Queued for {PREVIEW_DURATION_SECONDS}-second vocal/instrumental preview.",
        "stems": [],
        "zip_url": None,
        "stem_urls": {},
        "track_name": safe_stem,
        "user_uid": user["uid"],
        "user_email": user["email"],
        "requested_stems": stems,
        "preview_duration_seconds": PREVIEW_DURATION_SECONDS,
        "output_format": output_format,
        "quality": quality,
        "started_at": now,
        "paid": False,
        "original_input_path": str(input_path),
    }
    write_job_json(job_record)
    with JOBS_LOCK:
        JOBS[job_id] = job_record.copy()
    with AUTH_LOCK:
        user["jobs_created"] += 1
        user["stem_count"] += stems

    thread = threading.Thread(
        target=run_demucs,
        args=(job_id, job_dir, input_path, stems, safe_stem, output_format, quality),
        daemon=True,
    )
    thread.start()

    return JSONResponse(
        {
            "job_id": job_id,
            "status": "processing",
            "status_detail": f"Queued for {PREVIEW_DURATION_SECONDS}-second vocal/instrumental preview.",
            "track_name": safe_stem,
            "preview_duration_seconds": PREVIEW_DURATION_SECONDS,
            "output_format": output_format,
            "quality": quality,
            "available_output_formats": sorted(OUTPUT_FORMATS),
            "available_qualities": sorted(QUALITY_SETTINGS),
            "user": usage_user(user),
        }
    )


def build_demucs_command(
    job_dir: Path, input_audio_path: Path, stems: int, quality: str = "fast"
) -> list[str]:
    settings = QUALITY_SETTINGS[normalize_quality(quality)]
    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        settings["models"][stems],
        "--out",
        str(job_dir),
        "--shifts",
        str(settings["shifts"]),
        "--overlap",
        str(settings["overlap"]),
    ]
    if settings["segment_seconds"] > 0:
        cmd += ["--segment", str(settings["segment_seconds"])]
    if settings["jobs"] > 0:
        cmd += ["--jobs", str(settings["jobs"])]
    if settings["device"]:
        cmd += ["--device", settings["device"]]
    if stems == 2:
        cmd += ["--two-stems", "vocals"]
    cmd.append(str(input_audio_path))
    return cmd


def demucs_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    if DEMUCS_CPU_THREADS > 0:
        thread_count = str(DEMUCS_CPU_THREADS)
        env["OMP_NUM_THREADS"] = thread_count
        env["MKL_NUM_THREADS"] = thread_count
        env["NUMEXPR_NUM_THREADS"] = thread_count
        env["TORCH_NUM_THREADS"] = thread_count
    return env


def run_demucs(
    job_id: str,
    job_dir: Path,
    input_path: Path,
    stems: int,
    track_name: str,
    output_format: str,
    quality: str = "fast",
) -> None:
    preview_path: Path | None = None
    try:
        update_job(
            job_id, status_detail=f"Creating {PREVIEW_DURATION_SECONDS}-second preview."
        )
        audio_logger.info(
            "Starting preview split job_id=%s input_path=%s", job_id, input_path
        )
        try:
            source_duration = audio_duration_seconds(input_path)
        except RuntimeError as exc:
            source_duration = None
            audio_logger.warning(
                "Could not determine source duration job_id=%s path=%s error=%s",
                job_id,
                input_path,
                exc,
            )
        preview_path = create_preview_input(job_id, input_path)
        cmd = build_demucs_command(job_dir, preview_path, stems, quality)
        env = demucs_subprocess_env()
        demucs_dir = demucs_output_dir(job_dir, preview_path, stems, quality)
        audio_logger.info(
            "Prepared Demucs job job_id=%s command=%s output_dir=%s preview_path=%s preview_size_bytes=%s",
            job_id,
            " ".join(cmd),
            demucs_dir,
            preview_path,
            preview_path.stat().st_size,
        )

        update_job(
            job_id,
            status_detail=(
                f"Waiting for an available Demucs worker for the {PREVIEW_DURATION_SECONDS}-second preview."
            ),
        )
        with DEMUCS_SEMAPHORE:
            update_job(
                job_id,
                status_detail=(
                    f"Separating the {PREVIEW_DURATION_SECONDS}-second preview with {demucs_model_for_quality(stems, quality)} ({quality} quality)."
                ),
            )
            print(f"[JOB {job_id[:8]}] Running: {' '.join(cmd)}", flush=True)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=DEMUCS_TIMEOUT_SECONDS,
                env=env,
            )
        print(f"[JOB {job_id[:8]}] STDOUT:\n{proc.stdout[-2000:]}", flush=True)
        print(f"[JOB {job_id[:8]}] STDERR:\n{proc.stderr[-2000:]}", flush=True)
        print(f"[JOB {job_id[:8]}] RETURN CODE: {proc.returncode}", flush=True)

        if proc.returncode != 0:
            err = (proc.stderr or "") + (proc.stdout or "")
            print(f"[JOB {job_id[:8]}] FAILED:\n{err[-800:]}")
            update_job(
                job_id,
                status="error",
                preview_status="error",
                status_detail="Preview stem separation failed.",
                error=f"Demucs error: {err[-400:]}",
            )
            return

        update_job(
            job_id,
            status_detail=f"Finalizing preview stem files as {output_format.upper()}.",
        )
        wavs = expected_demucs_stem_files(job_dir, preview_path, stems, quality)
        audio_logger.info(
            "Checking Demucs preview output job_id=%s output_dir=%s generated_stems=%s",
            job_id,
            demucs_dir,
            [str(path) for path in wavs],
        )
        missing_wavs = [path for path in wavs if not path.is_file()]
        if missing_wavs:
            update_job(
                job_id,
                status="error",
                preview_status="error",
                status_detail="Preview stem separation finished without the expected stem files.",
                error=(
                    "Missing Demucs output files: "
                    + ", ".join(str(path) for path in missing_wavs)
                ),
            )
            return
        validate_audio_files(wavs, "Demucs preview stem")

        try:
            preview_output_files, preview_file_info = convert_preview_wavs_to_mp3(
                job_id, wavs
            )
        except RuntimeError as exc:
            update_job(
                job_id,
                status="error",
                preview_status="failed",
                status_detail="Preview MP3 validation failed.",
                error=str(exc),
            )
            return
        preview_durations_seconds = {
            name: info["duration_seconds"] for name, info in preview_file_info.items()
        }
        audio_logger.info(
            "Prepared browser preview MP3 stems job_id=%s preview_files=%s file_info=%s",
            job_id,
            [str(path) for path in preview_output_files],
            preview_file_info,
        )

        stem_names = [public_stem_name(stem_file) for stem_file in preview_output_files]
        preview_urls = {
            public_stem_name(stem_file): f"/api/preview/{job_id}/stem/{stem_file.name}"
            for stem_file in preview_output_files
        }
        update_job(
            job_id,
            status="done",
            preview_status="ready",
            status_detail=f"{PREVIEW_DURATION_SECONDS}-second preview stems are ready. Complete payment to generate full stems and ZIP.",
            stems=stem_names,
            preview_stems=preview_urls,
            zip_url=None,
            stem_urls=preview_urls,
            preview_urls=preview_urls,
            preview_stem_paths=path_map_for_job(
                output_files_by_public_name(preview_output_files)
            ),
            download_stem_urls={},
            download_urls={"zip": None, "stems": {}},
            source_duration_seconds=source_duration,
            output_format="mp3",
            requested_output_format=output_format,
            preview_durations_seconds=preview_durations_seconds,
            preview_file_info=preview_file_info,
            quality=quality,
        )
        print(f"[JOB {job_id[:8]}] Preview done: {stem_names}")

    except subprocess.TimeoutExpired:
        update_job(
            job_id,
            status="error",
            preview_status="error",
            status_detail="Preview stem separation timed out.",
            error=f"Timed out after {DEMUCS_TIMEOUT_SECONDS} seconds. Try a shorter track.",
        )
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            preview_status="error",
            status_detail="Preview stem separation crashed.",
            error=str(exc),
        )
        print(f"[JOB {job_id[:8]}] Exception: {exc}")
    finally:
        if preview_path:
            preview_path.unlink(missing_ok=True)


@app.get("/api/job/{job_id}")
async def job_status(
    job_id: str, user: Annotated[dict[str, Any], Depends(current_user)]
):
    return JSONResponse(get_authorized_job(job_id, user))


@app.get("/api/preview/{job_id}/stem/{filename}")
async def preview_stem(job_id: str, filename: str):
    if not SAFE_DOWNLOAD_RE.fullmatch(filename):
        raise HTTPException(400, "Invalid stem filename.")

    job = get_public_job(job_id)
    enforce_public_job_done(job)
    preview_urls = job.get("preview_urls") or job.get("stem_urls") or {}
    if f"/api/preview/{job_id}/stem/{filename}" not in preview_urls.values():
        raise HTTPException(404, "Preview stem not found.")

    job_dir = (
        job_dir_for(job_id) if job_dir_for(job_id).is_dir() else OUTPUT_DIR / job_id
    )
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found.")

    public_name = public_stem_name(Path(filename))
    recorded_path = get_recorded_path(job, "preview_stem_paths", public_name)
    if recorded_path and recorded_path.name == filename and recorded_path.is_file():
        stem_path = recorded_path
    else:
        matches = sorted(path for path in job_dir.rglob(filename) if path.is_file())
        if not matches:
            raise HTTPException(404, "Preview stem not found.")
        stem_path = matches[0]
    if not stem_path.is_file():
        raise HTTPException(404, "Preview stem not found.")
    extension = stem_path.suffix.lower().lstrip(".")
    media_type = OUTPUT_FORMATS.get(extension, OUTPUT_FORMATS["wav"])["media_type"]
    audio_logger.info(
        "Serving preview stem job_id=%s filename=%s path=%s media_type=%s size_bytes=%s",
        job_id,
        filename,
        stem_path,
        media_type,
        stem_path.stat().st_size,
    )
    return FileResponse(stem_path, media_type=media_type, filename=filename)


@app.get("/api/download/{job_id}/zip")
async def download_zip(
    job_id: str, user: Annotated[dict[str, Any], Depends(current_user)]
):
    job = get_authorized_job(job_id, user)
    enforce_job_done(job)
    enforce_paid_download(job, user, "zip")
    job_dir = (
        job_dir_for(job_id) if job_dir_for(job_id).is_dir() else OUTPUT_DIR / job_id
    )
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found.")
    zip_path = Path(job["zip_path"]) if job.get("zip_path") else None
    if zip_path is None:
        zips = sorted(job_dir.glob("*.zip"))
        zip_path = zips[0] if zips else None
    if zip_path is None or not zip_path.is_file():
        raise HTTPException(404, "ZIP not ready yet.")
    reject_preview_paid_path(zip_path)
    return FileResponse(zip_path, media_type="application/zip", filename=zip_path.name)


@app.get("/api/download/{job_id}/stem/{filename}")
async def download_stem(
    job_id: str,
    filename: str,
    user: Annotated[dict[str, Any], Depends(current_user)],
):
    if not SAFE_DOWNLOAD_RE.fullmatch(filename):
        raise HTTPException(400, "Invalid stem filename.")

    job = get_authorized_job(job_id, user)
    enforce_job_done(job)
    enforce_paid_download(job, user, "stem", filename)
    job_dir = (
        job_dir_for(job_id) if job_dir_for(job_id).is_dir() else OUTPUT_DIR / job_id
    )
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found.")

    public_name = public_stem_name(Path(filename))
    stem_path = get_recorded_path(job, "full_stem_paths", public_name)
    if stem_path is None or stem_path.name != filename:
        matches = sorted(path for path in job_dir.rglob(filename) if path.is_file())
        stem_path = matches[0] if matches else None
    if stem_path is None:
        raise HTTPException(404, "Stem not found.")
    validate_paid_download_file(stem_path, job, "Stem")
    extension = stem_path.suffix.lower().lstrip(".")
    media_type = OUTPUT_FORMATS.get(extension, OUTPUT_FORMATS["wav"])["media_type"]
    return FileResponse(stem_path, media_type=media_type, filename=filename)


@app.delete("/api/cleanup/{job_id}")
async def cleanup(job_id: str, user: Annotated[dict[str, Any], Depends(current_user)]):
    get_authorized_job(job_id, user)
    job_dir = OUTPUT_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return {"status": "cleaned"}
