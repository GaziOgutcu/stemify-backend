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
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

# ── Modal client (optional — falls back to local Demucs if not configured) ───
MODAL_TOKEN_ID     = os.getenv("MODAL_TOKEN_ID", "").strip()
MODAL_TOKEN_SECRET = os.getenv("MODAL_TOKEN_SECRET", "").strip()
MODAL_WORKSPACE    = os.getenv("MODAL_WORKSPACE", "").strip()
MODAL_ENABLED = bool(MODAL_TOKEN_ID and MODAL_TOKEN_SECRET and MODAL_WORKSPACE)

_modal_fn = None  # lazy-loaded Modal function handle

def _get_modal_fn():
    """Lazy-load the Modal Demucs function. Returns None if Modal is not configured."""
    global _modal_fn
    if not MODAL_ENABLED:
        return None
    if _modal_fn is not None:
        return _modal_fn
    try:
        import modal
        modal.config._token_id     = MODAL_TOKEN_ID
        modal.config._token_secret = MODAL_TOKEN_SECRET
        _app = modal.App.lookup("stemify-demucs", environment_name="main")
        _modal_fn = modal.Function.lookup("stemify-demucs", "separate_stems")
        print("[OK] Modal Demucs function loaded", flush=True)
        return _modal_fn
    except Exception as exc:
        print(f"[WARN] Modal not available, falling back to local Demucs: {exc}", flush=True)
        return None

def run_demucs_modal(
    input_path: Path,
    model: str,
    shifts: int,
    overlap: float,
    segment: int,
    two_stems: bool,
    output_dir: Path,
    timeout: int = 300,
) -> bool:
    """
    Run Demucs via Modal GPU. Returns True on success, False if Modal unavailable.
    On success, writes WAV files into output_dir/<model>/<stem>.wav
    """
    fn = _get_modal_fn()
    if fn is None:
        return False
    try:
        audio_bytes = input_path.read_bytes()
        stems: dict[str, bytes] = fn.remote(
            audio_bytes,
            input_path.name,
            model=model,
            two_stems=two_stems,
            shifts=shifts,
            overlap=overlap,
            segment=segment,
        )
        # Write returned WAV bytes to expected output paths
        stem_dir = output_dir / model / input_path.stem
        stem_dir.mkdir(parents=True, exist_ok=True)
        for stem_name, wav_bytes in stems.items():
            out_path = stem_dir / f"{stem_name}.wav"
            out_path.write_bytes(wav_bytes)
            print(f"[Modal] Wrote {out_path} ({len(wav_bytes)} bytes)", flush=True)
        return True
    except Exception as exc:
        print(f"[Modal] Error, falling back to local: {exc}", flush=True)
        return False
# ─────────────────────────────────────────────────────────────────────────────

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

# ── R2 / S3 storage (optional — falls back to local if not configured) ──
try:
    import boto3
    from botocore.client import Config as BotocoreConfig
    _boto3_available = True
except ImportError:
    boto3 = None
    _boto3_available = False
# ──────────────────────────────────────────────────────────────────────────────

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

app = FastAPI(title="Stemify API", version="1.5.2")
stripe_logger = logging.getLogger("stemify.stripe")
audio_logger = logging.getLogger("stemify.audio")
logger = stripe_logger


def _split_csv_env(name: str, default: str = "") -> list[str]:
    return [
        item.strip() for item in os.getenv(name, default).split(",") if item.strip()
    ]


ALLOWED_ORIGINS = _split_csv_env("ALLOWED_ORIGINS", "*")
ALLOW_ALL_ORIGINS = "*" in ALLOWED_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if ALLOW_ALL_ORIGINS else ALLOWED_ORIGINS,
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
FULL_PROCESSING_STALE_SECONDS = int(os.getenv("FULL_PROCESSING_STALE_SECONDS", str(DEMUCS_TIMEOUT_SECONDS + 180)))
DEMUCS_MODEL = os.getenv("DEMUCS_MODEL", os.getenv("DEMUCS_MODEL_2_STEMS", "mdx_q"))
# Preview must stay fast. Do not let a production DEMUCS_MODEL=htdemucs setting
# accidentally make the first 15-second preview take minutes.
PREVIEW_DEMUCS_MODEL = os.getenv("PREVIEW_DEMUCS_MODEL", "mdx_q")
FULL_DEMUCS_MODEL = os.getenv("FULL_DEMUCS_MODEL", DEMUCS_MODEL)
PREVIEW_DEMUCS_TIMEOUT_SECONDS = int(os.getenv("PREVIEW_DEMUCS_TIMEOUT_SECONDS", "180"))
DEMUCS_SHIFTS = int(os.getenv("DEMUCS_SHIFTS", "0"))
DEMUCS_OVERLAP = float(os.getenv("DEMUCS_OVERLAP", "0.05"))  # 0.05 is enough for 15s preview; saves ~5% compute
DEMUCS_SEGMENT_SECONDS = int(float(os.getenv("DEMUCS_SEGMENT_SECONDS", "0")))  # 0 = demucs default (no --segment flag = faster for short clips)
DEMUCS_JOBS = int(os.getenv("DEMUCS_JOBS", "0"))
DEMUCS_DEVICE = os.getenv("DEMUCS_DEVICE", "")
DEMUCS_HIGH_MODEL = os.getenv("DEMUCS_HIGH_MODEL", "htdemucs")
DEMUCS_WARM_MODELS = _split_csv_env("DEMUCS_WARM_MODELS", DEMUCS_MODEL)
DEMUCS_HIGH_SHIFTS = int(os.getenv("DEMUCS_HIGH_SHIFTS", "1"))
DEMUCS_HIGH_OVERLAP = float(os.getenv("DEMUCS_HIGH_OVERLAP", "0.25"))
DEMUCS_HIGH_SEGMENT_SECONDS = int(float(os.getenv("DEMUCS_HIGH_SEGMENT_SECONDS", "0")))
DEMUCS_HIGH_JOBS = int(os.getenv("DEMUCS_HIGH_JOBS", str(DEMUCS_JOBS)))
DEMUCS_HIGH_DEVICE = os.getenv("DEMUCS_HIGH_DEVICE", DEMUCS_DEVICE)
DEMUCS_CPU_THREADS = int(os.getenv("DEMUCS_CPU_THREADS", "0"))  # 0 = let PyTorch auto-detect
DEMUCS_CONCURRENCY = max(1, int(os.getenv("DEMUCS_CONCURRENCY", "1")))
AUTO_START_FULL_AFTER_PREVIEW = (
    os.getenv("AUTO_START_FULL_AFTER_PREVIEW", "false").lower() == "true"
)
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}
DEFAULT_OUTPUT_FORMAT = "mp3"
DOWNLOAD_HISTORY_RETENTION_SECONDS = int(os.getenv("DOWNLOAD_HISTORY_RETENTION_SECONDS", "86400"))

# ── Cloudflare R2 config ──────────────────────────────────────────────────────
R2_ENDPOINT_URL      = os.getenv("R2_ENDPOINT_URL", "").rstrip("/")
R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME       = os.getenv("R2_BUCKET_NAME", "stemify-audio")
R2_ENABLED = bool(
    _boto3_available and R2_ENDPOINT_URL and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY
)

def _get_r2_client():
    if not R2_ENABLED:
        return None
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=BotocoreConfig(signature_version="s3v4"),
        region_name="auto",
    )

def r2_upload_file(local_path: Path, r2_key: str) -> bool:
    """Upload a file to R2. Returns True on success."""
    client = _get_r2_client()
    if not client:
        return False
    try:
        client.upload_file(str(local_path), R2_BUCKET_NAME, r2_key)
        print(f"[R2] Uploaded {local_path} -> {r2_key}", flush=True)
        return True
    except Exception as exc:
        print(f"[R2] Upload failed {r2_key}: {exc}", flush=True)
        return False

