#!/usr/bin/env python3.11
"""
photo_importer.py — CLI para importar fotos desde tarjetas SD a ~/Pictures.

Estructura de destino:
    ~/Pictures/<AÑO>/<MM-DD>/<FABRICANTE>/<SOOC|RAW|EDITED>/

    El año y la fecha provienen de los metadatos EXIF de cada foto
    (DateTimeOriginal). Si no hay EXIF, se usa la fecha de modificación
    del archivo como fallback.

Tipos de archivo soportados:
    SOOC  : .jpg .jpeg .heif .heic .hif
    RAW   : .arw .raf .nef .cr2 .cr3 .dng .orf .rw2

Uso:
    python3.11 photo_importer.py

Requisitos:
    Python 3.11+
    questionary  (pip install questionary)
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import questionary
from questionary import Choice

# ---------------------------------------------------------------------------
# Constantes globales
# ---------------------------------------------------------------------------

PICTURES_ROOT = Path.home() / "Pictures"

# Extensiones clasificadas (en minúsculas)
SOOC_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".heif", ".heic", ".hif"})
RAW_EXTENSIONS: frozenset[str]  = frozenset({".arw", ".raf", ".nef", ".cr2", ".cr3", ".dng", ".orf", ".rw2"})
ALL_EXTENSIONS: frozenset[str]  = SOOC_EXTENSIONS | RAW_EXTENSIONS

# Nombre de la subcarpeta cuando no se puede determinar el fabricante
UNKNOWN_CAMERA = "NO_CAMERA"

# Subcarpetas que se crean siempre dentro de cada directorio de fabricante
CAMERA_SUBDIRS = ("SOOC", "RAW", "EDITED")

# Volúmenes del sistema que se excluyen del menú de selección
SYSTEM_VOLUMES: frozenset[str] = frozenset({"Macintosh HD", "Data", "Preboot", "Recovery", "VM"})


# ---------------------------------------------------------------------------
# Tipos de datos
# ---------------------------------------------------------------------------

class PhotoInfo(NamedTuple):
    """Metadatos extraídos de una foto durante el escaneo."""
    path: Path       # Ruta absoluta al archivo original en la SD
    date: datetime   # Fecha de captura (EXIF) o de modificación (fallback)
    make: str        # Fabricante de la cámara, ej: "SONY", "FUJIFILM"
    category: str    # "SOOC" o "RAW"


class CopyResult(NamedTuple):
    """Estadísticas del proceso de copia."""
    copied: int
    skipped: int
    errors: int


# ---------------------------------------------------------------------------
# Detección de medios externos (macOS)
# ---------------------------------------------------------------------------

def list_external_volumes() -> list[Path]:
    """
    Devuelve una lista de rutas a volúmenes montados en /Volumes que no
    pertenecen al sistema interno del Mac.

    Usa `diskutil info` para identificar volúmenes internos/sistema y
    excluirlos. Como fallback, filtra por nombres conocidos del sistema.

    Returns:
        Lista de Path a los puntos de montaje de volúmenes externos,
        ordenados alfabéticamente.
    """
    volumes_root = Path("/Volumes")
    if not volumes_root.exists():
        return []

    external: list[Path] = []
    for entry in sorted(volumes_root.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name in SYSTEM_VOLUMES:
            continue
        if _is_internal_volume(entry):
            continue
        external.append(entry)

    return external


def _is_internal_volume(mount_point: Path) -> bool:
    """
    Consulta `diskutil info` para determinar si un volumen es interno.

    Args:
        mount_point: Ruta al punto de montaje del volumen.

    Returns:
        True si el volumen es interno o del sistema, False si es externo.
    """
    try:
        result = subprocess.run(
            ["diskutil", "info", str(mount_point)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout.lower()
        if "internal:                  yes" in output or "internal:                 yes" in output:
            return True
        if "protocol:               apple fabric" in output:
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def display_volume_menu(volumes: list[Path]) -> Path | None:
    """
    Muestra un menú interactivo con los volúmenes externos disponibles y
    devuelve el elegido por el usuario.

    Args:
        volumes: Lista de rutas a volúmenes externos.

    Returns:
        Path al volumen seleccionado, o None si el usuario cancela.
    """
    print("\n╔══════════════════════════════════════════╗")
    print("║         PHOTO IMPORTER — SD Selector     ║")
    print("╚══════════════════════════════════════════╝\n")

    if not volumes:
        print("  No se encontraron tarjetas SD ni volúmenes externos.")
        print("  Conecta una tarjeta SD e intenta de nuevo.\n")
        return None

    choices = [
        Choice(
            title=f"{vol.name:<22} {_get_volume_size(vol):>9}   {vol}",
            value=vol,
        )
        for vol in volumes
    ]
    choices.append(Choice(title="Salir", value=None))

    selected = questionary.select(
        "Selecciona el volumen a importar:",
        choices=choices,
    ).ask()

    if selected is not None:
        print(f"\n  Volumen seleccionado: {selected.name} ({selected})\n")
    return selected


def _get_volume_size(path: Path) -> str:
    """
    Devuelve el espacio total del volumen como cadena legible (ej: "64.0 GB").

    Args:
        path: Ruta al punto de montaje.

    Returns:
        Cadena con el tamaño formateado, o "" si no se puede obtener.
    """
    try:
        stat = os.statvfs(path)
        return _format_bytes(stat.f_blocks * stat.f_frsize)
    except OSError:
        return ""


def _format_bytes(n: int) -> str:
    """Convierte bytes a cadena legible con la unidad apropiada (B → TB)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Parser EXIF (sin dependencias externas)
