"""Integración con la API de Active-IA (corrección automática con Gemini) para la Skill.

Portado del copiloto (`backend/moodle/active_ia.py`), con dos desacoples respecto del
original:

1. **Sin `config.py`**: las credenciales salen de ENV VARS (`ACTIVEIA_URL`,
   `ACTIVEIA_USER`, `ACTIVEIA_PASS`), igual que el resto de la Skill lee su config del
   entorno. NO hace falta `GEMINI_API_KEY`: la corrección la corre Active-IA del lado del
   servidor (nosotros solo disparamos y consultamos), así que Gemini nunca se toca por
   request desde acá.

2. **Sin browser / sin `parsers`**: para bajar el archivo del alumno de Moodle se usa el
   `MobileWSClient` de la Skill por API REST (mismo patrón que `ws_api.bajar_entrega`:
   `mod_assign_get_submissions` + `token_download`), en vez del `find_zip_url` +
   `client_moodle.download` que scrapeaba el HTML de calificación en el copiloto. El
   nombre del alumno lo da `core_user_get_users_by_field` (fullname), sin parsear HTML.

Autocontenido, mismo contrato que el resto del paquete: cada rama de error devuelve un
dict con "error"; NUNCA propaga una excepción hacia afuera. El flujo completo
(`corregir_con_active_ia`) baja el archivo del alumno, lo sube a Active-IA, dispara la
corrección, hace polling hasta que Gemini termina y, al final, DESCARGA LOCAL el PDF de
devolución.

Auth: JWT Bearer. El token se cachea en memoria y se re-loguea automáticamente ante un
401. Depende de `httpx` (ya está en requirements) + stdlib. La corrección la hace Gemini
del lado de Active-IA: es async y a veces DA TIMEOUT del servicio de IA (caso real y
frecuente); se maneja como error recuperable, no como excepción."""

import asyncio
import json
import os
from pathlib import Path

import httpx

from . import ws_api
from . import almacen
from .cliente import MobileWSClient

# ---------- Constantes ----------

# Estados que devuelve Active-IA para una entrega (confirmados por GET /entregas).
_ESTADO_OK = "CORREGIDA"
_ESTADO_ERROR = "ERROR"
# Cada cuántos segundos preguntamos si ya está corregida.
_POLL_INTERVAL_S = 5.0
# Timeout de red de las requests simples (login, GET). El poll tiene su propio reloj.
# 90 s y no 30: GET /pendientes/moodle medido tres veces tardó 25,1 / 40,1 / 23,7 s, así
# que con 30 fallaba ~1 de cada 3 llamadas — y encima sin decir por qué (ver _detalle_error).
_HTTP_TIMEOUT_S = 90.0
# Timeout de la descarga del PDF de devolución (puede ser un archivo grande).
_PDF_TIMEOUT_S = 60.0


def _detalle_error(e: BaseException) -> str:
    """Descripción de una excepción que NUNCA queda vacía.

    Varias excepciones de httpx traen `str(e) == ""` (verificado con ReadTimeout), así que
    el patrón `f"...: {e}"` producía mensajes cortados tipo "No pude consultar X: " y nada
    más. El tipo solo ya dice bastante ("ReadTimeout" = se pasó del timeout)."""
    txt = str(e).strip()
    return f"{type(e).__name__}: {txt}" if txt else type(e).__name__


def _normalizar(texto: str) -> str:
    """Título normalizado para comparar (minúsculas, sin espacios de más)."""
    return " ".join(str(texto).lower().split())


# ---------- Cliente HTTP con auth cacheada ----------

