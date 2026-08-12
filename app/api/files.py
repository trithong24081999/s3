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
