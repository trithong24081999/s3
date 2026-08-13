# FastAPI S3-Compatible Upload Service

A FastAPI service for reliable, streaming uploads to LocalStack S3, Amazon S3, MinIO, or Garage object storage systems.

## Features
- Native memory-efficient stream passing: Routes `UploadFile` spooled files directly to `boto3`.
- Safe architecture using `StorageInterface` dependency injection instead of leaking `boto3` endpoints.
- Avoids blocking event loop natively by utilizing FastAPI's `run_in_threadpool`.
- Bulletproof file sanitization against path traversal payloads.
- Detailed transient error mapping for safe retry logic.
- On-the-fly image resizing (`GET /api/files/resize`).
- Presigned direct browser → storage uploads, with size and content-type signed into the policy.
- Age-based tiering: uploads older than 30 days move to a cheap self-hosted Garage tier.

## Installation

```bash
# Create venv and install
python3 -m venv .venv
source .venv/bin/activate
pip install .[test]
```

## Running the Stack

1. Setup environment variables:
   ```bash
   cp .env.example .env
   ```
2. Start LocalStack S3; it creates the `my-bucket` bucket on startup:
   ```bash
   docker compose up -d
   ```
   > If you are using the LocalStack `latest` image, set `LOCALSTACK_AUTH_TOKEN` in `.env` before startup.
3. Run the API:
   ```bash
   uvicorn app.main:app --reload
   ```

## Example Usage

```bash
# Upload
curl -X POST http://localhost:8000/api/files \
  -F "file=@/path/to/some/example.pdf"
# {"key":"uploads/<uuid>/example.pdf","filename":"example.pdf"}

# Resize (aspect ratio preserved when only one dimension is given).
# Works whether the object is in the hot or the cold tier.
curl -G http://localhost:8000/api/files/resize \
  --data-urlencode "key=uploads/<uuid>/photo.jpg" \
  -d "width=320" -o resized.jpg
```

`--data-urlencode` matters on `key` — object keys contain slashes.

## Configuration

The default `.env.example` is ready for LocalStack. To use another S3-compatible provider, update the S3 settings below.

Since this uses `addressing_style: path` and `s3v4` authentication, switching to Garage or genuine AWS S3 only requires modifying your `.env`:

**Garage Object Storage:**
```env
S3_ENDPOINT=http://<garage-gateway-ip>:3900
AWS_ACCESS_KEY_ID=<garage-key>
AWS_SECRET_ACCESS_KEY=<garage-secret>
```

**AWS S3:**
```env
S3_ENDPOINT=https://s3.us-east-1.amazonaws.com
AWS_REGION=us-east-1
...
```

## Direct Browser Uploads (presigned)

For large files, routing bytes through this service is wasted bandwidth. Ask for
a credential instead and let the browser upload straight to storage:

```
Browser ──1. POST /api/files/presign──▶ FastAPI
Browser ◀─2. url + form fields ────────┘
Browser ──3. POST file ───────────────▶ S3     (FastAPI not involved)
```

```bash
curl -X POST http://localhost:8000/api/files/presign \
  -H 'Content-Type: application/json' \
  -d '{"filename":"photo.jpg","content_type":"image/jpeg"}'
```

```json
{
  "key": "uploads/<uuid>/photo.jpg",
  "url": "http://127.0.0.1:4566/my-bucket",
  "fields": { "key": "...", "Content-Type": "image/jpeg", "policy": "...", "...": "..." },
  "expires_in": 900,
  "max_bytes": 26214400
}
```

The browser then posts every entry of `fields` **before** the file part — S3
ignores anything after it:

```js
const { url, fields, key } = await (await fetch('/api/files/presign', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ filename: file.name, content_type: file.type }),
})).json();

const form = new FormData();
Object.entries(fields).forEach(([k, v]) => form.append(k, v));
form.append('file', file);              // must be last

await fetch(url, { method: 'POST', body: form });   // 204 on success
// `key` is what you store in your DB and pass to /api/files/resize
```

### What the signature actually guarantees

The bytes never reach this service, so the signed POST policy is the only
enforcement left. It pins three things, and the **storage backend** rejects a
mismatch:

| Tampering attempt | Result |
| --- | --- |
| File larger than `MAX_UPLOAD_BYTES` | `400 EntityTooLarge` |
| `Content-Type` changed after signing | `403 AccessDenied` |
| `key` changed to target another object | `403 AccessDenied` |
| Signature or policy forged | `403 AccessDenied` |

The server also picks the key, so a client cannot choose where its upload lands
or overwrite an existing object.

