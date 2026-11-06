from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path


TEXT_SUFFIXES = {".txt", ".ocr", ".abc", ".ly", ".mei", ".mscx"}
NOTATION_SUFFIXES = {".abc", ".ly", ".mei", ".mscx", ".mscz", ".capx"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".jp2"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | NOTATION_SUFFIXES | IMAGE_SUFFIXES | {".pdf"}


def extract_local_content(path: str | Path, *, max_pdf_pages: int = 50,
                          max_ocr_pages: int = 5, max_archive_member_bytes: int = 20 * 1024 * 1024) -> dict:
    """Extract searchable text locally from text, notation, PDF and image files.

    No provider/database dependencies: callers persist results with their own provenance.
    OCR is optional and never sends documents to an external service.
    """
    source = Path(path)
    suffix = source.suffix.lower()
    result = {"status": "error", "text": "", "page_count": 0,
              "extracted_characters": 0, "error_text": None}
    if not source.is_file():
        result["error_text"] = "No existe el fichero en la ruta local registrada."
        return result
    if suffix not in SUPPORTED_SUFFIXES:
        result.update(status="unsupported", error_text="Formato sin extractor de texto.")
        return result
    try:
        text = ""
        if suffix in TEXT_SUFFIXES:
            text = source.read_text(encoding="utf-8-sig", errors="replace")
            result["status"] = "text_checked" if text.strip() else "text_no_content"
        elif suffix in {".mscz", ".capx"}:
            endings = (".mscx",) if suffix == ".mscz" else (".xml", ".capella", ".capx")
            with zipfile.ZipFile(source) as archive:
                candidates = [x for x in archive.infolist()
                              if x.filename.lower().endswith(endings)
                              and not x.filename.startswith("/")
                              and ".." not in Path(x.filename).parts]
                if not candidates:
                    raise ValueError("El paquete no contiene un archivo de notación reconocible.")
                member = candidates[0]
                if member.file_size > max_archive_member_bytes:
                    raise ValueError("El archivo interno supera el límite de 20 MiB.")
                with archive.open(member) as stream:
                    payload = stream.read(max_archive_member_bytes + 1)
                if len(payload) > max_archive_member_bytes:
                    raise ValueError("El archivo interno supera el límite de 20 MiB.")
                text = payload.decode("utf-8-sig", errors="replace")
            result["status"] = "text_checked" if text.strip() else "text_no_content"
        elif suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(source))
            result["page_count"] = len(reader.pages)
            page_texts = [(reader.pages[n].extract_text() or "")
                          for n in range(min(len(reader.pages), max_pdf_pages))]
            result["extracted_characters"] = sum(len(page) for page in page_texts)
            text = "\n".join(f"\n[PAGE {n + 1}]\n{page}" for n, page in enumerate(page_texts))
            if any(x.strip() for x in page_texts):
                result["status"] = "text_available"
                if len(reader.pages) > max_pdf_pages:
                    result["error_text"] = f"Texto extraído de las primeras {max_pdf_pages} páginas."
            else:
                try:
                    import fitz
                    import pytesseract
                    from PIL import Image
                    pytesseract.get_tesseract_version()
                except Exception as exc:
                    result.update(status="ocr_unavailable",
                                  error_text="OCR local no disponible: instala Tesseract y las dependencias Python. " + str(exc)[:300])
                    return result
                recognized_pages = []
                with fitz.open(str(source)) as document:
                    for page_index in range(min(len(reader.pages), max_ocr_pages)):
                        page = document[page_index]
                        dimension = max(float(page.rect.width), float(page.rect.height), 1.0)
                        scale = min(2.0, 2500.0 / dimension)
                        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                        image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                        language = os.getenv("AI_IMPORTACION_OCR_LANG", "eng").strip() or "eng"
                        recognized_pages.append(pytesseract.image_to_string(image, lang=language) or "")
                result["extracted_characters"] = sum(len(page) for page in recognized_pages)
                text = "\n".join(f"\n[PAGE {n + 1} OCR]\n{page}"
                                  for n, page in enumerate(recognized_pages))
                result["status"] = "ocr_completed" if any(x.strip() for x in recognized_pages) else "ocr_no_text"
                if len(reader.pages) > max_ocr_pages:
                    result["error_text"] = f"OCR limitado a las primeras {max_ocr_pages} páginas."
        elif suffix in IMAGE_SUFFIXES:
            try:
                import pytesseract
                from PIL import Image, ImageOps
                pytesseract.get_tesseract_version()
            except Exception as exc:
                result.update(status="ocr_unavailable",
                              error_text="OCR de imágenes no disponible: instala Tesseract, Pillow y pytesseract. " + str(exc)[:300])
                return result
            with Image.open(source) as original:
                image = ImageOps.exif_transpose(original)
                image.thumbnail((2500, 2500))
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                language = os.getenv("AI_IMPORTACION_OCR_LANG", "eng").strip() or "eng"
                text = pytesseract.image_to_string(image, lang=language) or ""
            result["page_count"] = 1
            result["status"] = "image_ocr_completed" if text.strip() else "image_ocr_no_text"
        result["text"] = text
        if suffix != ".pdf":
            result["extracted_characters"] = len(text)
        return result
    except Exception as exc:
        result.update(status="error", error_text=str(exc)[:1000])
        return result
