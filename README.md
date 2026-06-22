# Stemify Backend

FastAPI backend for uploading an audio file and splitting a 15-second preview into vocals and instrumental with Demucs, with optional Stripe Checkout for a safe $3 AUD full-song download flow.

## API endpoints

- `GET /api` - service metadata and configured limits.
- `GET /api/health` - health check for deployments.
- `GET /api/me` - return the signed-in Firebase user's `uid`, `email`, `name`, and `provider`.
- `GET /api/payments/config` - return paid-download settings such as price per song and currency.
- `POST /api/create-checkout-session` - frontend-safe endpoint that creates a Stripe Checkout Session for a specific `job_id` from the FastAPI backend when paid downloads are enabled.
- `POST /api/stripe/webhook` - Stripe webhook endpoint that confirms successful payment before downloads are unlocked.
- `GET /api/payment/verify?session_id=...&job_id=...` - verify a Stripe Checkout Session with Stripe using the backend secret key, ensure it belongs to the requested job and signed-in user, mark the job paid, and return download URLs.
- `POST /api/payments/confirm` - legacy payment verification endpoint that verifies a Stripe Checkout Session with Stripe using the backend secret key and returns whether the signed-in user has paid.
- `POST /api/split` - multipart form upload with:
  - `file`: `.mp3`, `.wav`, `.flac`, `.aac`, `.ogg`, or `.m4a`
  - `stems`: always `2` for vocals and instrumental
  - `output_format`: optional `wav`, `mp3`, `flac`, `ogg`, or `m4a` for the generated download files; defaults to `wav`
  - `quality`: optional `fast` or `high`; defaults to `fast` for backward-compatible uploads
- `GET /api/job/{job_id}` - poll job status until it is `done` or `error`.
- `GET /api/download/{job_id}/zip` - download all produced stems as a ZIP.
- `GET /api/preview/{job_id}/stem/{filename}` - stream a generated 15-second preview stem for browser audio players. These URLs are returned in `stem_urls` / `preview_urls` after the job is `done`.
- `GET /api/download/{job_id}/stem/{filename}` - download one paid stem in the requested output format.
- `DELETE /api/cleanup/{job_id}` - remove output files and forget the job.

