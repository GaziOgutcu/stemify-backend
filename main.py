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
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
DEMUCS_TIMEOUT_SECONDS = int(os.getenv("DEMUCS_TIMEOUT_SECONDS", "900"))
DEMUCS_MODEL = os.getenv("DEMUCS_MODEL", os.getenv("DEMUCS_MODEL_2_STEMS", "mdx_q"))
DEMUCS_SHIFTS = int(os.getenv("DEMUCS_SHIFTS", "0"))
DEMUCS_OVERLAP = float(os.getenv("DEMUCS_OVERLAP", "0.1"))
DEMUCS_SEGMENT_SECONDS = int(float(os.getenv("DEMUCS_SEGMENT_SECONDS", "8")))
DEMUCS_JOBS = int(os.getenv("DEMUCS_JOBS", "0"))
DEMUCS_DEVICE = os.getenv("DEMUCS_DEVICE", "")
DEMUCS_CPU_THREADS = int(os.getenv("DEMUCS_CPU_THREADS", "2"))
DEMUCS_CONCURRENCY = max(1, int(os.getenv("DEMUCS_CONCURRENCY", "1")))
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}
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
    with JOBS_LOCK:
        if job_id in JOBS:
            values.setdefault("updated_at", time.time())
            JOBS[job_id].update(values)


def serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    public_job = job.copy()
    started_at = public_job.get("started_at")
    if started_at:
        public_job["elapsed_seconds"] = round(time.time() - float(started_at), 1)
    public_job["timeout_seconds"] = DEMUCS_TIMEOUT_SECONDS
    return public_job


def get_authorized_job(job_id: str, user: dict[str, Any]) -> dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job.get("user_uid") != user["uid"]:
            raise HTTPException(404, "Job not found")
        return serialize_job(job)


def get_public_job(job_id: str) -> dict[str, Any]:
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
    job_id: str, wavs: list[Path], output_format: str
) -> list[Path]:
    if output_format == "wav":
        return wavs

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            f"FFmpeg is required to export {output_format.upper()} stems."
        )

    format_config = OUTPUT_FORMATS[output_format]
    converted_dir = OUTPUT_DIR / job_id / output_format
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
        validate_audio_file(converted, f"{output_format.upper()} preview stem")
        audio_logger.info(
            "Generated encoded preview stem job_id=%s path=%s size_bytes=%s",
            job_id,
            converted,
            converted.stat().st_size,
        )
        converted_files.append(converted)

    return converted_files


def demucs_output_dir(job_dir: Path, preview_path: Path, stems: int) -> Path:
    return job_dir / STEM_MODELS[stems] / preview_path.stem


def expected_demucs_stem_files(
    job_dir: Path, preview_path: Path, stems: int
) -> list[Path]:
    output_dir = demucs_output_dir(job_dir, preview_path, stems)
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
        "default_output_format": "wav",
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
        "output_formats": sorted(OUTPUT_FORMATS),
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
    enforce_job_done(job)
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
    with JOBS_LOCK:
        if job_id in JOBS and JOBS[job_id].get("user_uid") == user_uid:
            JOBS[job_id].update(
                {
                    "paid": True,
                    "payment_status": "paid",
                    "paid_checkout_session_id": session_id,
                    "updated_at": time.time(),
                }
            )
        return payment.copy()


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
        job = get_authorized_job(payment["job_id"], user)
        response.update(
            {
                "zip_url": job.get("zip_url"),
                "stem_urls": job.get("stem_urls", {}),
                "download_urls": {
                    "zip": job.get("zip_url"),
                    "stems": job.get("download_stem_urls", job.get("stem_urls", {})),
                },
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
    output_format: str = Form("wav"),
):
    if stems not in STEM_MODELS:
        raise HTTPException(
            400, "Stemify only supports 2 stems: vocals and instrumental."
        )

    output_format = output_format.strip().lower()
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
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    safe_stem = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(original_name).stem)[:80] or "track"
    input_path = UPLOAD_DIR / f"{job_id}{suffix}"
    input_path.write_bytes(contents)
    now = time.time()

    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "processing",
            "status_detail": f"Queued for {PREVIEW_DURATION_SECONDS}-second vocal/instrumental preview.",
            "stems": [],
            "zip_url": None,
            "stem_urls": {},
            "error": None,
            "track_name": safe_stem,
            "user_uid": user["uid"],
            "user_email": user["email"],
            "requested_stems": stems,
            "preview_duration_seconds": PREVIEW_DURATION_SECONDS,
            "output_format": output_format,
            "started_at": now,
            "updated_at": now,
            "payment_status": "pending",
            "paid": False,
        }
    with AUTH_LOCK:
        user["jobs_created"] += 1
        user["stem_count"] += stems

    thread = threading.Thread(
        target=run_demucs,
        args=(job_id, job_dir, input_path, stems, safe_stem, output_format),
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
            "available_output_formats": sorted(OUTPUT_FORMATS),
            "user": usage_user(user),
        }
    )


