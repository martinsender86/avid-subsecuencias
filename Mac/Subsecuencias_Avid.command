#!/bin/bash
# Subsecuencias Avid - lanzador para Mac
# Doble clic: abre la ventana. La primera vez arma un entorno de Python propio
# en ~/.avid_scripts/venv_subsecuencias (pyavb + tkinterdnd2).

cd "$(dirname "$0")" || exit 1
SCRIPT="$(pwd)/Subsecuencias_Avid.py"
VENV="$HOME/.avid_scripts/venv_subsecuencias"

pausa() { echo; read -r -p "Apretá Enter para cerrar..." _; }

if [ ! -f "$SCRIPT" ]; then
    echo "No encuentro Subsecuencias_Avid.py al lado de este archivo."
    pausa; exit 1
fi

# Python con tkinter (la ventana lo necesita)
tiene_tk() { "$1" -c "import tkinter; tkinter.Tcl()" >/dev/null 2>&1; }

if [ ! -x "$VENV/bin/python3" ] || ! tiene_tk "$VENV/bin/python3"; then
    PY=""
    for c in /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 \
             /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if [ -x "$c" ] && tiene_tk "$c"; then PY="$c"; fi
    done
    # el python3 de Apple solo si están las Command Line Tools
    # (si no, macOS abre un cartel para instalarlas)
    if [ -z "$PY" ] && xcode-select -p >/dev/null 2>&1 && tiene_tk /usr/bin/python3; then
        PY=/usr/bin/python3
    fi
    if [ -z "$PY" ]; then
        echo "No encontré un Python 3 con tkinter."
        echo "Instalá Python desde https://www.python.org/downloads/macos/"
        echo "(trae tkinter incluido) y volvé a abrir este archivo."
        pausa; exit 1
    fi
    echo "Preparando el entorno (solo la primera vez)..."
    echo "Python: $PY"
    rm -rf "$VENV"
    mkdir -p "$HOME/.avid_scripts"
    "$PY" -m venv "$VENV" || { echo "No se pudo crear el entorno."; pausa; exit 1; }
fi

if ! "$VENV/bin/python3" -c "import avb, tkinterdnd2" >/dev/null 2>&1; then
    "$VENV/bin/python3" -m pip install --upgrade pip >/dev/null 2>&1
    "$VENV/bin/python3" -m pip install --upgrade pyavb tkinterdnd2 || {
        echo "Falló la instalación de pyavb / tkinterdnd2. Copiá lo de arriba y pasámelo."
        pausa; exit 1; }
fi

# si se pasa un bin como argumento (o desde la Terminal), se abre ya cargado
"$VENV/bin/python3" "$SCRIPT" "$@"
EST=$?
if [ $EST -ne 0 ]; then
    echo "El programa terminó con error ($EST)."
    pausa
fi
exit $EST
