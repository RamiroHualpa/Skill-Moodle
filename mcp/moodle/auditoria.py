"""Auditoría de aula virtual por API REST (presencia / ausencia / consistencia).

Complementa a `ws_api` (que responde "¿quién entregó qué?") con "¿el aula está bien
armada?". El método viene del handoff de Neyén (dos auditorías reales de Prog 1 y Prog 4):

  REGLA QUE ORDENA TODO — un agente verifica PRESENCIA, AUSENCIA y CONSISTENCIA, nunca
  CALIDAD. La calidad la juzga una persona. Por eso el puntaje se autolimita:

      0           = componente AUSENTE (verificado que no existe)
      3           = componente PRESENTE (existe y cumple el mínimo)
      None/blanco = NO verificable sin leer contenido → "sin dato", NUNCA se infiere

  Esto es la MISMA doctrina que el resto de la skill: verificar en vivo, nunca inventar.
  El "blanco" no es una omisión: es la categoría que evita que el informe mienta.

Todo acá es READ-ONLY sobre Moodle: ninguna función escribe en el campus. La única
salida es un worksheet `.md` local (un BORRADOR basado en evidencia, no el veredicto del
evaluador — la persona que firma ajusta los puntajes finos).

Ventaja de hacerlo por API y no por navegador: `core_course_get_contents` devuelve TODAS
las secciones en el JSON, así que el gotcha del formato `onetopic` (cada sección es una
pestaña, la home no muestra todo) que sufría el crawl con Playwright acá no existe.
"""

import asyncio
import datetime
import re
from urllib.parse import quote, urlparse

from .cliente import MoodleWSError
from .ws_api import _norm, _plano

# ---------------------------------------------------------------------------
# Mapeo modname -> componente de la matriz de Unidades (handoff §5.2).
# Un módulo puede aportar a más de un componente; la desambiguación fina (mini
# cuestionario vs autoevaluación) se hace por nombre más abajo.
# ---------------------------------------------------------------------------

# Los 9 componentes de la matriz, en el orden de la planilla oficial.
COMPONENTES = [
    "Introducción y portada",
    "PDF de lectura",
    "Ejemplos de código (Colab)",
    "NotebookLM del tema",
    "Mini cuestionarios (4 preg.)",
    "Práctica (guía de ejercicios)",
    "Autoevaluación (10 preg.)",
    "Encuesta de cierre",
    "Foro de consultas",
]

_HREF_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.I)
# Una UNIDAD arranca con una cabecera numerada: "1- Estructuras Secuenciales",
# "10- Recursividad". Las secciones que siguen (Actividades, Práctica, Microteaching,
# Autoevaluación, Encuesta de cierre) son sus HIJAS, no unidades sueltas.
_RE_CABECERA_UNIDAD = re.compile(r"^\s*\d+\s*[-–—·.)]")
# Secciones que cierran el bloque de unidades: de acá en adelante ya no es una unidad
# temática (integrador, evaluaciones, gestión). Cortan la recolección de hijas.
# OJO: `\bevaluac` con límite de palabra, para NO matar la sub-sección "Autoevaluación"
# de cada unidad (que empieza con 'auto' y quedaría fuera del grupo — pasó de verdad).
_RE_FIN_UNIDADES = re.compile(
    r"integrador|\bevaluac|encuentro|comision|gesti[oó]n|active|honor|s[ií]ncron|parcial", re.I)
# Sub-secciones hijas: dónde vive cada componente dentro de la unidad.
_RE_SUB_ACTIVIDADES = re.compile(r"actividad")
_RE_SUB_AUTOEVAL = re.compile(r"autoevaluac")
_RE_SUB_PRACTICA = re.compile(r"practic")
# ID de YouTube (11 chars) desde cualquier formato: /embed/, youtu.be/, watch?v=, /shorts/.
# Clave para oembed: solo traga watch?v=, NO /embed/ (un /embed/ válido daba 404 → falso
# "video borrado"; verificado a mano).
_RE_YT_ID = re.compile(r"(?:embed/|youtu\.be/|[?&]v=|/v/|shorts/)([A-Za-z0-9_-]{11})")
# Instancias que NO deberían estar visibles al alumno (fuga de examen — handoff §7).
_RE_EXTRAORDINARIA = re.compile(r"extraordinari|instancia\s+de|recuperatori", re.I)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _clasificar_link(url: str) -> str:
    """Etiqueta el destino de un link por su host, para saber qué componente alimenta y
    cómo testearlo."""
    h = _host(url)
    if "colab.research.google.com" in h:
        return "colab"
    if "notebooklm.google.com" in h:
        return "notebooklm"
    if "docs.google.com" in h or "drive.google.com" in h:
        return "google_docs"
    if "youtube.com" in h or "youtu.be" in h:
        return "youtube"
    if "genial.ly" in h or "genially" in h:
        return "genially"
    return "otro"


# ---------------------------------------------------------------------------
# Relevamiento del aula (core_course_get_contents)
# ---------------------------------------------------------------------------

async def relevar_aula(client, course_id: int) -> dict:
    """Inventario del aula por sección: cada módulo clasificado por `modname`, más los
    links (internos y externos) que aparecen en URLs de módulo y en el HTML de las
    descripciones/summaries. Read-only. Devuelve dict con `error` si el WS falla."""
    try:
        secciones = await client.ws("core_course_get_contents", {"courseid": course_id})
    except MoodleWSError as e:
        return {"error": f"No pude leer el contenido del curso {course_id}: {e.errorcode} — {e.message}"}
    if not isinstance(secciones, list):
        return {"error": f"Respuesta inesperada de core_course_get_contents para el curso {course_id}."}

    out_secciones = []
    for sec in secciones:
        modulos = []
        # Links embebidos en el summary de la propia sección.
        links = _extraer_links(sec.get("summary", ""))
        for mod in sec.get("modules", []) or []:
            modname = mod.get("modname", "")
            m_links = list(mod.get("url") and [mod["url"]] or [])
            m_links += _extraer_links(mod.get("description", ""))
            for c in mod.get("contents", []) or []:
                fu = c.get("fileurl")
                if fu:
                    m_links.append(fu)
            modulos.append({
                "cmid": mod.get("id"),
                "modname": modname,
                "nombre": _plano(mod.get("name", ""), 200),
                "visible": bool(mod.get("visible", 1)),
                "links": m_links,
            })
            links += m_links
        out_secciones.append({
            "seccion": sec.get("section"),
            "nombre": _plano(sec.get("name", ""), 200),
            "visible": bool(sec.get("visible", 1)),
            "resumen_len": len(_plano(sec.get("summary", ""), 4000)),
            "modulos": modulos,
            "links": links,
        })
    return {"ok": True, "course_id": course_id, "secciones": out_secciones}


def _extraer_links(html: str) -> list[str]:
    if not html:
        return []
    return [m.strip() for m in _HREF_RE.findall(html)]


# ---------------------------------------------------------------------------
# Testeo de links (handoff §7 — lo que un humano no ve y el script encuentra)
# ---------------------------------------------------------------------------

async def testear_links(client, inventario: dict, base_url: str) -> list[dict]:
    """Testea los links EXTERNOS del inventario. Por cada uno: OK / roto / pide login /
    link a otro campus / href con espacio. Los internos de Moodle (pluginfile del propio
    campus) NO se piden por HTTP: su existencia ya la confirma el WS de contenidos.

    Un link no testeado NO es un link que funciona: lo que no se probó va a 'a revisar'.
    """
    import httpx

    base_host = _host(base_url)
    # Deduplicar: la misma URL suele repetirse en varias unidades.
    urls: dict[str, dict] = {}
    for sec in inventario.get("secciones", []):
        for url in sec.get("links", []):
            if url in urls:
                urls[url]["secciones"].append(sec.get("nombre"))
                continue
            urls[url] = {"url": url, "secciones": [sec.get("nombre")]}

    resultados = []
    sem = asyncio.Semaphore(5)  # no martillar servicios externos

    async def _get(url: str):
        """GET tolerante: devuelve la respuesta o None si la conexión falla."""
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
                return await http.get(url, headers={"User-Agent": "Mozilla/5.0 (auditoria-aula)"})
        except (httpx.HTTPError, ValueError):
            return None

    async def _test(entry: dict) -> dict:
        url = entry["url"]
        h = _host(url)
        # Estáticos: no requieren request.
        if url != url.rstrip():  # href con espacio/whitespace al final → se rompe como %20
            return {**entry, "tipo": _clasificar_link(url), "estado": "href_con_espacio",
                    "detalle": "El href termina en espacio: al clic se rompe como %20."}
        if not h:
            return {**entry, "tipo": "otro", "estado": "no_testeable",
                    "detalle": "URL sin host reconocible (¿relativa?)."}
        # Interno del propio campus: existencia ya confirmada por el WS. No se pide.
        if h == base_host:
            return {**entry, "tipo": "interno", "estado": "interno_no_testeado",
                    "detalle": "Link del propio campus (existencia confirmada por la API)."}
        # Otro campus UTN/Moodle distinto del auditado → hallazgo (handoff §7).
        if h != base_host and ("utn.edu.ar" in h or "sied" in h or "campus" in h):
            return {**entry, "tipo": "otro_campus", "estado": "otro_campus",
                    "detalle": f"Apunta a otro dominio de campus ({h}), no al auditado."}
        tipo = _clasificar_link(url)
        # NotebookLM es una SPA con sesión: un GET sin navegador SIEMPRE cae en login,
        # exista o no el permiso. No se puede concluir por HTTP → lo resuelve el navegador.
        if tipo == "notebooklm":
            return {**entry, "tipo": tipo, "estado": "requiere_navegador",
                    "detalle": "NotebookLM necesita sesión: verificar en el pase navegador."}
        # YouTube tira 429 si lo pedís en masa. oembed es liviano y honesto, PERO solo
        # entiende watch?v=: hay que normalizar el ID (un /embed/ válido le da 404).
        if tipo == "youtube":
            m = _RE_YT_ID.search(url)
            if not m:
                return {**entry, "tipo": tipo, "estado": "no_concluyente",
                        "detalle": "No pude extraer el ID del video de YouTube."}
            canonica = f"https://www.youtube.com/watch?v={m.group(1)}"
            oe = "https://www.youtube.com/oembed?format=json&url=" + quote(canonica, safe="")
            async with sem:
                r = await _get(url=oe)
            if r is None:
                return {**entry, "tipo": tipo, "estado": "no_concluyente",
                        "detalle": "No pude consultar oembed de YouTube."}
            if r.status_code == 404:
                return {**entry, "tipo": tipo, "estado": "roto", "detalle": "Video de YouTube borrado (oembed 404)."}
            if r.status_code == 200:
                return {**entry, "tipo": tipo, "estado": "ok", "detalle": "Video de YouTube existe (oembed)."}
            return {**entry, "tipo": tipo, "estado": "no_concluyente",
                    "detalle": f"oembed HTTP {r.status_code} (¿rate-limit?)."}
        # Resto (Colab, Docs, genially, otros): GET conservador.
        async with sem:
            r = await _get(url=url)
        if r is None:
            return {**entry, "tipo": tipo, "estado": "no_concluyente", "detalle": "No pude conectar."}
        destino = str(r.url).lower()
        if "accounts.google.com" in _host(destino) or "servicelogin" in destino:
            return {**entry, "tipo": tipo, "estado": "pide_login",
                    "detalle": "Redirige a login: el recurso NO está compartido en abierto."}
        # Solo un 404/410/451 es inequívocamente ROTO. 429/403/5xx = anti-bot/rate-limit,
        # no concluyente: no lo cuento como link caído para no inflar el informe.
        if r.status_code in (404, 410, 451):
            return {**entry, "tipo": tipo, "estado": "roto", "detalle": f"HTTP {r.status_code}."}
        if r.status_code >= 400:
            return {**entry, "tipo": tipo, "estado": "no_concluyente",
                    "detalle": f"HTTP {r.status_code} (posible bloqueo anti-bot)."}
        return {**entry, "tipo": tipo, "estado": "ok", "detalle": f"HTTP {r.status_code}."}

    resultados = await asyncio.gather(*[_test(e) for e in urls.values()])
    # Los OK y los internos primero abajo; lo problemático arriba (para el informe).
    orden = {"roto": 0, "pide_login": 1, "otro_campus": 2, "href_con_espacio": 3,
             "requiere_navegador": 4, "no_concluyente": 5, "no_testeable": 6,
             "ok": 7, "interno_no_testeado": 8}
    resultados.sort(key=lambda x: orden.get(x["estado"], 9))
    return list(resultados)


# ---------------------------------------------------------------------------
# Matriz de unidades: presencia (3) / ausencia (0) / sin dato (None)
# ---------------------------------------------------------------------------

def _agrupar_unidades(secciones: list[dict]) -> list[list[dict]]:
    """Agrupa las secciones en unidades: cada bloque arranca en una cabecera numerada
    ('1- …') y se lleva las secciones hijas siguientes (Actividades, Práctica,
    Microteaching, Autoevaluación, Encuesta) hasta la próxima cabecera o hasta una
    sección de cierre (Integrador, Evaluaciones, Gestión)."""
    bloques: list[list[dict]] = []
    actual: list[dict] | None = None
    for sec in secciones:
        nombre = sec.get("nombre") or ""
        if _RE_CABECERA_UNIDAD.match(nombre):
            actual = [sec]
            bloques.append(actual)
        elif _RE_FIN_UNIDADES.search(nombre):
            actual = None  # a partir de acá ya no es unidad temática
        elif actual is not None:
            actual.append(sec)  # sección hija de la unidad en curso
    return bloques


def _evaluar_unidad(bloque: list[dict]) -> dict:
    """Los 9 componentes de una unidad, evaluados sobre TODAS sus secciones (cabecera +
    hijas). El quiz se clasifica por la sub-sección donde vive (Actividades → mini,
    Autoevaluación → autoeval), no por su nombre: es la estructura real del aula."""
    cabecera = bloque[0]
    # (nombre_subseccion_normalizado, modulo) para saber de qué sub-sección viene cada uno.
    mods = [(_norm(s.get("nombre", "")), m) for s in bloque for m in s.get("modulos", [])]
    modnames = {m.get("modname") for _, m in mods}
    links_tipos = {_clasificar_link(u) for s in bloque for u in s.get("links", [])}

    def quiz_en(sub_re) -> bool:
        return any(m.get("modname") == "quiz" and sub_re.search(sname) for sname, m in mods)

    cab_mods = {m.get("modname") for m in cabecera.get("modulos", [])}
    celdas = {
        # Intro: la cabecera trae su banner (label/page) o un resumen de sección.
        "Introducción y portada": _pa(
            cabecera.get("resumen_len", 0) > 0 or bool(cab_mods & {"label", "page"})),
        "PDF de lectura": _pa("resource" in modnames),
        "Ejemplos de código (Colab)": _pa("colab" in links_tipos),
        "NotebookLM del tema": _pa("notebooklm" in links_tipos),
        # Presencia sí; la CANTIDAD de preguntas la deja el humano (la API mobile no la da).
        "Mini cuestionarios (4 preg.)": _pa(quiz_en(_RE_SUB_ACTIVIDADES)),
        "Práctica (guía de ejercicios)": _pa(
            "assign" in modnames or any(_RE_SUB_PRACTICA.search(sn) for sn, _ in mods)),
        "Autoevaluación (10 preg.)": _pa(quiz_en(_RE_SUB_AUTOEVAL)),
        "Encuesta de cierre": _pa("feedback" in modnames),
        "Foro de consultas": _pa("forum" in modnames),
    }
    return {"unidad": cabecera.get("nombre"), "seccion": cabecera.get("seccion"),
            "celdas": celdas}


def construir_matriz(inventario: dict) -> dict:
    """Matriz de unidades × 9 componentes. Cada celda: 3 (presente), 0 (ausente) o None
    (no verificable sin leer contenido). Los nombres de unidad salen del PROPIO aula, no
    se hardcodean: una planilla vale para Prog 1/2/3 aunque cambien los temas."""
    bloques = _agrupar_unidades(inventario.get("secciones", []))
    unidades = [_evaluar_unidad(b) for b in bloques]
    return {"unidades": unidades, "componentes": COMPONENTES}


def listar_unidades(inventario: dict) -> list[dict]:
    """Nombre y número de cada unidad del aula (para que el tutor elija cuál auditar)."""
    out = []
    for b in _agrupar_unidades(inventario.get("secciones", [])):
        nombre = b[0].get("nombre") or ""
        m = re.match(r"^\s*(\d+)", nombre)
        out.append({"numero": int(m.group(1)) if m else None, "nombre": nombre})
    return out


def _inventario_de_unidad(inventario: dict, unidad: int) -> dict | None:
    """Recorta el inventario a una sola unidad (su cabecera + secciones hijas). Devuelve
    None si esa unidad no existe. Así el tutor audita SU unidad, no las 10, y testea solo
    los links de esa unidad."""
    for b in _agrupar_unidades(inventario.get("secciones", [])):
        m = re.match(r"^\s*(\d+)", b[0].get("nombre") or "")
        if m and int(m.group(1)) == unidad:
            return {**inventario, "secciones": b}
    return None


def _pa(presente: bool) -> int:
    """Presencia/ausencia → 3 (presente) o 0 (ausente). Nunca un valor de calidad."""
    return 3 if presente else 0


# ---------------------------------------------------------------------------
# Detección de hallazgos (handoff §7)
# ---------------------------------------------------------------------------