class ActiveIAClient:
    """Cliente async de la API de Active-IA con token JWT cacheado en memoria.

    Re-loguea solo ante un 401 (token vencido). No abre un `AsyncClient`
    persistente: cada request usa uno efímero (`async with`), como el `MobileWSClient`
    de la Skill, para no arrastrar estado de conexión entre consultas del MCP."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None

    async def _login(self) -> None:
        """POST /auth/login → guarda el access_token en memoria. Lanza en caso de fallo
        (lo captura `request`, que lo convierte en dict de error)."""
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as http:
            resp = await http.post(
                f"{self._base}/auth/login",
                json={"username": self._username, "password": self._password},
            )
            resp.raise_for_status()
            self._token = resp.json()["access_token"]

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def request(
        self, method: str, path: str, *, timeout: float = _HTTP_TIMEOUT_S, **kwargs
    ) -> httpx.Response:
        """Hace una request autenticada. Si no hay token, se loguea primero. Ante un
        401, re-loguea UNA vez y reintenta. Devuelve la Response cruda (el caller
        decide qué hacer con el status)."""
        if self._token is None:
            await self._login()
        url = f"{self._base}{path}"
        # follow_redirects=True: la API redirige 307 sin trailing slash y httpx, por
        # default, NO sigue el redirect (y en un POST perdería el body). Con esto lo
        # sigue preservando método y cuerpo.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
            resp = await http.request(method, url, headers=self._auth_headers(), **kwargs)
            if resp.status_code == 401:
                # Token vencido: re-login y un único reintento.
                await self._login()
                resp = await http.request(method, url, headers=self._auth_headers(), **kwargs)
            return resp


# Singleton del módulo, configurado desde ENV VARS (no `config.py`, a diferencia del
# copiloto). El default de la URL es el mismo que traía el copiloto en su config.
def _default_client() -> ActiveIAClient:
    return ActiveIAClient(
        base_url=os.environ.get("ACTIVEIA_URL", "https://api.active-ia.com/api/v1"),
        username=os.environ.get("ACTIVEIA_USER", ""),
        password=os.environ.get("ACTIVEIA_PASS", ""),
    )


_client: ActiveIAClient | None = None


def _get_client() -> ActiveIAClient:
    global _client
    if _client is None:
        _client = _default_client()
    return _client


# ---------- FUNCIÓN 1: pendientes / mapa Moodle↔Active-IA ----------

async def activeia_pendientes() -> dict:
    """GET /pendientes/moodle, devuelto LIMPIO para que el agente resuelva
    `comision_id`/`rubrica_id` a partir del `cmid` (assign_id) + `groupId` de Moodle.

    Forma real de la API: `{materias:[{id,nombre,unidades:[{cmid,titulo,subtitulo,
    comisiones:[{id,nombre,codigo,groupId,moodleGraderUrl,...}]}]}]}`. Devolvemos una
    versión aplanada por unidad, con la rúbrica inferida (por título) cuando se puede.

    ⚠️ LOS CONTADORES SON DE MOODLE, NO DE ACTIVE-IA. `espera` / `corregidos` /
    `sin_entrega` cuentan el estado de calificación **en el campus**: `corregidos: 0`
    significa "ninguna tiene nota cargada en Moodle", NO "Active-IA no corrigió nada".
    Los dos números difieren siempre, porque `corregir_con_active_ia` deja la corrección
    en Active-IA y NO escribe la nota (eso es `cargar_nota`, aparte). El 2026-08-04 esto
    hizo concluir que no se había corregido nada cuando ya había dos correcciones hechas.
    Para ver lo que Active-IA realmente corrigió, usá `activeia_correcciones`."""
    try:
        resp = await _get_client().request("GET", "/pendientes/moodle")
    except httpx.HTTPError as e:
        return {"error": f"No pude consultar /pendientes/moodle: {_detalle_error(e)}"}
    if resp.status_code != 200:
        return {"error": f"/pendientes/moodle devolvió {resp.status_code}", "body": resp.text[:300]}

    data = resp.json()
    materias_out: list[dict] = []
    for materia in data.get("materias", []):
        materia_id = materia.get("id")
        # Rúbricas de la materia una sola vez (para inferir rubrica_id por título).
        rubricas = await _rubricas_de_materia(materia_id)
        unidades_out: list[dict] = []
        for unidad in materia.get("unidades", []):
            titulo = unidad.get("titulo", "")
            rub = _match_rubrica(titulo, rubricas)
            unidades_out.append(
                {
                    "cmid": unidad.get("cmid"),  # = assign_id de Moodle
                    "titulo": titulo,
                    "subtitulo": unidad.get("subtitulo"),
                    "espera": unidad.get("espera"),
                    "corregidos": unidad.get("corregidos"),
                    "sin_entrega": unidad.get("sinEntrega"),
                    "rubrica_id": rub.get("id") if rub else None,
                    "comisiones": [
                        {
                            "comision_id": c.get("id"),
                            "nombre": c.get("nombre"),
                            "codigo": c.get("codigo"),
                            "group_id": c.get("groupId"),  # = group de Moodle
                            "espera": c.get("espera"),
                            "moodle_grader_url": c.get("moodleGraderUrl"),
                        }
                        for c in unidad.get("comisiones", [])
                    ],
                }
            )
        materias_out.append(
            {"materia_id": materia_id, "nombre": materia.get("nombre"), "unidades": unidades_out}
        )
    if not materias_out:
        # Vacío NO es "estás al día": puede ser que tus comisiones no estén cargadas en
        # Active-IA (p. ej. al cambiar de cuatrimestre, las del cuatrimestre viejo salen
        # de su config). Devolver [] a secas se lee como "no hay nada pendiente" y deja al
        # tutor tranquilo cuando en realidad hay que actuar. Preguntamos qué comisiones sí
        # tiene configuradas para poder decir CUÁL de las dos cosas es.
        conocidas: list[str] = []
        detalle_error = None
        try:
            r = await _get_client().request("GET", "/comisiones/")
            if r.status_code == 200:
                conocidas = [c.get("nombre") for c in (r.json() or {}).get("items", [])
                             if c.get("nombre")]
        except httpx.HTTPError as e:
            detalle_error = _detalle_error(e)
        salida = {"ok": True, "materias": [], "sin_pendientes": True,
                  "comisiones_en_active_ia": conocidas}
        if detalle_error:
            salida["aviso"] = ("Active-IA no devolvió pendientes y tampoco pude listar tus "
                               f"comisiones ({detalle_error}), así que NO sé si estás al día "
                               "o si no tenés comisiones cargadas ahí.")
        elif conocidas:
            salida["aviso"] = (
                "Active-IA no devolvió pendientes. Las comisiones que tiene cargadas para vos "
                f"son: {', '.join(conocidas)}. Si la comisión que buscás no está en esa lista, "
                "NO es que esté al día: es que no está configurada en Active-IA, y ahí hay que "
                "corregir a mano o pedir que la den de alta."
            )
        else:
            salida["aviso"] = ("Active-IA no tiene NINGUNA comisión cargada para tu usuario. "
                               "Esto no es 'estás al día': no hay nada configurado para corregir "
                               "por ahí. Corregí a mano o pedí el alta de tus comisiones.")
        return salida
    return {"ok": True, "materias": materias_out}


async def _rubricas_de_materia(materia_id: int | None) -> list[dict]:
    """Listado de rúbricas de una materia (GET /rubricas/?materia_id=...). Devuelve []
    ante cualquier problema — es un helper de inferencia, no debe romper el flujo."""
    if materia_id is None:
        return []
    try:
        resp = await _get_client().request(
            "GET", "/rubricas/", params={"materia_id": materia_id, "per_page": 100}
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("items", [])
    except httpx.HTTPError:
        return []


def _match_rubrica(unidad_titulo: str, rubricas: list[dict]) -> dict | None:
    """Cruza el título de la unidad de Moodle con el `titulo` de una rúbrica. La API usa
    el MISMO texto en ambos lados (confirmado), así que un match exacto normalizado es
    fiable; si hay varias con el mismo título (ej. parcial + su recuperatorio) devuelve
    la primera y el caller debería desambiguar."""
    objetivo = _normalizar(unidad_titulo)
    if not objetivo:
        return None
    for rub in rubricas:
        if _normalizar(rub.get("titulo", "")) == objetivo:
            return rub
    return None


# ---------- Mapa local de rúbricas (respaldo del resolver) ----------

_RUTA_MAPA = Path.home() / ".moodle-skill" / "activeia_rubricas.json"


def _mapa_local() -> dict:
    """
    Rúbricas que Active-IA tiene cargadas pero no mapeó al cmid de Moodle.

    Vive en la carpeta personal del tutor y NO en el repo, por la misma razón que
    `comisiones.json` es candidato a podarse: un catálogo copiado a mano vence sin
    avisar, y acá equivocarse no es errar un id — es corregir un TP de listas con
    la rúbrica de condicionales y ponerle ese número al legajo de alguien.

    Formato:

        {"17792": {"rubrica_id": 149, "materia_id": 19, "titulo": "Práctico 5: Listas"}}

    Un archivo ilegible se ignora: el resolver sigue funcionando sin respaldo.
    """
    if not _RUTA_MAPA.is_file():
        return {}
    try:
        datos = json.loads(_RUTA_MAPA.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {str(k): v for k, v in datos.items() if isinstance(v, dict)}


# ---------- FUNCIÓN 2: resolver comision_id + rubrica_id desde Moodle ----------

async def activeia_resolver(assign_id: int | str, group_id: int) -> dict:
    """A partir del `cmid` (assign_id) + `groupId` de Moodle devuelve
    `{comision_id, rubrica_id, unidad_titulo}` cruzando /pendientes/moodle y /rubricas.

    Si no encuentra la unidad, el grupo o la rúbrica, devuelve un dict con "error"
    claro (nunca lanza)."""
    try:
        resp = await _get_client().request("GET", "/pendientes/moodle")
    except httpx.HTTPError as e:
        return {"error": f"No pude consultar /pendientes/moodle: {_detalle_error(e)}"}
    if resp.status_code != 200:
        return {"error": f"/pendientes/moodle devolvió {resp.status_code}"}

    cmid = str(assign_id)
    data = resp.json()
    for materia in data.get("materias", []):
        for unidad in materia.get("unidades", []):
            if str(unidad.get("cmid")) != cmid:
                continue
            # Encontramos la unidad: buscamos la comisión por groupId.
            comision = next(
                (c for c in unidad.get("comisiones", []) if c.get("groupId") == group_id), None
            )
            if comision is None:
                grupos = [c.get("groupId") for c in unidad.get("comisiones", [])]
                return {
                    "error": f"El groupId {group_id} no está en la tarea {cmid}. "
                    f"Grupos disponibles: {grupos}."
                }
            # Inferimos la rúbrica por título dentro de la materia.
            rubricas = await _rubricas_de_materia(materia.get("id"))
            rub = _match_rubrica(unidad.get("titulo", ""), rubricas)
            if rub is None:
                return {
                    "error": f"No pude inferir la rúbrica para '{unidad.get('titulo')}' "
                    f"(materia {materia.get('id')}). Pasá rubrica_id a mano.",
                    "comision_id": comision.get("id"),
                    "unidad_titulo": unidad.get("titulo"),
                }
            return {
                "ok": True,
                "comision_id": comision.get("id"),
                "rubrica_id": rub.get("id"),
                "unidad_titulo": unidad.get("titulo"),
                "materia_id": materia.get("id"),
                "moodle_grader_url": comision.get("moodleGraderUrl"),
            }
    # Active-IA no mapeó este cmid. NO significa que no haya rúbrica: significa que
    # el cruce cmid -> unidad no está hecho de ese lado. Pasó con la unidad 5 de
    # Prog I, que tiene la rúbrica 149 cargada y sin embargo acá daba "no existe" —
    # y con esa respuesta se dio por hecho que había que corregir a mano.
    #
    # **Un error del resolver no es prueba de que no haya rúbrica.** Por eso hay un
    # mapa de respaldo que el tutor completa a mano, y por eso la respuesta declara
    # de dónde salió el dato.
    respaldo = _mapa_local().get(cmid)
    if respaldo and respaldo.get("rubrica_id"):
        materia_id = respaldo.get("materia_id")
        comision_id = None
        # Las comisiones son las mismas para todas las unidades de una materia, así
        # que el comision_id se resuelve EN VIVO de cualquier otra unidad. No se
        # guarda en el mapa: un id copiado a mano es un id que vence sin avisar.
        for materia in data.get("materias", []):
            if materia_id is not None and materia.get("id") != materia_id:
                continue
            for unidad in materia.get("unidades", []):
                c = next(
                    (x for x in unidad.get("comisiones", []) if x.get("groupId") == group_id),
                    None,
                )
                if c is not None:
                    comision_id = c.get("id")
                    materia_id = materia.get("id")
                    break
            if comision_id is not None:
                break

        if comision_id is None:
            return {
                "error": f"Tengo la rúbrica {respaldo['rubrica_id']} para cmid={cmid} en el "
                f"mapa local, pero no pude resolver el comision_id del grupo {group_id} "
                f"en vivo. No lo invento.",
                "fuente": "mapa_local_incompleto",
            }

        return {
            "ok": True,
            "comision_id": comision_id,
            "rubrica_id": respaldo["rubrica_id"],
            "unidad_titulo": respaldo.get("titulo") or f"(cmid {cmid}, del mapa local)",
            "materia_id": materia_id,
            "verificado": bool(respaldo.get("verificado")),
            "_meta": {
                "fuente": "mapa_local",
                # Un par sin confirmar y uno confirmado NO pueden avisar lo mismo:
                # el segundo es un dato, el primero es una deducción por número de
                # unidad, y con una rúbrica equivocada la nota no sale floja, sale
                # de otra cosa.
                "degradado": not respaldo.get("verificado"),
                "avisos": (
                    [
                        f"La rúbrica {respaldo['rubrica_id']} salió del mapa local del "
                        f"tutor, no de Active-IA ({_RUTA_MAPA}). Está marcada como "
                        f"VERIFICADA: {respaldo.get('como', 'sin detalle')}"
                    ]
                    if respaldo.get("verificado")
                    else [
                        f"⚠️ La rúbrica {respaldo['rubrica_id']} es una DEDUCCIÓN sin "
                        f"confirmar, del mapa local ({_RUTA_MAPA}). "
                        f"{respaldo.get('como', '')} "
                        "Abrí la rúbrica en Active-IA y confirmá que sea la de esta "
                        "unidad ANTES de cargar la nota: una rúbrica equivocada no da "
                        "una nota floja, corrige otra cosa."
                    ]
                ),
            },
        }

    return {
        "error": f"No encontré la tarea con cmid={cmid} en /pendientes/moodle.",
        "ojo": "Esto NO prueba que no haya rúbrica: Active-IA puede tenerla cargada y "
        "sin mapear al cmid de Moodle. Fijate en el panel de Active-IA; si existe, "
        f"anotala en {_RUTA_MAPA} y esta tool la va a encontrar sola la próxima vez.",
        "como_anotarla": '{"%s": {"rubrica_id": 149, "materia_id": 19, "titulo": "..."}}' % cmid,
    }


# ---------- Descarga del archivo del alumno de Moodle (por API REST, sin browser) ----------

async def _bajar_archivo_alumno(
    client_moodle: MobileWSClient, assign_id: str, email: str
) -> dict:
    """Baja la entrega del alumno de Moodle por API REST, reusando el MISMO patrón que
    `ws_api.bajar_entrega`: resuelve instanceid (`_instanceid`) + userid por email
    (`_userid_por_email`) → `mod_assign_get_submissions` → `token_download`. Reemplaza
    el `find_zip_url` + `client_moodle.download` (browser/scraping) del copiloto.

    Devuelve `{fname, data, alumno_nombre}` con los bytes del archivo principal, o un
    dict `{error}`. Nunca lanza (mismo contrato que el resto del módulo)."""
    # instanceid de la tarea (cmid -> assignment id): los WS de assign usan instanceid,
    # no cmid (lección aprendida del copiloto).
    try:
        inst = await ws_api._instanceid(client_moodle, assign_id)
    except Exception as e:  # noqa: BLE001 -- cualquier fallo de red/WS = dato, no excepción.
        return {"error": f"No pude resolver la tarea {assign_id}: {_detalle_error(e)}"}
    if inst is None:
        return {"error": f"No encontré la tarea con cmid={assign_id} (¿existe / es de tu curso?)."}

    # userid + fullname del alumno (el fullname nos sirve como alumno_nombre por default).
    try:
        uid, fullname = await ws_api._userid_por_email(client_moodle, email)
    except Exception as e:  # noqa: BLE001
        return {"error": f"No pude resolver al alumno {email}: {_detalle_error(e)}"}
    if uid is None:
        return {"error": f"No encontré al alumno {email} (¿entregó / está en el curso?)."}

    # Archivos entregados por ese uid (mod_assign_get_submissions).
    try:
        subs = await client_moodle.ws("mod_assign_get_submissions", {"assignmentids": [inst]})
    except Exception as e:  # noqa: BLE001
        return {"error": f"get_submissions falló para {assign_id}: {_detalle_error(e)}"}

    fileurl: str | None = None
    fname: str | None = None
    for asg in (subs or {}).get("assignments", []):
        for s in asg.get("submissions", []):
            if int(s.get("userid", -1)) != uid:
                continue
            for pl in s.get("plugins", []):
                for fa in pl.get("fileareas", []):
                    for f in fa.get("files", []):
                        if f.get("fileurl"):
                            # Nos quedamos con el primer archivo entregado (el principal):
                            # Active-IA recibe UN archivo por entrega.
                            fileurl = f["fileurl"]
                            fname = f.get("filename") or "entrega"
                            break
                    if fileurl:
                        break
                if fileurl:
                    break
            if fileurl:
                break
        if fileurl:
            break

    if not fileurl:
        return {"error": f"No encontré archivo entregado de {email} en la tarea {assign_id}."}

    # Descarga protegida (agrega el token del mobile a la pluginfile URL).
    try:
        data = await client_moodle.token_download(fileurl)
    except Exception as e:  # noqa: BLE001
        return {"error": f"No pude descargar la entrega de {email}: {_detalle_error(e)}"}

    return {"fname": fname or f"{assign_id}_{email}", "data": data, "alumno_nombre": fullname or email}


# ---------- FUNCIÓN 3: flujo completo de corrección ----------

def diagnosticar_error(err: dict, reusada: bool) -> dict:
    """Distingue los tres modos de falla de una corrección. PURA.

    `GEMINI_OVERLOADED` significa dos cosas OPUESTAS con el mismo texto, y confundirlas costó
    dos días de reintentos inútiles (informe de un tutor, 2026-08-17):

    - **Servicio saturado**: Gemini está ocupado. Se destraba solo, esperar sirve.
    - **Entrega atascada**: la entrega quedó en estado ERROR del lado de Active-IA. NO se
      destraba nunca. `corregir_con_active_ia` no crea una entrega nueva cuando se la vuelve
      a disparar: **retoma la que ya está subida**, así que cada reintento vuelve a chocar
      contra el mismo registro roto. El número de intentos no importa: no se está
      reintentando la corrección, se está reintentando el error.

    La señal que las separa es la que ya estaba en el código sin usarse: si la entrega fue
    RETOMADA (vino de un 409) y encima figura en ERROR, ese error es viejo y persistido, no
    una saturación de este momento. Medido: 8 correcciones limpias en el mismo rato en que
    4 entregas viejas seguían fallando — el servicio nunca estuvo caído.

    El tercer modo es el ZIP sobredimensionado (`NBN_TIMEOUT`): tampoco se destraba, y subir
    el timeout no lo arregla.
    """
    code = str(err.get("error_code") or "").upper()
    mensaje = str(err.get("error") or "")

    if "NBN_TIMEOUT" in code or "NBN_TIMEOUT" in mensaje.upper():
        return {
            **err,
            "diagnostico": "zip_sobredimensionado",
            "reintentar_sirve": False,
            "que_hacer": (
                "El ZIP es demasiado grande para procesar. Subir `timeout_s` NO lo arregla. "
                "Pedile al alumno que reentregue SÓLO los archivos de código (los `.java` o "
                "`.py` de `src/`), sin `build/`, `.gradle/` ni el proyecto entero."),
        }

    if reusada:
        return {
            **err,
            "diagnostico": "entrega_atascada",
            "reintentar_sirve": False,
            "que_hacer": (
                "Esta entrega ya estaba subida y quedó en ERROR del lado de Active-IA. "
                "Reintentar NO la va a arreglar: la tool retoma la entrega existente, así que "
                "cada intento vuelve a chocar contra el mismo registro roto. Hay que BORRARLA "
                "desde la aplicación de Active-IA para que el retomado deje de engancharla, o "
                "corregir ese trabajo a mano. Confirmalo con "
                "`activeia_correcciones(comision_id, solo_corregidas=False)`: si figura en "
                "ERROR, es este caso."),
        }

    return {
        **err,
        "diagnostico": "servicio_saturado",
        "reintentar_sirve": True,
        "que_hacer": (
            "Puede ser saturación del servicio, que se destraba sola. Esperá y reintentá UNA "
            "vez. **Si vuelve a fallar sobre el mismo alumno, dejá de reintentar**: ahí ya no "
            "es el servicio, es la entrega, y no se destraba nunca. Antes de redisparar corré "
            "`activeia_correcciones(comision_id, solo_corregidas=False)` — puede que ya esté "
            "corregida y sólo falte cargar la nota."),
    }


async def corregir_con_active_ia(
    client_moodle: MobileWSClient,
    assign_id: str,
    email: str,
    comision_id: int,
    rubrica_id: int,
    alumno_nombre: str | None = None,
    moodle_url: str | None = None,
    timeout_s: int = 180,
    dest_dir: str | None = None,
) -> dict:
    """FLUJO COMPLETO de corrección con Active-IA. Toda rama devuelve dict (nunca lanza):

    a. Baja el archivo del alumno de Moodle por API REST (`_bajar_archivo_alumno`,
       mismo patrón que `ws_api.bajar_entrega` — sin browser).
    b. POST /entregas (multipart: archivo + alumno_nombre + comision_id + rubrica_id +
       moodle_url opcional). Si 409 → `{conflicto: True}` (no reintenta solo).
    c. POST /correcciones/entregas/{id}/corregir (dispara Gemini, async).
    d. Poll hasta CORREGIDA / ERROR o `timeout_s`. Si Active-IA da timeout del servicio
       de IA → `{error: "Active-IA timeout del servicio de IA, reintentá", estado}`.
    e. Si corrigió: DESCARGA LOCAL el PDF de devolución (`exportar_devolucion_pdf`) y lo
       agrega como `devolucion_pdf_local` (path en disco).
    f. Devuelve `{ok, nota, correccion_id, entrega_id, devolucion_pdf_url,
       devolucion_pdf_local, estado}`."""
    cli = _get_client()

    # --- a. Bajar el archivo del alumno de Moodle (API REST) ---
    archivo = await _bajar_archivo_alumno(client_moodle, assign_id, email)
    if "error" in archivo:
        return archivo
    fname = archivo["fname"]
    data = archivo["data"]
    # Si no vino alumno_nombre a mano, usamos el fullname que resolvió Moodle.
    if alumno_nombre is None:
        alumno_nombre = archivo["alumno_nombre"]

    # --- b. POST /entregas (multipart) ---
    entrega = await _subir_entrega(
        cli, fname, data, alumno_nombre, comision_id, rubrica_id, moodle_url
    )
    if "error" in entrega or entrega.get("conflicto"):
        return entrega
    entrega_id = entrega["entrega_id"]
    reusada = bool(entrega.get("reusada"))

    # --- c. Disparar la corrección ---
    # Si la entrega venía de un intento anterior (409 → retomada), NO se dispara de nuevo:
    # puede estar corrigiéndose o ya corregida. Se va derecho al poll, que resuelve los dos
    # casos y baja el PDF. Re-disparar sobre una corrección terminada es pedirle a Gemini
    # que rehaga trabajo ya hecho, y encima puede fallar.
    if not reusada:
        disparo = await _disparar_correccion(cli, entrega_id)
        if "error" in disparo:
            # Un timeout al DISPARAR no es fatal: la corrección puede haber arrancado
            # igual; seguimos al polling. Cualquier otro error sí corta.
            if not disparo.get("timeout"):
                return {**disparo, "entrega_id": entrega_id}

    # --- d. Polling hasta corregida / error / timeout ---
    resultado = await _poll_correccion(cli, entrega_id, comision_id, timeout_s)

    # Un error sin diagnóstico manda a reintentar a ciegas, y hay un caso donde reintentar
    # no sirve NUNCA. Se dice cuál de los tres es y qué hacer con cada uno.
    if "error" in resultado and not resultado.get("ok"):
        resultado = diagnosticar_error(resultado, reusada)

    # --- e. Descarga LOCAL del PDF de devolución (si hay correccion_id) ---
    correccion_id = resultado.get("correccion_id")
    if resultado.get("ok") and correccion_id is not None:
        pdf = await exportar_devolucion_pdf(cli, correccion_id, dest_dir)
        # Si la descarga del PDF falla, NO tumbamos la corrección (que ya salió bien):
        # devolvemos el path si se pudo, o un aviso si no.
        resultado["devolucion_pdf_local"] = pdf.get("path")
        if "error" in pdf:
            resultado["devolucion_pdf_aviso"] = pdf["error"]

    return resultado


# ---------- Descarga LOCAL del PDF de devolución ----------

async def exportar_devolucion_pdf(
    cli_activeia: ActiveIAClient, correccion_id: int, dest_dir: str | None = None
) -> dict:
    """Descarga LOCAL el PDF de devolución de una corrección de Active-IA.

    GET /documentos/correcciones/{correccion_id}/pdf con el JWT (mismo cliente que ya
    tiene el token cacheado) y guarda el archivo en `dest_dir`. Por default va al
    `salidas/` del tenant activo de la Skill (`almacen.salidas_dir()`), el mismo lugar
    donde `armar_informe` deja sus PDFs.

    Devuelve `{ok, path, bytes}` con la ruta absoluta del PDF descargado, o `{error}`
    (nunca lanza)."""
    destino = dest_dir or almacen.salidas_dir()
    try:
        resp = await cli_activeia.request(
            "GET", f"/documentos/correcciones/{correccion_id}/pdf", timeout=_PDF_TIMEOUT_S
        )
    except httpx.HTTPError as e:
        return {"error": f"No pude descargar el PDF de devolución (correccion {correccion_id}): {_detalle_error(e)}"}
    if resp.status_code != 200:
        return {
            "error": f"GET /documentos/correcciones/{correccion_id}/pdf devolvió {resp.status_code}",
            "detalle": resp.text[:300],
        }

    try:
        os.makedirs(destino, exist_ok=True)
        path = os.path.join(destino, f"devolucion_{correccion_id}.pdf")
        with open(path, "wb") as fh:
            fh.write(resp.content)
    except OSError as e:
        return {"error": f"No pude guardar el PDF de devolución en {destino}: {_detalle_error(e)}"}

    return {"ok": True, "path": path, "bytes": len(resp.content)}


async def _subir_entrega(
    cli: ActiveIAClient,
    fname: str,
    data: bytes,
    alumno_nombre: str,
    comision_id: int,
    rubrica_id: int,
    moodle_url: str | None,
) -> dict:
    """POST /entregas multipart. Primero SIN modo de procesamiento (lo trae la rúbrica);
    si la API lo exige (422), reintenta con `modo_consolidacion="solo_codigo"`. Devuelve
    `{entrega_id}`, o `{conflicto: True}` ante 409, o `{error}`."""
    files = {"archivo": (fname, data, "application/zip")}
    form: dict[str, str] = {
        "alumno_nombre": alumno_nombre,
        "comision_id": str(comision_id),
        "rubrica_id": str(rubrica_id),
    }
    if moodle_url:
        form["moodle_url"] = moodle_url

    try:
        resp = await cli.request("POST", "/entregas/", data=form, files=files)
        # Si la API exige el modo de procesamiento, reintentamos con solo_codigo.
        if resp.status_code == 422 and "modo" in resp.text.lower():
            form["modo_consolidacion"] = "solo_codigo"
            resp = await cli.request("POST", "/entregas/", data=form, files=files)
    except httpx.HTTPError as e:
        return {"error": f"No pude subir la entrega a Active-IA: {_detalle_error(e)}"}

    if resp.status_code == 409:
        # La entrega YA existe. El caso típico no es "quiero pisarla": es que un intento
        # anterior la subió y después Gemini se saturó, así que quedó ahí — y muchas veces
        # la corrección termina bien igual, minutos después. Antes se devolvía un
        # `conflicto` sugiriendo `sobrescribir=true`, un parámetro que NO existe en la
        # cadena: el alumno quedaba trabado sin salida desde la skill.
        # Ahora la buscamos y devolvemos su id para RETOMAR el flujo (poll + PDF).
        existente = await _buscar_entrega_existente(cli, alumno_nombre, comision_id, rubrica_id)
        if existente is not None:
            return {"entrega_id": existente, "reusada": True}
        return {
            "conflicto": True,
            "aviso": f"Ya existe una entrega de '{alumno_nombre}' para esa rúbrica/comisión "
            "en Active-IA, pero no pude ubicarla para retomarla. Revisala en el panel.",
            "detalle": resp.text[:300],
        }
    if resp.status_code not in (200, 201):
        return {"error": f"POST /entregas devolvió {resp.status_code}", "detalle": resp.text[:300]}

    try:
        entrega_id = resp.json()["id"]
    except (KeyError, ValueError):
        return {"error": "POST /entregas no devolvió un id de entrega.", "detalle": resp.text[:300]}
    return {"entrega_id": entrega_id}


async def _disparar_correccion(cli: ActiveIAClient, entrega_id: int) -> dict:
    """POST /correcciones/entregas/{id}/corregir. Un timeout de red acá NO es fatal (la
    corrección corre async del lado de Active-IA): se marca `timeout=True` para que el
    caller siga al polling en vez de abortar."""
    try:
        # Timeout generoso: el servicio a veces tarda en responder al disparo.
        resp = await cli.request(
            "POST", f"/correcciones/entregas/{entrega_id}/corregir", timeout=60.0
        )
    except httpx.TimeoutException:
        return {"error": "Timeout al disparar la corrección (sigo esperando).", "timeout": True}
    except httpx.HTTPError as e:
        return {"error": f"No pude disparar la corrección: {_detalle_error(e)}"}

    # 504/408 del gateway = el servicio tardó, pero la corrección puede seguir corriendo.
    if resp.status_code in (408, 504):
        return {"error": f"El servicio tardó ({resp.status_code}).", "timeout": True}
    if resp.status_code not in (200, 201, 202):
        return {
            "error": f"POST /corregir devolvió {resp.status_code}",
            "detalle": resp.text[:300],
        }
    return {"ok": True}


async def _poll_correccion(
    cli: ActiveIAClient, entrega_id: int, comision_id: int, timeout_s: int
) -> dict:
    """Poll de la corrección hasta que termina o vence `timeout_s`.

    Estrategia (GET /entregas/{id} está roto del lado del server → 500): consultamos
    GET /correcciones/entregas/{id} — 200 = corregida (trae nota + correccion_id), 404 =
    todavía no hay corrección. Ante 404 chequeamos la lista `?estado=ERROR` de la comisión
    para cazar fallos de Gemini (ej. GEMINI_OVERLOADED) sin esperar todo el timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s

    while loop.time() < deadline:
        try:
            resp = await cli.request("GET", f"/correcciones/entregas/{entrega_id}")
        except httpx.HTTPError:
            # Error de red puntual: reintentamos en el próximo tick.
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue

        if resp.status_code == 200:
            corr = resp.json()
            correccion_id = corr.get("id")
            return {
                "ok": True,
                "estado": _ESTADO_OK,
                "entrega_id": entrega_id,
                "correccion_id": correccion_id,
                "nota": corr.get("nota"),
                "devolucion_pdf_url": (
                    f"{cli._base}/documentos/correcciones/{correccion_id}/pdf"
                    if correccion_id is not None
                    else None
                ),
            }

        if resp.status_code == 404:
            # Todavía no hay corrección: ¿falló Gemini? Miramos la lista de ERROR.
            err = await _buscar_error_entrega(cli, entrega_id, comision_id)
            if err is not None:
                return err
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue

        # Otro status inesperado: esperamos y reintentamos.
        await asyncio.sleep(_POLL_INTERVAL_S)

    return {
        "error": "Active-IA timeout del servicio de IA, reintentá",
        "estado": "pendiente",
        "entrega_id": entrega_id,
    }


