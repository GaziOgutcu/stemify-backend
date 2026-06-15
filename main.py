import os
import sys
import re
import uuid
import shutil
import zipfile
import subprocess
import threading
from pathlib import Path

# Auto-configure ffmpeg
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
    print("[OK] ffmpeg ready")
except ImportError:
    pass

from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="Stemify API", version="1.0.0")

# Allow requests from Vercel frontend + localhost
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}

STEM_MODELS = {
    2:  "htdemucs",
    4:  "htdemucs",
    6:  "htdemucs_6s",
    8:  "htdemucs_6s",
    10: "htdemucs_6s",
}

# In-memory job store
JOBS: dict = {}


@app.get("/api")
async def root():
    return {"status": "ok", "message": "Stemify API is running"}


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/split")
async def split_audio(
    file: UploadFile = File(...),
    stems: int = Form(4),
):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {suffix}")

    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(413, f"File too large ({size_mb:.1f} MB). Max {MAX_FILE_SIZE_MB} MB.")

    job_id = str(uuid.uuid4())
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True)

    # Sanitize filename (remove non-ASCII)
    safe_stem = re.sub(r'[^a-zA-Z0-9_\-]', '_', Path(file.filename).stem) or "track"
    input_path = UPLOAD_DIR / f"{job_id}{suffix}"
    input_path.write_bytes(contents)

    JOBS[job_id] = {
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


def run_demucs(job_id, job_dir, input_path, stems, track_name):
    try:
        model = STEM_MODELS.get(stems, "htdemucs")
        cmd = [sys.executable, "-m", "demucs", "-n", model, "--out", str(job_dir)]
        if stems == 2:
            cmd += ["--two-stems", "vocals"]
        cmd.append(str(input_path))

        print(f"[JOB {job_id[:8]}] Running: {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if proc.returncode != 0:
            err = (proc.stderr or "") + (proc.stdout or "")
            print(f"[JOB {job_id[:8]}] FAILED:\n{err[-800:]}")
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = f"Demucs error: {err[-400:]}"
            return

        wavs = list(job_dir.rglob("*.wav"))
        if not wavs:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = "No output files produced."
            return

        # Build zip
        zip_path = job_dir / f"stemify_{track_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for wav in wavs:
                zf.write(wav, arcname=wav.name)

        input_path.unlink(missing_ok=True)

        stem_names = [w.stem for w in wavs]
        stem_urls  = {w.stem: f"/api/download/{job_id}/stem/{w.name}" for w in wavs}

        JOBS[job_id].update({
            "status":    "done",
            "stems":     stem_names,
            "zip_url":   f"/api/download/{job_id}/zip",
            "stem_urls": stem_urls,
        })
        print(f"[JOB {job_id[:8]}] Done: {stem_names}")

    except subprocess.TimeoutExpired:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = "Timed out. Try a shorter track."
    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)
        print(f"[JOB {job_id[:8]}] Exception: {e}")


@app.get("/api/job/{job_id}")
async def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


@app.get("/api/download/{job_id}/zip")
async def download_zip(job_id: str):
    job_dir = OUTPUT_DIR / job_id
    zips = list(job_dir.glob("*.zip"))
    if not zips:
        raise HTTPException(404, "ZIP not ready yet.")
    return FileResponse(zips[0], media_type="application/zip", filename=zips[0].name)


@app.get("/api/download/{job_id}/stem/{filename}")
async def download_stem(job_id: str, filename: str):
    matches = list((OUTPUT_DIR / job_id).rglob(filename))
    if not matches:
        raise HTTPException(404, "Stem not found.")
    return FileResponse(matches[0], media_type="audio/wav", filename=filename)


@app.delete("/api/cleanup/{job_id}")
async def cleanup(job_id: str):
    job_dir = OUTPUT_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    JOBS.pop(job_id, None)
    return {"status": "cleaned"}
