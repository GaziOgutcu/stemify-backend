# Stemify Backend

FastAPI backend for uploading an audio file and splitting a 15-second preview into vocals and instrumental with Demucs, with optional Stripe Checkout for a safe $3 full-song download flow.

## API endpoints

- `GET /api` - service metadata and configured limits.
- `GET /api/health` - health check for deployments.
- `GET /api/me` - return the signed-in Firebase user's `uid`, `email`, `name`, and `provider`.
- `GET /api/payments/config` - return paid-download settings such as price per song and currency.
- `POST /api/create-checkout-session` - frontend-safe endpoint that creates a Stripe Checkout Session from the FastAPI backend when paid downloads are enabled.
- `POST /api/stripe/webhook` - Stripe webhook endpoint that confirms successful payment before downloads are unlocked.
- `POST /api/payments/confirm` - return local payment status after the webhook has run; it does not confirm payment itself.
- `POST /api/split` - multipart form upload with:
  - `file`: `.mp3`, `.wav`, `.flac`, `.aac`, `.ogg`, or `.m4a`
  - `stems`: always `2` for vocals and instrumental
  - `output_format`: optional `wav`, `mp3`, `flac`, `ogg`, or `m4a` for the generated download files; defaults to `wav`
- `GET /api/job/{job_id}` - poll job status until it is `done` or `error`.
- `GET /api/download/{job_id}/zip` - download all produced stems as a ZIP.
- `GET /api/download/{job_id}/stem/{filename}` - download one stem in the requested output format.
- `DELETE /api/cleanup/{job_id}` - remove output files and forget the job.

All split, job status, payment checkout/status, download, cleanup, and profile endpoints require an `Authorization: Bearer <firebase_id_token>` header from Firebase Google Sign-In. The Stripe webhook endpoint is called by Stripe and is verified with `STRIPE_WEBHOOK_SECRET`. Each submitted split job is tied to that signed-in Firebase user.

Job status responses include `status_detail`, `elapsed_seconds`, `timeout_seconds`, and `output_format` so the frontend can show a clear message instead of a vague "finalising" spinner. Downloads and checkout return `409` until the preview job status is `done`.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/api/health` to verify the server is running.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `ALLOWED_ORIGINS` | `*` | Comma-separated frontend origins. Set this to your Vercel/local frontend URLs in production. |
| `MAX_FILE_SIZE_MB` | `50` | Upload size limit. |
| `DEMUCS_TIMEOUT_SECONDS` | `900` | Maximum processing time per job. |
| `DEMUCS_SHIFTS` | `0` | Demucs test-time shifts. `0` is fastest; raise only if you prefer quality over speed. |
| `DEMUCS_OVERLAP` | `0.1` | Demucs split overlap. Lower values are faster. |
| `DEMUCS_JOBS` | `0` | Optional Demucs worker count; `0` leaves the default behavior. |
| `DEMUCS_DEVICE` | empty | Optional Demucs device override such as `cuda` or `cpu`. |
| `UPLOAD_DIR` | `uploads` | Temporary upload directory. |
| `OUTPUT_DIR` | `outputs` | Generated stems directory. |
| `PREVIEW_DURATION_SECONDS` | `15` | Free preview length to split into vocals and instrumental. |
| `PAYMENTS_ENABLED` | `true` when `STRIPE_SECRET_KEY` is present, otherwise `false` | Explicit feature flag for paid downloads and Checkout. Set to `true` in production, or leave unset when `STRIPE_SECRET_KEY` is configured. |
| `PRICE_PER_SONG_CENTS` | `300` | Full-song download price in cents; default is `$3.00`. |
| `PAYMENT_CURRENCY` | `usd` | Currency for Stripe Checkout. |
| `STRIPE_SECRET_KEY` | empty | Stripe secret key used only by the FastAPI backend on Railway to create Checkout Sessions. If present and `PAYMENTS_ENABLED` is unset, paid downloads are enabled automatically. Never expose this in frontend code or `index.html`. |
| `STRIPE_WEBHOOK_SECRET` | empty | Stripe webhook signing secret used by `/api/stripe/webhook` to verify Stripe events before unlocking downloads. |
| `FRONTEND_URL` | `http://localhost:3000` | Frontend URL used for Stripe Checkout success/cancel redirects. |
| `FIREBASE_PROJECT_ID` | empty | Firebase project ID used by Firebase Admin SDK. |
| `FIREBASE_CLIENT_EMAIL` | empty | Firebase service account client email. |
| `FIREBASE_PRIVATE_KEY` | empty | Firebase service account private key. Store this in Railway, not in code. |