# La API topea `per_page` en 100, así que se pagina. Pedir 200 devuelve 422 y, si no se
# mira el status, se lee como "esta comisión no tiene entregas" — que es lo contrario.
_ENTREGAS_PER_PAGE = 100
_ENTREGAS_MAX_PAGINAS = 10


async def _entregas_de_comision(cli: ActiveIAClient, comision_id: int) -> list | dict:
    """Todas las entregas de una comisión, paginando. Devuelve la lista, o un dict de
    error (para que el caller lo propague en vez de confundirlo con 'no hay nada')."""
    todas: list = []
    for pagina in range(1, _ENTREGAS_MAX_PAGINAS + 1):
        try:
            resp = await cli.request(
                "GET", "/entregas/",
                params={"comision_id": comision_id,
                        "per_page": _ENTREGAS_PER_PAGE, "page": pagina},
            )
        except httpx.HTTPError as e:
            return {"error": f"No pude consultar /entregas/: {_detalle_error(e)}"}
        if resp.status_code != 200:
            return {"error": f"/entregas/ devolvió {resp.status_code}",
                    "body": resp.text[:300]}
        lote = resp.json().get("items", [])
        todas.extend(lote)
        if len(lote) < _ENTREGAS_PER_PAGE:
            break
    return todas


# Los tres nombres con los que `/entregas/` viene devolviendo la nota según el caso. Se
# prueban en orden y se corta en el PRIMERO QUE EXISTE, no en el primero que sea
# "verdadero". Con un `or` encadenado, una nota **0** —entrega vacía, plagio, no entregó
# nada evaluable— es falsy: caía hasta `None` y la entrega desaparecía de la lista como si
# Active-IA nunca la hubiera corregido. Es el mismo fallo silencioso que esta tool vino a
# arreglar, así que acá no se usa `or`.
_CAMPOS_NOTA = ("nota", "calificacion", "puntaje")


