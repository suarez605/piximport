#!/usr/bin/env python3.11
"""
photo_importer.py — CLI para importar fotos desde tarjetas SD a ~/Pictures.

Estructura de destino:
    ~/Pictures/<AÑO>/<MM-DD>/<FABRICANTE>/<SOOC|RAW|EDITED>/

Tipos de archivo soportados:
    SOOC  : .jpg .jpeg .heif .heic .hif
    RAW   : .arw .raf .nef .cr2 .cr3 .dng .orf .rw2

Uso:
    python3.11 photo_importer.py

Requisitos:
    Python 3.11+ — solo biblioteca estándar (sin dependencias externas)
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Constantes globales
# ---------------------------------------------------------------------------

PICTURES_ROOT = Path.home() / "Pictures"

# Año base para la carpeta raíz de destino
IMPORT_YEAR = datetime.now().year

# Extensiones clasificadas (en minúsculas)
SOOC_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".heif", ".heic", ".hif"})
RAW_EXTENSIONS: frozenset[str] = frozenset({".arw", ".raf", ".nef", ".cr2", ".cr3", ".dng", ".orf", ".rw2"})
ALL_EXTENSIONS: frozenset[str] = SOOC_EXTENSIONS | RAW_EXTENSIONS

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
    """Metadatos extraídos de una foto."""
    path: Path           # Ruta absoluta al archivo original
    date: datetime       # Fecha de captura (o de modificación como fallback)
    make: str            # Fabricante de la cámara (ej: "SONY", "FUJIFILM")
    category: str        # "SOOC" o "RAW"


class CopyResult(NamedTuple):
    """Resultado del proceso de copia."""
    copied: int
    skipped: int
    errors: int


# ---------------------------------------------------------------------------
# Detección de medios externos (macOS)
# ---------------------------------------------------------------------------

def list_external_volumes() -> list[Path]:
    """
    Devuelve una lista de rutas a volúmenes montados en /Volumes que no son
    parte del sistema interno del Mac.

    Utiliza `diskutil info` para identificar si un volumen es interno/sistema
    y excluirlo. Como fallback, excluye por nombre conocido.

    Returns:
        Lista de Path a los puntos de montaje de volúmenes externos.
    """
    volumes_root = Path("/Volumes")
    if not volumes_root.exists():
        return []

    external: list[Path] = []

    for entry in sorted(volumes_root.iterdir()):
        # Ignorar aliases y entradas no-directorio
        if not entry.is_dir() or entry.is_symlink():
            continue
        # Excluir por nombre conocido del sistema
        if entry.name in SYSTEM_VOLUMES:
            continue
        # Preguntar a diskutil si el volumen es interno
        if _is_internal_volume(entry):
            continue
        external.append(entry)

    return external


def _is_internal_volume(mount_point: Path) -> bool:
    """
    Consulta `diskutil info` para determinar si el volumen es interno.

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
        # diskutil marca los volúmenes internos con "internal: yes"
        if "internal:                  yes" in output or "internal:                 yes" in output:
            return True
        # Los volúmenes APFS internos suelen ser "synthesized"
        if "protocol:               apple fabric" in output:
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def display_volume_menu(volumes: list[Path]) -> Path | None:
    """
    Muestra un menú interactivo en terminal con los volúmenes disponibles
    y retorna el volumen seleccionado por el usuario.

    Args:
        volumes: Lista de rutas a volúmenes externos disponibles.

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

    print("  Volúmenes externos detectados:\n")
    for idx, vol in enumerate(volumes, start=1):
        size_str = _get_volume_size(vol)
        print(f"  [{idx}] {vol.name:<20} {size_str:>10}   ({vol})")

    print("\n  [0] Salir\n")

    while True:
        try:
            raw = input("  Selecciona un volumen: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelado por el usuario.")
            return None

        if raw == "0":
            return None

        if raw.isdigit():
            choice = int(raw)
            if 1 <= choice <= len(volumes):
                selected = volumes[choice - 1]
                print(f"\n  Volumen seleccionado: {selected.name} ({selected})\n")
                return selected

        print(f"  Opción inválida. Escribe un número entre 0 y {len(volumes)}.")


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
        total_bytes = stat.f_blocks * stat.f_frsize
        return _format_bytes(total_bytes)
    except OSError:
        return ""


def _format_bytes(n: int) -> str:
    """Convierte bytes a cadena legible (KB, MB, GB, TB)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Parser EXIF (sin dependencias externas)
# ---------------------------------------------------------------------------

# Tags EXIF que nos interesan
_TAG_MAKE = 0x010F           # IFD0: fabricante de la cámara
_TAG_EXIF_IFD = 0x8769       # IFD0: puntero al IFD EXIF
_TAG_DATE_ORIGINAL = 0x9003  # EXIF IFD: fecha de captura original

# Formato EXIF de fecha: "YYYY:MM:DD HH:MM:SS"
_EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"


def read_exif(file_path: Path) -> tuple[str, datetime | None]:
    """
    Extrae el fabricante de la cámara y la fecha de captura de un archivo
    de imagen usando solo la biblioteca estándar de Python.

    Soporta:
    - JPEG (APP1 EXIF)
    - TIFF-based RAW: ARW, NEF, CR2, CR3, DNG, ORF, RW2
    - RAF (Fujifilm): extrae el JPEG embebido y lee su EXIF

    Args:
        file_path: Ruta al archivo de imagen.

    Returns:
        Tupla (fabricante, fecha). El fabricante es UNKNOWN_CAMERA si no se
        encuentra. La fecha es None si no se encuentra (usar fallback externo).
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
    Busca el segmento APP1 con la cabecera "Exif\x00\x00" en un JPEG
    y delega el parsing a _parse_tiff_block.
    """
    # Verificar cabecera JPEG (SOI marker: FF D8)
    if fh.read(2) != b"\xff\xd8":
        return UNKNOWN_CAMERA, None

    while True:
        marker = fh.read(2)
        if len(marker) < 2:
            break
        if marker[0] != 0xFF:
            break

        segment_len_bytes = fh.read(2)
        if len(segment_len_bytes) < 2:
            break
        segment_len = struct.unpack(">H", segment_len_bytes)[0] - 2  # incluye los 2 bytes de longitud

        # APP1 marker: 0xFF 0xE1
        if marker[1] == 0xE1:
            data = fh.read(segment_len)
            if data[:6] == b"Exif\x00\x00":
                # El bloque TIFF comienza en el offset 6 del segmento APP1
                return _parse_tiff_block(data[6:])
            # Si no es Exif (puede ser XMP), continuar
        else:
            fh.seek(segment_len, 1)

    return UNKNOWN_CAMERA, None


# — TIFF / RAW ————————————————————————————————————————————————————————————

def _parse_tiff_exif(fh) -> tuple[str, datetime | None]:
    """Lee el bloque TIFF completo desde el inicio del archivo."""
    data = fh.read()
    return _parse_tiff_block(data)


def _parse_tiff_block(data: bytes) -> tuple[str, datetime | None]:
    """
    Parsea un bloque de datos en formato TIFF (IFD0 + EXIF IFD).

    Extrae:
    - Tag 0x010F (Make): fabricante de la cámara
    - Tag 0x9003 (DateTimeOriginal): fecha de captura

    Args:
        data: Bytes del bloque TIFF (comienza con "II" o "MM").

    Returns:
        Tupla (fabricante, fecha).
    """
    if len(data) < 8:
        return UNKNOWN_CAMERA, None

    # Determinar byte order: "II" = little-endian, "MM" = big-endian
    byte_order = data[:2]
    if byte_order == b"II":
        endian = "<"
    elif byte_order == b"MM":
        endian = ">"
    else:
        return UNKNOWN_CAMERA, None

    # Verificar magic number TIFF (42)
    magic = struct.unpack_from(f"{endian}H", data, 2)[0]
    if magic != 42:
        return UNKNOWN_CAMERA, None

    # Offset al primer IFD
    ifd0_offset = struct.unpack_from(f"{endian}I", data, 4)[0]

    make, exif_ifd_offset = _read_ifd(data, ifd0_offset, endian, {_TAG_MAKE, _TAG_EXIF_IFD})

    date_str: str | None = None
    if exif_ifd_offset:
        result, _ = _read_ifd(data, exif_ifd_offset, endian, {_TAG_DATE_ORIGINAL})
        date_str = result

    return _build_result(make, date_str)