> **LocalStack does not verify presigned POST signatures.** A request with a
> forged `x-amz-signature` is accepted (`204`) there, while Garage correctly
> rejects it with `403`. The size cap *is* enforced by LocalStack. Do not
> conclude from a passing local test that the signature path works — verify
> against Garage or real S3.

> Uploading from a browser is cross-origin, so the bucket needs a CORS rule.
> `init-s3.sh` installs a permissive dev one; narrow `AllowedOrigins` to your
> frontend before deploying. Without it the browser blocks the request before
> sending, and you get an opaque network error rather than an S3 error.

> `/api/files/presign` is unauthenticated, like the rest of this service. Anyone
> who can reach it can obtain an upload credential. Put auth in front of it
> before exposing it.

> If the browser reaches storage at a different address than this service does
> (`localstack:4566` inside compose vs `localhost:4566` outside), set
> `S3_PUBLIC_ENDPOINT`. The host is part of the signature, so a URL signed for
> the wrong host cannot be rewritten afterwards.

## Storage Tiering

Fresh uploads land in the **hot** tier (LocalStack/S3). After `TIERING_MAX_AGE_DAYS`
they are moved to the **cold** tier — a self-hosted [Garage](https://garagehq.deuxfleurs.fr/)
cluster that runs alongside LocalStack in `docker-compose.yml`.

```
uploads/                          hot: LocalStack s3://my-bucket
   ├── photo1.jpg
   ├── photo2.jpg
   └── photo3.jpg
        │
        │  older than 30 days  →  python -m app.tiering
        ▼
uploads/                          cold: Garage s3://uploads-archive
   └── photo1.jpg
```

Objects keep the **same key** in both tiers. `TieredStorage` reads hot first and
falls back to cold, so `GET /api/files/resize?key=…` keeps working unchanged
after an object is archived — callers never learn which tier served them.

### Why a job and not a lifecycle rule

S3 lifecycle **transitions** target AWS storage classes (`STANDARD_IA`, `GLACIER`);
they cannot move an object to a different server. Garage implements neither
storage classes nor lifecycle rules. So the move is an explicit
copy → verify → delete, in that order: a crash at any point leaves the object
readable from at least one tier, never zero.

### Running the tiering job

```bash
# See what would move — no copies, no deletes
python -m app.tiering --dry-run

# Move everything past the cutoff
python -m app.tiering

# Narrower passes
python -m app.tiering --max-age-days 7 --limit 100 --prefix uploads/2025/
```

Exit code is `1` if any object failed to move, so cron can alert on it:

```cron
17 3 * * *  cd /path/to/fastapi-s3-project && .venv/bin/python -m app.tiering >> /var/log/tiering.log 2>&1
```

Re-running is safe. A complete cold copy short-circuits the transfer and the job
resumes at the delete; a *truncated* copy is redone rather than trusted.

### Garage setup

`docker compose up -d` starts Garage and runs `scripts/garage-init.sh`, which is
idempotent — it assigns the cluster layout, creates the archive bucket, imports
the key pair from `.env`, and grants access. Re-running it against a configured
cluster changes nothing.

Generate the credentials once and put them in `.env`:

```bash
printf 'GARAGE_ACCESS_KEY_ID=GK%s\n' "$(openssl rand -hex 12)"
echo "GARAGE_SECRET_ACCESS_KEY=$(openssl rand -hex 32)"
echo "GARAGE_RPC_SECRET=$(openssl rand -hex 32)"
echo "GARAGE_ADMIN_TOKEN=$(openssl rand -hex 32)"
```

Because `garage-init` *imports* these rather than generating them, the same
credentials keep working across restarts and volume wipes.

> Leave `GARAGE_ACCESS_KEY_ID` / `GARAGE_SECRET_ACCESS_KEY` empty to run
> hot-only. The API behaves exactly as before and the tiering job exits with a
> clear message instead of guessing.

> The Garage S3 port is published on `127.0.0.1:3910` (not the usual 3900, which
> is often already taken by another Garage on the same machine). Change
> `GARAGE_S3_PORT` if it clashes — whichever container wins the port silently
> answers the other project's clients, which looks like an authentication failure.

> `replication_factor = 1` makes this a single-node dev cluster. Raise it and add
> nodes before trusting it with real data: once the job deletes the hot original,
> the archive is the **only** copy.

## How Streaming Upload Works
We intentionally do **not** use `await file.read()` (which buffers into RAM). FastAPI provides `file.file` as a `SpooledTemporaryFile`. This file-like object gets mapped directly via `run_in_threadpool` into `boto3`'s `upload_fileobj`, allowing `boto3`'s highly optimized transfer manager to handle bounded chunk streaming based on the `multipart_threshold` environment parameters.
# s3
