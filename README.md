# photo-importer

CLI para importar fotos desde tarjetas SD a `~/Pictures`, organizadas
automáticamente por fecha EXIF y fabricante de cámara.

## Estructura de destino

```
~/Pictures/
└── 2026/
    └── 01-15/
        └── SONY/
            ├── SOOC/    ← JPEG, HEIF, HIF
            ├── RAW/     ← ARW, RAF, NEF, CR2, CR3, DNG, ORF, RW2
            └── EDITED/  ← vacío, listo para tu flujo de edición
```

## Instalación

### Con pip (recomendado)

```bash
pip install photo-importer
```

### Con pipx (aislado, sin tocar el entorno global)

```bash
pipx install photo-importer
```

### Con Homebrew (macOS)

```bash
brew install alvaromb/tap/photo-importer
```

## Uso

```bash
photo-importer
```

El CLI detecta automáticamente las tarjetas SD conectadas, muestra un
selector interactivo de días a importar y copia las fotos preservando
los metadatos del sistema de ficheros.

## Formatos soportados

| Tipo | Extensiones |
|------|-------------|
| SOOC | `.jpg` `.jpeg` `.heif` `.heic` `.hif` |
| RAW  | `.arw` `.raf` `.nef` `.cr2` `.cr3` `.dng` `.orf` `.rw2` |

## Requisitos

- macOS (usa `/Volumes` y `diskutil`)
- Python 3.11+

## Desarrollo

```bash
git clone https://github.com/alvaromb/photo-importer
cd photo-importer
python3.11 -m venv env
source env/bin/activate
pip install -e ".[dev]"
python -m unittest tests -v
```
