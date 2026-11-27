# Subsecuencias Avid — v1.2.4

App para **Avid Media Composer** (Windows y Mac, Intel y Apple Silicon).

## Qué hace

1. **Crear subsecuencias**: soltás un bin (`.avb`), elegís una secuencia y la pista que define los cortes, y la app agrega al mismo bin **una secuencia por plano**, con todas las pistas recortadas a ese tramo (video, audio, TC, efectos, keyframes y locators).
2. **Convertir en subclips**: con el segundo botón, las subsecuencias elegidas se convierten en **subclips**, como hace AutoSync en Avid. Cada pista apunta al mismo clip que tenía la subsecuencia (master clip o subclip), conservando la generación del clip de origen y efectos como FrameFlex y cambios de velocidad.

Avid puede quedar abierto: solo hay que cerrar **ese bin**. Hace backup en `_Backup_Subsecuencias`.

## Requisitos

- **Windows:** el `.exe` de la carpeta `descargas/` no necesita nada.
- **Mac:** Python 3 con tkinter (el de python.org lo trae). La primera vez arma su propio entorno en `~/.avid_scripts/venv_subsecuencias` (baja `pyavb` y `tkinterdnd2`; necesita internet).

## Cómo se usa

1. En Avid, cerrá el bin.
2. Abrí la app y soltá el `.avb` en la ventana (o "Elegir…"). En Windows también se puede soltar el bin sobre el `.exe`.
3. Elegí la secuencia y de qué pista salen los cortes: V1 (por defecto), otra pista, "Todas las de video" o "Todas".
4. **Previsualizar** muestra la lista de planos con su TC, sin tocar nada.
5. **Crear subsecuencias** las agrega al bin. Después, si querés, **convertir en subclips**.
6. Volvé a abrir el bin en Avid.

En Mac, la primera vez: `xattr -cr` sobre la carpeta (ver `Mac/LEEME_Mac.txt`).

Línea de comandos (pruebas):
```
python Subsecuencias_Avid.py --cli BIN.avb [--secuencia NOMBRE] [--pistas V1] [--simular]
                             [--con-huecos] [--sin-unir-add-edits] [--nombre "{secuencia}_{n}"]
python Subsecuencias_Avid.py --cli BIN.avb --subclips --secuencia NOMBRE [--conservar] [--simular]
```

## Contenido

| Archivo | Para qué |
|---|---|
| `Mac/Subsecuencias_Avid.py` | Código de la app (el mismo con el que se compila el `.exe` de Windows) |
| `Mac/Subsecuencias_Avid.command` | Lanzador para Mac |
| `Mac/LEEME_Mac.txt` | Instrucciones para Mac |

## Descargas (listo para usar)

En la carpeta **`descargas/`**: `Subsecuencias_Avid.exe` (Windows) y `Subsecuencias_Avid_v1.2.4_Mac.zip`.
