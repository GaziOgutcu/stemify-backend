import threading
import zipfile

import main
import pytest
from fastapi.testclient import TestClient


client = TestClient(main.app)
FIREBASE_TOKENS = {}


@pytest.fixture(autouse=True)
def reset_auth_state(monkeypatch):
    main.USERS.clear()
    main.JOBS.clear()
    main.PAYMENTS.clear()
    FIREBASE_TOKENS.clear()

    main.firebase_admin._apps["test"] = object()

    def fake_verify_id_token(token):
        if token not in FIREBASE_TOKENS:
            raise ValueError("bad token")
        return FIREBASE_TOKENS[token]

    monkeypatch.setattr(main.firebase_auth, "verify_id_token", fake_verify_id_token)
    yield
    main.USERS.clear()
    main.JOBS.clear()
    main.PAYMENTS.clear()
    FIREBASE_TOKENS.clear()
    main.firebase_admin._apps.pop("test", None)


def auth_headers(uid="test-uid", email="tester@example.com", name="Test User"):
    token = f"token-{uid}"
    FIREBASE_TOKENS[token] = {
        "uid": uid,
        "email": email,
        "name": name,
        "firebase": {"sign_in_provider": "google.com"},
    }
    return {"Authorization": f"Bearer {token}"}


def test_health_includes_runtime_details():
    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "ffmpeg_available" in payload
    assert "upload_dir" in payload
    assert "output_dir" in payload


def test_me_returns_firebase_user_profile():
    response = client.get("/api/me", headers=auth_headers())

    assert response.status_code == 200
    assert response.json() == {
        "uid": "test-uid",
        "email": "tester@example.com",
        "name": "Test User",
        "provider": "google.com",
    }


def test_payments_config_defaults_to_disabled():
    response = client.get("/api/payments/config")

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["price_per_song_cents"] == 300


def test_split_requires_authentication():
    response = client.post(
        "/api/split",
        files={"file": ("song.mp3", b"audio", "audio/mpeg")},
        data={"stems": "2"},
    )

    assert response.status_code == 401
    assert "Authentication required" in response.json()["detail"]


def test_split_rejects_unsupported_stem_count():
    response = client.post(
        "/api/split",
        files={"file": ("song.mp3", b"audio", "audio/mpeg")},
        data={"stems": "4"},
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert "only supports 2 stems" in response.json()["detail"]


def test_split_rejects_unsupported_file_type():
    response = client.post(
        "/api/split",
        files={"file": ("notes.txt", b"audio", "text/plain")},
        data={"stems": "2"},
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]


def test_split_creates_job_and_sanitizes_track_name(monkeypatch):
    def fake_thread(*, target, args, daemon):
        class DummyThread:
            def start(self):
                return None

        return DummyThread()

    monkeypatch.setattr(threading, "Thread", fake_thread)
    headers = auth_headers()
    response = client.post(
        "/api/split",
        files={"file": ("My Song!!!.mp3", b"audio", "audio/mpeg")},
        data={"stems": "2"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "processing"
    assert payload["track_name"] == "My_Song___"
    assert payload["preview_duration_seconds"] == 15
    assert payload["user"]["stem_count"] == 2
    assert payload["user"]["jobs_created"] == 1
    assert payload["job_id"] in main.JOBS
    client.delete(f"/api/cleanup/{payload['job_id']}", headers=headers)


def test_download_stem_rejects_invalid_filename():
    response = client.get("/api/download/example/stem/..%2Fsecret.wav", headers=auth_headers())

    assert response.status_code in {400, 404}


def test_download_zip_returns_ready_archive(tmp_path, monkeypatch):
    job_id = "zip-test"
    headers = auth_headers()
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    main.JOBS[job_id] = {"job_id": job_id, "user_uid": "test-uid", "status": "done"}
    zip_path = job_dir / "stemify_song.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("vocals.wav", b"wav")

    response = client.get(f"/api/download/{job_id}/zip", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"


def test_download_zip_reports_not_ready_for_processing_job(tmp_path, monkeypatch):
    job_id = "processing-zip-test"
    headers = auth_headers()
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    main.JOBS[job_id] = {
        "job_id": job_id,
        "user_uid": "test-uid",
        "status": "processing",
        "status_detail": "Separating audio with Demucs.",
        "started_at": 100.0,
    }

    response = client.get(f"/api/download/{job_id}/zip", headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "processing"


def test_download_zip_requires_payment_when_enabled(tmp_path, monkeypatch):
    job_id = "paid-zip-test"
    headers = auth_headers()
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "PAYMENTS_ENABLED", True)
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    main.JOBS[job_id] = {
        "job_id": job_id,
        "user_uid": "test-uid",
        "status": "done",
        "requested_stems": 2,
    }
    zip_path = job_dir / "stemify_song.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("vocals.wav", b"wav")

    response = client.get(f"/api/download/{job_id}/zip", headers=headers)

    assert response.status_code == 402
    assert response.json()["detail"]["price_per_song_cents"] == 300
