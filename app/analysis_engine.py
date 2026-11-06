from __future__ import annotations

from pathlib import Path

from app.musicxml_analysis import NotMusicXMLFileError, analyze_musicxml
from app.midi_analysis import analyze_midi


def analyze_local_music_file(path: str | Path) -> dict:
    """Run the shared structural music-file analyzer for any source provider.

    The function is deliberately independent of Archive.org and GitHub database
    models. Callers persist the returned result alongside their own provenance.
    """
    source = Path(path)
    suffix = source.suffix.lower()
    if not source.is_file():
        return {
            "analysis_type": "midi" if suffix in {".mid", ".midi", ".kar"} else "musicxml",
            "status": "error",
            "analysis": {"format": suffix.lstrip("."), "status": "error"},
            "error_text": "El archivo descargado no existe en la ruta local registrada.",
        }

    analysis_type = "midi" if suffix in {".mid", ".midi", ".kar"} else "musicxml"
    try:
        analysis = analyze_midi(source) if analysis_type == "midi" else analyze_musicxml(source)
        return {
            "analysis_type": analysis_type,
            "status": "ok",
            "analysis": analysis,
            "error_text": None,
        }
    except NotMusicXMLFileError as exc:
        if suffix == ".xml":
            return {
                "analysis_type": analysis_type,
                "status": "not_musicxml",
                "analysis": {
                    "format": "xml",
                    "status": "not_musicxml",
                    "message": "XML válido, pero no es una partitura MusicXML.",
                },
                "error_text": None,
            }
        return {
            "analysis_type": analysis_type,
            "status": "error",
            "analysis": {"format": suffix.lstrip("."), "status": "error"},
            "error_text": str(exc)[:1000],
        }
    except Exception as exc:
        return {
            "analysis_type": analysis_type,
            "status": "error",
            "analysis": {"format": suffix.lstrip("."), "status": "error"},
            "error_text": str(exc)[:1000],
        }