def r2_download_file(r2_key: str, local_path: Path) -> bool:
    """Download a file from R2 to local path. Returns True on success."""
    client = _get_r2_client()
    if not client:
        return False
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(R2_BUCKET_NAME, r2_key, str(local_path))
        print(f"[R2] Downloaded {r2_key} -> {local_path}", flush=True)
        return True
    except Exception as exc:
        print(f"[R2] Download failed {r2_key}: {exc}", flush=True)
        return False

def r2_key_for_input(job_id: str, filename: str) -> str:
    return f"jobs/{job_id}/input/{filename}"

def r2_key_for_full_stem(job_id: str, filename: str) -> str:
    return f"jobs/{job_id}/full/{filename}"

def r2_key_for_zip(job_id: str, filename: str) -> str:
    return f"jobs/{job_id}/zip/{filename}"

def r2_presigned_url(r2_key: str, expires_in: int = 604800) -> str | None:
    """Generate a presigned URL valid for expires_in seconds (default 7 days)."""
    client = _get_r2_client()
    if not client:
        return None
    try:
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET_NAME, "Key": r2_key},
            ExpiresIn=expires_in,
        )
        return url
    except Exception as exc:
        print(f"[R2] Presigned URL failed {r2_key}: {exc}", flush=True)
        return None

def r2_delete_prefix(prefix: str) -> int:
    """Delete all objects under an R2 prefix. Returns deleted object count."""
    client = _get_r2_client()
    if not client:
        return 0

    deleted_count = 0
    continuation_token: str | None = None
    try:
        while True:
            kwargs: dict[str, Any] = {"Bucket": R2_BUCKET_NAME, "Prefix": prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = client.list_objects_v2(**kwargs)
            objects = response.get("Contents") or []
            if objects:
                keys = [{"Key": item["Key"]} for item in objects if item.get("Key")]
                for chunk_start in range(0, len(keys), 1000):
                    chunk = keys[chunk_start : chunk_start + 1000]
                    client.delete_objects(
                        Bucket=R2_BUCKET_NAME,
                        Delete={"Objects": chunk, "Quiet": True},
                    )
                    deleted_count += len(chunk)
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
        if deleted_count:
            print(f"[R2] Deleted {deleted_count} object(s) under {prefix}", flush=True)
        return deleted_count
    except Exception as exc:
        print(f"[R2] Delete prefix failed {prefix}: {exc}", flush=True)
        return deleted_count

print(f"[OK] R2 storage: {'enabled' if R2_ENABLED else 'disabled (local fallback)'}", flush=True)
# ─────────────────────────────────────────────────────────────────────────────
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
    os.getenv("PRICE_PER_SONG_CENTS", os.getenv("PRICE_PER_STEM_CENTS", "99"))
)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
PAYMENTS_ENABLED = (
    os.getenv("PAYMENTS_ENABLED", "true" if STRIPE_SECRET_KEY else "false").lower()
    == "true"
)
PAYMENT_CURRENCY = os.getenv("PAYMENT_CURRENCY", "aud").lower()
PAYMENT_TEST_MODE = os.getenv("PAYMENT_TEST_MODE", "false").lower() == "true"
STRIPE_ALLOW_LIVE_PAYMENTS = os.getenv("STRIPE_ALLOW_LIVE_PAYMENTS", "false").lower() == "true"
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
FRONTEND_RETURN_PATH = os.getenv("FRONTEND_RETURN_PATH", "/").strip() or "/"
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
FIREBASE_CLIENT_EMAIL = os.getenv("FIREBASE_CLIENT_EMAIL", "")
FIREBASE_PRIVATE_KEY = os.getenv("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")

STEM_MODELS = {
    2: PREVIEW_DEMUCS_MODEL,
}
FULL_STEM_MODELS = {
    2: FULL_DEMUCS_MODEL,
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

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
SAFE_DOWNLOAD_RE = re.compile(r"^[A-Za-z0-9_. -]+\.(wav|mp3|flac|ogg|m4a)$")

USERS: dict[str, dict[str, Any]] = {}
AUTH_LOCK = threading.Lock()
PAYMENTS_LOCK = threading.Lock()
PAID_ASSET_EVENTS: dict[str, threading.Event] = {}
PAID_ASSET_LOCK = threading.Lock()

# ── FIX 1: Persist PAYMENTS to disk so they survive Railway restarts ──────────
PAYMENTS_FILE = JOBS_DIR / "payments.json"


def _load_payments_from_disk() -> dict[str, dict[str, Any]]:
    """Load payment records from disk on startup."""
    if PAYMENTS_FILE.is_file():
        try:
            data = json.loads(PAYMENTS_FILE.read_text(encoding="utf-8"))
            print(f"[OK] Loaded {len(data)} payment record(s) from disk.")
            return data
        except Exception as exc:
            print(f"[WARN] Could not load payments from disk: {exc}")
    return {}


def _save_payments_to_disk() -> None:
    """Atomically write the current PAYMENTS dict to disk."""
    try:
        tmp = PAYMENTS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(PAYMENTS, indent=2), encoding="utf-8")
        tmp.replace(PAYMENTS_FILE)
    except Exception as exc:
        print(f"[WARN] Could not save payments to disk: {exc}")


USER_PAYMENT_JOBS: dict[str, set[str]] = {}
PAYMENTS: dict[str, dict[str, Any]] = _load_payments_from_disk()
# ─────────────────────────────────────────────────────────────────────────────


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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def job_history_expires_at(job: dict[str, Any]) -> float:
    """Return the expiry timestamp for download history and retained files."""
    explicit_expiry = _safe_float(job.get("expires_at"), 0.0)
    if explicit_expiry > 0:
        return explicit_expiry
    base = (
        _safe_float(job.get("ready_at"), 0.0)
        or _safe_float(job.get("paid_at"), 0.0)
        or _safe_float(job.get("created_at"), 0.0)
        or time.time()
    )
    return base + DOWNLOAD_HISTORY_RETENTION_SECONDS


def is_job_expired(job: dict[str, Any], now: float | None = None) -> bool:
    return job_history_expires_at(job) <= (now or time.time())


def seconds_until_expiry(job: dict[str, Any], now: float | None = None) -> int:
    return max(0, int(job_history_expires_at(job) - (now or time.time())))


def delete_job_r2_objects(job_id: str) -> int:
    """Delete all R2 objects for a job. Safe no-op when R2 is disabled."""
    if not R2_ENABLED:
        return 0
    return r2_delete_prefix(f"jobs/{job_id}/")


def cleanup_expired_jobs() -> dict[str, Any]:
    """Remove expired jobs from local disk, memory, payment indexes, and R2."""
    now = time.time()
    cleaned: list[str] = []
    errors: dict[str, str] = {}

    for path in sorted(JOBS_DIR.glob("*/job.json")):
        job_id = path.parent.name
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors[job_id] = f"Could not read job metadata: {exc}"
            continue

        if not is_job_expired(job, now):
            continue

        try:
            delete_job_r2_objects(job_id)
            shutil.rmtree(path.parent, ignore_errors=True)
            output_dir = OUTPUT_DIR / job_id
            if output_dir.exists():
                shutil.rmtree(output_dir, ignore_errors=True)
            upload_preview = UPLOAD_DIR / f"{job_id}_preview.wav"
            upload_preview.unlink(missing_ok=True)
            with JOBS_LOCK:
                JOBS.pop(job_id, None)
            with PAYMENTS_LOCK:
                expired_sessions = [
                    session_id
                    for session_id, payment in PAYMENTS.items()
                    if payment.get("job_id") == job_id
                ]
                for session_id in expired_sessions:
                    PAYMENTS.pop(session_id, None)
                for job_ids in USER_PAYMENT_JOBS.values():
                    job_ids.discard(job_id)
                if expired_sessions:
                    _save_payments_to_disk()
            cleaned.append(job_id)
        except Exception as exc:
            errors[job_id] = str(exc)

    if cleaned or errors:
        print(
            f"[CLEANUP] expired_jobs cleaned={len(cleaned)} errors={len(errors)}",
            flush=True,
        )
    return {"cleaned": cleaned, "errors": errors, "retention_seconds": DOWNLOAD_HISTORY_RETENTION_SECONDS}


