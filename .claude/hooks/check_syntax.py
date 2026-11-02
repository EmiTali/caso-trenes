import json
import sys


def main() -> int:
    datos = json.load(sys.stdin)
    ruta = datos.get("tool_input", {}).get("file_path", "")
    if not ruta.endswith(".py"):
        return 0
    try:
        with open(ruta, encoding="utf-8") as f:
            compile(f.read(), ruta, "exec")
    except FileNotFoundError:
        return 0
    except SyntaxError as e:
        print(f"Error de sintaxis en {ruta}, línea {e.lineno}: {e.msg}", file=sys.stderr)
        return 2
    return 0


sys.exit(main())