# ---------------------------------------------------------------------------

# Tags EXIF relevantes
_TAG_MAKE          = 0x010F   # IFD0: fabricante de la cámara
_TAG_EXIF_IFD      = 0x8769   # IFD0: puntero al IFD EXIF subyacente
_TAG_DATE_ORIGINAL = 0x9003   # EXIF IFD: fecha y hora de captura original

# Formato estándar de fecha en EXIF: "YYYY:MM:DD HH:MM:SS"
_EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"


def read_exif(file_path: Path) -> tuple[str, datetime | None]:
    """
    Extrae el fabricante de la cámara y la fecha de captura de un archivo
    de imagen usando únicamente la biblioteca estándar de Python.

    Soporta:
    - JPEG / HEIF / HIF  → busca segmento APP1 con bloque EXIF
    - TIFF-based RAW     → ARW, NEF, CR2, CR3, DNG, ORF, RW2
    - RAF (Fujifilm)     → lee el JPEG embebido en la cabecera RAF

    Args:
        file_path: Ruta al archivo de imagen.

    Returns:
        Tupla (fabricante, fecha).
        - fabricante es UNKNOWN_CAMERA si no se puede determinar.
        - fecha es None si no hay EXIF (usar mtime como fallback externo).
    """
    suffix = file_path.suffix.lower()
    try:
        with open(file_path, "rb") as fh:
            if suffix == ".raf":
                return _parse_raf(fh)
            elif suffix in (".jpg", ".jpeg", ".heif", ".heic", ".hif"):
                return _parse_jpeg_exif(fh)
            else:
                # ARW, NEF, CR2, CR3, DNG, ORF, RW2 — todos son TIFF-based
                return _parse_tiff_exif(fh)
    except (OSError, struct.error, ValueError, UnicodeDecodeError):
        return UNKNOWN_CAMERA, None


# — JPEG ——————————————————————————————————————————————————————————————————

def _parse_jpeg_exif(fh) -> tuple[str, datetime | None]:
    """
    Localiza el segmento APP1 con magic "Exif\\x00\\x00" en un stream JPEG
    y delega el parsing al lector de bloques TIFF.

    Ignora otros segmentos APP1 (p.ej. XMP) y continúa buscando.
    """
    if fh.read(2) != b"\xff\xd8":
        return UNKNOWN_CAMERA, None

    while True:
        marker = fh.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            break

        raw_len = fh.read(2)
        if len(raw_len) < 2:
            break
        seg_len = struct.unpack(">H", raw_len)[0] - 2  # longitud excluye los 2 bytes propios

        if marker[1] == 0xE1:           # APP1
            data = fh.read(seg_len)
            if data[:6] == b"Exif\x00\x00":
                return _parse_tiff_block(data[6:])
            # Otro APP1 (XMP, etc.) → seguir buscando
        else:
            fh.seek(seg_len, 1)

    return UNKNOWN_CAMERA, None


# — TIFF / RAW ————————————————————————————————————————————————————————————

def _parse_tiff_exif(fh) -> tuple[str, datetime | None]:
    """Lee el archivo completo como bloque TIFF (para RAW TIFF-based)."""
    return _parse_tiff_block(fh.read())


