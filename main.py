import json
import os
import re
import secrets
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
from hashlib import pbkdf2_hmac
from pathlib import Path
from typing import Annotated, Any

import static_ffmpeg
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# Auto-configure ffmpeg. Prefer a system ffmpeg when available, and do not let
# static-ffmpeg download/network failures prevent the API from starting.
if shutil.which("ffmpeg"):
    print("[OK] ffmpeg ready")
else:
    try:
        static_ffmpeg.add_paths()
        print("[OK] ffmpeg ready")
    except Exception as exc:
        print(f"[WARN] ffmpeg auto-configuration failed: {exc}")

app = FastAPI(title="Stemify API", version="1.4.0")


def _split_csv_env(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


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
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}
PREVIEW_DURATION_SECONDS = int(os.getenv("PREVIEW_DURATION_SECONDS", "15"))
PRICE_PER_SONG_CENTS = int(os.getenv("PRICE_PER_SONG_CENTS", os.getenv("PRICE_PER_STEM_CENTS", "300")))
PAYMENTS_ENABLED = os.getenv("PAYMENTS_ENABLED", "false").lower() == "true"
PAYMENT_CURRENCY = os.getenv("PAYMENT_CURRENCY", "usd").lower()
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
SOCIAL_AUTH_PROVIDERS = {
    "google": {
        "display_name": "Google",
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
    },
    "apple": {
        "display_name": "Apple",
        "client_id": os.getenv("APPLE_CLIENT_ID", ""),
    },
}

STEM_MODELS = {
    2: "htdemucs",
}

# In-memory job store. This is intentionally simple for a single-instance API;
# use Redis/Postgres if you scale to multiple workers or need durable history.
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
SAFE_DOWNLOAD_RE = re.compile(r"^[A-Za-z0-9_. -]+\.wav$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Simple in-memory auth store. This keeps the current deployment lightweight,
# while making every split job attributable to a signed-in user. Move these
# dictionaries to a database before scaling beyond one backend instance.
USERS: dict[str, dict[str, Any]] = {}
TOKENS: dict[str, str] = {}
AUTH_LOCK = threading.Lock()
PASSWORD_ITERATIONS = 390_000
PAYMENTS: dict[str, dict[str, Any]] = {}
PAYMENTS_LOCK = threading.Lock()


class AuthRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class UserProfile(BaseModel):
    email: str
    stem_count: int
    jobs_created: int


class AuthResponse(BaseModel):
    token: str
    user: UserProfile


class CheckoutRequest(BaseModel):
    job_id: str
    item_type: str = Field(default="song", pattern="^(song|zip|stem)$")
    filename: str | None = Field(default=None, max_length=120)


class ConfirmPaymentRequest(BaseModel):
    checkout_session_id: str


def normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if not EMAIL_RE.fullmatch(normalized):
        raise HTTPException(400, "Enter a valid email address.")
    return normalized


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return secrets.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": user["email"],
        "stem_count": user["stem_count"],
        "jobs_created": user["jobs_created"],
    }


def create_auth_response(user: dict[str, Any]) -> AuthResponse:
    token = secrets.token_urlsafe(32)
    with AUTH_LOCK:
        TOKENS[token] = user["email"]
    return AuthResponse(token=token, user=UserProfile(**public_user(user)))


