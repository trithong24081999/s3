import re
import uuid
from fastapi import APIRouter, File, UploadFile, Depends, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.storage.interface import (
    StorageInterface,
    StorageError,
    StorageObjectNotFoundError,
)


import io
from typing import Optional
from PIL import Image, UnidentifiedImageError

router = APIRouter()

def get_storage_interface() -> StorageInterface:
    raise NotImplementedError("Storage interface not configured")

def generate_object_key(filename: str) -> str:
    safe_filename = re.sub(r'[^a-zA-Z0-9.\-_]', '_', filename or 'unnamed_file')
    safe_filename = safe_filename.replace('..', '_')
    return f"uploads/{uuid.uuid4()}/{safe_filename}"

class PresignRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    content_type: str = Field(
        "application/octet-stream",
        max_length=255,
        description="Pinned into the signed policy; the upload must match it.",
    )


@router.post("/files/presign")
async def presign_upload(
    request: PresignRequest,
    storage: StorageInterface = Depends(get_storage_interface),
    settings: Settings = Depends(get_settings),
):
    """
    Hand the browser a short-lived credential so it can POST the file straight
    to object storage. The bytes never pass through this service.

    The server still chooses the key, so a client cannot pick where its upload
    lands or overwrite somebody else's object.
    """
    key = generate_object_key(request.filename)

    try:
        presigned = await run_in_threadpool(
            storage.generate_presigned_upload,
            object_key=key,
            content_type=request.content_type,
            max_bytes=settings.max_upload_bytes,
            expires_in=settings.presign_expires_seconds,
        )
    except StorageError:
        raise HTTPException(status_code=500, detail="Could not create an upload URL.")

    return {
        "key": presigned.key,
        "url": presigned.url,
        "fields": presigned.fields,
        "expires_in": presigned.expires_in,
        "max_bytes": presigned.max_bytes,
    }


@router.post("/files")
async def upload_file(
    file: UploadFile = File(...),
    storage: StorageInterface = Depends(get_storage_interface),
):
    key = generate_object_key(file.filename)

    try:
        await run_in_threadpool(
            storage.upload_file,
            stream=file.file,
            object_key=key,
            content_type=file.content_type or "",
        )
    except StorageError:
        raise HTTPException(status_code=500, detail="Storage operation failed")

    return {
        "key": key,
        "filename": file.filename,
    }


# Refuse to decode absurdly large images before allocating pixel buffers.
MAX_SOURCE_PIXELS = 50_000_000


def _compute_target_size(
    size: tuple[int, int],
    width: Optional[int],
    height: Optional[int],
) -> tuple[int, int]:
    """Target size, preserving aspect ratio when only one dimension is given."""
    orig_width, orig_height = size
    if width and height:
        target_width, target_height = width, height
    elif width:
        target_width, target_height = width, round(width * orig_height / orig_width)
    else:
        target_width, target_height = round(height * orig_width / orig_height), height

    # Extreme aspect ratios can round a dimension down to 0, which Pillow rejects.
    return max(1, target_width), max(1, target_height)


# --- Helper function to resize images in a worker thread ---
def process_and_resize_image(
    image_bytes: bytes,
    width: Optional[int],
    height: Optional[int],
) -> tuple[bytes, str]:
    """
    Resizes image data while preserving aspect ratio if only one dimension is provided.
    Returns (processed_bytes, mime_type).
    """
    Image.init()  # populate Image.SAVE / Image.MIME with every plugin

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            source_format = (img.format or "").upper()
            orig_width, orig_height = img.size
            if orig_width * orig_height > MAX_SOURCE_PIXELS:
                raise ValueError("Source image is too large to process.")

            img.load()  # decode now so truncated data fails here, not mid-save

            target_size = _compute_target_size(img.size, width, height)
            resized_img = img.resize(target_size, Image.Resampling.LANCZOS)

            # Not every readable format has a writer (e.g. MPO); fall back to PNG.
            out_format = source_format if source_format in Image.SAVE else "PNG"

            save_kwargs = {}
            if out_format == "JPEG":
                # JPEG has no alpha/palette support.
                if resized_img.mode not in ("RGB", "L", "CMYK"):
                    resized_img = resized_img.convert("RGB")
                save_kwargs = {"quality": 90, "optimize": True}

            buffer = io.BytesIO()
            resized_img.save(buffer, format=out_format, **save_kwargs)

            mime_type = Image.MIME.get(out_format, f"image/{out_format.lower()}")
            return buffer.getvalue(), mime_type

    except UnidentifiedImageError:
        raise ValueError("File content is not a valid image format.")
    except Image.DecompressionBombError:
        raise ValueError("Source image is too large to process.")
    except OSError as e:
        raise ValueError(f"Image data could not be decoded: {e}")


# --- Resized Image Endpoint ---
@router.get("/files/resize")
async def get_resized_file(
    key: str = Query(..., min_length=1, description="S3 Object Key"),
    width: Optional[int] = Query(None, gt=0, le=4000, description="Target width in pixels"),
    height: Optional[int] = Query(None, gt=0, le=4000, description="Target height in pixels"),
    storage: StorageInterface = Depends(get_storage_interface),
):
    if width is None and height is None:
        raise HTTPException(
            status_code=400,
            detail="At least one dimension ('width' or 'height') must be specified."
        )

    # 1. Fetch original file from S3 / LocalStack
    try:
        file_bytes = await run_in_threadpool(storage.download_file, object_key=key)
    except StorageObjectNotFoundError:
        raise HTTPException(status_code=404, detail="File not found in storage.")
    except StorageError as e:
        # Transient backend trouble is not the client's fault.
        status = 503 if e.transient else 502
        raise HTTPException(status_code=status, detail="Failed to retrieve file from storage.")
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to retrieve file from storage.")

    # 2. Resize image in worker threadpool (non-blocking for CPU work)
    try:
        resized_bytes, mime_type = await run_in_threadpool(
            process_and_resize_image,
            image_bytes=file_bytes,
            width=width,
            height=height
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Image processing failed.")

    # 3. Return image stream
    return Response(
        content=resized_bytes,
        media_type=mime_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )