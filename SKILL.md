---
name: tup-campus-navigator
description: >-
  Operar el campus Moodle de la TUP (UTN) — Programación I a IV, Matemática y Probabilidad y Estadística — por la API
  REST oficial, desde Claude Code. Usá esta skill cuando el usuario mencione "campus
  TUP", "moodle TUP", "tup.sied", "Programación 1/2/3", "Matemática", "Probabilidad y Estadística", "PyE", una "comisión" (C1/C2/C3),
  "informe de seguimiento", "qué me falta corregir", "quién no entregó", "cargar
  nota", "pendientes", "mapeá mis comisiones", "actualizá mis datos", o pida revisar
  entregas / calificaciones / parciales por comisión. También cubre **auditoría de aula
  virtual** ("auditá el aula", "puesta a punto del Moodle", "links rotos", "revisá cómo
  está armada el aula", "planilla de auditoría") y corrección automática con Active-IA
  (importar entregas → corregir con IA → devolver la nota).
  Cubre además la **vista del profesor / coordinador de materia**, que mira TODAS las
  comisiones del curso a la vez y no sólo la propia: "cómo viene el curso", "todas las
  comisiones", "qué comisión está atrasada", "quién tiene que corregir", "cuánto tardan
  en corregir", "panorama del curso". Esas vistas devuelven HECHOS por comisión y nombran
  al tutor a cargo como dato de ruteo — nunca un ranking ni un puntaje de personas.
  Además cubre el **apartado de Tareas del equipo** sobre ClickUp (opcional): "conectá
  ClickUp", "ayudame a conectar ClickUp", "quiero usar lo de tareas" (primera vez, sin
  nada configurado todavía) tanto como el uso ya conectado — un tutor puede ver y
  marcar como hechas sus tareas asignadas ("mis tareas", "qué tengo pendiente en
  ClickUp"), y un profesor/coordinador puede crear y asignar tareas al equipo
  ("asignale una tarea a X", "creá una tarea para revisar el parcial", "cómo viene la
  carga de tareas del equipo").
  La skill habla con Moodle por peticiones REST (token moodle_mobile_app), NO por
  navegador. NO la uses para campus que no sean el de la TUP, ni para tareas de
  Moodle como administrador (crear cursos, matricular).
license: Apache-2.0
---

# TUP Campus Navigator — API REST

Skill para que un **tutor** de la Tecnicatura Universitaria en Programación (UTN)
opere su trabajo en el campus Moodle `https://tup.sied.utn.edu.ar` desde Claude
Code: ver qué le falta corregir, generar informes, cargar notas y (opcional)
corregir con Active-IA — todo por la **API REST oficial** de Moodle, sin navegador.

**Cómo funciona por dentro:** la skill trae un MCP server liviano (`mcp/`) que el
tutor corre local con sus credenciales. Ese MCP saca un token del campus
(`login/token.php?service=moodle_mobile_app`, el mismo que usa la app móvil, sin
admin) y hace peticiones a `webservice/rest/server.php`. Devuelve JSON estructurado,
no HTML: por eso no se rompe cuando el campus cambia de diseño.

> **Regla que ordena todo: verificar en vivo, nunca inventar.** Cada ID (curso,
> comisión, tarea) se descubre del campus y se valida contra lo que el campus
> devuelve. Si un dato no se pudo confirmar, se dice "no pude", nunca se rellena con
> algo plausible.

## Cuándo activarla

- "Entrá al campus" / "abrí Moodle TUP" / "tup.sied"
- "Mapeá mis comisiones" / "actualizá mis datos" / "configurá mi cohorte"
- "¿Qué me falta corregir?" / "pendientes" / "quién no entregó" / "deudas"
- "Informe de la comisión X" / "PDF de pendientes"
- "Cargá 8 al TP de tal alumno" (escritura → pide OK antes)
- "Corregí los TPs con Active-IA" / "importá entregas" / "devolución automática"
- "Conectá ClickUp" / "ayudame a conectar ClickUp" / "quiero ver mis tareas" — vale
  aunque sea la primera vez que se usa la skill y todavía no haya nada configurado
  (ni Moodle ni ClickUp): activá igual y arrancá por el bootstrap que corresponda.

## El menú — cómo guiar al tutor (hacé esto al activarte)

Cuando la skill se activa **sin un pedido concreto** (el tutor dice "campus", "moodle",
"hola" o abre la skill), NO le preguntes en abstracto qué necesita: **mostrale el menú**
y llevalo de la mano. Un tutor no sabe qué tools existen; el menú es su interfaz.

**Antes del menú, chequeá el estado con `mis_datos` y encadená el gate correcto:**

- **Sin credenciales configuradas** (una tool falla pidiendo credenciales) → primero
  `configurar`: pedile usuario y contraseña de Moodle. No sigas sin esto.
- **Configurado pero `mis_datos` vacío** → primero el mapeo (Paso 0): descubrí y guardá
  sus comisiones. Sin mapeo no hay sobre qué trabajar.
- **Todo listo** → mostrá el menú. **El ítem 7 depende de si ClickUp está conectado**:
  fijate si tenés alguna tool `mcp__clickup__*` en tu caja de herramientas (esto es
  gratis, no pega a la red — es solo mirar qué tenés disponible) y elegí la línea que
  corresponda:

```
📚 Campus TUP — ¿qué querés hacer?

  1. 🔴 Quiénes están abandonando  — cruza inactividad + entregas faltantes
  2. ✍️  Corregir TPs              — de a uno, o una tarea entera en tanda
  3. 📊 Qué falta corregir         — de una tarea puntual, o informe en PDF
  4. 👤 Ver un alumno              — su avance, entregas y notas
  5. 📈 En qué falla la comisión   — los errores que más se repiten al corregir
  6. ⚙️  Mis datos / remapear      — ver o rehacer la configuración
  7. 📋 Mis tareas (ClickUp)       — ver lo que te asignaron, marcar como hecha

Decime el número, o contame con tus palabras qué necesitás.
```

  Si NO tenés ninguna tool `mcp__clickup__*` (nunca se conectó en esta máquina),
  cambiá esa última línea por esta, y ofrecé conectarlo ahí mismo si dice que sí —
  **no esperes a que lo pida él**, es la única forma de que descubra que existe:

  ```
  7. 🔌 Tareas del equipo (ClickUp) — no conectado todavía, ¿lo conectamos? (2 min)
  ```

**Qué hace cada opción** (mapeo a tools; el tutor puede pedir por número o por palabras):

1. **Quiénes están abandonando** → `alumnos_en_riesgo` **+ `sin_entrar_al_aula`**. Es lo
   primero del menú porque es lo que más impacto tiene: la deserción avisa antes de pasar y
   ninguna otra vista cruza las señales. Presentá los 🔴 primero con su motivo; ofrecé
   escribirles (va por `responder_mensaje`, con OK). Si devuelve `sin_alumnos`, decilo tal
   cual: la comisión todavía no tiene matriculados, NO es que estén todos al día.

   **Cuál de las dos.** `alumnos_en_riesgo` cruza dos señales (días sin abrir la materia +
   racha de entregas); `sin_entrar_al_aula` mira sólo el reloj de la materia, pero ordena la
   comisión entera por él y **no depende de que haya vencimientos**.

   Usá `sin_entrar_al_aula` cuando **la racha no sirve**, que pasa más de lo que parece:
   - a principio de cuatrimestre, cuando todavía no venció nada;
   - y sobre todo si las actividades **no tienen fecha de entrega**. Medido en Prog I: las 10
     de cierre no tienen `duedate`, así que "sin entrega" no se distingue de "no vencía" y la
     racha marca a **94 de 94 alumnos en rojo**. Si `alumnos_en_riesgo` devuelve
     `alarma_saturada`, no lo presentes como "todos están abandonando": explicá que la alarma
     está saturada y pasá al reloj de la materia.

   **Los días son SIN ABRIR ESTA MATERIA en las dos tools** (`dias_sin_abrir_la_materia`), y
   el acceso al campus va aparte (`dias_sin_entrar_al_campus`). Son dos relojes distintos y
   hasta la v1.13.0 esto leía el equivocado: el que entra todos los días para otra materia y
   nunca abrió la tuya daba `0` → verde → y los verdes no se devuelven, así que **era
   invisible**. Corrido contra el código viejo con datos reales de un tutor: de 6 alumnos que
   había que contactar, **5 salían verdes**. Si el tutor pregunta "quién no entra",
   preguntale si es al campus o a la materia — y nunca presentes uno como si fuera el otro.

   El campo `estado_aula` decide cómo hablarle a cada uno: `nunca_abrio` + activo en el campus
   = **eligió no entrar** (es el llamado más urgente y el más recuperable); `nunca_abrio` sin
   pasar por el campus = capaz dejó de cursar o se quedó sin acceso (otra conversación);
   `sin_dato` = no se pudo leer, **no digas que no la abrió**.
2. **Corregir TPs** → preguntá si es **uno puntual** o **una tarea entera**:
   - Uno puntual: `ver_entrega` (LEELA, no califiques a ciegas) → proponé nota y devolución
     con el porqué → `cargar_nota` con preview → OK → confirmado.
   - Tarea entera: el flujo de lote (ver más abajo). Es el que conviene con más de 3 o 4.
3. **Qué falta corregir** → OJO: `pendientes_por_corregir` y `entregas_tarea` trabajan
   sobre UNA tarea, no sobre la comisión entera. Si el tutor pide algo puntual ("¿quién no
   entregó el TP5?"), usalas directo. Si pide el panorama completo ("cómo viene mi
   comisión"), NO le vayas preguntando tarea por tarea: ofrecele
   **`armar_informe`** (PDF con todas las tareas de una) o **`actualizar_tableros`**
   (histórico, avisá que tarda minutos). Si `armar_informe` devuelve `degradado`, decile
   qué tareas quedaron afuera: el total del PDF está incompleto.
4. **Ver un alumno** → pedí nombre o email y usá `buscar_alumno` (en vivo, no necesita
   snapshot). Con `traza=true` trae qué entregó y qué nota sacó en cada tarea — tarda más,
   pedilo sólo si hace falta la situación académica completa.
5. **En qué falla la comisión** → `errores_frecuentes`. Cuando un tema aparece en más del
   40% de los corregidos viene marcado `sistemico`: a esa altura el problema ya no es de
   los alumnos, es del material o de cómo se dio el tema. Decíselo así.
6. **Mis datos / remapear** → `mis_datos` para mostrar la config; si cambió la cohorte,
   bootstrap de nuevo (`aulas` → elegir materia → `descubrir_comisiones` → validar →
   `guardar_mis_datos`). **Si `mis_datos` devuelve `config_incompleta`**, la config NO
   está vacía pero tiene huecos que rompen el panel en silencio (un curso sin `tareas`,
   o ninguna materia que parezca Programación) — decíselo al tutor ANTES de seguir como
   si todo estuviera bien, y ofrecé rehacer el mapeo del curso puntual.
7. **Mis tareas (ClickUp)** → antes de nada, comprobá la conexión (Paso 0-bis, más
   abajo): si falla, explicá qué falta y NO muestres este ítem, seguí con Moodle
   normalmente. Si el tutor todavía no tiene `user_id` de ClickUp guardado en "Mis
   datos", resolvelo una vez: `clickup_find_member_by_name` (probablemente devuelva
   `null`, no matchea nombres de cátedra tal cual — ver `references/clickup-tareas.md`)
   → si falló, `clickup_get_workspace_members` y buscá la coincidencia a mano.
   **Confirmá con el tutor que es él/ella** antes de guardarlo con
   `guardar_clickup_id`. Después,
   `clickup_filter_tasks` sobre la(s) `tareas_list_id` de SUS materias (cruzando
   `mis_datos` con `mcp/clickup.json`), filtrando `assignees=[user_id]`. Para marcar
   una como hecha: mostrá cuál vas a cerrar, pedí OK, `clickup_update_task(task_id,
   status="complete")`. Detalle completo en `references/clickup-tareas.md`.

Después de resolver una opción, ofrecé volver al menú ("¿algo más? volvemos al menú").
El menú es una ayuda, no una jaula: si el tutor pide algo directo ("qué me falta en la
23"), hacelo sin pasar por el menú.

**Si `mis_datos` trae `actualizacion_disponible`**, avisale al tutor en una línea antes de
seguir: hay una versión nueva de la skill y se actualiza con `actualizar_skill` (después
hay que reiniciar Claude Code). No lo conviertas en una conversación: una línea y seguí.

## Corregir una tarea entera — el flujo de lote

Con más de 3 o 4 entregas, corregir de a una es ir y volver una vez por alumno. La cola
resuelve eso: se va anotando alumno por alumno **sin tocar Moodle** y al final una sola
confirmación escribe todo.

```
preparar_correccion(assign_id, group_id)   arma la cola con los pendientes
siguiente_para_corregir()                  próximo alumno CON su entrega ya bajada
   → leé el trabajo, decidí nota y devolución
anotar_correccion(...)                     lo guarda en la cola (NO escribe en Moodle)
   → repetí hasta que no quede nadie
confirmar_cola(confirmado=false)           mostrale las N juntas
confirmar_cola(confirmado=true)            recién acá se escriben, y se verifican
```

Reglas del lote:

- **Mostrale el preview completo antes de confirmar.** "Un solo OK" no es "OK a ciegas":
  el tutor tiene que ver las N notas con su devolución antes de decidir.
- **La cola es persistente.** Si la sesión se corta, al volver se sigue donde estaba: no
  vuelvas a preparar la cola salvo que el tutor quiera descartarla (`reemplazar=true`).
- **Si alguna falla, las demás igual se escriben.** Reportá cuáles fallaron y por qué; las
  fallidas quedan en la cola para reintentar sin duplicar las que ya salieron.
- **Etiquetá siempre** (`etiquetas=[...]`): son los temas del error, en kebab-case y
  **reutilizando el mismo nombre entre alumnos** (`perimetro-circulo`, no "el perímetro"
  en uno y "circunferencia" en otro). Es lo único que alimenta `errores_frecuentes`, y ese
  dato **no se puede reconstruir después**: exigiría releer todas las entregas de nuevo.

## Instalación (para el tutor)

Ver `README.md`. En resumen: instalar la skill, instalar las deps del MCP
(`pip install -r mcp/requirements.txt`), conectar el MCP en la config de Claude Code y
configurar las credenciales con la tool `configurar` (el tutor se las dice a Claude; no
hay que setear variables de entorno a mano).

## Paso 0 — Bootstrap (SIEMPRE primero, una sola vez)

**Sin esto, nada funciona.** La skill no puede pedir pendientes ni informes si todavía
no sabe cuáles son las comisiones del tutor. El orden es obligatorio:

0. **Antes de pedir NADA, mirá qué ya hay.** Llamá `mis_datos`: si el campus activo ya tiene
   credenciales guardadas (porque se dio de alta con `agregar_campus`/`configurar`, p. ej.
   desde el asistente), `mis_datos` **detecta solo** las materias y las comisiones que el tutor
   tiene asignadas y las guarda — no le pidas usuario, contraseña ni nombre. Si más adelante
   cambió la cohorte, `mapear_mis_datos` rehace ese mapeo con las credenciales ya guardadas.
   Sólo si una tool falla diciendo que faltan credenciales, seguí con el punto 1.
1. **Credenciales.** Pedile al tutor su usuario de Moodle (para muchos es el DNI,
   **pero no para todos** — no lo asumas) y su contraseña, y llamá la tool
   `configurar(moodle_user, moodle_pass)`. Esa tool guarda las credenciales en un
   `.env` local (permisos 600, fuera del repo — no se versiona) y **valida el login
   contra el campus** antes de darlo por bueno. El tutor NO setea variables de entorno
   a mano. Si el login falla, la tool lo dice: revisá usuario/contraseña.
2. **Preguntale el nombre y usá `mi_comision`.** Es el atajo: con el nombre del tutor,
   `mi_comision(nombre)` devuelve —de una— su comisión en cada materia (`group_id` real)
   y sus actividades de cursada con `cmid`. El tutor no descubre cursos, ni grupos, ni
   tareas: dice cómo se llama y ya está. Matchea sin acentos ni mayúsculas y acepta el
   apellido suelto; si el nombre da ambiguo devuelve los candidatos —**preguntá cuál es,
   no elijas vos**— y si no está en el reparto, lo dice y caés al mapeo manual del punto
   siguiente. Cada `group_id` se valida contra el campus antes de devolverse, así que un
   catálogo viejo avisa en vez de mentir.

   Un tutor puede tener **varias comisiones** (misma materia o distintas): devolvelas
   todas, no te quedes con la primera.

3. **Si `mi_comision` no lo encuentra — mapeo manual.** Llamá `aulas`: trae las materias de la cohorte
   vigente (Prog I/II/III) ya resueltas a su curso, así el tutor NO tiene que descubrir
   cursos — solo elige su materia. `aulas` valida cada curso contra la cuenta del tutor,
   así que nunca ofrece un aula que no tenga. Si `aulas` avisa que venció o no encuentra
   nada, caé a `descubrir_cursos` en vivo. Con la materia elegida, `descubrir_comisiones`
   trae sus comisiones (group_id REAL) y `listar_tareas` sus actividades.
4. **Validar antes de guardar.** Mostrá el mapeo propuesto y confirmá con el tutor.
   Cada `group_id` que vayas a guardar **tiene que estar en la lista que devolvió
   `descubrir_comisiones`**. Si proponés uno que no está, es un ID inventado:
   rechazalo y volvé a preguntar. (Pasó de verdad: un modelo "mapeó" la comisión 8
   con un group_id que no existía, y TODOS los datos quedaron mal en silencio.)
5. **Guardar** con `guardar_mis_datos`. De ahí en más el tutor no mapea más.

Consultá el estado con `mis_datos`. Si viene vacío, corré el bootstrap antes de nada.

## Paso 0-bis — Conectar ClickUp (opcional, solo para el menú de Tareas)

El apartado de Tareas (menú 7, y la sección del profesor más abajo) corre sobre el MCP
oficial de ClickUp — es OTRO servidor MCP, aparte de esta skill. Es opcional: sin esto,
el resto de la skill funciona igual.

**Si el tutor pide conectar ClickUp de entrada, ANTES de tener Moodle configurado**
(típico en una instalación nueva: "ayudame a conectar ClickUp" como primer mensaje,
sin haber corrido nunca `configurar` ni el Paso 0): podés instalar y loguear el MCP de
ClickUp igual (pasos 1-2 de abajo no dependen de Moodle para nada), pero
`guardar_clickup_id` SÍ necesita que exista "Mis datos" — sin eso no tiene dónde
anexar el ID y devuelve error. No lo trates como una falla: explicale en una línea que
falta ese paso ("para guardar tu usuario de ClickUp primero necesito tus datos de
Moodle") y ofrecele arrancar el Paso 0 ahí mismo, antes de seguir con ClickUp.

**Antes de ofrecer el menú de Tareas, comprobá la conexión:**
- Si no ves NINGUNA tool `mcp__clickup__*` en tu caja de herramientas, el MCP nunca se
  agregó → seguí la instalación de abajo.
- Si las tools SÍ están pero una prueba (`clickup_get_workspace_hierarchy` con
  `max_depth="0"`) falla con error de autenticación, el MCP está agregado pero sin
  sesión válida → solo hace falta repetir el paso 2 (login) de abajo.
- **Nunca muestres el menú de Tareas si esto falla.** Explicá qué falta con el paso
  exacto, sin tecnicismos, y seguí con Moodle normalmente.

**Instalación, una sola vez por máquina — LA HACÉS VOS (el agente), no el tutor:**

1. **Agregar el servidor.** Corré el comando **vos mismo, con tu propia herramienta de
   Bash/shell** (no le pidas al tutor que lo tipee — este paso no necesita terminal
   interactiva, así que no hay motivo para delegarlo):
   ```
   claude mcp add --transport http clickup https://mcp.clickup.com/mcp -s user
   ```
   El `-s user` importa: sin él el MCP queda pegado SOLO a la carpeta desde la que se
   corrió el comando (scope "local"), y casi nunca el tutor va a abrir Claude Code
   parado justo ahí. Mismo criterio que ya usa esta skill consigo misma en
   `install.sh` (`claude mcp add moodle-tutor -s user`). Confirmá el resultado antes
   de seguir (el comando devuelve "Added HTTP MCP server..." si salió bien).

2. **Login — EL PASO QUE SE TRABA, y este SÍ lo tiene que correr el tutor.**
   `claude mcp login clickup` abre un flujo OAuth que necesita una terminal
   interactiva de verdad: tu Bash NO sirve acá (falla con `stdin isn't a terminal`,
   verificado dos veces), así que explicaselo así de directo:
   - "Abrí PowerShell (o cmd) desde el menú de Windows — **aparte de esta ventana de
     Claude Code**, no adentro."
   - "Ahí pegá: `claude mcp login clickup` y Enter."
   - "Se te va a abrir el navegador para loguearte con tu cuenta de ClickUp — iniciá
     sesión y autorizá."
   - "Cuando termines, avisame acá."
   No lo dejes ahí: cuando el tutor te confirme, **vos volvé a probar la conexión**
   (paso 3) automáticamente — no le pidas que él mismo reintente el menú.

3. **Confirmar — lo hacés vos.** Apenas el tutor te avise que terminó el login, corré
   `clickup_get_workspace_hierarchy(max_depth="0")`. Si responde, seguí derecho con el
   bootstrap de Tareas (resolver `user_id`, punto 7 del menú más arriba) sin pedirle
   nada más. Si todavía falla, decíselo con claridad (a veces hace falta abrir una
   sesión nueva de Claude Code para que tome la conexión) y ofrecé reintentar.

Detalle completo de tools, catálogo y gotchas en `references/clickup-tareas.md`.

## Paso 0-ter — Agregar un campus nuevo (opcional, multi-campus)

Por defecto la skill opera contra UN solo campus (`tup`, TUP-UTN — es el que
`configurar` deja armado en el Paso 0). Si el tutor necesita operar OTRO campus desde
la misma instalación (otra UTN, otra sede), usá estas tres tools en vez de reinstalar
la skill o pisar las credenciales que ya tiene:

1. **`listar_campus()`** — muestra los campus registrados en esta máquina y cuál está
   ACTIVO ahora mismo. Una instalación recién hecha siempre tiene al menos `tup`.
2. **`agregar_campus(tenant_id, nombre, url, moodle_user, moodle_pass, activeia_user="",
   activeia_pass="")`** — registra un campus nuevo SIN tocar el que ya está
   configurado ni cambiar cuál está activo. `tenant_id` es un slug propio y único
   (ej. `"tup"`, `"otra-utn"`); si ya existe, se rechaza sin persistir nada. Valida el
   login contra `url` ANTES de guardar cualquier cosa — con credenciales inválidas no
   queda ni `.env` ni entrada en `listar_campus`. Con login OK, guarda las
   credenciales (permisos 600) y siembra el catálogo del campus nuevo corriendo
   `descubrir_cursos`/`descubrir_comisiones` automáticamente.
   Además arma solo su **"Mis datos"**: tus materias, las comisiones que tenés asignadas (los
   grupos de tipo comisión de los que sos miembro) y las tareas de cada curso — sin pedirte
   nada más. `mapear_mis_datos` lo rehace cuando quieras (con el campus activo).
3. **`usar_campus(tenant_id)`** — cambia cuál campus opera TODAS las demás tools (hoy
   ninguna pide un tenant explícito: siempre es "el activo"). Rechaza un `tenant_id`
   no registrado sin tocar el activo — usá `listar_campus` primero si no estás
   seguro del id exacto.

Cada campus tiene sus propias credenciales, su propio `mis_datos`, su propio caché de
snapshots/correcciones y su propio catálogo de aulas/comisiones — están completamente
aislados entre sí (viven en `~/.moodle-skill/<tenant_id>/`). Conmutar con `usar_campus`
nunca mezcla datos de un campus con los del otro.

Una instalación que ya venía de antes de este multi-campus (con `.env`/`mis_datos.json`
sueltos en `~/.moodle-skill/`) se migra SOLA la primera vez que arranca el MCP: esos
datos pasan a `~/.moodle-skill/tup/` sin que el tutor haga nada, y los originales
quedan intactos (nunca se borran).

## Las herramientas (qué pedir al MCP)

| Querés… | Tool |
|---|---|
| **Abrir el panel en el navegador** (chat + estado de tus comisiones) | `abrir_panel` |
| Ver tu config guardada | `mis_datos` |
| **Saber tu comisión y actividades diciendo tu nombre** | `mi_comision` |
| Elegir materia (aulas pre-cargadas) | `aulas` (fallback: `descubrir_cursos`) |
| Descubrir comisiones / tareas | `descubrir_comisiones`, `listar_tareas` |
| Guardar el mapeo (validado) | `guardar_mis_datos` |
| **Refrescar los datos (snapshot on-demand)** | `actualizar_tableros` |
| Conteo confiable de una tarea | `sumario` |
| **Quiénes entregaron y quiénes deben, con nombre** | `entregas_tarea` |
| Quién entregó y falta corregir | `pendientes_por_corregir` |
| Buscar un alumno (en vivo, sin depender del snapshot) | `buscar_alumno` |
| **Quién dejó de abrir ESTA materia** (reloj del curso, no del sitio) | `sin_entrar_al_aula` |
| PDF de pendientes | `armar_informe` |
| **Auditar cómo está armada un aula** (read-only) | `auditar_aula` |
| **Vista del PROFESOR: todas las comisiones del curso a la vez** | `reporte_coordinacion` |
| **Cuánto espera un alumno para que le corrijan, por comisión** | `demora_correccion` |
| **Informe para los TUTORES NEXO (+ PDF): alumnos que no abren la materia, por regional** | `informes_nexos` |
| **Qué hizo cada ALUMNO y con qué nota, por comisión, con el tutor a cargo (+ 1 PDF por comisión)** | `informe_alumnos` |
| Cargar una nota (con devolución, y `adjunto` si va un PDF) | `cargar_nota` |
| **Mensajes privados que te faltan contestar** | `mensajes_pendientes` |
| Bandeja de conversaciones · hilo completo | `leer_mensajes` · `leer_conversacion` |
| Mandar un privado a un alumno (pide OK) | `responder_mensaje` |
| **Consultas de foro que te faltan contestar** | `foros_pendientes` |
| Foros del curso · hilos de un foro | `listar_foros` · `leer_foro` |
| Mensajes de una discusión | `leer_discusion` |
| Responder en el foro (pide OK) | `responder_foro` |
| **El material del encuentro, para contestarle una duda a un alumno** | `material_encuentro` |
| **Abrir un tema nuevo: aviso, bienvenida** (pide OK) | `crear_discusion` |
| Ver el mapa Moodle ↔ Active-IA | `activeia_pendientes` |
| **Qué corrigió Active-IA de verdad, con su nota** | `activeia_correcciones` |
| Resolver comisión/rúbrica de Active-IA | `activeia_resolver` |
| **Corregir con Active-IA + PDF de devolución** | `corregir_con_active_ia` |
| Ver el estado actual de una corrección (antes de editarla) | `ver_correccion` |
| **Editar a mano una corrección de Active-IA cuando Gemini se equivoca** (pide OK) | `actualizar_correccion` |
| **Ver mis tareas asignadas (ClickUp)** | `clickup_filter_tasks` |
| Marcar una tarea como hecha (ClickUp, pide OK) | `clickup_update_task` |
| **Crear/asignar una tarea al equipo (profesor, pide OK)** | `clickup_create_task` |
| Resolver un tutor a su ID de ClickUp | `clickup_find_member_by_name` / `clickup_resolve_assignees` |
| Guardar el ID de ClickUp resuelto del tutor | `guardar_clickup_id` |

## El panel (misma skill, en el navegador)

Si el tutor dice **«abrí el panel»**, **«quiero verlo en el navegador»** o pide una
interfaz, es `abrir_panel`. Levanta un servidor local y abre
`http://127.0.0.1:8787`.

Adentro tiene lo mismo que acá: la conversación con las mismas 48 tools, y arriba
el estado de sus comisiones (padrón, pendientes y en qué unidad están), relevado
con `sumario`.

Tres cosas que conviene saber para explicárselo:

- **No necesita API key.** Usa la sesión de Claude Code que el tutor ya tiene.
- **Escucha sólo en `127.0.0.1`.** Corre con sus credenciales del campus y puede
  escribir en él: no se expone a la red, y no hay servidor central.
- **Toda escritura sigue pidiendo OK**, igual que acá, pero el freno lo aplica el
  panel y no el prompt: `cargar_nota`, `responder_mensaje`, `responder_foro`,
  `crear_discusion` y `confirmar_cola` quedan detenidas hasta que el tutor
  confirme en pantalla.

Si responde que no está compilado, la instalación es vieja: `actualizar_skill`.

## Encuentros — contestarle la duda a un alumno

En la modalidad de Encuentros el alumno no viene a una clase: mira **cápsulas de video** a
su ritmo, repasa con un **apunte interactivo** y, durante la ventana horaria del encuentro,
pregunta en un **foro** donde el tutor está de guardia. El foro es la única pata sincrónica
de todo el esquema, y es lo que esta sección ordena.

El flujo, cuando llega una duda:

```
material_encuentro(course_id)   el apunte de ESA unidad, video por video
leer_discusion(discussion_id)   la duda completa, no sólo el título
   → redactá la respuesta CON el apunte a la vista
responder_foro(..., confirmado=false)   preview
   → el tutor lo lee y da el OK
responder_foro(..., confirmado=true)    recién acá se publica
```

**`material_encuentro` va ANTES de redactar, no después.** El apunte está escrito video por
video y trae los mismos ejemplos y los mismos gotchas que el docente dio en cámara. Esa es
toda la razón por la que existe la tool.

Cuatro reglas, y la primera es la que sostiene a las otras tres:

1. **Se contesta con lo que está en el apunte.** Si la duda cae afuera, decilo y pasásela al
   tutor. No la completes con lo que sabés de Python: que suene bien no lo hace lo que el
   docente enseñó, y contradecirlo confunde más que no contestar. Es la misma regla que
   ordena el resto de la skill —**verificar, nunca inventar**— aplicada a un texto en vez de
   a un ID.
2. **Ese foro es del CURSO ENTERO**, no de una comisión (27 en Prog I). La duda que atendés
   puede ser de un alumno de otro tutor, y está bien: para eso hay alguien de guardia. Pero
   la respuesta la van a leer todas las comisiones, y va firmada con el nombre del tutor.
3. **Mirá `apunte.titulo` antes de usar el material.** Dice de qué unidad es. El link sale
   del campus y lo actualiza el equipo docente en cada encuentro: si quedó en la unidad
   pasada, la tool baja ese con total confianza y no tiene cómo saberlo. Un título que no
   coincide con el tema del encuentro es la señal — ahí pasá la `url` correcta a mano.
4. **Publicar sigue pidiendo OK**, como toda escritura. El tutor decide qué se le dice a un
   alumno; la IA prepara y ejecuta.

> El label de la modalidad y el foro suelen estar **ocultos a los estudiantes** fuera del
> horario (`visible=0`, verificado en vivo). Eso es a propósito: el foro se habilita para la
> ventana del encuentro. Si el foro está vacío y todavía no es la hora, **no es que nadie
> preguntó** — es que todavía no pueden.

## Reglas de oro (no negociables)

1. **Mapear primero.** Sin `mis_datos` no hay sobre qué trabajar (Paso 0).
2. **Nunca inventar un ID.** Todo group_id/assign_id sale del campus y se valida
   contra lo que el campus devolvió. Un ID que no está en la lista real no se usa.
3. **Nunca reportar éxito si una tool falló.** Si una tool devuelve `omitido`,
   `error` o vacío, **decilo** — no digas "listo, 0 pendientes, al día". Un "0"
   puede significar "no hay pendientes" o "no se relevó nada": son OPUESTOS y hay que
   distinguirlos. (Pasó: se reportó "all clear" cuando el snapshot se había omitido.)
4. **Multi-curso.** Un tutor puede tener Prog I, II y III a la vez. Nunca fijes un
   curso: operá sobre TODOS los de `mis_datos`.
5. **Escrituras con OK explícito.** `cargar_nota`, `responder_foro`,
   `responder_mensaje` y `crear_discusion` tocan el campus de alumnos reales. Mostrá
   exactamente qué vas a escribir y esperá el OK del tutor ANTES de ejecutar.
   **En `crear_discusion`, mostrá siempre `alcance_alumnos`**: es a cuánta gente le llega
   el aviso, y un tema publicado NO se borra desde la API. Si la tool se niega porque no
   pudo determinar el alcance, **no la fuerces con `group_id=0`**: ese valor significa
   "que lo vea el curso entero" y eso lo decide el tutor, no vos.
6. **Snapshot solo a pedido.** `actualizar_tableros` corre cuando el tutor lo pide, no
   solo. Avisá que puede tardar. Releva en paralelo (6 requests simultáneos; bajalo con
   `SNAPSHOT_CONCURRENCIA=3` si el campus está lento, subí el techo con
   `REFRESCO_TIMEOUT_S=600`). **Si devuelve `timeout`, leé `que_quedo` y decíselo al
   tutor**: se guarda por curso, así que el que estaba a mitad de camino no guardó nada, y
   padrón y entregas siguen mostrando la corrida anterior.
7. **Conteo confiable = `sumario`.** No cuentes filas a mano; el sumario oficial es la
   fuente.
8. **Tareas de ClickUp con OK explícito.** Crear, reasignar, cambiar estado o borrar
   una tarea sigue la misma regla que Moodle: mostrá exactamente qué vas a hacer y
   esperá el OK antes de `clickup_create_task` / `clickup_update_task` /
   `clickup_delete_task` o cualquier otra escritura de ClickUp.

## Gotchas de la API (ya resueltos en el código, no re-tropezar)

- **instanceid ≠ cmid.** Los web services de assign usan el **instanceid** de la
  tarea, no el cmid de la URL. El cliente ya mapea cmid→instanceid.
- **`mod_assign_save_grade` exige la devolución** (plugindata con contenido) o tira un
  error de base de datos engañoso. `cargar_nota` ya la manda.
- **Existe un registro de nota en `-1` para lo que TODAVÍA no se corrigió.**
  `mod_assign_get_grades` devuelve fila igual, con `grade` = `-1.00000`. Tomar "hay
  registro de nota" como "está corregida" apagó las 22 pendientes del curso entero en la
  primera corrida de `reporte_coordinacion`: el tablero decía 0 pendientes en las 16
  comisiones. Quien decide es **`gradingstatus` de la ENTREGA** (`graded`/`released` =
  corregida), que viene en la misma request. Verificado: el cruce dio
  `('graded','1.00000')→13`, `('graded','2.00000')→2`, `('notgraded','-1.00000')→10`, y ese
  10 coincide exacto con `sumario`.
- **`get_submissions` devuelve también los que NO entregaron.** 46 registros donde el
  conteo oficial decía 25: los otros 21 están en `status: "new"` (abrieron la tarea y no
  entregaron). Filtrar por `status == "submitted"` o el trabajo pendiente sale casi al doble.
- **El `groupid` de una entrega viene 0 y no es "grupo 0".** Es "no aplica" (la tarea no es
  de entrega grupal). No sirve para repartir entregas por comisión: eso se hace cruzando el
  `userid` contra el padrón de cada grupo.
- **El tutor de una comisión no siempre tiene el mismo rol.** En Prog I, 15 de 16 son
  `editingteacher` y C1-14 es `teacher`. Filtrar por una lista blanca de roles reportaba una
  comisión de 35 alumnos como huérfana. Se toma **todo el que no es `student`**.
- **Escala invertida.** En Aprobado/Desaprobado los valores están invertidos
  (Aprobado=1, Desaprobado=2). NO hardcodear: leer la opción por texto.
- **Hay más de una escala, y no siguen la misma regla.** La 5 (Aprobado/Desaprobado) va
  invertida; la 3 (No satisfactorio / Satisfactorio / Supera lo esperado, en Prog IV) va
  en orden creciente. **No existe "1 es la peor"**: cada escala se releva del `<select>`
  del grader. Si `cargar_nota` dice que no sabe si la tarea usa escala o número, **no
  insistas con un número**: andá al grader del campus y confirmá.
- **"Pendiente de corregir" ≠ "deuda del alumno".** El que no entregó nada tiene 0
  para corregir y NO está al día: es el que más debe. `pendientes_por_corregir` solo ve la
  cola de corrección: si devuelve 0, eso NO significa "todos entregaron". Para el padrón
  completo con nombres, `entregas_tarea`.
- **`forum_id` ≠ `cmid`.** `leer_foro` quiere el `forum_id`; el `cmid` es el módulo en
  el aula. Los dos vienen en `listar_foros` y no son intercambiables.
- **Los foros de consultas NO están separados por comisión.** Vienen con `groupid`
  vacío: son de todo el curso (27 comisiones en Prog I). El único group-separado es
  "Avisos de la comisión". Por eso `foros_pendientes` no puede decir "estas consultas
  son de TUS alumnos" en los foros de dudas — las muestra todas, y está bien: cualquier
  tutor puede contestarlas.
- **Filtrar foros por tipo no alcanza.** "Avisos de la comision" está declarado
  `general`, no `news`: filtrar solo por tipo metía cientos de avisos de otros tutores
  como si fueran consultas sin responder (106 falsos positivos en una prueba real). Por
  eso `foros_pendientes` filtra en positivo: solo foros de consultas/dudas, y saltea el
  de "buscar dupla" (que es entre alumnos). Lo salteado se informa en `foros_salteados`,
  nunca en silencio.
- **`leer_mensajes` NO trae el hilo**, solo el último mensaje de cada conversación.
  Antes de contestar un privado, abrí `leer_conversacion`: responder con el último
  mensaje como único contexto lleva a repetir lo ya dicho.
- **"Último mensaje del alumno" ≠ "necesita respuesta".** `mensajes_pendientes` marca
  como pendiente todo hilo cuya última palabra fue del alumno, y ahí caen muchos
  "gracias!" de cierre. Es a propósito: preferimos que sobre y el tutor descarte, antes
  que esconder una consulta real. No lo presentes como "tenés N mensajes sin contestar"
  sin haberlos mirado.
- **Por nombre solo se encuentra a quien ya escribió.** `responder_mensaje` resuelve el
  destinatario entre las conversaciones existentes; para escribirle por primera vez a
  alguien hace falta su email (`buscar_alumno`).
- **Ordenar antes de cortar.** Al toparse la cantidad de discusiones, hay que ordenar
  por fecha primero: cortar en el orden que devuelve el campus escondía pendientes
  viejos sin avisar.
- **ClickUp: `assignees` son IDs numéricos, no nombres.** Resolvé siempre primero con
  `clickup_find_member_by_name` / `clickup_resolve_assignees` antes de filtrar, crear
  o reasignar.
- **ClickUp: pasá siempre `workspace_id`** (del catálogo, `90171440963`) en las
  llamadas que lo aceptan. Un tutor puede tener OTROS workspaces de ClickUp
  personales; sin el `workspace_id` explícito, una resolución de nombre puede buscar
  en el equivocado.

## Matemática — en qué NO se parece a Programación

Todo lo verificado en vivo el 2026-09-02 contra el course 77 (15 comisiones, 552
participantes). La skill la cubre igual que a Programación, pero **cuatro cosas cambian y
las cuatro se pueden decir mal sin que salte ningún error**:

0. **La cursada NO pasa por las entregas: pasa por el CALIFICADOR.** Es lo primero que
   hay que saber y lo que más caro sale ignorar. `mod_assign` está en **cero absoluto**:
   549 participantes y 0 enviados en las 15 tareas, comisión por comisión. Lo que el
   alumno hace son **videos interactivos H5P (`hvp`), lecciones y autoevaluaciones**, y
   eso vive en el libro de calificaciones. Medido en la comisión 01 (40 alumnos): 240
   notas de video, 89 de lección, 99 de autoevaluación y **0 de entrega**. Entonces
   `sumario`, `entregas_tarea`, `pendientes_por_corregir` y `reporte_coordinacion`
   devuelven CERO para Matemática y ese cero es real pero se lee al revés: parece "la
   comisión no arrancó" cuando 34 de esos 40 alumnos vienen trabajando. Para Matemática
   usá **`informe_alumnos`**, que lee el calificador. Para Programación seguí con las
   otras: ahí la entrega sí es la entrega.

1. **Califica con NOTA NUMÉRICA, no con la escala invertida.** Las 13 actividades de
   cadencia van `/100` y "Entrega trabajo 1 y 2" van `/10`. Programación usa `scaleid 5`
   (Aprobado=1, Desaprobado=2). `cargar_nota` lee el tipo de la tarea en vivo y no hay que
   decírselo — pero **no traduzcas una nota de Matemática a Aprobado/Desaprobado**: ahí un
   68 no significa nada parecido a lo que significa un 1 en Programación.

2. **La cadencia tiene DOS ejes: unidad y semana.** Se llama `ENTREGA U3S1: …` y una unidad
   puede tener dos entregas (la U5 tiene tres). Nunca acortes la etiqueta a `U5`: tres
   columnas que digan lo mismo son tres columnas que el lector no puede distinguir. Lo
   resuelve `moodle/titulos.py` — no vuelvas a parsear el título a mano.

3. **Los alumnos entregan PDF, no código.** `ver_entrega` baja el archivo y devuelve
   `"0 legible(s) como texto"` con la `ruta` local: eso NO es un error ni una entrega
   vacía, es un binario. Abrilo desde `ruta` con la herramienta de lectura antes de
   calificar. En Programación llegaba un `.zip` con texto y se leía solo.

4. **Active-IA no tiene rúbricas de Matemática.** `activeia_resolver` devuelve "no
   existe" para sus cmid. Corregir con IA no está disponible acá hasta que la cátedra las
   cargue; la corrección es a mano, con `ver_entrega` + `cargar_nota`.

Lo que **sí** funciona igual y no hay que tocar: `descubrir_comisiones` (descarta solo los
12 grupos de horario "Miercoles 19:00 Hs." y las 17 regionales), `sin_entrar_al_aula`,
`auditar_aula`, `foros_pendientes`, `mensajes_pendientes` y `cargar_nota`.

Lo que **corre sin error y devuelve cero** por el punto 0 —no está roto, es que mira el
módulo que esta materia no usa—: `sumario`, `entregas_tarea`, `pendientes_por_corregir`,
`demora_correccion` y `reporte_coordinacion`. Si te piden "cómo viene Matemática", ésas
NO son la respuesta.

**El tutor de cada comisión se resuelve en vivo y NO está escrito en ningún lado.** El
padrón trae dos docentes por comisión: el tutor y el profesor de cátedra. `elegir_tutor`
se queda con el que aparece en MENOS comisiones del curso, porque el que cubre cinco es
la cátedra — contrastado contra el reparto oficial, 15 de 15. Antes se tomaba el primero
del padrón y el web service ordena por `userid`, así que devolvía la cuenta más vieja (la
de cátedra) en 13 de 15 comisiones.

Y una ventaja sobre Programación: **Matemática SÍ tiene fechas de entrega** (13 de 15).
`alumnos_en_riesgo` mide la racha bien desde el día uno, sin el problema de Prog I —donde
ninguna actividad de cierre tiene `duedate` y por eso marcaba 94 de 94 en rojo.

> Al auditar el aula, `auditar_aula` va a marcar como faltantes "Ejemplos de código
> (Colab)" y "Mini cuestionarios": esa matriz es la doctrina de Programación. En
> Matemática son **falsos positivos** — decilo al presentar el resultado, no lo reportes
> como hallazgo.

## Cuando un docente te está enseñando su materia

**Leé `aprendizajes_materia(course_id)` ANTES de empezar a trabajar en una materia**, no
cuando algo ya salió mal. Es lo que otros docentes ya corrigieron. En las materias que no
son Programación importa más, porque ahí la skill sabe menos y el docente sabe más.

Y al revés: **cuando el docente te corrige un supuesto, anotalo con
`anotar_aprendizaje`.** No la charla — la REGLA, en una frase que se entienda sola dentro
de seis meses. Corregir un supuesto suena así:

> *"Esa entrega no cuenta para la cursada"* · *"El TP2 lo calificamos distinto"* ·
> *"La U5 tiene tres semanas porque la partimos a mitad de cuatrimestre"* ·
> *"Ese cuestionario es obligatorio aunque no diga nada"*

Tres reglas, y las tres importan:

1. **Avisale ANTES de anotar, no después.** Queda registrado en un archivo que viaja en
   el repo y que ven los demás docentes de su materia. No se graba a nadie sin que lo
   sepa. Una línea alcanza: *"esto lo anoto para que le sirva a los otros docentes, ¿va?"*.
2. **`estado="dicho"` es el valor honesto** cuando acabás de escucharlo. Poné
   `confirmado` **sólo** si lo corroboraste contra el campus en esa misma sesión, y
   escribí en `verificacion` con qué tool y qué devolvió. Que lo diga un profesor lo hace
   creíble, no verificado — y la diferencia entre esas dos cosas es toda esta skill.
3. **Nunca anotes datos de alumnos** (nombres, mails, notas) ni credenciales. Lo que se
   guarda es cómo funciona la materia, no quién cursa.

Si lo que el docente dice **contradice** lo que ves en vivo, no elijas por tu cuenta:
mostrale las dos cosas y preguntale. Puede ser un aula mal configurada —y entonces el
hallazgo es para la cátedra— o un criterio que el campus no expresa. Las dos pasan.

> **Matemática, ahora mismo**: las 6 entradas que hay salieron de leer el campus, **sin
> ningún docente presente**. Dos están en `dicho` y son preguntas abiertas para
> **Cristian Mut** (profesor, com5), que es quien va a probar la materia: (a) si la
> actividad que CIERRA una unidad es la de mayor semana, y (b) si "Entrega trabajo 1 y 2"
> cuentan para la cursada o no. Si hablás con él, esas dos van primero.

## Probabilidad y Estadística — la tercera forma de cursar

Course 79, relevado en vivo el 2026-09-03: **15 comisiones, 580 alumnos, 16 semanas
agrupadas en 4 unidades**. No se parece ni a Programación ni a Matemática, y hasta esta
versión **la skill no la veía en absoluto**.

**Por qué era invisible.** Sus grupos se llaman `Comisión 1`…`Comisión 15`, no
`A26 C1-01`. El criterio de "qué grupo es una comisión" estaba escrito dos veces
(`panorama` y `ws_api`), las dos exigían el prefijo de cohorte, y las dos descartaban las
15. Sin comisiones no hay padrón, y sin padrón `reporte_coordinacion`, `informes_nexos`,
`demora_correccion` e `informe_alumnos` **no daban error: daban un curso vacío**. Ahora el
criterio vive sólo en `moodle/grupos.py` y entiende los dos vocabularios — no vuelvas a
parsear el nombre de un grupo a mano.

Cuatro cosas que se pueden decir mal sin que salte ningún error:

1. **El número de una sección es la SEMANA, no la unidad.** `5- Aprender a Contar` es la
   semana 5 y contiene el foro *"dudas sobre la **Unidad N° 2**"*. Aplicarle el criterio de
   Matemática devuelve "unidad 16" en una materia de cuatro. `calificador.eje_de_secciones`
   lo **deriva** cruzando el prefijo `S«n»-` de los títulos contra el número de sección (41
   coincidencias de 42 en PyE; cero en Matemática) — no está configurado por materia y no
   hay que decírselo.

2. **La cursada son 36 cuestionarios, no las entregas.** Más 4 **foros calificados** (que
   no existen en ninguna otra materia), 2 lecciones y un glosario. Los 9 `assign` sí tienen
   entregas reales —344 en el curso, a diferencia de Matemática— pero son el TAI y no la
   cadencia. `reporte_coordinacion` mira sólo los `assign`: en esta materia eso es el 20%.
   Para el avance usá **`informe_alumnos`**.

3. **Tres de los 9 `assign` no son entregas.** `CONDICIONES FINALES` (escala de 4 valores),
   `Promedio Trabajo Práctico Integrador` y `Calificación Coloquio` son casillas donde el
   docente vuelca una nota. Salen marcadas `naturaleza: administrativa` y quedan fuera de
   las tablas, declaradas. Por el título solo no alcanzaba: "Calificación Coloquio" se lee
   como el coloquio; lo que las delata es la **sección** que las contiene.

4. **La cursada va por la semana 5 de 16, y el campus no lo dice.** 32 de los 36
   cuestionarios no tienen fecha de cierre. `calificador.hasta_donde_llego_el_curso` lo
   deriva de la última semana con alguna nota cargada, el informe lo **declara como
   derivado**, y las semanas posteriores salen con «·» en vez de «0/4». Un cero y un
   "todavía no pasó" son cosas distintas, y once columnas de ceros se leen como abandono.

Y un bug que esta materia destapó y afectaba a todas: la lista de "instancias fuera de la
cadencia" contenía `"tio"` como substring suelto, que matchea **"cues-tio-nario"**. En
Programación y Matemática nunca se disparó; acá excluía los 36 cuestionarios de su propia
cadencia. Ahora va con límites de palabra.

> El tutor de cada comisión se resuelve **en vivo**, como en Matemática. Los 15 tienen rol
> `teacher`; sólo la Comisión 15 trae un segundo docente y ahí el desempate lo hace el rol,
> y el informe lo dice: *"se toma X sólo por el rol, que es un criterio más débil"*.

## Cosas que NO hacer

- No mapear a mano ni hardcodear IDs de cohortes viejas.
- No correr snapshots automáticos ni de otros tutores: solo el actual, a pedido.
- No escribir en el campus sin el OK del tutor.
- No dar por bueno un dato que no se pudo verificar en vivo.
- No asignar una tarea de ClickUp por nombre en texto libre: resolvé siempre a un
  `user_id` numérico primero, y confirmá con la persona si hay ambigüedad.
- No confundir la lista "Material PROG N" (materiales) con la lista "Tareas" del
  folder de la materia (trabajo asignable): usá siempre `tareas_list_id` del catálogo.
- No armar ranking ni podio de tareas completadas por tutor (misma regla que
  `reporte_coordinacion`).

## Vista del profesor — el curso entero, por comisión (read-only)

Todo el resto de la skill mira **una** comisión: la del tutor logueado. Éstas miran las
**16 a la vez**, y son para quien coordina la materia.

**Hay TRES informes de curso y contestan preguntas distintas.** Elegí por la pregunta:

- **`informes_nexos(course_id)`** → ¿el alumno **APARECE** por la materia? Los desenganchados
  agrupados por regional, con el Tutor Nexo de cada sede. Va a los nexos.
- **`reporte_coordinacion(course_id)`** → ¿el **TUTOR CORRIGIÓ**? El trabajo de corrección por
  comisión, por tutor y por actividad. Va a coordinación. **No sirve en Matemática**: mide
  entregas y esa materia no tiene ninguna (ver la sección de Matemática).
- **`informe_alumnos(course_id)`** → ¿qué **HIZO cada alumno y con qué nota**? Lee el LIBRO DE
  CALIFICACIONES —videos, lecciones, autoevaluaciones y entregas— y escribe **un PDF por
  comisión**, con su tutor arriba. Es el que se le manda a cada tutor.

### `informe_alumnos(course_id, group_id?, pdf=True, detalle?)`

Lee el calificador y no las entregas, y por eso ve lo que las otras no ven. Devuelve por
comisión: el **tutor a cargo**, sus alumnos, cuántas actividades hizo cada uno **por tipo y
por unidad**, y **la última vez que abrió LA MATERIA** — el reloj del curso, nunca el del
campus: el que entra todos los días para otra materia y hace un mes que no abre la propia
figura al día si se mira el reloj equivocado.

**Un PDF por comisión y no uno del curso**, y es una decisión de destinatario: un documento
donde la comisión de alguien aparece al lado de las otras catorce se lee como una comparación
entre personas. Misma separación que ya hay entre `informes_nexos` y `reporte_coordinacion`.
El PDF va apaisado y trae, además del panorama por alumno, una **matriz alumnos × actividades
con la nota en la celda**, una por tipo. Una matriz no resume: **muestra**. Deja ver lo que
ningún promedio deja — la unidad vacía en toda la comisión, el que hizo la 4 sin la 1 y la 2,
el que arrancó y paró.

**No hay porcentaje de avance, y es deliberado.** Un porcentaje necesita saber cuántas
actividades ya deberían estar hechas y ese dato no existe. Se cuenta lo que pasó
(`hechas/total`), no se estima lo que faltaría. Y el denominador cuenta **sólo los tipos que
el informe muestra**: sumarle las 15 tareas de entrega —que en Matemática no tienen una sola
entrega en todo el curso— dejaba al alumno que hizo todo lo posible en "7/9", que se lee como
si le faltara algo. Un denominador inalcanzable no mide nada.

**De dónde sale la unidad de cada video.** De la **estructura del curso**, no del título: los
títulos del calificador dicen "Video 2 Semana 1 SN" y nunca la unidad. La sección numerada
("2- Sistema binario") abre la unidad y los bloques que siguen (Videos, Lecciones, Trabajo
Práctico, Autoevaluaciones) la heredan; una sección con nombre propio **corta la herencia** y
sus actividades quedan sin unidad en vez de recibir la de al lado. Eso es lo que deja afuera a
los 7 cuestionarios de COLOQUIOS, a los dos integradores y al video de bienvenida. Se verifica
solo: las 13 tareas `ENTREGA U{n}S{m}` caen 13 de 13 en la unidad que dice su propio título.

**Tamaños.** El curso entero son ~2 min y 15 PDFs; la respuesta trae el índice (tutor, números,
ruta del PDF) más los alumnos que no hicieron nada. El alumno por alumno con su nota sale
pidiendo **una** comisión (`group_id`), donde `detalle` se prende sola. Con 562 alumnos × 81
actividades el detalle completo no entra en una respuesta y no lo lee nadie.

#### Sin `group_id` suma la vista del COORDINADOR

Otro documento y otro destinatario: `coordinacion_calificador_curso«N».pdf` pone las
comisiones **lado a lado** con su tutor, y por eso NO lleva nombres de alumnos salvo donde el
nombre ES la acción. Trae dos cortes que ninguna vista por comisión da:

- **`huecos_de_calificacion`** — actividades que andan en el curso y están en **CERO** en
  alguna comisión. Es el corte que no existía, y encuentra lo que a ojo no se ve: en PyE
  destapó que el cuestionario de la semana 3 tiene 98 notas en el curso y **ninguna en seis
  comisiones**, y que el de la semana 2 tiene **324** y cero en una. Sólo se listan
  actividades con al menos 5 notas en el curso: una que todavía no arrancó en ningún lado
  tiene catorce comisiones en cero y no dice nada.
  **Esto NO dice que esos alumnos no participaron.** Dice que ahí la actividad no tiene una
  sola nota mientras en el resto sí — puede ser que no se calificó, que no se dictó o que
  quedó restringida, y las tres se arreglan hablando con alguien distinto.
- **`alumnos_sin_comision`**, con nombre y mail. No los ve ningún tutor porque toda la skill
  trabaja por comisión: son invisibles por construcción. Decir "hay 1" no le sirve a nadie.

La columna **`al_dia`** (alumnos con alguna nota en la última unidad/semana que el curso
dictó) es la única comparable entre comisiones, y aun así no del todo. **Se audita el
TRABAJO, nunca se califica a la PERSONA**: nombrar al tutor es ruteo —a quién llamar— y va;
un ranking o un puntaje NO va, ni en la tabla ni en cómo lo contás.

`reporte_coordinacion(course_id, cmids?, incluir_foros?, pdf=True)` — el trabajo de corrección
del curso, cortado de **tres** maneras, con PDF:

- **por comisión**: tutor, alumnos, entregadas, corregidas, sin corregir, calificado sin nota,
  espera máxima, demora mediana, consultas de foro sin responder.
- **por tutor**: la carga sumando SUS comisiones — varios llevan dos y su cola real no está en
  ninguna fila. Ordenado por la espera más antigua.
- **por actividad**: la misma cola al revés. Cuando una actividad se atrasa en varias comisiones
  a la vez el problema suele ser de la consigna o del calendario, y por comisión no se ve.
  Medido en Prog I: la actividad de la unidad 4 tenía 12 sin corregir repartidas en 7
  comisiones — una o dos por tutor, invisible de a una.

Trae también `actividades_sin_fecha_de_entrega`, y eso importa: sin `duedate` no se puede
distinguir "no entregó" de "todavía no vencía". En Prog I no la tiene **ninguna** de las 10
actividades de cierre, y es lo que hace que la lista de riesgo marque al padrón entero.
Contrastada contra el conteo oficial de Moodle: da exacto. ~16 s en Prog I.

`demora_correccion(course_id, cmids?)` — días entre la entrega y la nota, por comisión.
Devuelve dos bloques que **no** son lo mismo: `demora_*` es historia (ya corregidas) y
`espera_*` es lo que un alumno está esperando ahora. Para actuar se mira `espera_max_dias`.

### Cómo presentar el resultado (esto no es opcional)

**Son HECHOS por comisión, no una evaluación del tutor.** Nombrar al docente es un dato de
ruteo — a quién llamar — no un juicio. Es la misma línea que ya traza `auditar_aula` cuando
deja la hoja EQUIPO vacía: un agente verifica trabajo, nunca califica personas.

- **NO** armes un ranking, un podio ni un puntaje por tutor. Ordenar por `espera_max_dias`
  para saber por dónde empezar está bien; presentarlo como "tabla de posiciones" no.
- **Leé `sin_dato` de cada fila antes de concluir nada.** Distingue "0 porque está al día"
  de "0 porque la comisión está vacía", "porque nadie entregó todavía" o "porque no pude
  leer". Un 0 con motivo NO es trabajo terminado, y esa confusión cae sobre una persona.
- Un número alto puede ser una comisión más grande, una consigna más difícil o una semana
  de parcial. Ofrecé el dato, no el veredicto.
- Requiere ver comisiones ajenas. Si el rol del usuario no lo permite, las filas vuelven con
  `sin_dato` explicando el motivo — nunca con un 0.
- **El desenganche de `informes_nexos` es sobre ALUMNOS, no sobre tutores.** "com4 tiene 6
  desenganchados" es un hecho del aula y sirve para ruteo; "el tutor de com4 no engancha a su
  gente" es un juicio sobre una persona y no va. Misma línea que el resto: la comisión es la
  unidad de medida, no el docente.

## Tareas del equipo (ClickUp) — crear y asignar (profesor)

Aparte de corregir, un profesor/coordinador puede necesitar repartir trabajo puntual
entre tutores ("armá el parcial de la unidad 4", "revisá esto antes del viernes").
Eso vive en ClickUp — un tablero por materia que el equipo ya usa activamente
(`mcp/clickup.json` tiene el mapa materia → lista). Requiere ClickUp conectado
(Paso 0-bis, más arriba).

**Crear y asignar una tarea:**
1. Preguntá la materia → resolvé `tareas_list_id` del catálogo.
2. Preguntá a quién va. Cruzá el nombre con `comisiones.json` y resolvé el ID real de
   ClickUp: probá `clickup_find_member_by_name` (**casi siempre devuelve `null`** — el
   nombre de cátedra y el de ClickUp no suelen ser la misma cadena, no es matching
   difuso) → si falló, `clickup_get_workspace_members` y resolvé a mano. Mostrale al
   profesor el candidato (o candidatos) y que confirme antes de seguir.
3. Armá el preview completo (título, descripción, `due_date`, `priority`, asignado) y
   **esperá el OK** — misma regla que `cargar_nota`.
4. `clickup_create_task(list_id=..., name=..., markdown_description=..., due_date=...,
   priority=..., assignees=[user_id], workspace_id="90171440963")`.
5. Devolvé el link/ID de la tarea creada.

**Carga de tareas por tutor (read-only, MISMA doctrina que `reporte_coordinacion` —
NUNCA un ranking):** `clickup_filter_tasks(list_ids=[...], statuses=["to do","in
progress"], include_closed=false, workspace_id="90171440963")`, agrupado por
`assignees`. Es **dato de ruteo**: "Fulano tiene 4 tareas abiertas, 1 vencida" sirve
para repartir mejor, no es una nota. No armes podio ni compares tutores entre sí como
mérito.

El detalle de catálogo, semántica de cada tool y gotchas completos está en
`references/clickup-tareas.md`.

## Auditoría de aula virtual (read-only)

`auditar_aula(course_id, materia, evaluador, rol, con_navegador, unidad)` releva cómo está
**armada** un aula (no "quién entregó qué") y escribe un **worksheet `.md`** en `salidas/` —
un BORRADOR basado en evidencia que el evaluador humano completa. Pensada para la "puesta a
punto del Moodle" con la planilla oficial de 6 hojas (Aula Virtual, Unidades, Evaluaciones,
Equipo, Checklist, Desarrollo).

**PREGUNTÁ QUÉ UNIDAD antes de auditar.** Un tutor audita SU unidad, no las 10 — y auditar
todo tarda mucho más (testea todos los links y cuestionarios). Cuando el tutor pida auditar,
ofrecele: "¿qué unidad? (1-10)" o "todo el aula". Con la respuesta corré `auditar_aula` con
`unidad=N` (o sin `unidad` para el aula entera). Si el número no existe, la tool devuelve
`unidades_disponibles` para reintentar.

**La regla que la ordena (la misma de la skill): un agente verifica presencia / ausencia
/ consistencia, NUNCA calidad.** Puntaje autolimitado: `0` = ausente (verificado), `3` =
presente, **vacío** = no verificable sin leer contenido → "sin dato", jamás se infiere.
La calidad y los puntajes finos los pone la persona que firma.

Cómo trabaja:

- **Matriz de unidades** (el 80% del valor): agrupa las secciones por cabecera numerada
  (`1- …`, `10- …`) con sus hijas (Actividades, Práctica, Microteaching, Autoevaluación,
  Encuesta) y marca presencia de los 9 componentes. Los nombres de unidad salen del aula,
  no se hardcodean: vale para Prog I/II/III aunque cambien los temas.
- **Links**: testea los externos (404 real = roto; otro campus; href con espacio). Los del
  propio campus no se piden (su existencia la confirma la API). **Las apps de Google
  (NotebookLM/Colab/YouTube) NO se juzgan por un GET** —siempre parece login o rate-limit—:
  se marcan "requiere navegador" y las resuelve el pase navegador.
- **Hallazgos**: componente faltante sistemático, hueco en un patrón (9 de 10), instancia
  extraordinaria con `visible=1` (posible fuga de examen — se enmarca como *verificar*).

**Pase navegador (`con_navegador=True`, opcional):** para lo que la API no ve. Loguea por
navegador (Playwright, sesión persistida) y cuenta las **preguntas** de cada cuestionario
abriendo su edición (mini=4 / autoeval=10 según la planilla → hallazgo si difiere) y
clasifica las **apps Google** en *abren* (Colab público) vs *piden cuenta* (NotebookLM).
Requiere `pip install playwright && playwright install chromium`; si falta, se saltea con
aviso y la auditoría por API igual corre. `mod/quiz/edit.php` necesita rol docente sobre
el quiz (el tutor de la TUP lo tiene). Tarda: abre una página por cuestionario.

Guardrails (no negociables):

1. **Read-only sobre Moodle.** No corrige, no publica, no mensajea. Solo escribe el `.md`.
2. **La hoja EQUIPO se deja VACÍA.** Un agente no evalúa el desempeño de personas.
3. **Describe el hecho y su impacto, no juzga a la persona.** "El foro U6 tiene 12
   consultas sin responder" ✅, no "el tutor no atiende" ❌.
4. **El worksheet es un BORRADOR, no un veredicto.** Lo dice el propio archivo.
5. **Lo no verificado va a "a revisar", nunca a "Sí".** Un link no testeado no funciona.

## Corrección automática con Active-IA

Pipeline opcional, por API REST (JWT). Requiere cuenta de Active-IA (env vars
`ACTIVEIA_URL`, `ACTIVEIA_USER`, `ACTIVEIA_PASS`). Gemini corre server-side en
Active-IA — el tutor NO pasa una API key de Gemini.

**Flujo** (para "corregí el TP X del alumno Y con Active-IA"):

1. `activeia_pendientes` / `activeia_resolver(assign_id, group_id)` → obtené el
   `comision_id` y `rubrica_id` de Active-IA para esa tarea.
2. `corregir_con_active_ia(assign_id, email, comision_id, rubrica_id, confirmado=...)`:
   baja el trabajo del alumno de Moodle (REST), lo sube a Active-IA, que lo corrige
   con Gemini, y **descarga el PDF de devolución** a `$MOODLE_SKILL_HOME/salidas/`.
   Devuelve `{ok, nota, devolucion_pdf_local, correccion_id, estado}`.
3. La corrección **NO carga la nota en Moodle**: eso es `cargar_nota`, un paso aparte.
   Si le pasás el PDF en `adjunto`, va adjunto a la devolución — pero sólo si la tarea
   acepta archivos de retroalimentación; si no, te avisa y **no promete lo que no hizo**.

**Ante `GEMINI_OVERLOADED`, NO vuelvas a disparar la corrección.** Ese error significa
que la respuesta no llegó a tiempo, **no** que la corrección se perdió: la entrega quedó
subida y muchas veces termina bien minutos después. Mirá primero
`activeia_correcciones(comision_id)`, que es lo que Active-IA corrigió **de verdad** con
su nota. Si reintentás igual, la skill detecta la entrega ya subida y **retoma** ese
trabajo en vez de duplicarlo.

⚠️ **`activeia_pendientes` NO sirve para saber qué corrigió Active-IA.** Sus contadores
`espera`/`corregidos` son del estado **en Moodle**: `corregidos: 0` quiere decir "sin nota
cargada en el campus", no "Active-IA no corrigió". Confundirlos hizo dar por perdidas
correcciones que ya estaban hechas. Para eso está `activeia_correcciones`.

**Es una ESCRITURA, pero en Active-IA — NO en Moodle.** Sube la entrega del alumno y
dispara una corrección con IA: cuesta tiempo de cómputo y deja registro del lado de
Active-IA. La nota del campus NO la toca ninguna rama de esta tool; eso es `cargar_nota`,
aparte y con su propia confirmación. Con `confirmado=false` devuelve un preview de lo que
va a hacer SIN ejecutar. Mostráselo al tutor y volvé a llamar con `confirmado=true` solo
tras su OK explícito.

**Si la devolución de Gemini no coincide con lo entregado, no la cargues igual.** Caso
real (2026-08-31, Molinari, correccion_id 24794, Prog III com2): Active-IA marcó como
ausentes clases CSS que SÍ estaban en el código real del alumno, sugiriendo 16/100 sobre
una entrega que valía 100/100. Antes de confiar en la nota sugerida, comparala contra
`ver_entrega` (lo que el alumno mandó de verdad). Si no coincide: `ver_correccion`
(estado actual) → `actualizar_correccion(..., confirmado=...)` (edita nota/criterios/
fortalezas/recomendaciones/comentario, marca `editado_manualmente=True` del lado de
Active-IA, regenera el PDF). Misma regla de oro que el resto de la skill: **nunca editar
a ciegas, nunca sin haber leído el código real primero.**

El detalle de estados y modo híbrido está en **`references/active-ia.md`**.
