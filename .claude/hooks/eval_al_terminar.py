"""
Hook Stop: corre evals.py solo si cambió el código del sistema desde la última corrida.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

EXPEDIENTE = "C3"
ARCHIVOS = ["agente.py", "orquestador.py", "servidor_mcp.py",
            "criterio.py", "contrato.py", "evals.py"]
TIMEOUT_EVAL = 270  # segundos; el timeout del hook en settings.json es mayor


def mensaje(texto: str) -> None:
    print(json.dumps({"systemMessage": texto}))


def main() -> int:
    datos = json.load(sys.stdin)
    if datos.get("stop_hook_active"):
        return 0

    raiz = Path(os.environ.get("CLAUDE_PROJECT_DIR") or datos.get("cwd", "."))
    salida = raiz / "salida"
    salida.mkdir(exist_ok=True)
    marca = salida / ".ultimo_eval_hash"

    h = hashlib.sha256()
    for nombre in ARCHIVOS:
        ruta = raiz / nombre
        if ruta.exists():
            h.update(nombre.encode())
            h.update(ruta.read_bytes())
    huella = h.hexdigest()

    if marca.exists() and marca.read_text().strip() == huella:
        return 0  # el código no cambió: no se gasta nada

    entorno = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        r = subprocess.run(
            [sys.executable, "evals.py", EXPEDIENTE],
            cwd=raiz, env=entorno, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT_EVAL,
        )
    except subprocess.TimeoutExpired:
        mensaje(f"Eval {EXPEDIENTE}: superó {TIMEOUT_EVAL}s y se canceló. "
                "Se reintentará al terminar el próximo turno.")
        return 0

    texto = r.stdout + r.stderr
    (salida / "ultimo_eval.txt").write_text(
        f"{datetime.now():%Y-%m-%d %H:%M} | expediente {EXPEDIENTE}\n\n{texto}",
        encoding="utf-8")

    resumen = [re.sub(r"\s+", " ", l.strip(" =")) for l in r.stdout.splitlines()
               if l.startswith("=== EVAL")]
    sin_datos = [l for l in resumen if "(0/0)" in l]
    if r.returncode != 0 or not resumen or sin_datos:
        if sin_datos:
            mensaje(f"Eval {EXPEDIENTE} no evaluó nada en: {' | '.join(sin_datos)}. "
                    "Los verificadores no devolvieron veredictos. Detalle en salida/ultimo_eval.txt")
        else:
            ultima = (r.stderr.strip().splitlines() or ["sin detalle"])[-1]
            mensaje(f"Eval {EXPEDIENTE} falló al ejecutarse: {ultima}. "
                    "Detalle en salida/ultimo_eval.txt")
        return 0  # sin guardar el hash: se reintenta en el próximo turno

    marca.write_text(huella)  # solo se guarda si el eval terminó bien
    mensaje(f"Eval {EXPEDIENTE} (1 corrida; el modelo varía entre corridas): "
            + " | ".join(resumen) + ". Detalle en salida/ultimo_eval.txt")
    return 0


sys.exit(main())