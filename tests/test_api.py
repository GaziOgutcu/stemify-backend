import hmac
import json
import time
from hashlib import sha256
from pathlib import Path
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


def stripe_signature(payload: bytes, secret: str) -> str:
    timestamp = str(int(time.time()))
    signed_payload = timestamp.encode("utf-8") + b"." + payload
    signature = hmac.new(secret.encode("utf-8"), signed_payload, sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


def test_health_includes_runtime_details():
    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "ffmpeg_available" in payload
    assert "upload_dir" in payload
    assert "output_dir" in payload
    assert "mp3" in payload["output_formats"]


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
    assert response.json()["checkout_endpoint"] == "/api/create-checkout-session"


def test_create_checkout_session_available_on_frontend_endpoint(monkeypatch):
    job_id = "checkout-job"
    headers = auth_headers()
    monkeypatch.setattr(main, "PAYMENTS_ENABLED", True)
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_test_backend_only")
    main.JOBS[job_id] = {
        "job_id": job_id,
        "user_uid": "test-uid",
        "status": "done",
        "requested_stems": 2,
    }

    def fake_stripe_request(method, path, data=None):
        assert method == "POST"
        assert path == "/checkout/sessions"
        assert data["metadata[job_id]"] == job_id
        assert data["metadata[user_uid]"] == "test-uid"
        return {"id": "cs_test_123", "url": "https://checkout.stripe.test/session"}

    monkeypatch.setattr(main, "stripe_request", fake_stripe_request)

    response = client.post(
        "/api/create-checkout-session",
        json={"job_id": job_id, "item_type": "song"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["checkout_session_id"] == "cs_test_123"
    assert main.PAYMENTS["cs_test_123"]["status"] == "pending"


def test_create_checkout_session_uses_legacy_payments_endpoint(monkeypatch):
    job_id = "checkout-legacy-job"
    headers = auth_headers()
    monkeypatch.setattr(main, "PAYMENTS_ENABLED", True)
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_live_backend_only")
    main.JOBS[job_id] = {
        "job_id": job_id,
        "user_uid": "test-uid",
        "status": "done",
        "requested_stems": 2,
    }

    def fake_stripe_request(method, path, data=None):
        assert (
            data["success_url"]
            == "http://localhost:3000/?payment=success&session_id={CHECKOUT_SESSION_ID}"
        )
        assert data["cancel_url"] == "http://localhost:3000/?payment=cancelled"
        assert data["line_items[0][price_data][unit_amount]"] == "300"
        assert data["line_items[0][price_data][currency]"] == "usd"
        return {"id": "cs_live_123", "url": "https://checkout.stripe.com/session"}

    monkeypatch.setattr(main, "stripe_request", fake_stripe_request)

    response = client.post(
        "/api/payments/checkout",
        json={"job_id": job_id, "item_type": "song"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["checkout_session_id"] == "cs_live_123"


def test_checkout_uses_static_frontend_root_redirects(monkeypatch):
    job_id = "checkout-static-frontend-job"
    headers = auth_headers()
    monkeypatch.setattr(main, "PAYMENTS_ENABLED", True)
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_live_backend_only")
    monkeypatch.setattr(main, "FRONTEND_URL", "https://vocalsplitter.app")
    main.JOBS[job_id] = {
        "job_id": job_id,
        "user_uid": "test-uid",
        "status": "done",
        "requested_stems": 2,
    }

    def fake_stripe_request(method, path, data=None):
        assert (
            data["success_url"]
            == "https://vocalsplitter.app/?payment=success&session_id={CHECKOUT_SESSION_ID}"
        )
        assert data["cancel_url"] == "https://vocalsplitter.app/?payment=cancelled"
        return {"id": "cs_live_static", "url": "https://checkout.stripe.com/session"}

    monkeypatch.setattr(main, "stripe_request", fake_stripe_request)

    response = client.post(
        "/api/create-checkout-session",
        json={"job_id": job_id, "item_type": "song"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["checkout_session_id"] == "cs_live_static"


def test_checkout_reports_specific_configuration_error_when_payments_disabled(
    monkeypatch,
):
    job_id = "checkout-disabled-job"
    headers = auth_headers()
    monkeypatch.setattr(main, "PAYMENTS_ENABLED", False)
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_live_backend_only")
    main.JOBS[job_id] = {
        "job_id": job_id,
        "user_uid": "test-uid",
        "status": "done",
    }

    response = client.post(
        "/api/payments/checkout",
        json={"job_id": job_id, "item_type": "song"},
        headers=headers,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Payments are not enabled on this server."


def test_checkout_rejects_non_absolute_frontend_url(monkeypatch):
    job_id = "checkout-url-job"
    headers = auth_headers()
    monkeypatch.setattr(main, "PAYMENTS_ENABLED", True)
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_live_backend_only")
    monkeypatch.setattr(main, "FRONTEND_URL", "stemify.app")
    main.JOBS[job_id] = {
        "job_id": job_id,
        "user_uid": "test-uid",
        "status": "done",
    }

    response = client.post(
        "/api/create-checkout-session",
        json={"job_id": job_id, "item_type": "song"},
        headers=headers,
    )

    assert response.status_code == 503
    assert (
        response.json()["detail"]
        == "Checkout redirect URL is not configured correctly."
    )


def test_payment_status_verifies_pending_checkout_with_stripe(monkeypatch):
    headers = auth_headers()
    monkeypatch.setattr(main, "PAYMENTS_ENABLED", True)
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_test_backend_only")
    main.PAYMENTS["cs_pending"] = {
        "checkout_session_id": "cs_pending",
        "payment_key": main.payment_key("job-id", "song"),
        "job_id": "job-id",
        "item_type": "song",
        "filename": None,
        "user_uid": "test-uid",
        "status": "pending",
    }

    def fake_retrieve_checkout_session(checkout_session_id):
        assert checkout_session_id == "cs_pending"
        return {
            "id": "cs_pending",
            "payment_status": "unpaid",
            "metadata": {
                "job_id": "job-id",
                "item_type": "song",
                "user_uid": "test-uid",
            },
        }

    monkeypatch.setattr(
        main, "retrieve_checkout_session", fake_retrieve_checkout_session
    )

    response = client.post(
        "/api/payments/confirm",
        json={"checkout_session_id": "cs_pending"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["stripe_payment_status"] == "unpaid"
    assert main.PAYMENTS["cs_pending"]["status"] == "pending"


def test_payment_status_marks_paid_from_stripe_api(monkeypatch):
    headers = auth_headers()
    monkeypatch.setattr(main, "PAYMENTS_ENABLED", True)
    monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_test_backend_only")

    def fake_retrieve_checkout_session(checkout_session_id):
        assert checkout_session_id == "cs_paid_by_api"
        return {
            "id": "cs_paid_by_api",
            "payment_status": "paid",
            "amount_total": 300,
            "currency": "usd",
            "customer_email": "tester@example.com",
            "metadata": {
                "job_id": "job-id",
                "item_type": "song",
                "user_uid": "test-uid",
                "user_email": "tester@example.com",
            },
        }

    monkeypatch.setattr(
        main, "retrieve_checkout_session", fake_retrieve_checkout_session
    )

    response = client.post(
        "/api/payments/confirm",
        json={"checkout_session_id": "cs_paid_by_api"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "paid"
    assert main.PAYMENTS["cs_paid_by_api"]["status"] == "paid"
    assert main.PAYMENTS["cs_paid_by_api"]["stripe_api_verified"] is True


def test_stripe_webhook_marks_payment_paid(monkeypatch):
    secret = "whsec_test"
    monkeypatch.setattr(main, "STRIPE_WEBHOOK_SECRET", secret)
    main.PAYMENTS["cs_paid"] = {
        "checkout_session_id": "cs_paid",
        "payment_key": main.payment_key("job-id", "song"),
        "job_id": "job-id",
        "item_type": "song",
        "filename": None,
        "user_uid": "test-uid",
        "status": "pending",
    }
    payload = json.dumps(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_paid",
                    "payment_status": "paid",
                    "metadata": {
                        "job_id": "job-id",
                        "item_type": "song",
                        "user_uid": "test-uid",
                    },
                }
            },
        }
    ).encode("utf-8")

    response = client.post(
        "/api/stripe/webhook",
        content=payload,
        headers={"stripe-signature": stripe_signature(payload, secret)},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "paid"
    assert main.PAYMENTS["cs_paid"]["status"] == "paid"


def test_requirements_include_diffq_for_quantized_demucs_model():
    requirements = Path("requirements.txt").read_text()

    assert "diffq" in requirements


def test_demucs_command_uses_fast_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DEMUCS_MODEL", "mdx_q")
    monkeypatch.setitem(main.STEM_MODELS, 2, "mdx_q")
    monkeypatch.setattr(main, "DEMUCS_SHIFTS", 0)
    monkeypatch.setattr(main, "DEMUCS_OVERLAP", 0.1)
    monkeypatch.setattr(main, "DEMUCS_SEGMENT_SECONDS", 8)
    monkeypatch.setattr(main, "DEMUCS_JOBS", 0)
    monkeypatch.setattr(main, "DEMUCS_DEVICE", "")

    cmd = main.build_demucs_command(tmp_path / "out", tmp_path / "preview.wav", 2)

    assert cmd[cmd.index("-n") + 1] == "mdx_q"
    assert cmd[cmd.index("--shifts") + 1] == "0"
    assert cmd[cmd.index("--overlap") + 1] == "0.1"
    assert cmd[cmd.index("--segment") + 1] == "8"
    assert "--two-stems" in cmd
    assert "--jobs" not in cmd


def test_demucs_subprocess_env_limits_cpu_threads(monkeypatch):
    monkeypatch.setattr(main, "DEMUCS_CPU_THREADS", 3)

    env = main.demucs_subprocess_env()

    assert env["OMP_NUM_THREADS"] == "3"
    assert env["MKL_NUM_THREADS"] == "3"
    assert env["NUMEXPR_NUM_THREADS"] == "3"
    assert env["TORCH_NUM_THREADS"] == "3"


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


def test_split_rejects_unsupported_output_format():
    response = client.post(
        "/api/split",
        files={"file": ("song.mp3", b"audio", "audio/mpeg")},
        data={"stems": "2", "output_format": "exe"},
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert "Unsupported output format" in response.json()["detail"]


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
        data={"stems": "2", "output_format": "mp3"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "processing"
    assert payload["track_name"] == "My_Song___"
    assert payload["preview_duration_seconds"] == 15
    assert payload["output_format"] == "mp3"
    assert "wav" in payload["available_output_formats"]
    assert payload["user"]["stem_count"] == 2
    assert payload["user"]["jobs_created"] == 1
    assert payload["job_id"] in main.JOBS
    client.delete(f"/api/cleanup/{payload['job_id']}", headers=headers)


def test_download_stem_rejects_invalid_filename():
    response = client.get(
        "/api/download/example/stem/..%2Fsecret.wav", headers=auth_headers()
    )

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


def test_download_zip_allowed_after_stripe_api_verifies_payment(tmp_path, monkeypatch):
    job_id = "api-paid-zip-test"
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
    main.PAYMENTS["cs_api_paid"] = {
        "checkout_session_id": "cs_api_paid",
        "payment_key": main.payment_key(job_id, "zip"),
        "job_id": job_id,
        "item_type": "zip",
        "filename": None,
        "user_uid": "test-uid",
        "status": "paid",
        "stripe_api_verified": True,
    }
    zip_path = job_dir / "stemify_song.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("vocals.wav", b"wav")

    response = client.get(f"/api/download/{job_id}/zip", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"


def test_download_zip_allowed_after_webhook_confirms_payment(tmp_path, monkeypatch):
    job_id = "webhook-paid-zip-test"
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
    main.PAYMENTS["cs_paid"] = {
        "checkout_session_id": "cs_paid",
        "payment_key": main.payment_key(job_id, "zip"),
        "job_id": job_id,
        "item_type": "zip",
        "filename": None,
        "user_uid": "test-uid",
        "status": "paid",
        "stripe_event_confirmed": True,
    }
    zip_path = job_dir / "stemify_song.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("vocals.wav", b"wav")

    response = client.get(f"/api/download/{job_id}/zip", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
