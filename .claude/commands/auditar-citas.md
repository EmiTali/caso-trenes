---
description: Audita que cada cita de un archivo de veredictos de Trenes aparezca literal en el documento y la página citados.
argument-hint: [archivo de veredictos, ej. salida/veredictos_C1.json]
disable-model-invocation: true
allowed-tools: Read mcp__trenes-corpus__buscar
---

Auditá las citas del archivo de veredictos: $ARGUMENTS
(si no se indicó archivo, usá `salida/veredictos_C1.json`).

Cada entrada tiene `estado` (respaldada / sin_respaldo / contradicha) y una lista
`evidencia` con `documento_id`, `pagina` y `fragmento`.

Para cada cita de cada ítem:
1. Elegí del fragmento un tramo literal de 3 a 8 palabras con contenido propio (un
   monto, un número, una fecha, un nombre), sin saltos de línea y sin el encabezado
   institucional ("CCF ..." o "DOCUMENTO SINTETICO ...").
2. Llamá a `buscar` con ese tramo y `documento_id` igual al documento citado.
3. Clasificá la cita:
   - OK: el tramo aparece en la página citada.
   - PÁGINA DISTINTA: aparece, pero en otra página (indicá cuál).
   - NO ENCONTRADA: no aparece en ese documento. Probá con un segundo tramo antes
     de marcarla así.
Si `buscar` devuelve un campo `error`, informalo aparte: no cuenta como cita inválida.

Además, por cada ítem marcá incoherencias de estado: `respaldada` o `contradicha` sin
evidencia, o `sin_respaldo` con evidencia.

Reglas:
- Es una auditoría de solo lectura: no edites ni crees archivos.
- Usá únicamente lo que devuelva `buscar`; no completes con conocimiento del caso.
- Verificá cada cita en esta ejecución: no reutilices resultados de auditorías anteriores de la sesión.

Respondé con una tabla (ítem | estado | documento y página citados | resultado) y
debajo una línea con totales: OK / página distinta / no encontradas / incoherencias.