def _nota_de_entrega(item: dict):
    """Nota de una entrega de Active-IA, o `None` si no trae ninguna. **0 es una nota.**"""
    for campo in _CAMPOS_NOTA:
        valor = item.get(campo)
        if valor is not None:
            return valor
    return None


async def activeia_correcciones(comision_id: int, solo_corregidas: bool = True) -> dict:
    """QUÉ CORRIGIÓ ACTIVE-IA de verdad, con su nota. Es la vista que faltaba.

    `activeia_pendientes` NO sirve para esto: sus contadores son del campus (ver su
    docstring). Hasta que existió esta tool, la única forma de saber si una entrega se
    había corregido era el panel web — y eso llevó a dar por perdidas correcciones que
    estaban hechas (2026-08-04: dos alumnos figuraban trabados y ya tenían nota).

    Sirve especialmente después de un `GEMINI_OVERLOADED`: ese error NO significa que la
    corrección se perdió, sólo que la respuesta no llegó a tiempo. Muchas terminan bien
    minutos después, y acá se ven.

    Devuelve `{comision_id, total, correcciones:[{entrega_id, alumno, estado, nota,
    correccion_id, rubrica_id}]}`."""
    items = await _entregas_de_comision(_get_client(), comision_id)
    if isinstance(items, dict):          # dict = error
        return items

    filas = []
    for item in items:
        estado = item.get("estado") or item.get("status")
        nota = _nota_de_entrega(item)
        if solo_corregidas and nota is None:
            continue
        filas.append({
            "entrega_id": item.get("id"),
            "alumno": item.get("alumno_nombre"),
            "estado": estado,
            "nota": nota,
            "correccion_id": item.get("correccion_id"),
            "rubrica_id": item.get("rubrica_id"),
        })

    # `/entregas/` NO trae el correccion_id, y sin él no se puede bajar el PDF de
    # devolución: la lista quedaba mirable pero inservible para adjuntarle algo al alumno.
    # Se completa pidiendo /correcciones/entregas/{id} sólo para las que ya tienen nota.
    async def _completar(fila: dict) -> None:
        if fila.get("correccion_id") is not None or fila.get("nota") is None:
            return
        try:
            r = await cli.request("GET", f"/correcciones/entregas/{fila['entrega_id']}")
            if r.status_code != 200:
                return
            d = r.json()
            reg = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
            fila["correccion_id"] = reg.get("id") or reg.get("correccion_id")
        except httpx.HTTPError:
            return

    cli = _get_client()
    await asyncio.gather(*[_completar(f) for f in filas])
    filas.sort(key=lambda f: str(f.get("alumno") or ""))
    return {"comision_id": comision_id, "total": len(filas), "correcciones": filas,
            "nota_criterio": "Estado de ACTIVE-IA (no de Moodle). Que figure con nota acá "
                             "no significa que esté cargada en el campus: eso es cargar_nota."}