def build_demucs_command(job_dir: Path, preview_path: Path, stems: int) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        STEM_MODELS[stems],
        "--out",
        str(job_dir),
        "--shifts",
        str(DEMUCS_SHIFTS),
        "--overlap",
        str(DEMUCS_OVERLAP),
    ]
    if DEMUCS_SEGMENT_SECONDS > 0:
        cmd += ["--segment", str(DEMUCS_SEGMENT_SECONDS)]
    if DEMUCS_JOBS > 0:
        cmd += ["--jobs", str(DEMUCS_JOBS)]
    if DEMUCS_DEVICE:
        cmd += ["--device", DEMUCS_DEVICE]
    if stems == 2:
        cmd += ["--two-stems", "vocals"]
    cmd.append(str(preview_path))
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
) -> None:
    preview_path: Path | None = None
    try:
        update_job(
            job_id, status_detail=f"Creating {PREVIEW_DURATION_SECONDS}-second preview."
        )
        audio_logger.info(
            "Starting preview split job_id=%s input_path=%s", job_id, input_path
        )
        preview_path = create_preview_input(job_id, input_path)
        cmd = build_demucs_command(job_dir, preview_path, stems)
        env = demucs_subprocess_env()
        demucs_dir = demucs_output_dir(job_dir, preview_path, stems)
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
                    f"Separating the {PREVIEW_DURATION_SECONDS}-second preview with {STEM_MODELS[stems]}."
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
                status_detail="Preview stem separation failed.",
                error=f"Demucs error: {err[-400:]}",
            )
            return

        update_job(
            job_id,
            status_detail=f"Finalizing preview stem files as {output_format.upper()}.",
        )
        wavs = expected_demucs_stem_files(job_dir, preview_path, stems)
        audio_logger.info(
            "Checking Demucs output job_id=%s output_dir=%s generated_stems=%s",
            job_id,
            demucs_dir,
            [str(path) for path in wavs],
        )
        missing_wavs = [path for path in wavs if not path.is_file()]
        if missing_wavs:
            update_job(
                job_id,
                status="error",
                status_detail="Preview stem separation finished without the expected stem files.",
                error=(
                    "Missing Demucs output files: "
                    + ", ".join(str(path) for path in missing_wavs)
                ),
            )
            return
        validate_audio_files(wavs, "Demucs preview stem")

        output_files = prepare_output_files(job_id, wavs, output_format)
        validate_audio_files(output_files, "Browser preview stem")
        audio_logger.info(
            "Prepared browser preview stems job_id=%s preview_files=%s file_sizes=%s",
            job_id,
            [str(path) for path in output_files],
            {path.name: path.stat().st_size for path in output_files},
        )
        zip_path = job_dir / f"stemify_{track_name}_{output_format}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for stem_file in output_files:
                zf.write(stem_file, arcname=stem_file.name)

        stem_names = [public_stem_name(stem_file) for stem_file in output_files]
        preview_urls = {
            public_stem_name(stem_file): f"/api/preview/{job_id}/stem/{stem_file.name}"
            for stem_file in output_files
        }
        download_stem_urls = {
            public_stem_name(stem_file): f"/api/download/{job_id}/stem/{stem_file.name}"
            for stem_file in output_files
        }
        update_job(
            job_id,
            status="done",
            status_detail=f"{PREVIEW_DURATION_SECONDS}-second preview stems are ready.",
            stems=stem_names,
            zip_url=f"/api/download/{job_id}/zip",
            stem_urls=preview_urls,
            preview_urls=preview_urls,
            download_stem_urls=download_stem_urls,
            output_format=output_format,
        )
        print(f"[JOB {job_id[:8]}] Done: {stem_names}")

    except subprocess.TimeoutExpired:
        update_job(
            job_id,
            status="error",
            status_detail="Preview stem separation timed out.",
            error=f"Timed out after {DEMUCS_TIMEOUT_SECONDS} seconds. Try a shorter track.",
        )
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            status_detail="Preview stem separation crashed.",
            error=str(exc),
        )
        print(f"[JOB {job_id[:8]}] Exception: {exc}")
    finally:
        input_path.unlink(missing_ok=True)
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

    job_dir = OUTPUT_DIR / job_id
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found.")

    matches = sorted(path for path in job_dir.rglob(filename) if path.is_file())
    if not matches:
        raise HTTPException(404, "Preview stem not found.")
    extension = matches[0].suffix.lower().lstrip(".")
    media_type = OUTPUT_FORMATS.get(extension, OUTPUT_FORMATS["wav"])["media_type"]
    audio_logger.info(
        "Serving preview stem job_id=%s filename=%s path=%s media_type=%s size_bytes=%s",
        job_id,
        filename,
        matches[0],
        media_type,
        matches[0].stat().st_size,
    )
    return FileResponse(matches[0], media_type=media_type, filename=filename)


@app.get("/api/download/{job_id}/zip")
async def download_zip(
    job_id: str, user: Annotated[dict[str, Any], Depends(current_user)]
):
    job = get_authorized_job(job_id, user)
    enforce_job_done(job)
    enforce_paid_download(job, user, "zip")
    job_dir = OUTPUT_DIR / job_id
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found.")
    zips = sorted(job_dir.glob("*.zip"))
    if not zips:
        raise HTTPException(404, "ZIP not ready yet.")
    return FileResponse(zips[0], media_type="application/zip", filename=zips[0].name)


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
    job_dir = OUTPUT_DIR / job_id
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found.")

    matches = sorted(path for path in job_dir.rglob(filename) if path.is_file())
    if not matches:
        raise HTTPException(404, "Stem not found.")
    extension = matches[0].suffix.lower().lstrip(".")
    media_type = OUTPUT_FORMATS.get(extension, OUTPUT_FORMATS["wav"])["media_type"]
    return FileResponse(matches[0], media_type=media_type, filename=filename)


@app.delete("/api/cleanup/{job_id}")
async def cleanup(job_id: str, user: Annotated[dict[str, Any], Depends(current_user)]):
    get_authorized_job(job_id, user)
    job_dir = OUTPUT_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return {"status": "cleaned"}
