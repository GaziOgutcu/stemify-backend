# Stemify Backend

FastAPI backend for uploading an audio file and splitting a 15-second preview into vocals and instrumental with Demucs.

## API endpoints

- `GET /api` - service metadata and configured limits.
- `GET /api/health` - health check for deployments.
- `POST /api/auth/signup` - create an account with `email` and `password`, returning a bearer token and user stem totals.
- `POST /api/auth/signin` - sign in with `email` and `password`, returning a bearer token and user stem totals.
- `GET /api/auth/social/providers` - list social sign-in providers configured for the frontend, such as Google or Apple.
- `GET /api/me` - return the signed-in user's profile and stem usage totals.
- `GET /api/payments/config` - return paid-download settings such as price per song and currency.
- `POST /api/payments/checkout` - create a Stripe Checkout session for a full-song download when paid downloads are enabled.
- `POST /api/payments/confirm` - confirm a Stripe Checkout session before allowing paid downloads.
- `POST /api/split` - multipart form upload with:
  - `file`: `.mp3`, `.wav`, `.flac`, `.aac`, `.ogg`, or `.m4a`
  - `stems`: always `2` for vocals and instrumental
- `GET /api/job/{job_id}` - poll job status until it is `done` or `error`.
- `GET /api/download/{job_id}/zip` - download all produced stems as a ZIP.
- `GET /api/download/{job_id}/stem/{filename}` - download one WAV stem.
- `DELETE /api/cleanup/{job_id}` - remove output files and forget the job.

All split, job status, payment checkout/confirm, download, cleanup, and profile endpoints require an `Authorization: Bearer <token>` header from sign up or sign in. Each submitted split job is tied to that signed-in user, and the API increments the user's `jobs_created` and `stem_count` totals when the job is accepted.

Job status responses include `status_detail`, `elapsed_seconds`, and `timeout_seconds` so the frontend can show a clear message instead of a vague "finalising" spinner. Downloads and checkout return `409` until the preview job status is `done`.

Social sign in is not fully automatic from the backend alone. The frontend still needs Google/Apple buttons and provider SDKs, plus production token verification on the backend. `GET /api/auth/social/providers` exposes which providers are configured so the HTML can show or hide those buttons.

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
| `UPLOAD_DIR` | `uploads` | Temporary upload directory. |
| `OUTPUT_DIR` | `outputs` | Generated stems directory. |
| `PREVIEW_DURATION_SECONDS` | `15` | Free preview length to split into vocals and instrumental. |
| `PAYMENTS_ENABLED` | `false` | Set to `true` to require payment before downloads. |
| `PRICE_PER_SONG_CENTS` | `300` | Full-song download price in cents; default is `$3.00`. |
| `PAYMENT_CURRENCY` | `usd` | Currency for Stripe Checkout. |
| `STRIPE_SECRET_KEY` | empty | Stripe secret key used to create and confirm Checkout sessions. |
| `FRONTEND_URL` | `http://localhost:3000` | Frontend URL used for Stripe Checkout success/cancel redirects. |
| `GOOGLE_CLIENT_ID` | empty | Enables Google as a social sign-in option in `/api/auth/social/providers`. |
| `APPLE_CLIENT_ID` | empty | Enables Apple as a social sign-in option in `/api/auth/social/providers`. |

## Frontend integration guide

Set a frontend environment variable such as `NEXT_PUBLIC_API_URL=https://your-backend.up.railway.app`.

Example flow:

```ts
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function signUp(email: string, password: string) {
  const response = await fetch(`${API_URL}/api/auth/signup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<{
    token: string;
    user: { email: string; stem_count: number; jobs_created: number };
  }>;
}

export async function splitTrack(file: File, token: string) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("stems", "2");

  const upload = await fetch(`${API_URL}/api/split`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: formData,
  });
  if (!upload.ok) throw new Error(await upload.text());

  const { job_id } = await upload.json();

  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const statusResponse = await fetch(`${API_URL}/api/job/${job_id}`, {
      headers: { Authorization: `Bearer ${token}` },
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

export async function getSocialProviders() {
  const response = await fetch(`${API_URL}/api/auth/social/providers`);
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<{
    providers: Array<{
      provider: "google" | "apple";
      display_name: string;
      enabled: boolean;
      client_id: string | null;
    }>;
  }>;
}

export async function createDownloadCheckout(token: string, jobId: string) {
  const response = await fetch(`${API_URL}/api/payments/checkout`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ job_id: jobId, item_type: "song" }),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<{ checkout_session_id: string; checkout_url: string }>;
}
```

Production checklist for the frontend:

1. Require sign up/sign in before enabling uploads, then store the returned bearer token securely for API calls.
2. Validate file extensions and display the backend size limit before upload.
3. Disable the upload button while a job is processing.
4. Poll `/api/job/{job_id}` every 2-5 seconds with the bearer token. Make sure the frontend starts only one polling interval per job and clears it when the job reaches `done` or `error`.
5. Show the 15-second vocal/instrumental preview after `status === "done"`.
6. If `PAYMENTS_ENABLED=true`, call `/api/payments/checkout` when the user wants the full song and redirect them to the returned `checkout_url`.
7. Call `DELETE /api/cleanup/{job_id}` with the bearer token after the user downloads files or when leaving the result page.

No subscription, donation, or Buy Me A Coffee flow is required. The simple product is: free 15-second vocal/instrumental preview, then $3 per song for the full download.

## Deployment notes

The included Dockerfile installs FFmpeg and Demucs. Railway should use `railway.toml`; set `ALLOWED_ORIGINS` to your frontend origin, for example:

```text
ALLOWED_ORIGINS=https://your-frontend.vercel.app,http://localhost:3000
```

The current auth, token, user stem totals, and job stores are in memory, so deploy as a single backend instance only for early testing. Add Postgres/Redis before production so accounts, sessions, and stem counts survive restarts and work across multiple backend instances.