async def _buscar_entrega_existente(
    cli: ActiveIAClient, alumno_nombre: str, comision_id: int, rubrica_id: int
) -> int | None:
    """Ubica el id de una entrega ya subida de ese alumno en esa comisión/rúbrica.

    Se usa cuando POST /entregas devuelve 409. Sirve para RETOMAR: si un intento previo
    dejó la entrega arriba (típico cuando Gemini se satura), no hace falta volver a
    subirla — se sigue el flujo con la que ya está, se hace el poll y se baja el PDF.
    Devuelve None si no se puede ubicar (ahí sí, conflicto sin salida automática)."""
    objetivo = _normalizar(alumno_nombre)
    items = await _entregas_de_comision(cli, comision_id)
    if isinstance(items, dict):          # error consultando: no podemos ubicarla
        return None
    for item in items:
        # La rúbrica tiene que COINCIDIR, y sin `rubrica_id` en el payload NO se retoma.
        # Antes un item con el campo ausente o en null matcheaba cualquier rúbrica, con lo
        # cual alcanzaba el nombre del alumno: se retomaba la entrega de OTRO TP suyo, se
        # salteaba el disparo (`reusada=True`) y se bajaba el PDF de la corrección
        # equivocada — el tutor le adjuntaba al alumno la devolución de otra unidad.
        # Adjuntar la corrección de otra unidad es peor que un conflicto explícito.
        # Se compara como texto porque la API no garantiza el tipo (12 vs "12"); None
        # queda como "None" y no matchea ningún id real, que es justo lo que queremos.
        if str(item.get("rubrica_id")) != str(rubrica_id):
            continue
        if _normalizar(item.get("alumno_nombre", "")) == objetivo:
            return item.get("id")
    return None