## Frontend integration guide

Set a frontend environment variable such as `NEXT_PUBLIC_API_URL=https://your-backend.up.railway.app`.

Example flow after the static frontend signs in with Firebase Google Sign-In:

```ts
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function splitTrack(file: File, firebaseIdToken: string) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("stems", "2");
  formData.append("output_format", "mp3"); // or wav, flac, ogg, m4a

  const upload = await fetch(`${API_URL}/api/split`, {
    method: "POST",
    headers: { Authorization: `Bearer ${firebaseIdToken}` },
    body: formData,
  });
  if (!upload.ok) throw new Error(await upload.text());

  const { job_id } = await upload.json();

  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const statusResponse = await fetch(`${API_URL}/api/job/${job_id}`, {
      headers: { Authorization: `Bearer ${firebaseIdToken}` },
    });
    if (!statusResponse.ok) throw new Error(await statusResponse.text());

    const job = await statusResponse.json();
    if (job.status === "done") {
      return {
        ...job,
        zipUrl: `${API_URL}${job.zip_url}`,
        stemUrls: Object.fromEntries(
          Object.entries(job.stem_urls).map(([name, url]) => [name, `${API_URL}${url}`]),
        ),
      };
    }
    if (job.status === "error") throw new Error(job.error || "Stem split failed");
  }
}

export async function createDownloadCheckout(firebaseIdToken: string, jobId: string) {
  const response = await fetch(`${API_URL}/api/create-checkout-session`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${firebaseIdToken}`,
    },
    body: JSON.stringify({ job_id: jobId, item_type: "song" }),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<{ checkout_session_id: string; checkout_url: string }>;
}
```

Production checklist for the frontend:

1. Require Firebase Google Sign-In before enabling uploads, then call `user.getIdToken()` and send it as `Authorization: Bearer <firebase_id_token>`.
2. Validate file extensions and display the backend size limit before upload.
3. Disable the upload button while a job is processing.
4. Poll `/api/job/{job_id}` every 2-5 seconds with the bearer token. Make sure the frontend starts only one polling interval per job and clears it when the job reaches `done` or `error`.
5. Show the 15-second vocal/instrumental preview after `status === "done"`.
6. Let the user choose a download format (`wav`, `mp3`, `flac`, `ogg`, or `m4a`) before uploading; send it as `output_format`.
7. If `PAYMENTS_ENABLED=true`, call `/api/create-checkout-session` when the user wants the full song and redirect them to the returned `checkout_url`. Do not put `STRIPE_SECRET_KEY` in frontend files such as `index.html`; it belongs only in Railway backend environment variables.
8. Configure Stripe to send `checkout.session.completed` events to `/api/stripe/webhook`; downloads remain locked until that webhook verifies a successful paid session.
9. Call `DELETE /api/cleanup/{job_id}` with the bearer token after the user downloads files or when leaving the result page.

The simple product is: free 15-second vocal/instrumental preview, then a safe Stripe-hosted $3 per song checkout for the full download. The browser only asks the backend to create checkout; the backend creates the Checkout Session and the Stripe webhook is the source of truth for unlocking downloads.

## Deployment notes

The included Dockerfile installs FFmpeg and Demucs. Railway should use `railway.toml`; set `ALLOWED_ORIGINS` to your frontend origin, for example:

```text
ALLOWED_ORIGINS=https://your-frontend.vercel.app,http://localhost:3000
```

For speed, the backend now runs Demucs with no test-time shifts by default and low overlap; set `DEMUCS_DEVICE=cuda` on GPU infrastructure for much faster processing. Firebase handles user identity. The current user stem totals and job stores are still in memory, so deploy as a single backend instance only for early testing. Add Postgres/Redis before production so usage counts and jobs survive restarts and work across multiple backend instances.
