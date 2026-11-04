"""Extractor agent (no LLM): file -> raw text.

PDF:   pdfplumber per page; a page without a usable text layer is
       rendered with Poppler and OCR'd with Tesseract.
DOCX:  python-docx paragraphs and tables (table rows become "a | b | c").
Image: Tesseract with light preprocessing.

The full text is returned so it can be stored in PostgreSQL; OCR is
expensive and is never repeated for the same file.
"""
import hashlib
import json
import logging
import os
import resource
import signal
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .settings import agent_config

log = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    text: str
    method: str          # pdfplumber | pdfplumber+tesseract | tesseract | docx
    pages: int
    ocr_pages: int


def _ocr_settings() -> dict:
    return agent_config()["extraction"]["ocr"]


def _limits() -> dict:
    return agent_config()["extraction"]["limits"]


def _check_pixels(width: float, height: float) -> None:
    if width * height > _limits()["max_image_pixels"]:
        raise ValueError(f"Image of {int(width)}x{int(height)} pixels is over the OCR limit")


def preprocess_for_ocr(image: Image.Image) -> Image.Image:
    """Grayscale, upscale small images, stretch contrast, binarise."""
    cfg = _ocr_settings()
    img = ImageOps.exif_transpose(image).convert("L")
    _check_pixels(img.width, img.height)
    if img.width < cfg["min_width_px"]:
        scale = cfg["min_width_px"] / img.width
        _check_pixels(img.width * scale, img.height * scale)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    img = ImageOps.autocontrast(img)
    return img.point(lambda p: 255 if p > 160 else 0)


def ocr_image(image: Image.Image) -> str:
    import pytesseract

    cfg = _ocr_settings()
    return pytesseract.image_to_string(preprocess_for_ocr(image), lang=cfg["languages"],
                                       config=f"--psm {cfg['psm']}", timeout=_limits()["ocr_timeout_seconds"])


def extract_pdf(path: Path) -> ExtractionResult:
    import pdfplumber

    min_chars = agent_config()["extraction"]["min_text_chars_per_page"]
    texts: list[str] = []
    ocr_pages = 0
    with pdfplumber.open(path) as pdf:
        page_count = len(pdf.pages)
        if page_count > _limits()["max_pdf_pages"]:
            raise ValueError(f"PDF has {page_count} pages, over the limit")
        for index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if len(text.strip()) < min_chars:
                scale = _ocr_settings()["dpi"] / 72
                _check_pixels(float(page.width) * scale, float(page.height) * scale)
                log.info("%s page %s has no text layer; falling back to OCR", path.name, index)
                text = _ocr_pdf_page(path, index)
                ocr_pages += 1
            texts.append(text)
    if ocr_pages == 0:
        method = "pdfplumber"
    elif ocr_pages == page_count:
        method = "tesseract"
    else:
        method = "pdfplumber+tesseract"
    return ExtractionResult("\n\n".join(texts).strip(), method, page_count, ocr_pages)


def _ocr_pdf_page(path: Path, page_number: int) -> str:
    from pdf2image import convert_from_path

    images = convert_from_path(str(path), dpi=_ocr_settings()["dpi"],
                               first_page=page_number, last_page=page_number,
                               timeout=_limits()["ocr_timeout_seconds"])
    return "\n".join(ocr_image(img) for img in images)


def extract_docx(path: Path) -> ExtractionResult:
    import docx

    with zipfile.ZipFile(path) as package:
        members = package.infolist()
    # Everything except pictures counts as XML, because a part can be given any file name.
    xml_bytes = sum(i.file_size for i in members if not i.filename.startswith("word/media/"))
    total_bytes = sum(i.file_size for i in members)
    if xml_bytes > _limits()["max_docx_xml_mb"] * 1024 * 1024 or total_bytes > _limits()["max_file_mb"] * 1024 * 1024:
        raise ValueError("DOCX expands beyond the limit")
    document = docx.Document(str(path))
    lines = [p.text for p in document.paragraphs]
    for table in document.tables:
        lines.append("")
        for row in table.rows:
            cells, previous = [], None
            for cell in row.cells:
                if cell._tc is not previous:  # a merged cell appears once per column it spans
                    cells.append(cell.text.strip())
                previous = cell._tc
            lines.append(" | ".join(cells))
    text = "\n".join(lines).strip()
    return ExtractionResult(text, "docx", 1, 0)


def extract_image(path: Path) -> ExtractionResult:
    with Image.open(path) as img:
        _check_pixels(img.width, img.height)  # the size is in the header, so this runs before decoding
        text = ocr_image(img)
    return ExtractionResult(text.strip(), "tesseract", 1, 1)


EXTRACTORS = {"pdf": extract_pdf, "docx": extract_docx, "image": extract_image}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_in_child(path: Path, file_type: str) -> ExtractionResult:
    """Parse untrusted files in a separate process with no secrets in its environment.

    A parser flaw then can't read credentials, and a slow or memory-hungry file
    stops only this process, not the API.
    """
    limits = _limits()
    memory = limits["max_parser_memory_mb"] * 1024 * 1024
    env = {name: os.environ[name] for name in ("PATH", "CONFIG_DIR", "LANG", "LC_ALL") if name in os.environ}
    child = subprocess.Popen(
        [sys.executable, "-m", "app.extraction", str(path), file_type],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_AS, (memory, memory)))
    try:
        out, err = child.communicate(timeout=limits["max_document_seconds"])
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)  # also stops any OCR process it started
        child.wait()
        raise ValueError(f"Parsing took longer than {limits['max_document_seconds']} seconds") from None
    if child.returncode != 0:
        last_line = (err.strip().splitlines() or ["the parser stopped unexpectedly"])[-1]
        raise ValueError(f"Could not read the file: {last_line[:300]}")
    return ExtractionResult(**json.loads(out))


def extract(path: Path, file_type: str) -> ExtractionResult:
    if file_type not in EXTRACTORS:
        raise ValueError(f"No extractor for file type {file_type!r}")
    limits = _limits()
    if path.stat().st_size > limits["max_file_mb"] * 1024 * 1024:
        raise ValueError(f"File is over the {limits['max_file_mb']} MB limit")
    result = _extract_in_child(path, file_type)
    if len(result.text) > limits["max_text_chars"]:
        raise ValueError(f"Extracted text of {len(result.text)} characters is over the limit")
    log.info("Extracted %s chars from %s via %s", len(result.text), path.name, result.method)
    return result


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)  # stdout carries only the result
    print(json.dumps(asdict(EXTRACTORS[sys.argv[2]](Path(sys.argv[1])))))