def _read_ifd(
    data: bytes,
    offset: int,
    endian: str,
    tags_wanted: set[int],
) -> tuple[str | None, int | None]:
    """
    Lee un IFD (Image File Directory) en formato TIFF y extrae los valores
    de los tags solicitados.

    Args:
        data: Bytes del bloque TIFF completo.
        offset: Posición de inicio del IFD.
        endian: "<" para little-endian, ">" para big-endian.
        tags_wanted: Conjunto de tag IDs a extraer.

    Returns:
        Tupla (valor_ascii_o_None, valor_long_o_None).
        El primer elemento contiene el valor del primer tag ASCII encontrado.
        El segundo contiene el valor LONG del segundo tag (puntero a sub-IFD).
    """
    if offset + 2 > len(data):
        return None, None

    num_entries = struct.unpack_from(f"{endian}H", data, offset)[0]
    if num_entries > 1000:  # sanity check
        return None, None

    ascii_val: str | None = None
    long_val: int | None = None

    pos = offset + 2
    for _ in range(num_entries):
        if pos + 12 > len(data):
            break

        tag, dtype, count = struct.unpack_from(f"{endian}HHI", data, pos)
        value_offset = struct.unpack_from(f"{endian}I", data, pos + 8)[0]

        if tag in tags_wanted:
            if dtype == 2:  # ASCII
                # Los valores ASCII de hasta 4 bytes están en los últimos 4 bytes
                # de la entrada IFD; si count > 4, value_offset es un puntero
                if count <= 4:
                    raw = data[pos + 8 : pos + 8 + count]
                else:
                    raw = data[value_offset : value_offset + count]
                ascii_val = raw.rstrip(b"\x00").decode("ascii", errors="replace").strip()

            elif dtype == 4:  # LONG — puntero a sub-IFD (ej: EXIF IFD)
                long_val = value_offset

        pos += 12

    return ascii_val, long_val


