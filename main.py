import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any

import static_ffmpeg
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

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

app = FastAPI(title="Stemify API", version="1.1.0")


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

STEM_MODELS = {
    2: "mdx_q",
    4: "mdx_q",
    6: "mdx_q",
}

# In-memory job store. This is intentionally simple for a single-instance API;
# use Redis/Postgres if you scale to multiple workers or need durable history.
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
SAFE_DOWNLOAD_RE = re.compile(r"^[A-Za-z0-9_. -]+\.wav$")


def update_job(job_id: str, **values: Any) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)


@app.get("/api")
async def root():
    return {
        "status": "ok",
        "message": "Stemify API is running",
        "version": app.version,
        "allowed_stems": sorted(STEM_MODELS),
        "max_file_size_mb": MAX_FILE_SIZE_MB,
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "upload_dir": str(UPLOAD_DIR),
        "output_dir": str(OUTPUT_DIR),
    }


@app.post("/api/split")
async def split_audio(
    file: UploadFile = File(...),
    stems: int = Form(4),
):
    if stems not in STEM_MODELS:
        allowed_stems = ", ".join(str(value) for value in sorted(STEM_MODELS))
        raise HTTPException(400, f"Unsupported stem count: {stems}. Allowed values: {allowed_stems}.")

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

    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "processing",
            "stems": [],
            "zip_url": None,
            "stem_urls": {},
            "error": None,
            "track_name": safe_stem,
        }

    thread = threading.Thread(
        target=run_demucs,
        args=(job_id, job_dir, input_path, stems, safe_stem),
        daemon=True,
    )
    thread.start()

    return JSONResponse({"job_id": job_id, "status": "processing", "track_name": safe_stem})


def run_demucs(job_id: str, job_dir: Path, input_path: Path, stems: int, track_name: str) -> None:
    try:
        model = STEM_MODELS[stems]
        cmd = [sys.executable, "-m", "demucs", "-n", model, "--out", str(job_dir)]
        if stems == 2:
            cmd += ["--two-stems", "vocals"]
        cmd.append(str(input_path))

        print(f"[JOB {job_id[:8]}] Running: {' '.join(cmd)}", flush=True)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=DEMUCS_TIMEOUT_SECONDS)
        print(f"[JOB {job_id[:8]}] STDOUT:\n{proc.stdout[-2000:]}", flush=True)
        print(f"[JOB {job_id[:8]}] STDERR:\n{proc.stderr[-2000:]}", flush=True)
        print(f"[JOB {job_id[:8]}] RETURN CODE: {proc.returncode}", flush=True)

        if proc.returncode != 0:
            err = (proc.stderr or "") + (proc.stdout or "")
            print(f"[JOB {job_id[:8]}] FAILED:\n{err[-800:]}")
            update_job(job_id, status="error", error=f"Demucs error: {err[-400:]}")
            return

        wavs = sorted(job_dir.rglob("*.wav"))
        if not wavs:
            update_job(job_id, status="error", error="No output files produced.")
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
            stems=stem_names,
            zip_url=f"/api/download/{job_id}/zip",
            stem_urls=stem_urls,
        )
        print(f"[JOB {job_id[:8]}] Done: {stem_names}")

    except subprocess.TimeoutExpired:
        update_job(job_id, status="error", error="Timed out. Try a shorter track.")
    except Exception as exc:
        update_job(job_id, status="error", error=str(exc))
        print(f"[JOB {job_id[:8]}] Exception: {exc}")
    finally:
        input_path.unlink(missing_ok=True)


@app.get("/api/job/{job_id}")
async def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return JSONResponse(job.copy())


@app.get("/api/download/{job_id}/zip")
async def download_zip(job_id: str):
    job_dir = OUTPUT_DIR / job_id
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found.")
    zips = sorted(job_dir.glob("*.zip"))
    if not zips:
        raise HTTPException(404, "ZIP not ready yet.")
    return FileResponse(zips[0], media_type="application/zip", filename=zips[0].name)


@app.get("/api/download/{job_id}/stem/{filename}")
async def download_stem(job_id: str, filename: str):
    if not SAFE_DOWNLOAD_RE.fullmatch(filename):
        raise HTTPException(400, "Invalid stem filename.")

    job_dir = OUTPUT_DIR / job_id
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found.")

    matches = sorted(path for path in job_dir.rglob(filename) if path.is_file())
    if not matches:
        raise HTTPException(404, "Stem not found.")
    return FileResponse(matches[0], media_type="audio/wav", filename=filename)


@app.delete("/api/cleanup/{job_id}")
async def cleanup(job_id: str):
    job_dir = OUTPUT_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return {"status": "cleaned"}
