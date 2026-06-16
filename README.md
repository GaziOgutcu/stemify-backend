# Stemify Backend

FastAPI backend for uploading an audio file and splitting it into stems with Demucs.

## API endpoints

- `GET /api` - service metadata and configured limits.
- `GET /api/health` - health check for deployments.
- `POST /api/split` - multipart form upload with:
  - `file`: `.mp3`, `.wav`, `.flac`, `.aac`, `.ogg`, or `.m4a`
  - `stems`: `2`, `4`, or `6` (defaults to `4`)
- `GET /api/job/{job_id}` - poll job status until it is `done` or `error`.
- `GET /api/download/{job_id}/zip` - download all produced stems as a ZIP.
- `GET /api/download/{job_id}/stem/{filename}` - download one WAV stem.
- `DELETE /api/cleanup/{job_id}` - remove output files and forget the job.

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

## Frontend integration guide

Set a frontend environment variable such as `NEXT_PUBLIC_API_URL=https://your-backend.up.railway.app`.

Example flow:

```ts
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function splitTrack(file: File, stems = 4) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("stems", String(stems));

  const upload = await fetch(`${API_URL}/api/split`, {
    method: "POST",
    body: formData,
  });
  if (!upload.ok) throw new Error(await upload.text());

  const { job_id } = await upload.json();

  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const statusResponse = await fetch(`${API_URL}/api/job/${job_id}`);
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
```

Production checklist for the frontend:

1. Validate file extensions and display the backend size limit before upload.
2. Disable the upload button while a job is processing.
3. Poll `/api/job/{job_id}` every 2-5 seconds.
4. Show individual stem links and the ZIP link after `status === "done"`.
5. Call `DELETE /api/cleanup/{job_id}` after the user downloads files or when leaving the result page.

## Deployment notes

The included Dockerfile installs FFmpeg and Demucs. Railway should use `railway.toml`; set `ALLOWED_ORIGINS` to your frontend origin, for example:

```text
ALLOWED_ORIGINS=https://your-frontend.vercel.app,http://localhost:3000
```

The current job store is in memory, so deploy as a single backend instance unless you add Redis/Postgres for durable job state.
