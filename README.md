# FastAPI S3-Compatible Upload Service

A FastAPI service for reliable, streaming uploads to LocalStack S3, Amazon S3, MinIO, or Garage object storage systems.

## Features
- Native memory-efficient stream passing: Routes `UploadFile` spooled files directly to `boto3`.
- Safe architecture using `StorageInterface` dependency injection instead of leaking `boto3` endpoints.
- Avoids blocking event loop natively by utilizing FastAPI's `run_in_threadpool`.
- Bulletproof file sanitization against path traversal payloads.
- Detailed transient error mapping for safe retry logic.

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

## Example Upload Usage

```bash
curl -X POST http://localhost:8000/api/files \
  -F "file=@/path/to/some/example.pdf"
```

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

## How Streaming Upload Works
We intentionally do **not** use `await file.read()` (which buffers into RAM). FastAPI provides `file.file` as a `SpooledTemporaryFile`. This file-like object gets mapped directly via `run_in_threadpool` into `boto3`'s `upload_fileobj`, allowing `boto3`'s highly optimized transfer manager to handle bounded chunk streaming based on the `multipart_threshold` environment parameters.
# s3