def _parse_tiff_block(data: bytes) -> tuple[str, datetime | None]:
    """
    Parsea un bloque TIFF e extrae Make y DateTimeOriginal.

    Navega por IFD0 para encontrar el tag Make (0x010F) y el puntero al
    EXIF IFD (0x8769), luego lee DateTimeOriginal (0x9003) del EXIF IFD.

    Args:
        data: Bytes del bloque TIFF (debe comenzar con "II" o "MM").

    Returns:
        Tupla (fabricante, fecha).
    """
    if len(data) < 8:
        return UNKNOWN_CAMERA, None

    byte_order = data[:2]
    if byte_order == b"II":
        endian = "<"
    elif byte_order == b"MM":
        endian = ">"
    else:
        return UNKNOWN_CAMERA, None

    if struct.unpack_from(f"{endian}H", data, 2)[0] != 42:
        return UNKNOWN_CAMERA, None

    ifd0_offset = struct.unpack_from(f"{endian}I", data, 4)[0]
    make_raw, exif_ifd_offset = _read_ifd(data, ifd0_offset, endian, {_TAG_MAKE, _TAG_EXIF_IFD})

    date_raw: str | None = None
    if exif_ifd_offset:
        date_raw, _ = _read_ifd(data, exif_ifd_offset, endian, {_TAG_DATE_ORIGINAL})

    return _build_result(make_raw, date_raw)


def _read_ifd(
    data: bytes,
    offset: int,
    endian: str,
    tags_wanted: set[int],
) -> tuple[str | None, int | None]:
    """
    Lee un IFD TIFF y extrae los valores de los tags solicitados.

    Soporta tipos ASCII (2) y LONG (4). Solo extrae el primer tag ASCII
    y el primer tag LONG encontrados entre los tags solicitados.

    Args:
        data: Bytes del bloque TIFF completo.
        offset: Posición de inicio del IFD dentro de data.
        endian: "<" little-endian o ">" big-endian.
        tags_wanted: Conjunto de IDs de tags a buscar.

    Returns:
        Tupla (valor_ascii_o_None, valor_long_o_None).
    """
    if offset + 2 > len(data):
        return None, None

    num_entries = struct.unpack_from(f"{endian}H", data, offset)[0]
    if num_entries > 1000:  # sanity check contra datos corruptos
        return None, None

    ascii_val: str | None = None
    long_val: int | None  = None
    pos = offset + 2

    for _ in range(num_entries):
        if pos + 12 > len(data):
            break

        tag, dtype, count = struct.unpack_from(f"{endian}HHI", data, pos)
        value_or_offset   = struct.unpack_from(f"{endian}I",    data, pos + 8)[0]

        if tag in tags_wanted:
            if dtype == 2:   # ASCII
                if count <= 4:
                    raw = data[pos + 8 : pos + 8 + count]
                else:
                    raw = data[value_or_offset : value_or_offset + count]
                ascii_val = raw.rstrip(b"\x00").decode("ascii", errors="replace").strip()

            elif dtype == 4:  # LONG — puntero a sub-IFD
                long_val = value_or_offset

        pos += 12

    return ascii_val, long_val


def _build_result(make_raw: str | None, date_raw: str | None) -> tuple[str, datetime | None]:
    """
    Normaliza el fabricante y parsea la fecha extraídos del IFD.

    La normalización toma la primera palabra en mayúsculas y elimina
    comas/puntos finales (algunos fabricantes los incluyen en el tag Make).

    Args:
        make_raw: Valor bruto del tag Make, puede ser None.
        date_raw: Valor bruto del tag DateTimeOriginal, puede ser None.

    Returns:
        Tupla (fabricante_normalizado, fecha_o_None).
    """
    if make_raw:
        make = make_raw.split()[0].upper().rstrip(",.")
    else:
        make = UNKNOWN_CAMERA

    date: datetime | None = None
    if date_raw:
        try:
            date = datetime.strptime(date_raw.strip(), _EXIF_DATE_FORMAT)
        except ValueError:
            pass

    return make, date


# — RAF (Fujifilm) ————————————————————————————————————————————————————————

