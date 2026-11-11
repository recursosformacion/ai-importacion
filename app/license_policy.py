"""Clasificación de la licencia declarada por un repositorio de GitHub.

Resumen: decide si un recurso puede alojarse en OSAP (`hostable`), si solo puede
enlazarse al origen (`link_only`) o si no hay información suficiente (`unknown`).
Solo considera la licencia declarada por el repositorio; no verifica la licencia
de cada fichero ni sustituye una revisión jurídica.
"""

from __future__ import annotations

POLICY_VERSION = "2026-11-11"

HOSTABLE = "hostable"
LINK_ONLY = "link_only"
UNKNOWN = "unknown"

# Licencias que permiten redistribuir el fichero conservando atribución y, cuando
# procede, compartir igual. Las licencias ausentes o no reconocidas no habilitan
# el alojamiento: el recurso se enlaza al origen.
HOSTING_ALLOWED_SPDX = frozenset(
    {
        "cc0-1.0",
        "cc-by-4.0",
        "cc-by-sa-4.0",
        "mit",
        "apache-2.0",
        "bsd-2-clause",
        "bsd-3-clause",
        "gpl-2.0",
        "gpl-3.0",
        "lgpl-2.1",
        "lgpl-3.0",
        "mpl-2.0",
    }
)

UNKNOWN_SPDX = frozenset({"", "noassertion", "other"})


def normalize_spdx(value: str | None) -> str:
    return (value or "").strip().casefold()


def is_known(spdx: str | None) -> bool:
    return normalize_spdx(spdx) not in UNKNOWN_SPDX


def evaluate(spdx: str | None) -> str:
    """Clasifica una licencia declarada para decidir el alojamiento."""
    normalized = normalize_spdx(spdx)
    if normalized in UNKNOWN_SPDX:
        return UNKNOWN
    if normalized in HOSTING_ALLOWED_SPDX:
        return HOSTABLE
    return LINK_ONLY


def license_url(spdx: str | None) -> str | None:
    """Enlace informativo a la ficha SPDX, cuando la licencia es conocida."""
    normalized = normalize_spdx(spdx)
    if normalized in UNKNOWN_SPDX:
        return None
    return f"https://spdx.org/licenses/{normalized.upper()}.html"