def detectar_hallazgos(inventario: dict, matriz: dict, links: list[dict]) -> list[dict]:
    """Patrones que aparecen de verdad, cada uno con hecho + evidencia + criticidad.
    Describe el HECHO y su impacto, nunca juzga a una persona (handoff §4)."""
    hallazgos = []
    unidades = matriz.get("unidades", [])
    n = len(unidades)

    # 1. Componente ausente en TODAS las unidades / hueco en un patrón repetido.
    for comp in matriz.get("componentes", []):
        vals = [u["celdas"].get(comp) for u in unidades]
        presentes = sum(1 for v in vals if v == 3)
        ausentes = sum(1 for v in vals if v == 0)
        if n and presentes == 0 and ausentes == n:
            hallazgos.append(_h("alta", "Componente faltante sistemático",
                                f"'{comp}' no aparece en ninguna de las {n} unidades.",
                                "Matriz de unidades: columna entera en 0."))
        elif n >= 3 and ausentes == 1 and presentes == n - 1:
            falta = next((u["unidad"] for u in unidades if u["celdas"].get(comp) == 0), "?")
            hallazgos.append(_h("media", "Hueco en un patrón repetido",
                                f"'{comp}' está en {presentes} de {n} unidades; falta en «{falta}».",
                                "Un hueco en un patrón repetido suele ser un olvido."))

    # 2. Instancias extraordinarias VISIBLES al alumno (fuga de examen). Se exige que la
    # SECCIÓN también esté visible: un módulo visible dentro de una sección oculta no lo
    # ve el alumno. Se enmarca como "verificar" — describe el hecho, no acusa (handoff §4).
    for sec in inventario.get("secciones", []):
        if not sec.get("visible"):
            continue
        for mod in sec.get("modulos", []):
            if mod.get("visible") and _RE_EXTRAORDINARIA.search(mod.get("nombre", "")):
                hallazgos.append(_h("alta", "Instancia extraordinaria posiblemente visible al alumno",
                                    f"«{mod.get('nombre')}» tiene visible=1 en la sección «{sec.get('nombre')}».",
                                    "Verificar como estudiante: si se ve, es fuga de examen; "
                                    "debería estar oculta o con restricción de acceso."))

    # 3. Fechas de ciclos previos conviviendo con el semestre vigente.
    anio = datetime.date.today().year
    viejas = {str(a) for a in range(anio - 4, anio)}
    for sec in inventario.get("secciones", []):
        for a in viejas:
            if a in (sec.get("nombre") or ""):
                hallazgos.append(_h("media", "Referencia a un ciclo previo",
                                    f"«{sec.get('nombre')}» menciona el año {a}.",
                                    "Puede ser material del semestre anterior sin actualizar."))
                break

    # 4. Problemas de links, AGRUPADOS por tipo (un hallazgo por clase, no uno por link:
    # 20 videos rotos son 1 problema con 20 casos, no 20 problemas).
    graves = {"roto": ("alta", "Links rotos (404)"),
              "pide_login": ("media", "Recursos externos que piden login"),
              "otro_campus": ("media", "Links a otro campus"),
              "href_con_espacio": ("media", "Hrefs con espacio al final (se rompen al clic)")}
    por_estado: dict[str, list] = {}
    for l in links:
        if l["estado"] in graves:
            por_estado.setdefault(l["estado"], []).append(l)
    for estado, ls in por_estado.items():
        crit, titulo = graves[estado]
        ejemplos = "; ".join(l["url"][:70] for l in ls[:4])
        mas = f" (+{len(ls) - 4} más)" if len(ls) > 4 else ""
        hallazgos.append(_h(crit, f"{titulo} — {len(ls)}",
                            f"{len(ls)} caso(s). {ls[0]['detalle']} Ej.: {ejemplos}{mas}",
                            "Ver la tabla de links del worksheet para el detalle completo."))

    orden = {"alta": 0, "media": 1, "baja": 2}
    hallazgos.sort(key=lambda h: orden.get(h["criticidad"], 3))
    return hallazgos


def _h(criticidad: str, titulo: str, hecho: str, evidencia: str) -> dict:
    return {"criticidad": criticidad, "titulo": titulo, "hecho": hecho, "evidencia": evidencia}


# ---------------------------------------------------------------------------
# Insumos para el pase con navegador (Playwright)
# ---------------------------------------------------------------------------

# Cantidad de preguntas que pide la planilla por tipo de cuestionario.
PREG_ESPERADAS = {"mini": 4, "autoeval": 10}


def quizzes_clasificados(inventario: dict) -> list[dict]:
    """Cada quiz del aula con su clase según la sub-sección donde vive: 'mini'
    (Actividades → esperado 4 preg.), 'autoeval' (Autoevaluación → 10) u 'otro'."""
    out = []
    for bloque in _agrupar_unidades(inventario.get("secciones", [])):
        unidad = bloque[0].get("nombre")
        for s in bloque:
            sn = _norm(s.get("nombre", ""))
            clase = ("mini" if _RE_SUB_ACTIVIDADES.search(sn)
                     else "autoeval" if _RE_SUB_AUTOEVAL.search(sn) else "otro")
            for m in s.get("modulos", []):
                if m.get("modname") == "quiz":
                    out.append({"cmid": m.get("cmid"), "clase": clase,
                                "nombre": m.get("nombre"), "unidad": unidad})
    return out


def apps_google_urls(inventario: dict) -> list[str]:
    """URLs únicas de NotebookLM/Colab del aula, para que el navegador intente abrirlas.

    Ojo: el pase distingue "abre en abierto" de "no se pudo verificar", NO de "es privada":
    sin sesión, un recurso vivo y uno borrado se ven igual."""
    urls, vistos = [], set()
    for s in inventario.get("secciones", []):
        for u in s.get("links", []):
            if _clasificar_link(u) in ("notebooklm", "colab") and u not in vistos:
                vistos.add(u)
                urls.append(u)
    return urls