def _parse_raf(fh) -> tuple[str, datetime | None]:
    """
    Parsea archivos RAF de Fujifilm extrayendo el JPEG embebido.

    Estructura de la cabecera RAF (offsets big-endian):
        0x00  16 bytes  magic "FUJIFILMCCD-RAW "
        0x10   4 bytes  versión del formato
        0x14   8 bytes  número de cámara
        0x1C  32 bytes  nombre del modelo
        0x3C   4 bytes  versión de directorio
        0x40  20 bytes  reservado
        0x54   4 bytes  offset al JPEG embebido
        0x58   4 bytes  longitud del JPEG embebido

    El EXIF se encuentra dentro del JPEG embebido, por lo que se redirige
    el parsing a _parse_jpeg_exif().
    """
    # 0x58 + 4 = 92 bytes mínimos para leer ambos campos
    header = fh.read(92)
    if len(header) < 92:
        return UNKNOWN_CAMERA, None

    if header[:16] != b"FUJIFILMCCD-RAW ":
        return UNKNOWN_CAMERA, None

    jpeg_offset = struct.unpack_from(">I", header, 0x54)[0]
    jpeg_length = struct.unpack_from(">I", header, 0x58)[0]

    if jpeg_offset == 0 or jpeg_length == 0:
        return UNKNOWN_CAMERA, None

    fh.seek(jpeg_offset)
    jpeg_data = fh.read(jpeg_length)

    import io
    return _parse_jpeg_exif(io.BytesIO(jpeg_data))


# ---------------------------------------------------------------------------
# Clasificación y escaneo de archivos
# ---------------------------------------------------------------------------

def classify_file(path: Path) -> str | None:
    """
    Determina la categoría de importación de un archivo según su extensión.

    Args:
        path: Ruta al archivo (solo se examina la extensión).

    Returns:
        "SOOC" para JPEG/HEIF/HIF, "RAW" para formatos RAW, None si se ignora.
    """
    suffix = path.suffix.lower()
    if suffix in SOOC_EXTENSIONS:
        return "SOOC"
    if suffix in RAW_EXTENSIONS:
        return "RAW"
    return None


def scan_volume(volume: Path) -> list[PhotoInfo]:
    """
    Escanea recursivamente un volumen y recopila los metadatos de todas
    las fotos importables encontradas.

    Usa os.scandir en lugar de pathlib.rglob para mayor rendimiento en
    volúmenes grandes con miles de archivos.

    Args:
        volume: Ruta raíz del volumen a escanear.

    Returns:
        Lista de PhotoInfo ordenada por fecha de captura (más antigua primero).
    """
    photos: list[PhotoInfo] = []
    print(f"  Escaneando {volume} ...")
    _scan_dir(volume, photos)
    photos.sort(key=lambda p: p.date)
    print(f"  {len(photos)} foto(s) encontrada(s).\n")
    return photos


def _scan_dir(directory: Path, results: list[PhotoInfo]) -> None:
    """
    Escanea recursivamente un directorio acumulando fotos en results.

    Ignora archivos y directorios que comiencen por "." (sistema/ocultos).

    Args:
        directory: Directorio a escanear.
        results: Lista donde se acumulan los PhotoInfo encontrados.
    """
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    _scan_dir(Path(entry.path), results)
                elif entry.is_file(follow_symlinks=False):
                    path = Path(entry.path)
                    category = classify_file(path)
                    if category is None:
                        continue

                    make, date = read_exif(path)

                    # Fallback de fecha: mtime del archivo en el sistema de ficheros
                    if date is None:
                        date = datetime.fromtimestamp(entry.stat().st_mtime)

                    results.append(PhotoInfo(path=path, date=date, make=make, category=category))
    except PermissionError:
        pass


# ---------------------------------------------------------------------------
# Selector interactivo de fotos
# ---------------------------------------------------------------------------

# Tipo alias para el agrupamiento: año → {MM-DD → [PhotoInfo]}
_Groups = dict[int, dict[str, list[PhotoInfo]]]


def _group_by_date(photos: list[PhotoInfo]) -> _Groups:
    """
    Agrupa una lista de fotos por año y por fecha (MM-DD).

    Args:
        photos: Lista de PhotoInfo a agrupar.

    Returns:
        Diccionario {año: {MM-DD: [PhotoInfo, ...]}} ordenado cronológicamente.
    """
    groups: _Groups = defaultdict(lambda: defaultdict(list))
    for photo in photos:
        day_key = photo.date.strftime("%m-%d")
        groups[photo.date.year][day_key].append(photo)
    # Convertir a dicts ordenados para iteración predecible
    return {
        year: dict(sorted(days.items()))
        for year, days in sorted(groups.items())
    }