All split, job status, payment checkout/status, download, cleanup, and profile endpoints require an `Authorization: Bearer <firebase_id_token>` header from Firebase Google Sign-In. Preview stem URLs under `/api/preview/...` are intentionally bearer-token-free because native browser audio elements cannot attach Firebase authorization headers; the unguessable job ID scopes access to the generated 15-second preview. The Stripe webhook endpoint is called by Stripe and is verified with `STRIPE_WEBHOOK_SECRET`. Each submitted split job is tied to that signed-in Firebase user.

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
| `DEMUCS_MODEL` | `mdx_q` | Demucs model used for 2-stem vocal/instrumental previews. `mdx_q` is much faster on CPU than `htdemucs`; set `htdemucs` only if quality matters more than speed. |
| `DEMUCS_SHIFTS` | `0` | Demucs test-time shifts. `0` is fastest; raise only if you prefer quality over speed. |
| `DEMUCS_OVERLAP` | `0.1` | Demucs split overlap. Lower values are faster. |
| `DEMUCS_SEGMENT_SECONDS` | `8` | Smaller Demucs processing chunks reduce memory pressure and usually improve CPU stability on small Railway containers. |
| `DEMUCS_JOBS` | `0` | Optional Demucs worker count; keep `0` for one preview at a time unless you have spare CPU/RAM. |
| `DEMUCS_DEVICE` | empty | Optional Demucs device override such as `cuda` or `cpu`. |
| `DEMUCS_HIGH_MODEL` | `htdemucs` | Demucs model used when `/api/split` is called with `quality=high`. |
| `DEMUCS_HIGH_SHIFTS` | `1` | Higher-quality test-time shifts for `quality=high`; increase only where processing time is acceptable. |
| `DEMUCS_HIGH_OVERLAP` | `0.25` | Higher-quality split overlap for `quality=high`. |
| `DEMUCS_HIGH_SEGMENT_SECONDS` | `0` | Optional high-quality segment size; `0` lets Demucs use the selected model default. |
| `DEMUCS_HIGH_JOBS` | same as `DEMUCS_JOBS` | Optional Demucs worker count for `quality=high`. |
| `DEMUCS_HIGH_DEVICE` | same as `DEMUCS_DEVICE` | Optional device override for `quality=high`, such as `cuda` on GPU infrastructure. |
| `DEMUCS_CPU_THREADS` | `2` | CPU threads exposed to Torch/BLAS in the Demucs subprocess. Tune to your Railway CPU allocation. |
| `DEMUCS_CONCURRENCY` | `1` | Maximum concurrent Demucs subprocesses. Keep at `1` on CPU Railway deployments to avoid multiple jobs slowing each other down or exhausting memory. |
| `UPLOAD_DIR` | `uploads` | Temporary upload directory. |
| `OUTPUT_DIR` | `outputs` | Generated stems directory. |
| `PREVIEW_DURATION_SECONDS` | `15` | Free preview length to split into vocals and instrumental. |
| `PAYMENTS_ENABLED` | `true` when `STRIPE_SECRET_KEY` is present, otherwise `false` | Explicit feature flag for paid downloads and Checkout. Set to `true` in production, or leave unset when `STRIPE_SECRET_KEY` is configured. |
| `PRICE_PER_SONG_CENTS` | `300` | Full-song download price in cents; default is `$3.00`. |
| `PAYMENT_CURRENCY` | `aud` | Currency for Stripe Checkout. The production default is AUD for the $3.00 AUD download. |
| `STRIPE_SECRET_KEY` | empty | Stripe secret key used only by the FastAPI backend on Railway to create Checkout Sessions. If present and `PAYMENTS_ENABLED` is unset, paid downloads are enabled automatically. Never expose this in frontend code or `index.html`. |
| `STRIPE_WEBHOOK_SECRET` | empty | Stripe webhook signing secret used by `/api/stripe/webhook` to verify Stripe events before unlocking downloads. |
| `FRONTEND_URL` | `http://localhost:3000` | Frontend origin used for Stripe Checkout success/cancel redirects. Use only the scheme/host, for example `https://vocalsplitter.app`. |
| `FRONTEND_RETURN_PATH` | `/` | Default frontend route/path to receive Stripe redirects, for example `/download` or `/#/download`. The backend appends `payment`, `session_id`, and `job_id` query parameters. |
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
  formData.append("quality", "fast"); // or high

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
5. Show the 15-second vocal/instrumental preview after `status === "done"` using `job.stem_urls` / `job.preview_urls`; these point to `/api/preview/...` URLs that can be assigned directly to `<audio src>` without custom headers.
6. Let the user choose a download format (`wav`, `mp3`, `flac`, `ogg`, or `m4a`) and quality (`fast` or `high`) before uploading; send them as `output_format` and `quality`.
7. If `PAYMENTS_ENABLED=true`, call `/api/create-checkout-session` when the user wants the full song and redirect them to the returned `checkout_url`. Include `return_path` if your download UI is not at `/`, for example `{ "job_id": jobId, "item_type": "song", "return_path": "/download" }`. Do not put `STRIPE_SECRET_KEY` in frontend files such as `index.html`; it belongs only in Railway backend environment variables.
8. For a static Vercel `index.html`, store the current `job_id`, selected filename, output format, and UI state in `localStorage` as soon as `/api/split` returns. Stripe redirects back to `<return_path>?payment=success&session_id={CHECKOUT_SESSION_ID}&job_id=<job_id>` or `<return_path>?payment=cancelled&job_id=<job_id>`; parse those query params on page load and navigate/render the results/download section instead of showing the initial upload screen.
9. When `payment=success`, show a "Payment successful. Preparing your download..." message and call `GET /api/payment/verify?session_id=<session_id>&job_id=<job_id>` with the bearer token. The backend retrieves the Checkout Session from Stripe, verifies the session/job/user mapping, marks the job paid, and returns `download_urls.zip` plus paid stem URLs only when Stripe says it is paid.
10. When `payment=cancelled`, restore the saved job state from `localStorage` and show "Payment cancelled. Your preview is still available." Do not reset the upload UI after returning from Stripe; if `localStorage` has a previous `job_id`, offer a "Resume previous split" action.
11. Configure Stripe to send `checkout.session.completed` events to `/api/stripe/webhook`; webhook confirmation is still accepted, and backend Stripe API verification covers the immediate post-checkout redirect path.
12. Call `DELETE /api/cleanup/{job_id}` with the bearer token after the user downloads files or when leaving the result page.

The simple product is: free 15-second vocal/instrumental preview, then a safe Stripe-hosted $3 AUD per song checkout for the full download. The browser only asks the backend to create checkout and verify a returned session ID; the backend creates the Checkout Session, verifies session ownership/status with Stripe, and also accepts verified Stripe webhooks for unlocking downloads.

## Deployment notes

The included Dockerfile installs FFmpeg, Demucs, and DiffQ for the default quantized `mdx_q` model. Railway should use `railway.toml`; set `ALLOWED_ORIGINS` to your frontend origin, for example:

```text
ALLOWED_ORIGINS=https://your-frontend.vercel.app,http://localhost:3000
```

For speed, the default `quality=fast` path runs the quantized `mdx_q` Demucs model, no test-time shifts, low overlap, bounded segment size, one Demucs worker at a time, and explicit Torch/BLAS CPU thread limits. The optional `quality=high` path uses the configured higher-quality Demucs settings (`htdemucs`, shifts, and overlap by default) where the runtime has those model resources available. Set `DEMUCS_DEVICE=cuda` or `DEMUCS_HIGH_DEVICE=cuda` on GPU infrastructure for much faster processing. Firebase handles user identity. The current user stem totals and job stores are still in memory, so deploy as a single backend instance only for early testing. Add Postgres/Redis before production so usage counts and jobs survive restarts and work across multiple backend instances.
