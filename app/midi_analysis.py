from __future__ import annotations

from pathlib import Path

MAX_MIDI_BYTES = 100 * 1024 * 1024
MAX_TRACKS = 256


def analyze_midi(path: str | Path) -> dict:
    """Read conservative structural metadata from Standard MIDI/Karaoke files."""
    import mido

    source = Path(path)
    size = source.stat().st_size
    if size <= 0:
        raise ValueError("El archivo MIDI está vacío.")
    if size > MAX_MIDI_BYTES:
        raise ValueError(f"El archivo MIDI supera el límite de análisis de {MAX_MIDI_BYTES} bytes.")

    try:
        midi = mido.MidiFile(str(source))
    except Exception as exc:
        raise ValueError(f"El archivo no es un MIDI válido: {exc}") from exc
    if len(midi.tracks) > MAX_TRACKS:
        raise ValueError(f"El MIDI contiene más de {MAX_TRACKS} pistas; se deja pendiente por seguridad.")

    track_names: list[str] = []
    instruments: list[dict] = []
    seen_instruments: set[tuple[int, int]] = set()
    note_on_count = 0
    time_signatures: list[dict] = []
    key_signatures: list[str] = []
    tempos: list[int] = []

    for track_index, track in enumerate(midi.tracks):
        for message in track:
            if message.is_meta:
                if message.type == "track_name" and message.name.strip():
                    name = message.name.strip()
                    if name not in track_names:
                        track_names.append(name)
                elif message.type == "time_signature":
                    value = {
                        "numerator": int(message.numerator),
                        "denominator": int(message.denominator),
                    }
                    if value not in time_signatures:
                        time_signatures.append(value)
                elif message.type == "key_signature" and message.key not in key_signatures:
                    key_signatures.append(message.key)
                elif message.type == "set_tempo" and int(message.tempo) not in tempos:
                    tempos.append(int(message.tempo))
                continue

            if message.type == "note_on" and int(message.velocity) > 0:
                note_on_count += 1
            elif message.type == "program_change":
                channel = int(message.channel)
                program = int(message.program)
                key = (channel, program)
                if key not in seen_instruments:
                    seen_instruments.add(key)
                    instruments.append({
                        "channel": channel,
                        "program": program,
                        "name": "Percusión" if channel == 9 else f"Programa GM {program + 1}",
                    })

    return {
        "format": "kar" if source.suffix.lower() == ".kar" else "midi",
        "status": "ok",
        "title": None,
        "track_count": len(midi.tracks),
        "track_names": track_names,
        "instruments": instruments,
        "note_on_count": note_on_count,
        "duration_seconds": round(float(midi.length), 3),
        "ticks_per_beat": int(midi.ticks_per_beat),
        "time_signatures": time_signatures,
        "key_signatures": key_signatures,
        "tempos_microseconds_per_beat": tempos,
        "source_size_bytes": size,
    }