async def _buscar_error_entrega(
    cli: ActiveIAClient, entrega_id: int, comision_id: int
) -> dict | None:
    """Busca la entrega en la lista de ERROR de la comisión. Si aparece, devuelve el dict
    de error con `error_code`/`error_mensaje` de Active-IA (ej. GEMINI_OVERLOADED). Si no
    está en ERROR, devuelve None (sigue procesando)."""
    try:
        resp = await cli.request(
            "GET",
            "/entregas/",
            params={"comision_id": comision_id, "estado": _ESTADO_ERROR, "per_page": 100},
        )
        if resp.status_code != 200:
            return None
        for item in resp.json().get("items", []):
            if item.get("id") == entrega_id:
                mensaje = item.get("error_mensaje") or "La corrección falló en Active-IA."
                return {
                    "error": mensaje,
                    "error_code": item.get("error_code"),
                    "estado": _ESTADO_ERROR,
                    "entrega_id": entrega_id,
                }
    except httpx.HTTPError:
        return None
    return None


# ---------- Editar a mano una corrección ya hecha (cuando Gemini se equivocó) ----------
#
# Nace de un incidente real (2026-08-31): Active-IA marcó como ausentes clases CSS que
# SÍ estaban en la entrega real de un alumno (correccion_id 24794), sugiriendo 16/100
# sobre una entrega que valía 100/100. Ninguna tool existente podía corregir la
# corrección: activeia_pendientes/activeia_resolver/activeia_correcciones sólo LEEN o
# DISPARAN, ninguna EDITA una ya hecha. Se resolvió a mano esa vez; esto lo deja
# disponible como tool real.