def _build_result(make_raw: str | None, date_raw: str | None) -> tuple[str, datetime | None]:
    """
    Normaliza el fabricante y parsea la fecha extraídos del IFD.

    Args:
        make_raw: Cadena ASCII del tag Make, puede ser None.
        date_raw: Cadena ASCII del tag DateTimeOriginal, puede ser None.

    Returns:
        Tupla (fabricante_normalizado, fecha_o_None).
    """
    # Normalizar fabricante: tomar la primera palabra en mayúsculas
    if make_raw:
        make = make_raw.split()[0].upper().rstrip(",").rstrip(".")
    else:
        make = UNKNOWN_CAMERA

    # Parsear fecha
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
    Parsea archivos RAF de Fujifilm.

    El formato RAF contiene un JPEG embebido al que se puede acceder mediante
    la cabecera RAF: 84 bytes de cabecera fija + offsets al JPEG embebido.

    Estructura de la cabecera RAF (offsets en bytes, big-endian):
        0x00 - 15 bytes: magic "FUJIFILMCCD-RAW"
        0x10 - 4 bytes:  versión del formato
        0x14 - 8 bytes:  número de cámara
        0x1C - 32 bytes: nombre del modelo
        0x3C - 4 bytes:  versión de directorio
        0x40 - 4 bytes:  offset al JPEG embebido (big-endian)
        0x44 - 4 bytes:  tamaño del JPEG embebido
    """
    header = fh.read(84)
    if len(header) < 84:
        return UNKNOWN_CAMERA, None

    # Verificar magic RAF
    if header[:16] != b"FUJIFILMCCD-RAW ":
        return UNKNOWN_CAMERA, None

    jpeg_offset = struct.unpack_from(">I", header, 0x54)[0]
    jpeg_length = struct.unpack_from(">I", header, 0x58)[0]

    if jpeg_offset == 0 or jpeg_length == 0:
        return UNKNOWN_CAMERA, None

    fh.seek(jpeg_offset)
    jpeg_data = fh.read(jpeg_length)

    # Parsear el JPEG embebido
    import io
    return _parse_jpeg_exif(io.BytesIO(jpeg_data))


# ---------------------------------------------------------------------------
# Clasificación y escaneo de archivos
# ---------------------------------------------------------------------------

def classify_file(path: Path) -> str | None:
    """
    Determina la categoría de un archivo según su extensión.

    Args:
        path: Ruta al archivo.

    Returns:
        "SOOC", "RAW", o None si el archivo no es una foto importable.
    """
    suffix = path.suffix.lower()
    if suffix in SOOC_EXTENSIONS:
        return "SOOC"
    if suffix in RAW_EXTENSIONS:
        return "RAW"
    return None


def scan_volume(volume: Path) -> list[PhotoInfo]:
    """
    Escanea recursivamente un volumen y recopila información de todas las
    fotos importables.

    Usa os.scandir recursivo en lugar de rglob para mayor eficiencia en
    volúmenes grandes.

    Args:
        volume: Ruta raíz del volumen a escanear.

    Returns:
        Lista de PhotoInfo con metadatos de cada foto encontrada.
    """
    photos: list[PhotoInfo] = []

    print(f"  Escaneando {volume} ...")

    _scan_dir(volume, photos)

    print(f"  {len(photos)} foto(s) encontrada(s).\n")
    return photos


def _scan_dir(directory: Path, results: list[PhotoInfo]) -> None:
    """
    Escanea recursivamente un directorio con os.scandir (más eficiente que
    pathlib.rglob en volúmenes con muchos archivos).

    Args:
        directory: Directorio a escanear.
        results: Lista donde se acumulan los resultados.
    """
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue  # Ignorar archivos ocultos / sistema
                if entry.is_dir(follow_symlinks=False):
                    _scan_dir(Path(entry.path), results)
                elif entry.is_file(follow_symlinks=False):
                    path = Path(entry.path)
                    category = classify_file(path)
                    if category is None:
                        continue

                    make, date = read_exif(path)

                    # Fallback de fecha: usar mtime del archivo
                    if date is None:
                        mtime = entry.stat().st_mtime
                        date = datetime.fromtimestamp(mtime)

                    results.append(PhotoInfo(
                        path=path,
                        date=date,
                        make=make,
                        category=category,
                    ))
    except PermissionError:
        pass  # Ignorar directorios sin permiso de lectura


# ---------------------------------------------------------------------------
# Construcción de rutas de destino
# ---------------------------------------------------------------------------

def build_dest_path(photo: PhotoInfo, dest_root: Path) -> Path:
    """
    Construye la ruta de destino completa para una foto, incluyendo la
    creación de los directorios necesarios.

    Estructura: <dest_root>/<AÑO>/<MM-DD>/<FABRICANTE>/<CATEGORIA>/<archivo>

    Args:
        photo: Metadatos de la foto.
        dest_root: Carpeta raíz de destino (ej: ~/Pictures).

    Returns:
        Path completo al archivo de destino.
    """
    date_dir = photo.date.strftime("%m-%d")
    year_dir = str(photo.date.year)
    make_dir = photo.make.upper()

    dest_dir = dest_root / year_dir / date_dir / make_dir / photo.category
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Crear siempre las tres subcarpetas del fabricante
    _ensure_camera_subdirs(dest_root / year_dir / date_dir / make_dir)

    return dest_dir / photo.path.name


def _ensure_camera_subdirs(camera_dir: Path) -> None:
    """
    Crea las subcarpetas SOOC, RAW y EDITED dentro del directorio del
    fabricante si no existen.

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
    1. Si el destino no existe → usar dest_path tal cual.
    2. Si el destino existe y tiene el mismo tamaño → asumir duplicado,
       devolver la misma ruta (el llamador la saltará).
    3. Si el destino existe con distinto tamaño → añadir sufijo numérico
       (_1, _2, ...) hasta encontrar un nombre libre.

    Args:
        dest_path: Ruta de destino propuesta.
        source_path: Ruta al archivo fuente.

    Returns:
        Ruta definitiva donde copiar (puede ser la misma o con sufijo).
    """
    if not dest_path.exists():
        return dest_path

    # Mismo tamaño → probable duplicado, conservar ruta para señalarlo
    if dest_path.stat().st_size == source_path.stat().st_size:
        return dest_path  # Indicador de "ya existe igual"

    # Distinto tamaño → añadir sufijo numérico
    stem = dest_path.stem
    suffix = dest_path.suffix
    parent = dest_path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Motor de copia
# ---------------------------------------------------------------------------

def copy_photos(
    photos: list[PhotoInfo],
    dest_root: Path,
) -> CopyResult:
    """
    Copia todas las fotos al destino organizado, mostrando progreso en
    tiempo real.

    Args:
        photos: Lista de fotos a copiar.
        dest_root: Carpeta raíz de destino.

    Returns:
        CopyResult con estadísticas del proceso.
    """
    total = len(photos)
    copied = skipped = errors = 0

    for idx, photo in enumerate(photos, start=1):
        prefix = f"  [{idx:>{len(str(total))}}/{total}]"

        try:
            proposed_dest = build_dest_path(photo, dest_root)
            final_dest = resolve_collision(proposed_dest, photo.path)

            if final_dest == proposed_dest and proposed_dest.exists():
                # Mismo archivo ya existe → saltar
                print(f"{prefix} SALTAR  {photo.path.name} (ya existe)")
                skipped += 1
                continue

            shutil.copy2(photo.path, final_dest)
            rel = final_dest.relative_to(dest_root)
            print(f"{prefix} COPIAR  {photo.path.name}  →  {rel}")
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
    Imprime el resumen del proceso de importación.

    Args:
        result: Estadísticas del proceso de copia.
        dest_root: Carpeta raíz de destino usada.
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
    Función principal del CLI.

    Returns:
        Código de salida (0 = éxito, 1 = sin fotos/cancelado, 2 = error).
    """
    # 1. Detectar volúmenes externos
    volumes = list_external_volumes()

    # 2. Mostrar menú y obtener selección del usuario
    selected_volume = display_volume_menu(volumes)
    if selected_volume is None:
        print("  Sin cambios. Hasta luego.\n")
        return 0

    # 3. Escanear el volumen seleccionado
    photos = scan_volume(selected_volume)
    if not photos:
        print("  No se encontraron fotos con formatos soportados en el volumen.\n")
        return 1

    # 4. Confirmar antes de copiar
    dest_root = PICTURES_ROOT / str(IMPORT_YEAR)
    print(f"  Se copiarán {len(photos)} foto(s) a: {dest_root}\n")

    try:
        confirm = input("  ¿Continuar? [S/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelado por el usuario.")
        return 0

    if confirm not in ("", "s", "si", "sí", "y", "yes"):
        print("  Operación cancelada.\n")
        return 0

    print()

    # 5. Copiar fotos
    result = copy_photos(photos, PICTURES_ROOT)

    # 6. Mostrar resumen
    print_summary(result, PICTURES_ROOT)

    return 0 if result.errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