def hallazgos_navegador(quizzes: list[dict], nav: dict | None) -> list[dict]:
    """Hallazgos que solo salen del pase navegador: cuestionarios con cantidad de preguntas
    distinta a la que pide la planilla, y Colab que piden login (posible no-compartido)."""
    if not nav or nav.get("omitido") or not nav.get("login_ok"):
        return []
    preg = nav.get("preguntas", {})
    disc: dict[str, list] = {"mini": [], "autoeval": []}
    for q in quizzes:
        n = preg.get(q["cmid"])
        esperado = PREG_ESPERADAS.get(q["clase"])
        if n is not None and esperado is not None and n != esperado:
            disc[q["clase"]].append((q["unidad"], n))
    etiqueta = {"mini": "Mini cuestionarios", "autoeval": "Autoevaluaciones"}
    h = []
    for clase, casos in disc.items():
        if casos:
            ej = "; ".join(f"{u}: {n} preg." for u, n in casos[:5])
            h.append(_h("media",
                        f"{etiqueta[clase]} con ≠ {PREG_ESPERADAS[clase]} preguntas — {len(casos)}",
                        f"{len(casos)} cuestionario(s) no tienen {PREG_ESPERADAS[clase]} preguntas. Ej.: {ej}",
                        "Contado con navegador en la edición del cuestionario."))
    colab_login = [a["url"] for a in nav.get("apps", [])
                   if a["estado"] == "no_verificable" and _clasificar_link(a["url"]) == "colab"]
    if colab_login:
        h.append(_h("media", f"Colab que no se pudieron verificar (caen en login) — {len(colab_login)}",
                    f"{len(colab_login)} Colab no abren sin login (¿no compartidos con los alumnos?).",
                    "Verificado con navegador sin sesión Google."))
    return h


# ---------------------------------------------------------------------------
# Worksheet .md (el BORRADOR — handoff §3)
# ---------------------------------------------------------------------------

_ICONO = {"alta": "🔴", "media": "🟡", "baja": "🟢"}