def select_photos(photos: list[PhotoInfo]) -> list[PhotoInfo]:
    """
    Muestra un selector interactivo de checkbox que permite al usuario
    elegir qué días importar, agrupados por año.

    El selector muestra:
    - Una entrada por cada año (como separador visual no seleccionable)
    - Una entrada por cada día dentro de cada año, con conteo de fotos
      y fabricantes presentes

    El usuario puede marcar/desmarcar días individuales con Espacio, y
    confirmar con Enter. Para desmarcar un año completo basta con
    desmarcar todos sus días.

    Args:
        photos: Lista completa de fotos escaneadas.

    Returns:
        Lista filtrada de PhotoInfo correspondiente a los días seleccionados.
        Devuelve lista vacía si el usuario cancela (Ctrl+C).
    """
    groups = _group_by_date(photos)

    choices: list[Choice | questionary.Separator] = []

    for year, days in groups.items():
        year_total = sum(len(v) for v in days.values())
        choices.append(questionary.Separator(f"\n  ── {year}  ({year_total} fotos) ──"))

        for day_key, day_photos in days.items():
            makes = sorted({p.make for p in day_photos})
            makes_str = ", ".join(makes)
            n = len(day_photos)
            label = f"{year} / {day_key}  —  {n} foto{'s' if n != 1 else ''}   [{makes_str}]"
            choices.append(Choice(title=label, value=day_key, checked=True))

    print()
    selected_keys: list[str] | None = questionary.checkbox(
        "Selecciona los días a importar  (Espacio = marcar/desmarcar, Enter = confirmar):",
        choices=choices,
    ).ask()

    # ask() devuelve None si el usuario pulsa Ctrl+C
    if selected_keys is None:
        return []

    # El valor de cada Choice es solo "MM-DD"; puede haber el mismo MM-DD en
    # distintos años, así que filtramos por (año, MM-DD) combinados
    selected_set: set[tuple[int, str]] = set()
    for year, days in groups.items():
        for day_key in days:
            if day_key in selected_keys:
                selected_set.add((year, day_key))

    return [
        p for p in photos
        if (p.date.year, p.date.strftime("%m-%d")) in selected_set
    ]


# ---------------------------------------------------------------------------
# Construcción de rutas de destino
# ---------------------------------------------------------------------------

def build_dest_path(photo: PhotoInfo, dest_root: Path) -> Path:
    """
    Construye la ruta de destino para una foto y crea los directorios
    necesarios si no existen.

    La estructura sigue el esquema:
        <dest_root>/<AÑO>/<MM-DD>/<FABRICANTE>/<SOOC|RAW|EDITED>/

    El año y la fecha derivan exclusivamente de photo.date (fecha EXIF o
    mtime), nunca de la fecha actual del sistema.

    Args:
        photo: Metadatos de la foto.
        dest_root: Carpeta raíz de destino (ej: ~/Pictures).

    Returns:
        Path completo al archivo de destino (sin crear el archivo).
    """
    year_dir  = str(photo.date.year)
    date_dir  = photo.date.strftime("%m-%d")
    make_dir  = photo.make.upper()

    dest_dir = dest_root / year_dir / date_dir / make_dir / photo.category
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Garantizar que SOOC, RAW y EDITED siempre existen como hermanos
    _ensure_camera_subdirs(dest_root / year_dir / date_dir / make_dir)

    return dest_dir / photo.path.name


def _ensure_camera_subdirs(camera_dir: Path) -> None:
    """
    Crea las subcarpetas SOOC, RAW y EDITED dentro del directorio del
    fabricante si aún no existen.

    Args:
        camera_dir: Directorio del fabricante (ej: ~/Pictures/2026/01-15/SONY).
    """
    for subdir in CAMERA_SUBDIRS:
        (camera_dir / subdir).mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Manejo de colisiones de nombres
# ---------------------------------------------------------------------------

def resolve_collision(dest_path: Path, source_path: Path) -> Path:
    """
    Resuelve colisiones de nombre de archivo en el destino.

    Estrategia:
    1. Si el destino no existe → devolver dest_path tal cual.
    2. Si el destino existe y tiene el mismo tamaño que la fuente →
       asumir duplicado, devolver la misma ruta (el llamador lo saltará).
    3. Si el destino existe con distinto tamaño → añadir sufijo numérico
       (_1, _2, ...) hasta encontrar un nombre libre.

    Args:
        dest_path:   Ruta de destino propuesta.
        source_path: Ruta al archivo fuente.

    Returns:
        Ruta definitiva donde debe copiarse el archivo.
    """
    if not dest_path.exists():
        return dest_path

    if dest_path.stat().st_size == source_path.stat().st_size:
        return dest_path  # Señal de duplicado para el llamador

    stem, suffix, parent = dest_path.stem, dest_path.suffix, dest_path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Motor de copia
