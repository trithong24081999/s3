import re
import uuid
from fastapi import APIRouter, File, UploadFile, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from app.storage.interface import StorageInterface, StorageError

router = APIRouter()

def get_storage_interface() -> StorageInterface:
    raise NotImplementedError("Storage interface not configured")

def generate_object_key(filename: str) -> str:
    safe_filename = re.sub(r'[^a-zA-Z0-9.\-_]', '_', filename or 'unnamed_file')
    safe_filename = safe_filename.replace('..', '_')
    return f"uploads/{uuid.uuid4()}/{safe_filename}"

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
    except StorageError as e:
        raise HTTPException(status_code=500, detail="Storage operation failed")

    return {
        "key": key,
        "filename": file.filename,
    }


import io
import re
import uuid
from typing import Optional
from PIL import Image, UnidentifiedImageError

from fastapi import APIRouter, File, UploadFile, Depends, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool

from app.storage.interface import StorageInterface, StorageError

router = APIRouter()

def get_storage_interface() -> StorageInterface:
    raise NotImplementedError("Storage interface not configured")

def generate_object_key(filename: str) -> str:
    safe_filename = re.sub(r'[^a-zA-Z0-9.\-_]', '_', filename or 'unnamed_file')
    safe_filename = safe_filename.replace('..', '_')
    return f"uploads/{uuid.uuid4()}/{safe_filename}"


# --- Helper function to resize images in a worker thread ---
def process_and_resize_image(
    image_bytes: bytes, 
    width: Optional[int], 
    height: Optional[int]
) -> tuple[bytes, str]:
    """
    Resizes image data while preserving aspect ratio if only one dimension is provided.
    Returns (processed_bytes, mime_type).
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            original_format = img.format or "JPEG"
            
            # Determine target dimensions
            orig_width, orig_height = img.size
            if width and height:
                target_size = (width, height)
            elif width:
                target_height = int((width / orig_width) * orig_height)
                target_size = (width, target_height)
            elif height:
                target_width = int((height / orig_height) * orig_width)
                target_size = (target_width, height)
            else:
                target_size = (orig_width, orig_height)

            # High-quality downsampling
            resized_img = img.resize(target_size, Image.Resampling.LANCZOS)

            # Save to buffer
            buffer = io.BytesIO()
            resized_img.save(buffer, format=original_format)
            
            # Determine content-type header
            mime_type = Image.MIME.get(original_format, f"image/{original_format.lower()}")
            return buffer.getvalue(), mime_type

    except UnidentifiedImageError:
        raise ValueError("File content is not a valid image format.")


# --- Resized Image Endpoint ---
@router.get("/files/resize")
async def get_resized_file(
    key: str = Query(..., description="S3 Object Key"),
    width: Optional[int] = Query(None, gt=0, le=4000, description="Target width in pixels"),
    height: Optional[int] = Query(None, gt=0, le=4000, description="Target height in pixels"),
    storage: StorageInterface = Depends(get_storage_interface),
):
    if not width and not height:
        raise HTTPException(
            status_code=400, 
            detail="At least one dimension ('width' or 'height') must be specified."
        )

    # 1. Fetch original file from S3 / LocalStack
    try:
        file_bytes = await run_in_threadpool(storage.download_file, object_key=key)
    except StorageError:
        raise HTTPException(status_code=404, detail="File not found in storage.")
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
    return Response(content=resized_bytes, media_type=mime_type)