def worksheet_md(inventario: dict, matriz: dict, links: list[dict], hallazgos: list[dict],
                 materia: str, evaluador: str, rol: str,
                 nav: dict | None = None, quizzes: list[dict] | None = None) -> str:
    hoy = datetime.date.today().isoformat()
    L = []
    L.append(f"# Auditoría de aula virtual — {materia}\n")
    L.append("> **BORRADOR basado en evidencia — NO es el veredicto del evaluador.**")
    L.append("> Un agente verifica presencia/ausencia/consistencia; la calidad y los puntajes")
    L.append("> finos los ajusta la persona que firma. `0`=ausente · `3`=presente · vacío=sin dato.\n")

    # Portada
    L.append("## 0 · Portada\n")
    L.append(f"- **Evaluador:** {evaluador or '(anónimo — Celda de Control de Calidad)'}")
    L.append(f"- **Rol:** {rol or '—'}")
    L.append(f"- **Aula auditada:** {materia} (course_id {inventario.get('course_id')})")
    L.append(f"- **Fecha:** {hoy}")
    L.append(f"- **Secciones relevadas:** {len(inventario.get('secciones', []))}\n")

    # Matriz de unidades
    L.append("## 2 · Unidades (presencia/ausencia)\n")
    comps = matriz.get("componentes", [])
    cortos = ["Intro", "PDF", "Colab", "NotebookLM", "MiniQuiz", "Práctica",
              "Autoeval", "Encuesta", "Foro"]
    L.append("| Unidad | " + " | ".join(cortos) + " |")
    L.append("|" + "---|" * (len(cortos) + 1))
    for u in matriz.get("unidades", []):
        fila = [u["unidad"] or f"Sección {u['seccion']}"]
        for c in comps:
            v = u["celdas"].get(c)
            fila.append("" if v is None else str(v))
        L.append("| " + " | ".join(fila) + " |")
    L.append("\n_Vacío = no verificable sin leer el contenido (p. ej. cantidad de preguntas"
             " del cuestionario: la API mobile no la expone sin iniciar un intento)._\n")

    # Cuestionarios: cantidad de preguntas (solo si corrió el pase navegador).
    nav_ok = bool(nav and nav.get("login_ok") and not nav.get("omitido"))
    if nav_ok and quizzes:
        preg = nav.get("preguntas", {})
        con_esperado = [q for q in quizzes if q["clase"] in PREG_ESPERADAS]
        if con_esperado:
            L.append("## Cuestionarios — cantidad de preguntas (pase navegador)\n")
            L.append("| Unidad | Tipo | Preguntas | Esperado | ¿OK? |")
            L.append("|---|---|---|---|---|")
            for q in con_esperado:
                n = preg.get(q["cmid"])
                exp = PREG_ESPERADAS.get(q["clase"])
                tipo = "Mini" if q["clase"] == "mini" else "Autoeval"
                ok = "—" if n is None else ("✅" if n == exp else "⚠️")
                L.append(f"| {q['unidad']} | {tipo} | {'—' if n is None else n} | {exp} | {ok} |")
            L.append("")

    # Hoja 1 — Aula Virtual (solo lo verificable; el resto explícitamente a juicio del tutor)
    rotos = [l for l in links if l["estado"] in ("roto", "pide_login", "otro_campus", "href_con_espacio")]
    L.append("## 1 · Aula Virtual (aspectos transversales)\n")
    L.append("| # | Aspecto | Puntaje | Nota |")
    L.append("|---|---|---|---|")
    navegador_n = sum(1 for l in links if l["estado"] in ("requiere_navegador", "no_concluyente"))
    if rotos:
        estado_links, nota_links = "0", f"{len(rotos)} link(s) con problemas — ver §Links."
    elif navegador_n:
        estado_links, nota_links = "", (f"0 rotos confirmados; {navegador_n} apps Google "
                                        "(NotebookLM/Colab) pendientes del pase navegador.")
    else:
        estado_links, nota_links = "3", "Sin links rotos en lo testeado."
    L.append(f"| 10 | Funcionamiento de los enlaces (sin links rotos) | {estado_links} | {nota_links} |")
    L.append("| 1-9,11-14 | (estructura, diseño, cronograma, accesibilidad…) |  | → juicio del tutor: requieren leer/mirar, no inventariar |\n")

    # Hoja 4 — Equipo: VACÍA a propósito
    L.append("## 4 · Equipo de trabajo\n")
    L.append("> 🚫 **Se deja VACÍA a propósito.** Un agente no evalúa el desempeño de personas."
             " No hay dato objetivo del que extraerlo. Lo completa el evaluador humano.\n")

    # Links
    L.append("## Links — detalle del testeo\n")
    ok = sum(1 for l in links if l["estado"] == "ok")
    interno = sum(1 for l in links if l["estado"] == "interno_no_testeado")
    navegador = sum(1 for l in links if l["estado"] in ("requiere_navegador", "no_concluyente"))
    L.append(f"Testeados OK: **{ok}** · internos (no pedidos): {interno} · "
             f"para el pase navegador: {navegador} · con problema real: **{len(rotos)}**\n")
    if rotos:
        L.append("| Estado | Detalle | URL |")
        L.append("|---|---|---|")
        for l in rotos:
            L.append(f"| {l['estado']} | {l['detalle']} | {l['url'][:100]} |")
        L.append("")

    # Hallazgos
    L.append("## Hallazgos priorizados\n")
    if not hallazgos:
        L.append("_Sin hallazgos automáticos. (No significa 'todo bien': significa que los"
                 " patrones auto-detectables no dispararon.)_\n")
    else:
        for h in hallazgos:
            L.append(f"- {_ICONO.get(h['criticidad'], '·')} **{h['titulo']}** — {h['hecho']}  ")
            L.append(f"  _Evidencia:_ {h['evidencia']}")
        L.append("")

    # Datos sin verificar — la sección que da credibilidad al resto (handoff §6)
    L.append("## ⭐ Datos sin verificar (requieren ojo humano o pase navegador)\n")
    if nav_ok:
        apps = nav.get("apps", [])
        abren = sum(1 for a in apps if a["estado"] == "abre")
        piden = sum(1 for a in apps if a["estado"] == "no_verificable")
        nc = sum(1 for a in apps if a["estado"] == "no_concluyente")
        L.append(f"- **Apps Google (pase navegador):** {abren} abren en abierto · "
                 f"**{piden} NO verificables** · {nc} no concluyentes.")
        if piden:
            L.append(f"  - ⚠️ Esas {piden} caen en la pantalla de login de Google. Eso **no** "
                     "significa que existan y sean privadas: un enlace borrado devuelve la "
                     "misma pantalla (comprobado con un UUID inventado). **No las puntúes "
                     "como presentes**: van a «a revisar» hasta abrirlas con la cuenta dueña.")
    else:
        navegador = [l for l in links if l["estado"] in ("requiere_navegador", "no_concluyente")]
        L.append(f"- **{len(navegador)} links de apps Google** (NotebookLM/Colab/YouTube): un GET sin sesión "
                 "no concluye → corré el pase navegador (`con_navegador=true`).")
        L.append("- **Cantidad de preguntas** de cuestionarios: sin el pase navegador no se cuenta.")
    L.append("- **Calidad** de cualquier componente (diseño, claridad, dificultad): no se puntúa automático.")
    L.append("- **Contenido** de los PDF/consignas (que abran ≠ que estén actualizados y correctos).")
    L.append("- **Hoja Evaluaciones y Equipo:** juicio del evaluador.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Orquestador
# ---------------------------------------------------------------------------

async def auditar_aula(client, course_id: int, salidas_dir: str, materia: str = "",
                       evaluador: str = "", rol: str = "", con_navegador: bool = False,
                       unidad: int | None = None, tenant_id: str | None = None) -> dict:
    """Pipeline completo: relevar → testear links → matriz → hallazgos → (opcional) pase
    navegador → escribir el worksheet .md. READ-ONLY sobre Moodle. Con `con_navegador=True`
    suma el paso 2 (Playwright): cuenta preguntas de cuestionarios y clasifica las apps
    Google. Con `unidad=N` audita SOLO esa unidad (más rápido: testea solo sus links). Sin
    `unidad`, el aula entera. `tenant_id` (opcional) scopea la sesión de navegador
    (cookies/storage) a ese campus — sin él, cae al tenant activo de `almacen`. Devuelve
    resumen + ruta del worksheet, o {error} si falla."""
    import os

    inv = await relevar_aula(client, course_id)
    if inv.get("error"):
        return inv
    # Recortar a una unidad si se pidió (el tutor audita SU unidad, no las 10).
    unidad_nombre = ""
    if unidad is not None:
        reducido = _inventario_de_unidad(inv, unidad)
        if reducido is None:
            return {"error": f"No encontré la unidad {unidad} en el aula.",
                    "unidades_disponibles": listar_unidades(inv)}
        inv = reducido
        unidad_nombre = inv["secciones"][0].get("nombre") or f"Unidad {unidad}"
    base_url = getattr(client, "_base", "https://tup.sied.utn.edu.ar")
    links = await testear_links(client, inv, base_url)
    matriz = construir_matriz(inv)
    hallazgos = detectar_hallazgos(inv, matriz, links)

    nav, quizzes = None, None
    if con_navegador:
        from . import navegador
        quizzes = quizzes_clasificados(inv)
        nav = await navegador.pase_navegador(
            base_url, getattr(client, "_dni", ""), getattr(client, "_password", ""),
            quizzes, apps_google_urls(inv), tenant_id=tenant_id)
        extra = hallazgos_navegador(quizzes, nav)
        if extra:
            orden = {"alta": 0, "media": 1, "baja": 2}
            hallazgos = sorted(hallazgos + extra, key=lambda h: orden.get(h["criticidad"], 3))

    nombre_materia = materia or f"course_{course_id}"
    titulo = f"{nombre_materia} · {unidad_nombre}" if unidad is not None else nombre_materia
    md = worksheet_md(inv, matriz, links, hallazgos, titulo, evaluador, rol,
                      nav=nav, quizzes=quizzes)
    os.makedirs(salidas_dir, exist_ok=True)
    slug = _norm(nombre_materia).replace(" ", "-")[:40] or f"course-{course_id}"
    if unidad is not None:
        slug += f"-u{unidad}"
    fecha = datetime.date.today().isoformat()
    ruta = os.path.join(salidas_dir, f"auditoria-{slug}-{fecha}.md")
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write(md)

    rotos = [l for l in links if l["estado"] in
             ("roto", "pide_login", "otro_campus", "href_con_espacio")]
    resumen = {
        "secciones": len(inv.get("secciones", [])),
        "unidades": len(matriz.get("unidades", [])),
        "links_ok": sum(1 for l in links if l["estado"] == "ok"),
        "links_para_navegador": sum(1 for l in links if l["estado"] in
                                    ("requiere_navegador", "no_concluyente")),
        "links_con_problema": len(rotos),
        "hallazgos": len(hallazgos),
        "hallazgos_alta": sum(1 for h in hallazgos if h["criticidad"] == "alta"),
    }
    if nav is not None:
        if nav.get("omitido") or not nav.get("login_ok"):
            resumen["navegador"] = nav.get("aviso", "El pase navegador no corrió.")
        else:
            preg = nav.get("preguntas", {})
            apps = nav.get("apps", [])
            sin_verificar = sum(1 for a in apps if a["estado"] == "no_verificable")
            resumen["navegador"] = {
                "cuestionarios_contados": sum(1 for v in preg.values() if v is not None),
                "apps_abren": sum(1 for a in apps if a["estado"] == "abre"),
                # No se llama "piden_cuenta": caer en el login de Google NO prueba que el
                # recurso exista. Un enlace borrado devuelve la misma pantalla.
                "apps_no_verificables": sin_verificar,
            }
            if sin_verificar:
                resumen["navegador"]["aviso"] = (
                    f"{sin_verificar} app(s) Google caen en login y NO se pudieron verificar: "
                    "no las cuentes como presentes."
                )
    return {
        "ok": True,
        "course_id": course_id,
        "worksheet": ruta,
        "resumen": resumen,
        "aviso": "BORRADOR basado en evidencia (read-only). Revisá el worksheet .md y "
                 "completá los puntajes de calidad a mano antes de volcarlo a la planilla oficial.",
    }
