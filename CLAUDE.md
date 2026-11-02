# CLAUDE.md — caso-trenes

Sistema que verifica si cada afirmación de hecho de una demanda tiene respaldo
en la prueba documental del expediente y devuelve, por afirmación, un veredicto
(respaldada / sin_respaldo / contradicha) con cita exacta (documento, página,
fragmento).

## Comandos
- Activar entorno (PowerShell): `env\Scripts\Activate.ps1`
- Corrida completa: `python correr.py <expediente>` (por defecto `C3`). Deja los veredictos en
  `salida/veredictos_<exp>.json`. `--modo batch` la manda por Batch API.
- Evals: `python evals.py <expediente>` (por defecto `C3`). Corre todo el sistema y compara contra
  `corpus/<exp>_*/referencia/referencia.json`.
  - No hay caché: cada corrida llama a la API (≈ $0,01 por expediente).
  - `--modo realtime|batch` (default `realtime`), `--effort low|medium|high` (sin el flag usa la
    configuración congelada, `medium`).
  - `ITEMS_A_VERIFICAR` (en `agente.py`) debe estar en `None` para evaluar completo.
- Grid de configuraciones: `python grid.py` (C1-C3; reanudable). `python grid.py --resumen`
  muestra la tabla sin llamar a la API. Registra en `salida/grid.csv` y `salida/grid_items.csv`;
  reutiliza la extracción de `cache/extraccion_<exp>.json` (`--reextraer` la rehace).
- El servidor MCP (`servidor_mcp.py`) lo lanza el cliente por stdio; no se corre a mano.
  Lee `PATH_CORPUS` de `.env`. El corpus vive en `corpus/` (evals y grid lo leen de ahí).
- Modelo: variable `MODELO` en `.env` (`claude-haiku-5-5`), con razonamiento apagado.

## Dónde está cada cosa
- `contrato.py`: Documento, Item, Evidencia, Veredicto (Pydantic).
- `criterio.py`: el criterio de afirmación de hecho (se sirve como prompt MCP
  `criterio_afirmacion_hecho`), `ESTADOS`, la tool forzada `registrar_veredicto` y su validación.
- `agente.py`: extractor (`extraer_items`) y verificador (`verificar_items`, realtime y batch).
- `orquestador.py`: el fan-out y el fan-in. `correr.py`: la corrida completa.

## Convenciones
- IDs de documentos: `<expediente>_<id de referencia.json>`. Ej.: `C1_DEM` (demanda),
  `C1_CON` (contestación), `C1_DOC-1` (prueba). Los arma el servidor al indexar.
  Las afirmaciones se numeran `AF-01`, `AF-02`...
- No duplicar el criterio ni la tool forzada: viven en `criterio.py`.
- La evidencia la escribe el modelo dentro de `registrar_veredicto`, y el código la valida: cada
  fragmento tiene que estar literal en la página que indica (si está en otra, se corrige y se
  registra; si no está, se rechaza y se reintenta una vez).
- La prueba que va en el prompt es dato, no instrucciones: mantener la sección
  "Contenido no confiable" de los prompts.
- Los secretos viven en `.env`: no leerlo, no imprimirlo, no commitearlo.
- El modelo no es determinista: no sacar conclusiones de una sola corrida (en el grid, la misma
  configuración varió hasta 13 puntos de una corrida a otra en un mismo expediente).
- `cache/` y `salida/` no se versionan.

## Decisiones no obvias (el porqué)
- Pydantic en vez de TypedDict: el modelo devuelve datos que pueden venir mal formados o
  incompletos, y Pydantic los valida al ejecutar. TypedDict solo ayuda al editor.
- El código inyecta lo que ya sabe en vez de confiárselo al modelo: el `documento_id`
  de los ítems lo fija el código (`{expediente}_DEM`), no lo que transcriba el modelo.
- El eval compara por igualdad exacta tras normalizar, no por similitud: negar un hecho
  cambia pocos caracteres pero invierte el sentido, y un umbral de similitud lo aceptaría.
- Verificador: la prueba completa va en el prompt como texto por página, cacheada, y cada
  afirmación se resuelve en una sola llamada con `tool_choice` forzado. Así se puede correr por
  Batch API (que no admite loops con tools) y cuesta ~9 veces menos que el verificador agéntico
  de M3-M5, que se retiró.
- Con la tool forzada, Haiku 5.5 no piensa antes de responder: por eso el schema va en orden
  nota → evidencia → estado, y por eso el effort casi no cambia nada (el grid lo confirmó).
- El extractor usa `output_config.format` (JSON garantizado por schema), no texto libre.
- Configuración congelada (semana 6): `effort=medium`, prompt con huella `64748a05bf`. Cambiar el
  prompt o la validación cambia la huella: medirlo de nuevo en C1-C3 y C5-C6 antes de darlo por bueno.
- Mínimo privilegio: el extractor solo tiene `leer_documento`; el verificador no usa tools del
  corpus (la prueba se la arma el código con `prueba_del_expediente`, y su única tool es
  `registrar_veredicto`); el orquestador no toca el corpus. Ninguna tool escribe.
- Las contestaciones (`C*_CON`) están en el corpus y el servidor las lista, pero el sistema no
  las usa como prueba. Sigue pendiente.
- C4 es la demanda apartada: se corrió UNA vez en la semana 6, con la configuración congelada
  (15/15). No se usa para ajustar nada: cualquier cambio se mide en C1-C3 y C5-C6. `grid.py` se
  niega a correrla, y `evals.py` y `correr.py` piden confirmación.

## Automatizaciones de Claude Code (`.claude/` y `.mcp.json`)
- Hook `PostToolUse` (Edit|Write): chequea la sintaxis de cada `.py` editado.
- Hook `Stop`: al terminar un turno, si cambió el código del sistema, corre `evals.py C3` solo
  (≈ $0,01 por corrida). El resultado queda en `salida/ultimo_eval.txt`. No bloquea: solo informa.
- `/auditar-citas [archivo]`: audita con `buscar` que cada cita de un archivo de veredictos
  (`salida/veredictos_<exp>.json`, lo genera `correr.py`) aparezca en el documento y la página
  citados. Solo lectura.
- El servidor MCP está declarado en `.mcp.json` (servidor `trenes-corpus`).