async def ver_correccion(correccion_id: int) -> dict:
    """GET /correcciones/{id} -- estado actual de una corrección (para revisar ANTES de
    editar, contra `ver_entrega`, nunca a ciegas). Devuelve el dict crudo de la API, o
    {"error": ...} (nunca lanza, mismo contrato que el resto del módulo)."""
    try:
        resp = await _get_client().request("GET", f"/correcciones/{correccion_id}")
    except httpx.HTTPError as e:
        return {"error": f"No pude consultar la corrección {correccion_id}: {_detalle_error(e)}"}
    if resp.status_code != 200:
        return {"error": f"GET /correcciones/{correccion_id} devolvió {resp.status_code}",
                 "body": resp.text[:300]}
    return resp.json()


async def actualizar_correccion(
    correccion_id: int,
    nota: float | None = None,
    criterios: list[dict] | None = None,
    fortalezas: list[str] | None = None,
    recomendaciones: list[str] | None = None,
    comentario_general: str | None = None,
    regenerar_pdf: bool = True,
    dest_dir: str | None = None,
) -> dict:
    """PUT /correcciones/{id} -- edita a mano una corrección de Active-IA (nota,
    criterios, fortalezas, recomendaciones, comentario). Todos los campos de contenido
    son opcionales (update parcial: sólo se manda lo que cambia). Marca
    `editado_manualmente=True` del lado de Active-IA (auditoría propia).

    NO carga la nota en Moodle -- eso sigue siendo `cargar_nota`, aparte.

    Sin gate de confirmación acá adentro -- ese paso vive en el `@mcp.tool()` de
    `server.py` (mismo patrón de capas que `corregir_con_active_ia`: la tool decide si
    previsualiza o ejecuta, este módulo sólo sabe hablar con la API). Usar SIEMPRE que
    la devolución de Gemini no coincida con lo entregado -- comparar contra `ver_entrega`
    ANTES de tocar nada, nunca editar a ciegas."""
    payload: dict = {}
    if nota is not None:
        payload["nota"] = nota
    if criterios is not None:
        payload["criterios"] = criterios
    if fortalezas is not None:
        payload["fortalezas"] = fortalezas
    if recomendaciones is not None:
        payload["recomendaciones"] = recomendaciones
    if comentario_general is not None:
        payload["comentario_general"] = comentario_general

    if not payload:
        return {"error": "No pasaste ningún campo para actualizar."}

    try:
        resp = await _get_client().request("PUT", f"/correcciones/{correccion_id}", json=payload)
    except httpx.HTTPError as e:
        return {"error": f"No pude editar la corrección {correccion_id}: {_detalle_error(e)}"}
    if resp.status_code != 200:
        return {"error": f"PUT /correcciones/{correccion_id} devolvió {resp.status_code}",
                 "body": resp.text[:300]}

    resultado = resp.json()

    if regenerar_pdf:
        pdf = await exportar_devolucion_pdf(_get_client(), correccion_id, dest_dir)
        resultado["devolucion_pdf_local"] = pdf.get("path")
        if "error" in pdf:
            resultado["devolucion_pdf_aviso"] = pdf["error"]

    return resultado
