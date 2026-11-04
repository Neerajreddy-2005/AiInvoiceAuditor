"""Accepts an invoice uploaded through the API and queues it for the monitor."""
import hashlib
import io
import re
import uuid
import zipfile
from pathlib import PurePosixPath

from . import repository as repo
from . import storage
from .settings import agent_config

MAX_NAME_CHARS = 200


class UploadRejected(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def clean_name(raw: str) -> str:
    """Display only: the stored object gets a random name, so this never becomes a path."""
    name = PurePosixPath(raw.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        return name[:MAX_NAME_CHARS]
    return f"{stem[:MAX_NAME_CHARS - len(suffix) - 1]}.{suffix}"


def _is_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            return "word/document.xml" in package.namelist()
    except zipfile.BadZipFile:
        return False


def content_matches(extension: str, data: bytes) -> bool:
    """The extension says what a file claims to be; its first bytes say what it is."""
    if extension == ".pdf":
        return data.startswith(b"%PDF-")
    if extension == ".docx":
        return _is_docx(data)
    if extension == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if extension in (".tif", ".tiff"):
        return data.startswith((b"II*\x00", b"MM\x00*"))
    return False


def accept(raw_name: str, data: bytes, uploaded_by: str) -> str:
    """Validates and stores the file, then records it. Returns the new invoice's id."""
    if not storage.configured():
        raise UploadRejected(503, "Uploads aren't set up on this server yet")
    name = clean_name(raw_name)
    limit_mb = agent_config()["extraction"]["limits"]["max_file_mb"]
    if not data:
        raise UploadRejected(422, "That file is empty")
    if len(data) > limit_mb * 1024 * 1024:
        raise UploadRejected(413, f"That file is over the {limit_mb} MB limit")
    extension = PurePosixPath(name).suffix.lower()
    file_type = agent_config()["monitor"]["supported_extensions"].get(extension)
    if file_type is None:
        raise UploadRejected(422, "Upload a PDF, Word (.docx), PNG, JPEG or TIFF file")
    if not content_matches(extension, data):
        raise UploadRejected(422, f"That file's content isn't a real {extension} file")

    checksum = hashlib.sha256(data).hexdigest()
    existing = repo.get_by_checksum(checksum)
    if existing:
        raise UploadRejected(409, f"This exact file was already uploaded as {existing['file_name']}")

    key = f"{uuid.uuid4().hex}{extension}"
    storage.put(key, data)
    invoice_id = repo.claim_file(file_name=name, file_path=f"{storage.SCHEME}{key}", checksum=checksum,
                                 file_type=file_type, size=len(data), email_metadata={"uploaded_by": uploaded_by})
    if invoice_id is None:  # two uploads of one file at the same moment
        raise UploadRejected(409, "This exact file was already uploaded")
    return invoice_id