# ---------------------------------------------------------------------------

def copy_photos(photos: list[PhotoInfo], dest_root: Path) -> CopyResult:
    """
    Copia todas las fotos al árbol de destino mostrando progreso en tiempo real.

    Para cada foto:
    - Construye la ruta de destino con build_dest_path()
    - Resuelve colisiones de nombre con resolve_collision()
    - Si el archivo ya existe con el mismo tamaño, lo salta
    - Si no, lo copia preservando metadatos del FS con shutil.copy2()

    Args:
        photos:    Lista de fotos a copiar (ya filtrada por el selector).
        dest_root: Carpeta raíz de destino (ej: ~/Pictures).

    Returns:
        CopyResult con el conteo de archivos copiados, saltados y errores.
    """
    total  = len(photos)
    copied = skipped = errors = 0
    width  = len(str(total))

    for idx, photo in enumerate(photos, start=1):
        prefix = f"  [{idx:>{width}}/{total}]"
        try:
            proposed = build_dest_path(photo, dest_root)
            final    = resolve_collision(proposed, photo.path)

            if final == proposed and proposed.exists():
                print(f"{prefix} SALTAR  {photo.path.name}  (ya existe)")
                skipped += 1
                continue

            shutil.copy2(photo.path, final)
            print(f"{prefix} COPIAR  {photo.path.name}  →  {final.relative_to(dest_root)}")
            copied += 1

        except (OSError, shutil.Error) as exc:
            print(f"{prefix} ERROR   {photo.path.name}: {exc}")
            errors += 1

    return CopyResult(copied=copied, skipped=skipped, errors=errors)


# ---------------------------------------------------------------------------
# Resumen final
# ---------------------------------------------------------------------------

def print_summary(result: CopyResult, dest_root: Path) -> None:
    """
    Imprime una tabla con las estadísticas del proceso de importación.

    Args:
        result:    Estadísticas devueltas por copy_photos().
        dest_root: Carpeta raíz de destino usada (para informar al usuario).
    """
    total = result.copied + result.skipped + result.errors
    print("\n╔══════════════════════════════════════════╗")
    print("║            Importación completada        ║")
    print("╠══════════════════════════════════════════╣")
    print(f"║  Total procesadas : {total:<22}║")
    print(f"║  Copiadas         : {result.copied:<22}║")
    print(f"║  Saltadas (dup.)  : {result.skipped:<22}║")
    print(f"║  Errores          : {result.errors:<22}║")
    print(f"║  Destino          : {str(dest_root):<22}║")
    print("╚══════════════════════════════════════════╝\n")


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Función principal del CLI. Orquesta el flujo completo:
    detección → selección de volumen → escaneo → filtro de fechas → copia.

    Returns:
        Código de salida: 0 éxito, 1 sin fotos/cancelado, 2 errores de copia.
    """
    # 1. Detectar volúmenes externos
    volumes = list_external_volumes()

    # 2. Menú de selección de volumen
    selected_volume = display_volume_menu(volumes)
    if selected_volume is None:
        print("  Sin cambios. Hasta luego.\n")
        return 0

    # 3. Escanear el volumen (las fotos se ordenan por fecha EXIF)
    photos = scan_volume(selected_volume)
    if not photos:
        print("  No se encontraron fotos con formatos soportados en el volumen.\n")
        return 1

    # 4. Selector interactivo: el usuario elige qué días importar
    photos = select_photos(photos)
    if not photos:
        print("\n  Sin selección. No se copiará nada.\n")
        return 1

    # 5. Confirmación final con conteo real de la selección
    print(f"\n  {len(photos)} foto(s) seleccionadas → destino: {PICTURES_ROOT}\n")
    try:
        confirm = input("  ¿Continuar con la importación? [S/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelado.")
        return 0

    if confirm not in ("", "s", "si", "sí", "y", "yes"):
        print("  Operación cancelada.\n")
        return 0

    print()

    # 6. Copiar fotos
    result = copy_photos(photos, PICTURES_ROOT)

    # 7. Mostrar resumen
    print_summary(result, PICTURES_ROOT)

    return 0 if result.errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