def compact_download_history_job(job: dict[str, Any]) -> dict[str, Any]:
    job_id = job.get("job_id")
    download_urls = job.get("download_urls") or {}
    stem_urls = (
        job.get("download_stem_urls")
        or job.get("full_stems")
        or download_urls.get("stems")
        or {}
    )
    zip_url = job.get("zip_url") or download_urls.get("zip")
    expires_at = job_history_expires_at(job)
    created_at = _safe_float(job.get("created_at"), time.time())
    ready_at = _safe_float(job.get("ready_at"), 0.0) or None

    return {
        "job_id": job_id,
        "original_filename": job.get("original_filename") or job.get("track_name") or "Audio file",
        "track_name": job.get("track_name") or Path(str(job.get("original_filename") or "Audio file")).stem,
        "output_format": job.get("requested_output_format") or job.get("output_format") or DEFAULT_OUTPUT_FORMAT,
        "source_duration_seconds": job.get("source_duration_seconds"),
        "created_at": created_at,
        "paid_at": _safe_float(job.get("paid_at"), 0.0) or None,
        "ready_at": ready_at,
        "expires_at": expires_at,
        "seconds_remaining": seconds_until_expiry(job),
        "payment_status": job.get("payment_status"),
        "full_processing_status": job.get("full_processing_status"),
        "zip_url": zip_url or (f"/api/download/{job_id}/zip" if job_id else None),
        "stem_urls": stem_urls,
        "download_urls": {
            "zip": zip_url or (f"/api/download/{job_id}/zip" if job_id else None),
            "stems": stem_urls,
        },
    }


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
    if stripe_key_mode() == "live" and not STRIPE_ALLOW_LIVE_PAYMENTS:
        logger.error(
            "Checkout rejected because live Stripe key is configured but STRIPE_ALLOW_LIVE_PAYMENTS is not true."
        )
        raise HTTPException(
            503,
            "Live Stripe payments are disabled on this server. Set STRIPE_ALLOW_LIVE_PAYMENTS=true only when you intentionally want real charges.",
        )
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

    # ── FIX 2: Also check disk-persisted job payment_status so that a server
    # restart doesn't revoke access to already-paid downloads. ─────────────────
    if job_json_path(job_id).is_file():
        try:
            disk_job = load_job_json(job_id)
            if (
                disk_job.get("payment_status") == "paid"
                and disk_job.get("user_uid") == user["uid"]
            ):
                return True
        except Exception:
            pass
    # ──────────────────────────────────────────────────────────────────────────

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
    if job.get("status") == "done" and job.get("preview_status") == "ready":
        return

    raise HTTPException(
        409,
        {
            "message": "Preview stems are not ready yet.",
            "status": job.get("status"),
            "preview_status": job.get("preview_status"),
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
        "-threads",
        "0",        # use all available CPU threads for decoding
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


def _convert_single_wav(
    ffmpeg: str,
    wav: Path,
    converted: Path,
    format_config: dict,
    output_format: str,
    job_id: str,
    output_scope: str,
) -> Path:
    """Convert a single WAV to the target format. Runs in a thread."""
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
    return converted


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

    # Build (wav, output_path) pairs in original order
    pairs = [
        (wav, converted_dir / f"{wav.stem}{format_config['extension']}")
        for wav in wavs
    ]

    converted_files: list[Path] = [Path()] * len(pairs)  # preserve order

    with ThreadPoolExecutor(max_workers=len(pairs)) as executor:
        future_to_index = {
            executor.submit(
                _convert_single_wav,
                ffmpeg, wav, converted, format_config, output_format, job_id, output_scope,
            ): i
            for i, (wav, converted) in enumerate(pairs)
        }
        for future in as_completed(future_to_index):
            i = future_to_index[future]
            converted_files[i] = future.result()  # raises on error

    return converted_files


def _convert_preview_wav_to_mp3(
    ffmpeg: str,
    wav: Path,
    converted: Path,
    job_id: str,
) -> tuple[Path, dict]:
    """Convert a single preview WAV to MP3. Runs in a thread."""
    print(f"[JOB {job_id}] FFmpeg preview conversion starting: {wav.name}", flush=True)
    cmd = [
        ffmpeg, "-y", "-i", str(wav),
        "-codec:a", "libmp3lame", "-b:a", "192k",
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
    meta = {
        "path": str(converted),
        "input_wav_path": str(wav),
        "size_bytes": size,
        "duration_seconds": duration,
        "ffmpeg_return_code": proc.returncode,
        "media_type": "audio/mpeg",
    }
    print(f"[JOB {job_id}] {wav.name} -> MP3 done (rc={proc.returncode}, dur={duration:.2f}s, size={size})", flush=True)
    if validation_error:
        raise RuntimeError(validation_error)
    return converted, meta


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

    pairs = [(wav, converted_dir / f"{wav.stem}.mp3") for wav in wavs]
    converted_files: list[Path] = [Path()] * len(pairs)
    preview_metadata: dict[str, dict[str, float | int | str]] = {}

    with ThreadPoolExecutor(max_workers=len(pairs)) as executor:
        future_to_index = {
            executor.submit(_convert_preview_wav_to_mp3, ffmpeg, wav, converted, job_id): i
            for i, (wav, converted) in enumerate(pairs)
        }
        for future in as_completed(future_to_index):
            i = future_to_index[future]
            converted_path, meta = future.result()  # raises on error
            converted_files[i] = converted_path
            preview_metadata[public_stem_name(converted_path)] = meta

    audio_logger.info(
        "[JOB %s] Parallel MP3 preview conversion complete: %s",
        job_id,
        [str(p) for p in converted_files],
    )
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


def paid_full_quality_for_job(job: dict[str, Any]) -> str:
    # Paid full downloads are latency-sensitive and currently only support two stems.
    # Force the fastest configured two-stem path even if a preview requested high quality.
    return "fast"


def full_demucs_model_for_stems(stems: int) -> str:
    return FULL_STEM_MODELS[stems]


def demucs_settings_for_quality(stems: int, quality: str = "fast") -> dict[str, Any]:
    settings = QUALITY_SETTINGS[normalize_quality(quality)]
    return {
        "model": settings["models"][stems],
        "shifts": settings["shifts"],
        "overlap": settings["overlap"],
        "segment_seconds": settings["segment_seconds"],
        "jobs": settings["jobs"],
        "device": settings["device"],
        "stem_count": stems,
    }


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

    # Run duration and RMS checks in parallel
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_duration = executor.submit(audio_duration_seconds, path)
        f_rms = executor.submit(audio_rms_db, path)
        duration = f_duration.result()
        rms_db = f_rms.result()

    if duration <= 0:
        raise RuntimeError(f"{label} has no playable duration: {path}")

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


def paid_download_urls_for_job(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    stem_paths = job.get("full_stem_paths") or {}
    stem_urls = {
        public_name: f"/api/download/{job_id}/stem/{Path(path).name}"
        for public_name, path in stem_paths.items()
        if path
    }
    return {"zip": f"/api/download/{job_id}/zip", "stems": stem_urls}


def unlock_ready_paid_assets(job_id: str, job: dict[str, Any]) -> dict[str, Any] | None:
    zip_path = Path(job["zip_path"]) if job.get("zip_path") else None
    zip_exists = bool(zip_path and zip_path.is_file())
    stem_paths = {
        name: Path(path)
        for name, path in (job.get("full_stem_paths") or {}).items()
        if path
    }
    stems_exist = bool(stem_paths) and all(
        path.is_file() for path in stem_paths.values()
    )
    if not (zip_exists and stems_exist):
        return None

    download_urls = paid_download_urls_for_job(job_id, job)
    updates = {
        "status": "done",
        "status_detail": "Payment verified. Full paid stems and ZIP are ready.",
        "full_processing_status": "ready",
        "zip_url": download_urls["zip"],
        "full_stems": download_urls["stems"],
        "download_stem_urls": download_urls["stems"],
        "download_urls": download_urls,
    }
    update_job(job_id, **updates)
    return {**job, **updates}


def create_zip(zip_path: Path, stem_files: list[Path]) -> Path:
    # Audio files (MP3, AAC, OGG, FLAC) are already compressed — using
    # ZIP_DEFLATED burns CPU for zero size benefit.  ZIP_STORED is instant.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for stem_file in stem_files:
            zf.write(stem_file, arcname=stem_file.name)
    return zip_path


def get_recorded_path(job: dict[str, Any], key: str, public_name: str) -> Path | None:
    recorded = (job.get(key) or {}).get(public_name)
    return Path(recorded) if recorded else None


def preview_file_candidates(
    job_id: str, filename: str, job: dict[str, Any]
) -> list[Path]:
    public_name = public_stem_name(Path(filename))
    candidates: list[Path] = []
    recorded_path = get_recorded_path(job, "preview_stem_paths", public_name)
    if recorded_path:
        candidates.append(recorded_path)
    candidates.extend(
        [
            job_dir_for(job_id) / "preview" / "mp3" / filename,
            OUTPUT_DIR / job_id / "preview" / "mp3" / filename,
        ]
    )
    for base_dir in (job_dir_for(job_id), OUTPUT_DIR / job_id):
        if base_dir.is_dir():
            candidates.extend(
                sorted(path for path in base_dir.rglob(filename) if path.is_file())
            )

    unique_candidates: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            unique_candidates.append(path)
            seen.add(key)
    return unique_candidates


def preview_files_debug(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    preview_urls = job.get("preview_urls") or job.get("stem_urls") or {}
    files: dict[str, Any] = {}
    for name, url in preview_urls.items():
        filename = Path(str(urlparse(str(url)).path)).name
        candidates = preview_file_candidates(job_id, filename, job)
        files[name] = {
            "url": url,
            "filename": filename,
            "physical_paths": [str(path) for path in candidates],
            "exists": any(path.is_file() for path in candidates),
        }
    return files


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
    """Validate all audio files in parallel for speed."""
    if not paths:
        return
    with ThreadPoolExecutor(max_workers=len(paths)) as executor:
        futures = {
            executor.submit(validate_audio_file, path, f"{label} {path.name}"): path
            for path in paths
        }
        for future in as_completed(futures):
            future.result()  # re-raises any validation error


def public_stem_name(path: Path) -> str:
    if path.stem == "no_vocals":
        return "instrumental"
    return path.stem


def preview_debug_payload(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    preview_file_info = job.get("preview_file_info") or {}
    preview_stem_paths = job.get("preview_stem_paths") or {}
    mp3_paths = {
        name: info.get("path")
        for name, info in preview_file_info.items()
        if isinstance(info, dict)
    } or preview_stem_paths
    sizes_bytes = {
        name: info.get("size_bytes")
        for name, info in preview_file_info.items()
        if isinstance(info, dict)
    }
    durations_seconds = job.get("preview_durations_seconds") or {
        name: info.get("duration_seconds")
        for name, info in preview_file_info.items()
        if isinstance(info, dict)
    }
    media_types = {
        name: info.get("media_type", "audio/mpeg")
        for name, info in preview_file_info.items()
        if isinstance(info, dict)
    }
    return {
        "job_id": job_id,
        "preview_status": job.get("preview_status"),
        "preview_urls": job.get("preview_urls") or job.get("stem_urls") or {},
        "mp3_paths": mp3_paths,
        "mp3_file_sizes": sizes_bytes,
        "sizes_bytes": sizes_bytes,
        "ffprobe_durations": durations_seconds,
        "durations_seconds": durations_seconds,
        "media_type": media_types,
        "preview_files": preview_files_debug(job_id, job),
    }


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
            "preview_demucs_model": PREVIEW_DEMUCS_MODEL,
            "full_demucs_model": FULL_DEMUCS_MODEL,
            "demucs_shifts": DEMUCS_SHIFTS,
            "demucs_overlap": DEMUCS_OVERLAP,
            "demucs_segment_seconds": DEMUCS_SEGMENT_SECONDS,
            "demucs_jobs": DEMUCS_JOBS,
            "demucs_device": DEMUCS_DEVICE or "auto",
            "demucs_cpu_threads": DEMUCS_CPU_THREADS,
            "demucs_concurrency": DEMUCS_CONCURRENCY,
        },
        "download_history": {
            "retention_seconds": DOWNLOAD_HISTORY_RETENTION_SECONDS,
            "retention_hours": round(DOWNLOAD_HISTORY_RETENTION_SECONDS / 3600, 2),
        },
        "payments": {
            "enabled": PAYMENTS_ENABLED,
            "price_per_song_cents": PRICE_PER_SONG_CENTS,
            "currency": PAYMENT_CURRENCY,
            "test_mode": PAYMENT_TEST_MODE,
            "stripe_key_mode": stripe_key_mode(),
            "live_payments_allowed": STRIPE_ALLOW_LIVE_PAYMENTS,
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
        "download_history_retention_seconds": DOWNLOAD_HISTORY_RETENTION_SECONDS,
        "r2_enabled": R2_ENABLED,
    }


def warm_demucs_models() -> None:
    for model_name in dict.fromkeys(DEMUCS_WARM_MODELS):
        if not model_name:
            continue
        try:
            from demucs.pretrained import get_model

            started_at = time.monotonic()
            get_model(model_name)
            audio_logger.info(
                "Warmed Demucs model model=%s seconds=%.3f",
                model_name,
                time.monotonic() - started_at,
            )
        except Exception as exc:
            audio_logger.warning(
                "Unable to warm Demucs model model=%s error=%s", model_name, exc
            )


def start_demucs_model_warmup() -> None:
    threading.Thread(target=warm_demucs_models, daemon=True).start()


@app.on_event("startup")
async def startup_cleanup_expired_jobs() -> None:
    # Best-effort cleanup so old jobs and R2 files do not accumulate after deploy/restart.
    start_demucs_model_warmup()
    try:
        cleanup_expired_jobs()
    except Exception as exc:
        print(f"[CLEANUP] startup cleanup failed: {exc}", flush=True)


@app.get("/api/my-jobs")
async def my_jobs(user: Annotated[dict[str, Any], Depends(current_user)]):
    # Opportunistic cleanup keeps history and R2 storage close to the 24-hour policy
    # even without a separate cron job.
    cleanup_expired_jobs()

    jobs: list[dict[str, Any]] = []
    now = time.time()
    for path in sorted(JOBS_DIR.glob("*/job.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if job.get("user_uid") != user["uid"]:
            continue
        if job.get("payment_status") != "paid":
            continue
        if job.get("full_processing_status") != "ready":
            continue
        if is_job_expired(job, now):
            continue

        # Persist a computed expires_at for older jobs that do not have it yet.
        if not job.get("expires_at"):
            try:
                update_job(job["job_id"], expires_at=job_history_expires_at(job))
                job["expires_at"] = job_history_expires_at(job)
            except Exception:
                pass
        jobs.append(compact_download_history_job(job))

    jobs.sort(key=lambda item: item.get("ready_at") or item.get("paid_at") or item.get("created_at") or 0, reverse=True)
    return JSONResponse(
        {
            "jobs": jobs,
            "retention_seconds": DOWNLOAD_HISTORY_RETENTION_SECONDS,
            "retention_hours": round(DOWNLOAD_HISTORY_RETENTION_SECONDS / 3600, 2),
        }
    )


@app.post("/api/admin/cleanup-expired-jobs")
async def admin_cleanup_expired_jobs(request: Request):
    expected_secret = os.getenv("CLEANUP_SECRET", "").strip()
    supplied_secret = request.headers.get("x-cleanup-secret", "").strip()
    if not expected_secret or not hmac.compare_digest(expected_secret, supplied_secret):
        raise HTTPException(403, "Forbidden")
    return cleanup_expired_jobs()


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
        "test_mode": PAYMENT_TEST_MODE,
        "stripe_key_mode": stripe_key_mode(),
        "live_payments_allowed": STRIPE_ALLOW_LIVE_PAYMENTS,
    }



def create_local_test_payment(job: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """Mark a job paid without contacting Stripe. Use only for development/testing."""
    job_id = job["job_id"]
    session_id = f"cs_test_local_{job_id}_{int(time.time())}"
    payment = {
        "checkout_session_id": session_id,
        "payment_key": payment_key(job_id, "song", None),
        "job_id": job_id,
        "item_type": "song",
        "filename": None,
        "user_uid": user["uid"],
        "user_email": user["email"],
        "status": "paid",
        "stripe_payment_status": "paid",
        "stripe_verification_source": "local_test_mode",
        "stripe_api_verified": True,
        "amount_cents": PRICE_PER_SONG_CENTS,
        "currency": PAYMENT_CURRENCY,
    }
    with PAYMENTS_LOCK:
        PAYMENTS[session_id] = payment.copy()
        USER_PAYMENT_JOBS.setdefault(user["uid"], set()).add(job_id)
        _save_payments_to_disk()
    paid_at = time.time()
    update_job(
        job_id,
        paid=True,
        payment_status="paid",
        paid_at=paid_at,
        expires_at=paid_at + DOWNLOAD_HISTORY_RETENTION_SECONDS,
        checkout_session_id=session_id,
        paid_checkout_session_id=session_id,
        status_detail="Test payment approved. Preparing full downloads in the background.",
    )
    start_paid_assets_generation(job_id)
    return payment

@app.post("/api/payments/checkout")
@app.post("/api/create-checkout-session")
async def create_checkout_session(
    payload: CheckoutRequest,
    user: Annotated[dict[str, Any], Depends(current_user)],
):
    job = get_authorized_job(payload.job_id, user)

    if PAYMENT_TEST_MODE:
        if job.get("preview_status") != "ready" or job.get("status") != "done":
            raise HTTPException(
                409,
                {
                    "message": "Preview stems are not ready yet.",
                    "preview_status": job.get("preview_status"),
                    "status_detail": job.get("status_detail"),
                },
            )
        payment = create_local_test_payment(job, user)
        checkout_url = checkout_success_url(payload.job_id, payload.return_path).replace(
            "{CHECKOUT_SESSION_ID}", payment["checkout_session_id"]
        )
        return {
            "checkout_session_id": payment["checkout_session_id"],
            "checkout_url": checkout_url,
            "test_mode": True,
            "message": "Test payment approved. No real Stripe charge was created.",
        }

    validate_checkout_configuration()

    if job.get("preview_status") != "ready" or job.get("status") != "done":
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
        _save_payments_to_disk()  # FIX 1: persist pending payment immediately
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
    # ── FIX 3: was hmac.new() — correct call is hmac.new() via HMAC constructor
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
        _save_payments_to_disk()  # FIX 1: persist paid status to disk immediately
    paid_at = time.time()
    paid_job_updates = {
        "paid": True,
        "payment_status": "paid",
        "paid_at": paid_at,
        "expires_at": paid_at + DOWNLOAD_HISTORY_RETENTION_SECONDS,
        "paid_checkout_session_id": session_id,
        "checkout_session_id": session_id,
        "stripe_payment_intent": session.get("payment_intent"),
        "updated_at": time.time(),
    }
    should_start_paid_assets = False
    with JOBS_LOCK:
        if job_id in JOBS and JOBS[job_id].get("user_uid") == user_uid:
            JOBS[job_id].update(paid_job_updates)
            should_start_paid_assets = (
                not JOBS[job_id].get("zip_url")
                and JOBS[job_id].get("full_processing_status") != "processing"
            )
    if job_id in JOBS:
        with JOBS_LOCK:
            memory_job = JOBS.get(job_id, {}).copy()
        if memory_job.get("user_uid") == user_uid:
            unlocked_job = unlock_ready_paid_assets(job_id, memory_job)
            if unlocked_job:
                with JOBS_LOCK:
                    if job_id in JOBS:
                        JOBS[job_id].update(unlocked_job)
                should_start_paid_assets = False
    if job_json_path(job_id).is_file():
        disk_job = load_job_json(job_id)
        if disk_job.get("user_uid") == user_uid:
            disk_job.update(paid_job_updates)
            unlocked_job = unlock_ready_paid_assets(job_id, disk_job)
            if unlocked_job:
                disk_job = unlocked_job
            should_start_paid_assets = should_start_paid_assets or (
                not disk_job.get("zip_url")
                and disk_job.get("full_processing_status") != "processing"
            )
            update_job(job_id, **paid_job_updates)
    latest_job = (
        get_public_job(job_id)
        if job_json_path(job_id).is_file() or job_id in JOBS
        else {}
    )
    zip_path = Path(latest_job["zip_path"]) if latest_job.get("zip_path") else None
    audio_logger.info(
        "Payment confirmed for job_id=%s full_processing_status=%s zip_path_exists=%s",
        job_id,
        latest_job.get("full_processing_status"),
        bool(zip_path and zip_path.is_file()),
    )
    if should_start_paid_assets:
        start_paid_assets_generation(job_id)
    with PAYMENTS_LOCK:
        return payment.copy()


def _r2_upload_full_outputs(job_id: str, job_dir: Path) -> None:
    """Upload all full stem files and zip to R2 after processing."""
    if not R2_ENABLED:
        return
    full_dir = job_dir / "full"
    if full_dir.is_dir():
        for f in full_dir.rglob("*"):
            if f.is_file():
                r2_upload_file(f, r2_key_for_full_stem(job_id, f.name))
    # Upload zip
    for zip_file in job_dir.glob("*.zip"):
        r2_upload_file(zip_file, r2_key_for_zip(job_id, zip_file.name))


def full_processing_is_stale(job: dict[str, Any]) -> bool:
    if job.get("full_processing_status") != "processing":
        return False
    started_at = _safe_float(job.get("full_processing_started_at"), 0.0)
    # Older deployed jobs may not have full_processing_started_at. Fall back to
    # updated_at/paid_at/created_at so they cannot spin forever.
    if started_at <= 0:
        started_at = (
            _safe_float(job.get("updated_at"), 0.0)
            or _safe_float(job.get("paid_at"), 0.0)
            or _safe_float(job.get("created_at"), 0.0)
        )
    return bool(started_at and (time.time() - started_at) > FULL_PROCESSING_STALE_SECONDS)


def recover_or_restart_paid_processing(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """Keep paid jobs from spinning forever.

    If a paid job has ready files, unlock it. If it is queued/not_started, start
    the worker. If it has been processing longer than the configured stale window,
    reset and restart once so stale lock files/Railway worker death do not trap the
    UI in a permanent spinner.
    """
    unlocked = unlock_ready_paid_assets(job_id, job)
    if unlocked:
        return serialize_job(unlocked)

    if job.get("payment_status") != "paid":
        return job

    status = job.get("full_processing_status")
    if status in {None, "", "not_started"}:
        start_paid_assets_generation(job_id)
        return get_public_job(job_id)

    if status == "processing" and full_processing_is_stale(job):
        restart_count = int(_safe_float(job.get("full_processing_restart_count"), 0))
        if restart_count >= FULL_PROCESSING_MAX_RESTARTS:
            audio_logger.error(
                "Full processing stale after max restarts; marking failed job_id=%s restart_count=%s stale_seconds=%s",
                job_id,
                restart_count,
                FULL_PROCESSING_STALE_SECONDS,
            )
            stale_lock = job_dir_for(job_id) / "full_processing.lock"
            stale_lock.unlink(missing_ok=True)
            with PAID_ASSET_LOCK:
                event = PAID_ASSET_EVENTS.pop(job_id, None)
                if event is not None:
                    event.set()
            update_job(
                job_id,
                full_processing_status="failed",
                full_processing_error=(
                    f"Full processing stayed stuck for more than {FULL_PROCESSING_STALE_SECONDS} seconds after payment. Please retry with a shorter file or contact support."
                ),
                status_detail="Full processing failed instead of spinning forever.",
            )
            return get_public_job(job_id)

        audio_logger.warning(
            "Full processing stale; restarting job_id=%s restart_count=%s stale_seconds=%s",
            job_id,
            restart_count + 1,
            FULL_PROCESSING_STALE_SECONDS,
        )
        stale_lock = job_dir_for(job_id) / "full_processing.lock"
        stale_lock.unlink(missing_ok=True)
        with PAID_ASSET_LOCK:
            event = PAID_ASSET_EVENTS.pop(job_id, None)
            if event is not None:
                event.set()
        update_job(
            job_id,
            full_processing_status="not_started",
            full_processing_error=None,
            error=None,
            full_processing_restart_count=restart_count + 1,
            status_detail="Full processing was stuck and has been restarted.",
        )
        start_paid_assets_generation(job_id)
        return get_public_job(job_id)

    return job

def start_paid_assets_generation(job_id: str) -> None:
    # Idempotent starter: do not launch duplicate workers in the same process,
    # but allow a later request to restart if the previous worker already ended
    # and removed its event in the finally block.
    with PAID_ASSET_LOCK:
        existing_event = PAID_ASSET_EVENTS.get(job_id)
        if existing_event is not None and not existing_event.is_set():
            audio_logger.info("Full processing worker already active job_id=%s", job_id)
            return
        PAID_ASSET_EVENTS[job_id] = threading.Event()

    update_job(
        job_id,
        full_processing_status="processing",
        full_processing_started_at=time.time(),
        status_detail="Payment verified. Full downloads are queued for processing.",
    )

    thread = threading.Thread(
        target=_paid_assets_worker,
        args=(job_id,),
        daemon=True,
    )
    thread.start()


def _paid_assets_worker(job_id: str) -> None:
    try:
        ensure_paid_assets_ready(
            job_id, owns_processing=True, allow_unpaid_processing=True
        )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        update_job(
            job_id,
            full_processing_status="failed",
            status_detail="Full stem generation failed.",
            full_processing_error=str(detail),
            error=str(detail),
        )
        audio_logger.exception(
            "Paid full stem generation failed job_id=%s error=%s", job_id, exc
        )


def log_preview_response(status_code: int) -> None:
    audio_logger.info("Preview response status_code=%s", status_code)


def ensure_paid_assets_ready(
    job_id: str,
    owns_processing: bool = False,
    allow_unpaid_processing: bool = False,
) -> dict[str, Any]:
    try:
        job = get_public_job(job_id)
    except HTTPException as exc:
        log_preview_response(exc.status_code)
        raise
    unlocked_job = unlock_ready_paid_assets(job_id, job)
    if unlocked_job:
        return serialize_job(unlocked_job)
    if job.get("zip_url"):
        return job
    if not allow_unpaid_processing and job.get("payment_status") != "paid":
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
            lock_age = time.time() - lock_path.stat().st_mtime if lock_path.exists() else 0
            if lock_age > FULL_PROCESSING_STALE_SECONDS:
                audio_logger.warning(
                    "Removing stale full_processing.lock job_id=%s lock_age=%.1f",
                    job_id,
                    lock_age,
                )
                lock_path.unlink(missing_ok=True)
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                owns_lock = True
            else:
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
                if latest_job.get("full_processing_status") == "failed":
                    raise HTTPException(
                        500,
                        latest_job.get("full_processing_error")
                        or latest_job.get("error")
                        or "Full processing failed.",
                    )
            update_job(
                job_id,
                status_detail="Full stem separation timed out while waiting for the active worker.",
                full_processing_status="failed",
                full_processing_error=(
                    f"Timed out after {DEMUCS_TIMEOUT_SECONDS} seconds waiting for full processing."
                ),
            )
            raise HTTPException(409, "Full stems are still processing.")

        original_input_path = job.get("original_input_path")
        if not original_input_path:
            raise HTTPException(
                409, "Original upload is not available for full processing."
            )

        input_path = Path(original_input_path)
        if not input_path.is_file():
            # Try to restore from R2
            if R2_ENABLED:
                r2_key = r2_key_for_input(job_id, Path(original_input_path).name)
                print(f"[R2] Local file missing, restoring from R2: {r2_key}", flush=True)
                restored = r2_download_file(r2_key, input_path)
                if not restored or not input_path.is_file():
                    raise HTTPException(410, "Original upload is no longer available.")
                print(f"[R2] Restored input file: {input_path}", flush=True)
            else:
                raise HTTPException(410, "Original upload is no longer available.")

        job_dir = job_dir_for(job_id)
        stems = int(job.get("requested_stems") or 2)
        track_name = job.get("track_name") or "track"
        output_format = job.get("output_format") or DEFAULT_OUTPUT_FORMAT
        quality = paid_full_quality_for_job(job)
        source_duration = job.get("source_duration_seconds") or audio_duration_seconds(
            input_path
        )

        settings = demucs_settings_for_quality(stems, quality)
        update_job(
            job_id,
            status_detail=f"Payment verified. Separating the full track with {settings['model']} ({quality} quality).",
            full_processing_status="processing",
            full_processing_started_at=time.time(),
        )
        full_cmd = build_full_demucs_command(job_dir, input_path, stems)
        env = demucs_subprocess_env()
        full_demucs_dir = full_demucs_output_dir(job_dir, input_path, stems)
        processing_started_at = time.monotonic()
        audio_logger.info(
            "Paid full processing started job_id=%s input_duration_seconds=%.3f demucs_model=%s segment=%s shifts=%s overlap=%s stem_count=%s output_format=%s command=%s output_dir=%s input_path=%s input_size_bytes=%s",
            job_id,
            float(source_duration or 0),
            settings["model"],
            settings["segment_seconds"],
            settings["shifts"],
            settings["overlap"],
            settings["stem_count"],
            output_format,
            " ".join(full_cmd),
            full_demucs_dir,
            input_path,
            input_path.stat().st_size,
        )
        with DEMUCS_SEMAPHORE:
            # ── Try Modal GPU first, fall back to local CPU ───────────────────
            full_settings = QUALITY_SETTINGS["fast"]
            full_model = full_demucs_model_for_stems(stems)
            modal_success = run_demucs_modal(
                input_path=input_path,
                model=full_model,
                shifts=full_settings["shifts"],
                overlap=full_settings["overlap"],
                segment=full_settings["segment_seconds"],
                two_stems=(stems == 2),
                output_dir=job_dir,
                timeout=DEMUCS_TIMEOUT_SECONDS,
            )
            if modal_success:
                print(f"[JOB {job_id[:8]}] Modal GPU full separation complete", flush=True)
                proc_returncode = 0
                proc_stdout = ""
                proc_stderr = ""
            else:
                proc = subprocess.run(
                    full_cmd,
                    capture_output=True,
                    text=True,
                    timeout=DEMUCS_TIMEOUT_SECONDS,
                    env=env,
                )
                proc_returncode = proc.returncode
                proc_stdout = proc.stdout
                proc_stderr = proc.stderr
            # ─────────────────────────────────────────────────────────────────

        print(f"[JOB {job_id[:8]}] FULL STDOUT:\n{proc_stdout[-2000:]}", flush=True)
        print(f"[JOB {job_id[:8]}] FULL STDERR:\n{proc_stderr[-2000:]}", flush=True)
        print(f"[JOB {job_id[:8]}] FULL RETURN CODE: {proc_returncode}", flush=True)
        if proc_returncode != 0:
            err = proc_stderr + proc_stdout
            update_job(
                job_id,
                status_detail="Full stem separation failed after payment verification.",
                full_processing_status="failed",
                full_processing_error=f"Demucs error: {err[-400:]}",
            )
            raise HTTPException(500, "Full stem separation failed.")

        full_wavs = expected_full_demucs_stem_files(job_dir, input_path, stems)
        missing_full_wavs = [path for path in full_wavs if not path.is_file()]
        if missing_full_wavs:
            update_job(
                job_id,
                status_detail="Full stem separation finished without the expected stem files.",
                full_processing_status="failed",
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

        if R2_ENABLED:
            try:
                for full_stem_file in full_output_files:
                    r2_upload_file(
                        full_stem_file,
                        r2_key_for_full_stem(job_id, full_stem_file.name),
                    )
                r2_upload_file(zip_path, r2_key_for_zip(job_id, zip_path.name))
            except Exception as _r2_exc:
                print(
                    f"[R2] Final paid asset upload failed (non-fatal): {_r2_exc}",
                    flush=True,
                )

        total_processing_seconds = time.monotonic() - processing_started_at
        audio_logger.info(
            "Paid full processing completed job_id=%s input_duration_seconds=%.3f demucs_model=%s segment=%s shifts=%s overlap=%s stem_count=%s output_format=%s total_processing_seconds=%.3f",
            job_id,
            float(source_duration or 0),
            settings["model"],
            settings["segment_seconds"],
            settings["shifts"],
            settings["overlap"],
            settings["stem_count"],
            output_format,
            total_processing_seconds,
        )
        ready_at = time.time()
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
            ready_at=ready_at,
            expires_at=ready_at + DOWNLOAD_HISTORY_RETENTION_SECONDS,
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
    except subprocess.TimeoutExpired as exc:
        update_job(
            job_id,
            status_detail="Full stem separation timed out.",
            full_processing_status="failed",
            full_processing_error=(
                f"Timed out after {DEMUCS_TIMEOUT_SECONDS} seconds while preparing full downloads."
            ),
        )
        raise HTTPException(500, "Full stem separation timed out.") from exc
    except Exception as exc:
        if not isinstance(exc, HTTPException):
            update_job(
                job_id,
                status_detail="Full stem separation failed.",
                full_processing_status="failed",
                full_processing_error=str(exc),
            )
        raise
    finally:
        if "owns_lock" in locals() and owns_lock:
            lock_path.unlink(missing_ok=True)
        if owns_processing:
            with PAID_ASSET_LOCK:
                event = PAID_ASSET_EVENTS.pop(job_id, None)
                if event is not None:
                    event.set()


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
        job = get_public_job(payment["job_id"])
        if job.get("user_uid") != user["uid"]:
            raise HTTPException(404, "Job not found")
        if job.get("full_processing_status") not in {"ready", "processing"}:
            start_paid_assets_generation(payment["job_id"])
            job = get_public_job(payment["job_id"])
        response.update(
            {
                "zip_url": job.get("zip_url") if job.get("full_processing_status") == "ready" else None,
                "stem_urls": job.get("download_stem_urls", {}) if job.get("full_processing_status") == "ready" else {},
                "download_urls": job.get(
                    "download_urls",
                    {
                        "zip": job.get("zip_url"),
                        "stems": job.get("download_stem_urls", {}),
                    },
                ) if job.get("full_processing_status") == "ready" else {"zip": None, "stems": {}},
                "full_processing_status": job.get("full_processing_status"),
                "job_status_detail": job.get("status_detail"),
            }
        )
    return response


def verify_checkout_payment(
    checkout_session_id: str, job_id: str | None, user: dict[str, Any]
) -> dict[str, Any]:
    with PAYMENTS_LOCK:
        payment = PAYMENTS.get(checkout_session_id)

    if checkout_session_id.startswith("cs_test_local_"):
        if not payment:
            raise HTTPException(404, "Test payment not found.")
        if payment.get("user_uid") != user["uid"]:
            raise HTTPException(404, "Payment not found.")
        if job_id and payment.get("job_id") != job_id:
            raise HTTPException(400, "Checkout Session does not belong to this job.")
        return payment_verification_response(payment, user)

    validate_checkout_configuration()


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
    # Upload original input to R2 for persistent storage
    if R2_ENABLED:
        r2_upload_file(input_path, r2_key_for_input(job_id, original_name))

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
        "expires_at": now + DOWNLOAD_HISTORY_RETENTION_SECONDS,
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



def build_full_demucs_command(job_dir: Path, input_audio_path: Path, stems: int) -> list[str]:
    # Full processing uses its own model setting so preview speed cannot be affected
    # by production full-quality model choices.
    settings = QUALITY_SETTINGS["fast"].copy()
    model = full_demucs_model_for_stems(stems)
    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        model,
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


def full_demucs_output_dir(job_dir: Path, input_audio_path: Path, stems: int) -> Path:
    return job_dir / full_demucs_model_for_stems(stems) / input_audio_path.stem


def expected_full_demucs_stem_files(job_dir: Path, input_audio_path: Path, stems: int) -> list[Path]:
    output_dir = full_demucs_output_dir(job_dir, input_audio_path, stems)
    if stems == 2:
        return [output_dir / "vocals.wav", output_dir / "no_vocals.wav"]
    return sorted(output_dir.glob("*.wav"))

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
    else:
        # Remove any inherited thread pins so PyTorch/OMP can use all available cores
        for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "TORCH_NUM_THREADS"):
            env.pop(key, None)
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

            # ── Try Modal GPU first, fall back to local CPU ───────────────────
            settings = demucs_settings_for_quality(stems, quality)
            modal_success = run_demucs_modal(
                input_path=preview_path,
                model=demucs_model_for_quality(stems, quality),
                shifts=settings["shifts"],
                overlap=settings["overlap"],
                segment=settings["segment_seconds"],
                two_stems=(stems == 2),
                output_dir=job_dir,
                timeout=PREVIEW_DEMUCS_TIMEOUT_SECONDS,
            )
            if modal_success:
                print(f"[JOB {job_id[:8]}] Modal GPU separation complete", flush=True)
                proc_returncode = 0
                proc_stdout = ""
                proc_stderr = ""
            else:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=PREVIEW_DEMUCS_TIMEOUT_SECONDS,
                    env=env,
                )
                proc_returncode = proc.returncode
                proc_stdout = proc.stdout
                proc_stderr = proc.stderr
            # ─────────────────────────────────────────────────────────────────

        print(f"[JOB {job_id[:8]}] STDOUT:\n{proc_stdout[-2000:]}", flush=True)
        print(f"[JOB {job_id[:8]}] STDERR:\n{proc_stderr[-2000:]}", flush=True)
        print(f"[JOB {job_id[:8]}] RETURN CODE: {proc_returncode}", flush=True)

        if proc_returncode != 0:
            err = proc_stderr + proc_stdout
            print(f"[JOB {job_id[:8]}] FAILED:\n{err[-800:]}")
            update_job(
                job_id,
                status="error",
                preview_status="failed",
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
                preview_status="failed",
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
        invalid_preview_files = [
            path
            for path in preview_output_files
            if path.suffix.lower() != ".mp3"
            or preview_file_info.get(public_stem_name(path), {}).get(
                "duration_seconds", 0
            )
            <= 0
            or preview_file_info.get(public_stem_name(path), {}).get("size_bytes", 0)
            <= 0
        ]
        if invalid_preview_files:
            update_job(
                job_id,
                status="error",
                preview_status="failed",
                status_detail="Preview MP3 validation failed.",
                error=(
                    "Invalid preview MP3 files: "
                    + ", ".join(str(path) for path in invalid_preview_files)
                ),
            )
            return
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
            status_detail=(f"{PREVIEW_DURATION_SECONDS}-second preview stems are ready. Pay to prepare full downloads." if not AUTO_START_FULL_AFTER_PREVIEW else f"{PREVIEW_DURATION_SECONDS}-second preview stems are ready. Full downloads are preparing in the background."),
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
        if AUTO_START_FULL_AFTER_PREVIEW:
            start_paid_assets_generation(job_id)

    except subprocess.TimeoutExpired:
        update_job(
            job_id,
            status="error",
            preview_status="failed",
            status_detail="Preview stem separation timed out.",
            error=f"Preview timed out after {PREVIEW_DEMUCS_TIMEOUT_SECONDS} seconds. Please try a shorter file or try again.",
        )
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            preview_status="failed",
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
    job = get_authorized_job(job_id, user)
    if job.get("payment_status") == "paid":
        job = recover_or_restart_paid_processing(job_id, job)
    response_job = dict(job)
    for key in ("preview_stems", "stem_urls", "preview_urls"):
        urls = response_job.get(key)
        if isinstance(urls, dict):
            response_job[key] = {
                name: url for name, url in urls.items() if str(url).endswith(".mp3")
            }

    # ── FIX 4: Read payment_status from disk job.json (survives restarts) so
    # we never wrongly zero-out download URLs for a paid job. ─────────────────
    effective_payment_status = response_job.get("payment_status")
    if effective_payment_status != "paid" and job_json_path(job_id).is_file():
        try:
            disk_job = load_job_json(job_id)
            if disk_job.get("payment_status") == "paid":
                effective_payment_status = "paid"
                # Sync the in-memory response with the disk truth
                response_job["payment_status"] = "paid"
                response_job["paid"] = True
        except Exception:
            pass
    # ──────────────────────────────────────────────────────────────────────────

    if effective_payment_status != "paid":
        response_job["zip_url"] = None
        response_job["download_stem_urls"] = {}
        response_job["download_urls"] = {"zip": None, "stems": {}}
        response_job["full_stems"] = {}
    zip_path = Path(job["zip_path"]) if job.get("zip_path") else None
    audio_logger.info(
        "/api/job/%s paid_download_urls_returned=%s zip_path_exists=%s",
        job_id,
        json.dumps(response_job.get("download_urls"), default=str),
        bool(zip_path and zip_path.is_file()),
    )
    audio_logger.info(
        "[PREVIEW] /api/job/%s response: %s",
        job_id,
        json.dumps(response_job, default=str),
    )
    return JSONResponse(
        {
            **response_job,
            "preview_debug": preview_debug_payload(job_id, job),
        }
    )


@app.get("/api/debug/job/{job_id}")
async def debug_job(
    job_id: str, user: Annotated[dict[str, Any], Depends(current_user)]
):
    job = get_authorized_job(job_id, user)
    return JSONResponse(preview_debug_payload(job_id, job))


@app.get("/api/preview/{job_id}/stem/{filename}")
async def preview_stem(job_id: str, filename: str):
    def log_preview_response(
        status_code: int, resolved_path: Path | None = None, exists: bool = False
    ) -> None:
        audio_logger.info(
            "[PREVIEW] requested job_id=%s filename=%s resolved_path=%s exists=%s response_code=%s",
            job_id,
            filename,
            resolved_path,
            exists,
            status_code,
        )
        print(f"[PREVIEW] requested: {filename}", flush=True)
        print(f"[PREVIEW] requested job_id: {job_id}", flush=True)
        print(f"[PREVIEW] resolved path: {resolved_path}", flush=True)
        print(f"[PREVIEW] exists: {str(exists).lower()}", flush=True)
        print(f"[PREVIEW] response code: {status_code}", flush=True)

    if not SAFE_DOWNLOAD_RE.fullmatch(filename):
        log_preview_response(400)
        raise HTTPException(400, "Invalid stem filename.")
    if not filename.lower().endswith(".mp3"):
        log_preview_response(404)
        raise HTTPException(404, "Preview stems are only available as MP3 files.")

    try:
        job = get_public_job(job_id)
    except HTTPException as exc:
        log_preview_response(exc.status_code)
        raise
    try:
        enforce_public_job_done(job)
    except HTTPException as exc:
        log_preview_response(exc.status_code)
        raise
    preview_urls = job.get("preview_urls") or job.get("stem_urls") or {}
    if f"/api/preview/{job_id}/stem/{filename}" not in preview_urls.values():
        log_preview_response(404)
        raise HTTPException(404, "Preview stem not found.")

    candidates = preview_file_candidates(job_id, filename, job)
    stem_path = next((path for path in candidates if path.is_file()), None)
    resolved_path = stem_path or (candidates[0] if candidates else None)
    exists = bool(stem_path and stem_path.is_file())
    if not stem_path:
        log_preview_response(404, resolved_path, exists)
        raise HTTPException(404, "Preview stem not found.")
    media_type = "audio/mpeg"
    audio_logger.info(
        "Serving preview stem job_id=%s filename=%s path=%s media_type=%s size_bytes=%s",
        job_id,
        filename,
        stem_path,
        media_type,
        stem_path.stat().st_size,
    )
    log_preview_response(200, stem_path, True)
    return FileResponse(
        stem_path,
        media_type=media_type,
        filename=filename,
        headers={"Accept-Ranges": "bytes"},
    )


@app.get("/api/download/{job_id}/zip")
async def download_zip(
    job_id: str, user: Annotated[dict[str, Any], Depends(current_user)]
):
    job = get_authorized_job(job_id, user)
    if is_job_expired(job):
        cleanup_expired_jobs()
        raise HTTPException(410, "This download has expired.")
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
        # Try R2
        if R2_ENABLED and zip_path:
            r2_key = r2_key_for_zip(job_id, zip_path.name)
            r2_download_file(r2_key, zip_path)
        if zip_path is None or not zip_path.is_file():
            # Try any zip in R2
            if R2_ENABLED:
                client = _get_r2_client()
                if client:
                    try:
                        resp = client.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=f"jobs/{job_id}/zip/")
                        objs = resp.get("Contents", [])
                        if objs:
                            r2_key = objs[0]["Key"]
                            fname = Path(r2_key).name
                            zip_path = job_dir_for(job_id) / fname
                            r2_download_file(r2_key, zip_path)
                    except Exception:
                        pass
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
    if is_job_expired(job):
        cleanup_expired_jobs()
        raise HTTPException(410, "This download has expired.")
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
        # Try R2
        if R2_ENABLED:
            r2_key = r2_key_for_full_stem(job_id, filename)
            candidate = job_dir_for(job_id) / "full" / filename
            if r2_download_file(r2_key, candidate):
                stem_path = candidate
    if stem_path is None:
        raise HTTPException(404, "Stem not found.")
    validate_paid_download_file(stem_path, job, "Stem")
    extension = stem_path.suffix.lower().lstrip(".")
    media_type = OUTPUT_FORMATS.get(extension, OUTPUT_FORMATS["wav"])["media_type"]
    return FileResponse(stem_path, media_type=media_type, filename=filename)


@app.delete("/api/cleanup/{job_id}")
async def cleanup(job_id: str, user: Annotated[dict[str, Any], Depends(current_user)]):
    get_authorized_job(job_id, user)
    delete_job_r2_objects(job_id)
    for path in (OUTPUT_DIR / job_id, JOBS_DIR / job_id):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    with PAYMENTS_LOCK:
        expired_sessions = [
            session_id
            for session_id, payment in PAYMENTS.items()
            if payment.get("job_id") == job_id
        ]
        for session_id in expired_sessions:
            PAYMENTS.pop(session_id, None)
        for job_ids in USER_PAYMENT_JOBS.values():
            job_ids.discard(job_id)
        if expired_sessions:
            _save_payments_to_disk()
    return {"status": "cleaned"}