def current_user(authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authentication required.")

    token = authorization.removeprefix("Bearer ").strip()
    with AUTH_LOCK:
        email = TOKENS.get(token)
        user = USERS.get(email or "")
    if not user:
        raise HTTPException(401, "Invalid or expired token.")
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
        if job.get("user_email") != user["email"]:
            raise HTTPException(404, "Job not found")
        return serialize_job(job)


def payment_key(job_id: str, item_type: str, filename: str | None = None) -> str:
    return f"{job_id}:song"


def is_payment_required() -> bool:
    return PAYMENTS_ENABLED


def is_download_paid(job_id: str, user: dict[str, Any], item_type: str, filename: str | None = None) -> bool:
    if not is_payment_required():
        return True

    key = payment_key(job_id, item_type, filename)
    with PAYMENTS_LOCK:
        return any(
            payment
            for payment in PAYMENTS.values()
            if payment.get("user_email") == user["email"]
            and payment.get("status") == "paid"
            and payment.get("payment_key") == key
        )


def enforce_paid_download(job: dict[str, Any], user: dict[str, Any], item_type: str, filename: str | None = None) -> None:
    if is_download_paid(job["job_id"], user, item_type, filename):
        return

    raise HTTPException(
        402,
        {
            "message": "Payment required before download.",
            "price_per_song_cents": PRICE_PER_SONG_CENTS,
            "currency": PAYMENT_CURRENCY,
            "checkout_endpoint": "/api/payments/checkout",
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


def checkout_amount_for_job(job: dict[str, Any], item_type: str) -> tuple[int, str]:
    return PRICE_PER_SONG_CENTS, "Stemify full song download"


def stripe_request(method: str, path: str, data: dict[str, str] | None = None) -> dict[str, Any]:
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Stripe is not configured.")

    encoded_data = urllib.parse.urlencode(data or {}).encode("utf-8") if data is not None else None
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
        raise HTTPException(exc.code, f"Stripe error: {detail}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(503, f"Stripe request failed: {exc.reason}") from exc


def json_loads(raw: bytes) -> dict[str, Any]:
    return json.loads(raw.decode("utf-8"))


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
    return preview_path


@app.get("/api")
async def root():
    return {
        "status": "ok",
        "message": "Stemify API is running",
        "version": app.version,
        "allowed_stems": sorted(STEM_MODELS),
        "max_file_size_mb": MAX_FILE_SIZE_MB,
        "preview_duration_seconds": PREVIEW_DURATION_SECONDS,
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
    }


@app.post("/api/auth/signup", response_model=AuthResponse)
async def signup(payload: AuthRequest):
    email = normalize_email(payload.email)
    with AUTH_LOCK:
        if email in USERS:
            raise HTTPException(409, "An account already exists for this email.")
        USERS[email] = {
            "email": email,
            "password_hash": hash_password(payload.password),
            "stem_count": 0,
            "jobs_created": 0,
        }
        user = USERS[email]
    return create_auth_response(user)


@app.post("/api/auth/signin", response_model=AuthResponse)
async def signin(payload: AuthRequest):
    email = normalize_email(payload.email)
    with AUTH_LOCK:
        user = USERS.get(email)
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password.")
    return create_auth_response(user)


@app.get("/api/me", response_model=UserProfile)
async def me(user: Annotated[dict[str, Any], Depends(current_user)]):
    return UserProfile(**public_user(user))


@app.get("/api/auth/social/providers")
async def social_auth_providers():
    return {
        "providers": [
            {
                "provider": provider,
                "display_name": details["display_name"],
                "enabled": bool(details["client_id"]),
                "client_id": details["client_id"] or None,
            }
            for provider, details in SOCIAL_AUTH_PROVIDERS.items()
        ],
        "note": "Use the provider SDK on the frontend, then add a backend token-verification callback before production.",
    }


@app.get("/api/payments/config")
async def payments_config():
    return {
        "enabled": PAYMENTS_ENABLED,
        "provider": "stripe" if PAYMENTS_ENABLED else None,
        "price_per_song_cents": PRICE_PER_SONG_CENTS,
        "currency": PAYMENT_CURRENCY,
    }


@app.post("/api/payments/checkout")
async def create_checkout_session(
    payload: CheckoutRequest,
    user: Annotated[dict[str, Any], Depends(current_user)],
):
    if not PAYMENTS_ENABLED:
        raise HTTPException(400, "Payments are disabled.")

    job = get_authorized_job(payload.job_id, user)
    enforce_job_done(job)
    filename = None

    amount_cents, product_name = checkout_amount_for_job(job, payload.item_type)
    session = stripe_request(
        "POST",
        "/checkout/sessions",
        {
            "mode": "payment",
            "customer_email": user["email"],
            "success_url": f"{FRONTEND_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{FRONTEND_URL}/checkout/cancel",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": PAYMENT_CURRENCY,
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][price_data][product_data][name]": product_name,
            "metadata[job_id]": payload.job_id,
            "metadata[item_type]": payload.item_type,
            "metadata[filename]": filename or "",
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
            "user_email": user["email"],
            "status": "pending",
            "amount_cents": amount_cents,
            "currency": PAYMENT_CURRENCY,
        }

    return {"checkout_session_id": session["id"], "checkout_url": session.get("url")}


@app.post("/api/payments/confirm")
async def confirm_checkout_session(
    payload: ConfirmPaymentRequest,
    user: Annotated[dict[str, Any], Depends(current_user)],
):
    if not PAYMENTS_ENABLED:
        raise HTTPException(400, "Payments are disabled.")

    with PAYMENTS_LOCK:
        payment = PAYMENTS.get(payload.checkout_session_id)
    if not payment or payment.get("user_email") != user["email"]:
        raise HTTPException(404, "Payment not found.")

    session = stripe_request("GET", f"/checkout/sessions/{urllib.parse.quote(payload.checkout_session_id)}")
    if session.get("payment_status") != "paid":
        return {"status": "pending", "payment_status": session.get("payment_status")}

    with PAYMENTS_LOCK:
        payment["status"] = "paid"
        payment["stripe_payment_status"] = session.get("payment_status")
    return {"status": "paid", "job_id": payment["job_id"], "item_type": payment["item_type"]}


@app.post("/api/split")
async def split_audio(
    user: Annotated[dict[str, Any], Depends(current_user)],
    file: UploadFile = File(...),
    stems: int = Form(2),
):
    if stems not in STEM_MODELS:
        raise HTTPException(400, "Stemify only supports 2 stems: vocals and instrumental.")

    original_name = Path(file.filename or "").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {suffix or 'none'}")

    contents = await file.read(MAX_FILE_SIZE_BYTES + 1)
    if len(contents) > MAX_FILE_SIZE_BYTES:
        size_mb = len(contents) / (1024 * 1024)
        raise HTTPException(413, f"File too large ({size_mb:.1f} MB). Max {MAX_FILE_SIZE_MB} MB.")
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
            "user_email": user["email"],
            "requested_stems": stems,
            "preview_duration_seconds": PREVIEW_DURATION_SECONDS,
            "started_at": now,
            "updated_at": now,
        }
    with AUTH_LOCK:
        user["jobs_created"] += 1
        user["stem_count"] += stems

    thread = threading.Thread(
        target=run_demucs,
        args=(job_id, job_dir, input_path, stems, safe_stem),
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
            "user": public_user(user),
        }
    )


def run_demucs(job_id: str, job_dir: Path, input_path: Path, stems: int, track_name: str) -> None:
    preview_path: Path | None = None
    try:
        update_job(job_id, status_detail=f"Creating {PREVIEW_DURATION_SECONDS}-second preview.")
        preview_path = create_preview_input(job_id, input_path)
        model = STEM_MODELS[stems]
        cmd = [sys.executable, "-m", "demucs", "-n", model, "--out", str(job_dir)]
        if stems == 2:
            cmd += ["--two-stems", "vocals"]
        cmd.append(str(preview_path))

        update_job(
            job_id,
            status_detail=(
                f"Separating the {PREVIEW_DURATION_SECONDS}-second preview into vocals and instrumental."
            ),
        )
        print(f"[JOB {job_id[:8]}] Running: {' '.join(cmd)}", flush=True)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=DEMUCS_TIMEOUT_SECONDS)
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

        update_job(job_id, status_detail="Finalizing preview stem files.")
        wavs = sorted(job_dir.rglob("*.wav"))
        if not wavs:
            update_job(
                job_id,
                status="error",
                status_detail="Preview stem separation finished without producing WAV files.",
                error="No output files produced.",
            )
            return

        zip_path = job_dir / f"stemify_{track_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for wav in wavs:
                zf.write(wav, arcname=wav.name)

        stem_names = [w.stem for w in wavs]
        stem_urls = {w.stem: f"/api/download/{job_id}/stem/{w.name}" for w in wavs}
        update_job(
            job_id,
            status="done",
            status_detail=f"{PREVIEW_DURATION_SECONDS}-second preview stems are ready.",
            stems=stem_names,
            zip_url=f"/api/download/{job_id}/zip",
            stem_urls=stem_urls,
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
        update_job(job_id, status="error", status_detail="Preview stem separation crashed.", error=str(exc))
        print(f"[JOB {job_id[:8]}] Exception: {exc}")
    finally:
        input_path.unlink(missing_ok=True)
        if preview_path:
            preview_path.unlink(missing_ok=True)


@app.get("/api/job/{job_id}")
async def job_status(job_id: str, user: Annotated[dict[str, Any], Depends(current_user)]):
    return JSONResponse(get_authorized_job(job_id, user))


@app.get("/api/download/{job_id}/zip")
async def download_zip(job_id: str, user: Annotated[dict[str, Any], Depends(current_user)]):
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
    return FileResponse(matches[0], media_type="audio/wav", filename=filename)


@app.delete("/api/cleanup/{job_id}")
async def cleanup(job_id: str, user: Annotated[dict[str, Any], Depends(current_user)]):
    get_authorized_job(job_id, user)
    job_dir = OUTPUT_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return {"status": "cleaned"}
