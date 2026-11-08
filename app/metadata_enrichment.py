from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import httpx

MODEL = os.getenv("AI_IMPORTACION_GEMINI_MODEL", "gemini-flash-lite-latest").strip()
CONFIDENCE_THRESHOLD = 0.70
MAX_CONTEXT_CHARS = 5000


def _configured_keys() -> list[str]:
    raw = []
    for name in ("GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.getenv(name, "").strip()
        if value:
            raw.extend(re.split(r"[,;\s]+", value))
    root = Path(__file__).resolve().parents[1]
    for filename in ("secrets", "secrets.txt"):
        try:
            lines = (root / filename).read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if "=" not in line and ":" not in line:
                continue
            name, value = re.split(r"[:=]", line, maxsplit=1)
            if name.strip().lower() in {"gemini_api_key", "gemini_api_keys", "google_api_key"}:
                raw.extend(re.split(r"[,;\s]+", value.strip().strip('"').strip("'")))
    return list(dict.fromkeys(key.strip() for key in raw if key.strip()))


def _json_response(text: str) -> dict:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^\`\`\`(?:json)?\s*|\s*\`\`\`$", "", candidate, flags=re.I)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("La IA no devolvió un objeto JSON.")
    value = json.loads(candidate[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("La respuesta de IA no es un objeto.")
    return value


def _clean_creator_items(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    creators = []
    allowed = {"composer", "arranger", "lyricist", "poet", "translator", "editor", "artist", "performer", "unknown"}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split()).strip()
        role = str(item.get("type") or "unknown").strip().lower()
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0
        if name and role in allowed and confidence >= CONFIDENCE_THRESHOLD:
            creators.append({"name": name[:300], "type": role, "source": "ai:filename_and_metadata",
                             "confidence": round(min(confidence, 1.0), 3)})
    return creators


def validate_ai_metadata(payload: dict) -> dict:
    """Accept only high-confidence, typed musical metadata returned by the model."""
    title = " ".join(str(payload.get("title") or "").split()).strip()
    try:
        title_confidence = float(payload.get("title_confidence", 0))
    except (TypeError, ValueError):
        title_confidence = 0
    accepted_title = title[:500] if title and title_confidence >= CONFIDENCE_THRESHOLD else None
    creators = _clean_creator_items(payload.get("creators"))
    candidates = []
    if title and not accepted_title:
        candidates.append({"value": title[:500], "source": "ai:filename_and_metadata",
                           "confidence": round(min(max(title_confidence, 0), 1), 3)})
    return {
        "title": accepted_title,
        "title_confidence": round(min(max(title_confidence, 0), 1), 3),
        "creators": creators,
        "title_candidates": candidates,
        "status": "ok",
        "model": MODEL,
    }


def enrich_music_metadata(*, filename: str, repository: dict, metadata: dict) -> dict:
    """Use Gemini to interpret a filename against existing score metadata and repository context.

    Missing or low-confidence fields stay empty; the original filename is never copied
    into the title field as a fallback.
    """
    keys = _configured_keys()
    if not keys:
        return {"status": "not_configured", "model": MODEL}
    context = {
        "filename": filename,
        "repository": str(repository.get("full_name") or ""),
        "repository_description": str(repository.get("description") or "")[:1000],
        "existing_metadata": metadata,
    }
    prompt = (
        "Eres un catalogador musical experto. Interpreta el nombre de archivo y contrástalo "
        "con los metadatos estructurales y el contexto del repositorio. No confundas el nombre "
        "del fichero con el título de la obra. Extrae el compositor, arreglista y otros autores "
        "solo cuando su función sea identificable. Separa números de opus/catálogo, tonalidad, "
        "instrumentación y notas editoriales del título. No inventes datos ni conviertas "
        "software, editoriales o repositorios en autores. Si hay varias interpretaciones "
        "posibles, devuelve confianza baja y deja el campo sin resolver. Responde SOLO JSON "
        "con title, title_confidence y creators; cada creator lleva name, type y confidence. "
        "Roles permitidos: composer, arranger, lyricist, poet, translator, editor, artist, "
        "performer, unknown. Confianza entre 0 y 1.\nDATOS:\n"
        + json.dumps(context, ensure_ascii=False)[:MAX_CONTEXT_CHARS]
    )
    last_error = None
    for key in keys:
        try:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + quote(MODEL, safe="-") + ":generateContent?key=" + quote(key, safe="")
            )
            response = httpx.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.1,
                        "responseMimeType": "application/json",
                    },
                },
                timeout=40,
            )
            response.raise_for_status()
            body = response.json()
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            return validate_ai_metadata(_json_response(text))
        except Exception as exc:
            last_error = str(exc)[:500]
    return {"status": "error", "model": MODEL, "error": last_error or "No se pudo consultar el modelo."}
