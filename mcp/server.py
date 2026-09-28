"""MCP server "liviano de-un-tutor" para operar el campus TUP por API REST.

Un tutor lo corre LOCAL en su Claude Code con SUS credenciales de Moodle (env vars) y
opera su campus por la API REST oficial (token `moodle_mobile_app`). Reusa la lógica del
copiloto (`moodle.ws_api`, `moodle.informes`, `moodle.snapshot`) pero SIN nada del
multi-tenant: no hay SessionPool, ni `_tutor_actual`, ni `_validar_scope`, ni vault, ni
threading de `X-Tutor-Id`. Hay UN cliente global inicializado de env vars.

Config (env vars que el tutor setea):
    MOODLE_URL   base del campus (default https://tup.sied.utn.edu.ar)
    MOODLE_USER  usuario/DNI de login del tutor
    MOODLE_PASS  contraseña del tutor
    MOODLE_SKILL_HOME  (opcional) dir de datos locales (default ~/.moodle-skill)
    REFRESCO_TIMEOUT_S (opcional) techo de tiempo del snapshot on-demand (default 300)

Correr:  python server.py   (transport stdio: lo lanza el propio Claude Code)
"""

import asyncio
import json
import re
import logging
import os
import unicodedata
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from moodle import (
    active_ia,
    almacen,
    auditoria,
    calificador,
    encuentros,
    informes,
    panorama,
    snapshot,
    version,
    ws_api,
)
from moodle.cliente import MobileWSClient

log = logging.getLogger("skill.server")

mcp = FastMCP("moodle-tutor")

_BASE_DEFAULT = "https://tup.sied.utn.edu.ar"

# Migración automática (una vez, al importar): si esta máquina venía del layout flat
# single-tenant (`~/.moodle-skill/.env` suelto), la copia a `~/.moodle-skill/tup/` sin
# tocar los originales. No-op en instalaciones nuevas o ya migradas.
almacen.migrar_legacy_a_tup()


def _env_path(tenant_id: str | None = None) -> Path:
    """.env del tenant (fuera del repo, nunca se versiona)."""
    return Path(almacen.tenant_dir(tenant_id)) / ".env"


def _leer_env_archivo(path: Path) -> dict[str, str]:
    vals: dict[str, str] = {}
    if not path.exists():
        return vals
    for linea in path.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            k, _, v = linea.partition("=")
            vals[k.strip()] = v.strip()
    return vals


def _cargar_env() -> None:
    """Puebla os.environ desde el .env del tenant ACTIVO (KEY=valor por línea), UNA
    VEZ al importar el módulo. El entorno real gana sobre el .env (setdefault): quien
    ya exportó una var, la mantiene. Esto es SÓLO para compat con cosas que de verdad
    son proceso-wide (p. ej. `REFRESCO_TIMEOUT_S`) y con el tutor legacy que exporta
    `MOODLE_URL`/`MOODLE_USER`/`MOODLE_PASS` a mano en vez de usar `configurar`.

    IMPORTANTE — esto NO es la fuente de verdad de credenciales por-tenant: eso es
    `_credenciales_de()`, que lee el `.env` de cada tenant DIRECTO del archivo. Llamar
    `_cargar_env()` de nuevo después de un `usar_campus` NO debe usarse para refrescar
    credenciales — con `setdefault`, una vez que `os.environ` tiene las del primer
    tenant cargado en el proceso, las de cualquier tenant siguiente quedarían pisadas
    en silencio (el bug real que tenía esta función antes: cambiar de campus dejaba
    el cliente nuevo logueándose con usuario/contraseña del campus viejo)."""
    tid = almacen.tenant_activo()
    vals = _leer_env_archivo(_env_path(tid))
    if tid == "tup" and not vals:
        vals = _leer_env_archivo(Path(almacen.HOME) / ".env")
    for k, v in vals.items():
        os.environ.setdefault(k, v)
    if vals:
        # Sólo si de verdad vinieron valores del `.env` PROPIO de `tid` (no del
        # hand-export legacy, que no deja archivo): a partir de acá `os.environ`
        # queda "marcado" como de `tid`, y el fallback legacy de `_credenciales_de`
        # (y el de `active_ia`) deja de poder usarlo para resolver OTRO tenant.
        almacen.marcar_tenant_en_os_environ(tid)


_cargar_env()
_REFRESCO_TIMEOUT_S = int(os.environ.get("REFRESCO_TIMEOUT_S", "300"))

# Pool de clientes REST, uno por tenant/campus.
_clientes: dict[str, MobileWSClient] = {}


def _credenciales_de(tenant_id: str) -> dict[str, str]:
    """Credenciales de un tenant puntual, leídas DIRECTO de su propio `.env` —
    NUNCA de `os.environ`, que es compartido por TODO el proceso: usarlo como fuente
    de verdad por-tenant es lo que mezclaba credenciales entre campus después de un
    `usar_campus` (el `.env` de cada tenant, vía `_env_path`, es la única fuente).

    Único fallback a `os.environ`, y sólo para el tenant ACTIVO: un tutor legacy que
    nunca pasó por `configurar`/`agregar_campus` y en cambio exportó
    MOODLE_URL/MOODLE_USER/MOODLE_PASS a mano — mismo comportamiento single-tenant de
    siempre. Nunca se usa `os.environ` para resolver un tenant que NO es el activo."""
    vals = _leer_env_archivo(_env_path(tenant_id))
    if not vals and tenant_id == "tup":
        vals = _leer_env_archivo(Path(almacen.HOME) / ".env")
    if (not vals and tenant_id == almacen.tenant_activo()
            and almacen.os_environ_es_de(tenant_id)):
        # `os.environ` sólo es fuente válida acá si nunca se cargó ahí el `.env` de
        # OTRO tenant en este proceso (`os_environ_es_de`) — si no, un tutor que
        # conmutó de campus con `usar_campus` a un tenant sin `.env` propio heredaría
        # en silencio las credenciales del que estaba activo al arrancar el proceso.
        legacy = {
            "MOODLE_URL": os.environ.get("MOODLE_URL", ""),
            "MOODLE_USER": os.environ.get("MOODLE_USER", ""),
            "MOODLE_PASS": os.environ.get("MOODLE_PASS", ""),
            "ACTIVEIA_USER": os.environ.get("ACTIVEIA_USER", ""),
            "ACTIVEIA_PASS": os.environ.get("ACTIVEIA_PASS", ""),
        }
        if legacy.get("MOODLE_USER") and legacy.get("MOODLE_PASS"):
            vals = legacy
    return vals


def _cli(tenant_id: str | None = None) -> MobileWSClient:
    """Cliente REST del tenant pedido (default: el tenant activo). Cachea uno por
    tenant en `_clientes` — así conmutar de campus con `usar_campus` no pierde el
    cliente ya logueado del otro. Falla claro si aún no hay credenciales para ESE
    tenant."""
    tid = tenant_id or almacen.tenant_activo()
    if tid not in _clientes:
        creds = _credenciales_de(tid)
        base = (creds.get("MOODLE_URL") or _BASE_DEFAULT).rstrip("/")
        user = creds.get("MOODLE_USER")
        pw = creds.get("MOODLE_PASS")
        if not user or not pw:
            mensaje = (
                "Todavía no configuraste tus credenciales. Decile a Claude tu usuario "
                "y contraseña de Moodle y pedile que llame a `configurar` — las guarda "
                f"en {_env_path(tid)}. (No hace falta setear env vars a mano.)"
                if tid == almacen.tenant_activo() else
                f"El campus '{tid}' todavía no tiene credenciales guardadas. Usá "
                "`agregar_campus` para configurarlo."
            )
            raise RuntimeError(mensaje)
        _clientes[tid] = MobileWSClient(base, user, pw)
    return _clientes[tid]


def _invalidar_cliente(tenant_id: str) -> None:
    """Descarta el cliente cacheado de un tenant (para forzar recreación con
    credenciales nuevas, p. ej. tras `configurar`/`agregar_campus`)."""
    _clientes.pop(tenant_id, None)


def _escribir_env(vals: dict[str, str], tenant_id: str | None = None) -> None:
    """Escribe/actualiza el .env local del tenant. Preserva las claves que ya
    estaban y no se pasan de nuevo.

    Permisos: `os.chmod(path, 0o600)` es real seguridad de acceso en Linux/macOS
    (POSIX), pero en Windows NTFS `chmod` sólo alterna el flag de sólo-lectura — NO
    es equivalente a permisos Unix 600 y NO restringe qué otras cuentas de Windows
    pueden leer el archivo. Documentado acá como limitación conocida en vez de dejar
    que el comentario prometa una propiedad de seguridad que en Windows no se
    cumple."""
    tid = tenant_id or almacen.tenant_activo()
    path = _env_path(tid)
    existentes: dict[str, str] = _leer_env_archivo(path)
    existentes.update({k: v for k, v in vals.items() if v})
    path.parent.mkdir(parents=True, exist_ok=True)
    cuerpo = "# Credenciales de la skill TUP Campus Navigator. NO subir a git.\n" + \
             "\n".join(f"{k}={v}" for k, v in existentes.items()) + "\n"
    path.write_text(cuerpo, encoding="utf-8")
    os.chmod(path, 0o600)
    if tid == almacen.tenant_activo():
        for k, v in existentes.items():
            os.environ[k] = v  # disponibles ya en esta sesión
        almacen.marcar_tenant_en_os_environ(tid)


async def _configurar_credenciales(
    tenant_id: str,
    moodle_user: str,
    moodle_pass: str,
    moodle_url: str,
    activeia_user: str = "",
    activeia_pass: str = "",
) -> dict:
    """Helper compartido por `configurar` y `agregar_campus`: VALIDA el login ANTES
    de escribir nada a disco (requisito del spec — ver `specs/multi-tenant-moodle`,
    "Invalid credentials are rejected without side effects"). El cliente de prueba se
    arma 100% EN MEMORIA, sin tocar `.env` ni crear el directorio del tenant; sólo si
    el login funciona se persiste.

    Esto no es sólo orden estético: elimina de raíz toda la clase de bugs de
    "rollback con huecos" que tenía la versión anterior (escribir primero, loguear
    después, deshacer si falla) — un `asyncio.CancelledError` durante el login (que
    es `BaseException`, no `Exception`, así que un `except Exception` no lo atrapa) ya
    no puede dejar un `.env` huérfano con una contraseña real para un tenant nunca
    registrado, porque nunca se llegó a escribir nada."""
    base = (moodle_url or _BASE_DEFAULT).rstrip("/")
    user = moodle_user.strip()
    cliente_prueba = MobileWSClient(base, user, moodle_pass)
    try:
        cursos = await ws_api.descubrir_cursos(cliente_prueba)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"El login falló ({type(e).__name__}). Revisá "
                f"usuario y contraseña (el usuario de Moodle no siempre es el DNI). "
                f"No guardé nada. Detalle: {str(e)[:150]}"}

    vals = {"MOODLE_USER": user, "MOODLE_PASS": moodle_pass, "MOODLE_URL": base}
    if activeia_user:
        vals["ACTIVEIA_USER"] = activeia_user.strip()
    if activeia_pass:
        vals["ACTIVEIA_PASS"] = activeia_pass
    _escribir_env(vals, tenant_id)
    # Reusa el cliente ya logueado en vez de descartarlo y obligar a `_cli()` a
    # loguearse de nuevo — y de paso deja el pool consistente con lo recién escrito.
    _clientes[tenant_id] = cliente_prueba
    active_ia.invalidar_cliente(tenant_id)
    return {"ok": True, "env_path": str(_env_path(tenant_id)), "cursos": cursos}


@mcp.tool()
async def configurar(
    moodle_user: str,
    moodle_pass: str,
    moodle_url: str = "",
    activeia_user: str = "",
    activeia_pass: str = "",
) -> dict:
    """Guardá las credenciales del tutor y dejá la skill lista para operar. Pedile al
    tutor su usuario y contraseña de Moodle (y, si va a usar Active-IA, esas también) y
    llamá esta tool: escribe un .env local (permisos 600, fuera del repo — la
    contraseña NO se versiona) y VALIDA el login contra el campus antes de darlo por
    bueno. Reemplaza el tener que setear variables de entorno a mano.

    Configura SIEMPRE el campus activo (por defecto `tup`); para sumar un campus
    nuevo sin tocar el que ya está configurado, usá `agregar_campus`.

    Si el login falla, NO deja las credenciales como válidas: devuelve el error para
    que el tutor revise usuario/contraseña (ojo: el usuario de Moodle no siempre es el
    DNI)."""
    tid = almacen.tenant_activo()
    res = await _configurar_credenciales(tid, moodle_user, moodle_pass, moodle_url,
                                         activeia_user, activeia_pass)
    if not res["ok"]:
        return res
    return {"ok": True, "mensaje": f"Credenciales validadas y guardadas en "
            f"{res['env_path']}. Veo {len(res['cursos'])} cursos tuyos. Ya podés "
            "mapear tus comisiones.", "cursos": len(res["cursos"])}


# ---------- MULTI-CAMPUS ----------
@mcp.tool()
async def listar_campus() -> dict:
    """Campus (tenants) registrados en esta máquina, con cuál está ACTIVO ahora mismo.
    Una instalación nueva siempre tiene al menos `tup` (el default histórico). Usá
    `agregar_campus` para sumar uno nuevo y `usar_campus` para cambiar cuál opera
    `_cli()` por defecto en el resto de las tools."""
    activo = almacen.tenant_activo()
    return {"activo": activo,
            "campus": [dict(t, activo=(t["id"] == activo)) for t in almacen.tenants()]}


@mcp.tool()
async def usar_campus(tenant_id: str) -> dict:
    """Cambia el campus ACTIVO: todas las tools que no reciban un tenant explícito
    (o sea, todas — hoy ninguna lo pide) empiezan a operar contra ese campus.

    Rechaza un `tenant_id` que no esté registrado (usá `listar_campus` para ver los
    disponibles, o `agregar_campus` para sumarlo primero) y en ese caso NO toca el
    campus activo."""
    ids = {t["id"] for t in almacen.tenants()}
    if tenant_id not in ids:
        return {"ok": False,
                "error": f"'{tenant_id}' no está registrado. Campus disponibles: "
                         f"{sorted(ids)}. Usá agregar_campus para sumarlo."}
    almacen.set_tenant_activo(tenant_id)
    return {"ok": True, "activo": tenant_id}


_RE_COMISION_GENERICA = re.compile(r"comisi[oó]n|^com[\s_.-]*\d|\bC\d{1,2}\b|^\d+pro(?:g)?\d+$", re.IGNORECASE)


def _es_comision(nombre: str) -> bool:
    """¿Este grupo es una comisión? Primero el criterio de TUP (`grupos.clasificar`:
    «M25 C4-01»); si no, uno genérico por nombre — «Comision_6», «Comisión 3», «C2» — para
    campus que nombran distinto. Los regionales (R-*) y los grupos auxiliares nunca cuentan."""
    tipo = ws_api.clasificar_grupo(nombre or "")
    if tipo == "comision":
        return True
    return tipo == "otro" and bool(_RE_COMISION_GENERICA.search(nombre or ""))


async def _mapear_tenant(tenant_id: str) -> dict:
    """Arma y guarda el "Mis datos" de un campus SIN preguntarle nada al tutor: sus
    cursos (matrícula), SUS comisiones en cada uno (los grupos de los que es miembro,
    `core_group_get_course_user_groups`; sólo los de tipo `comision`, no regionales ni
    auxiliares) y las tareas de cada curso. Usa las credenciales YA guardadas del
    tenant — si no hay, falla igual que cualquier tool (`_cli`).

    Escribe en la carpeta del tenant PEDIDO (no del activo) y conserva la clave
    "clickup" si ya había un `mis_datos.json`. No pisa nada si no se pudo descubrir
    ningún curso. Devuelve `{"ok", "cursos", "comisiones", "aviso"?}`."""
    cli = _cli(tenant_id)
    cursos = await ws_api.descubrir_cursos(cli)
    if not cursos or (isinstance(cursos[0], dict) and cursos[0].get("error")):
        return {"ok": False, "error": (cursos[0].get("error") if cursos else "No veo ningún curso en tu cuenta.")}
    uid = await cli.api.userid()
    try:
        info = await cli.ws("core_webservice_get_site_info")
        nombre = (info or {}).get("fullname") or ""
    except Exception:  # noqa: BLE001
        nombre = ""

    armados: list[dict] = []
    for c in cursos:
        cid = c.get("course_id")
        try:
            r = await cli.ws("core_group_get_course_user_groups", {"courseid": cid, "userid": uid})
            grupos = (r or {}).get("groups", []) if isinstance(r, dict) else []
        except Exception:  # noqa: BLE001
            grupos = []
        mias = [{"comision": g.get("name"), "group_id": g.get("id")}
                for g in grupos if _es_comision(g.get("name") or "")]
        acceso_total = False
        if not grupos:
            # Sin membresía en ningún grupo (docente/manager con acceso a todo el curso):
            # las comisiones a su cargo son todas las del curso.
            try:
                todos = await cli.ws("core_group_get_course_groups", {"courseid": cid})
            except Exception:  # noqa: BLE001
                todos = []
            mias = [{"comision": g.get("name"), "group_id": g.get("id")}
                    for g in (todos or []) if _es_comision(g.get("name") or "")]
            acceso_total = bool(mias)
        try:
            tareas = [{"assign_id": str(t["id"]), "titulo": t.get("titulo", "")}
                      for t in await ws_api.listar_tareas(cli, cid)]
        except Exception:  # noqa: BLE001
            tareas = []
        armados.append({"course_id": cid, "nombre": c.get("nombre"),
                        "comisiones_del_tutor": mias, "tareas": tareas,
                        **({"acceso_total": True} if acceso_total else {})})

    # Sólo las materias donde el tutor tiene comisión; si en ninguna, todas (vacías).
    con_comision = [a for a in armados if a["comisiones_del_tutor"]]
    datos = {"tutor": {"nombre": nombre}, "cursos": con_comision or armados}

    ruta = Path(almacen.mis_datos_path(tenant_id))
    try:
        previos = json.loads(ruta.read_text(encoding="utf-8"))
        if isinstance(previos, dict) and "clickup" in previos:
            datos["clickup"] = previos["clickup"]
    except (OSError, ValueError):
        pass
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")

    salida = {"ok": True, "cursos": len(datos["cursos"]),
              "comisiones": sum(len(a["comisiones_del_tutor"]) for a in datos["cursos"])}
    if any(a.get("acceso_total") for a in datos["cursos"]):
        salida["nota"] = ("No figurás como miembro de ningún grupo en algunos cursos (tenés acceso "
                          "docente a todo el curso): se listaron todas sus comisiones.")
    if not con_comision:
        salida["aviso"] = ("Estás matriculado en estos cursos pero no figurás en ningún grupo de "
                           "tipo comisión: quedaron mapeados sin comisión asignada.")
    return salida


@mcp.tool()
async def mapear_mis_datos() -> dict:
    """Detecta SOLA, con las credenciales ya guardadas del campus ACTIVO, tus materias y
    las comisiones que tenés asignadas (más las tareas de cada materia) y las guarda en
    "Mis datos". No pide usuario ni contraseña ni nada: sirve para armar el mapeo la
    primera vez o rehacerlo si cambió la cohorte. (API REST.)"""
    return await _mapear_tenant(almacen.tenant_activo())


@mcp.tool()
async def agregar_campus(
    tenant_id: str,
    nombre: str,
    url: str,
    moodle_user: str,
    moodle_pass: str,
    activeia_user: str = "",
    activeia_pass: str = "",
) -> dict:
    """Registra un campus NUEVO (otra UTN, otra sede) sin tocar el que ya está
    configurado: no cambia el campus activo. Valida el login contra `url` ANTES de
    persistir nada — igual que `configurar`. Si las credenciales son inválidas, no
    queda ni `.env` ni entrada en `listar_campus`.

    `tenant_id` es un slug propio (ej. `"tup"`, `"otra-utn"`): sólo minúsculas,
    números y guiones, 1-40 caracteres, y tiene que ser único (comparado SIN importar
    mayúsculas — "TUP" se rechaza si ya existe "tup", porque en Windows terminarían
    siendo el mismo directorio). `.`, `..` y vacío se rechazan siempre.

    Con login OK: guarda el `.env` del tenant, lo registra en `tenants.json` y corre
    `descubrir_cursos`/`descubrir_comisiones` contra el campus nuevo para sembrar su
    catálogo (`aulas.json`/`comisiones.json` propios). Después de esto,
    `usar_campus(tenant_id)` lo deja operativo."""
    # Validar el id ANTES de tocar cualquier cosa (red, disco, registro) — un id
    # inválido (colisión de mayúsculas, "..", vacío) no debe ni intentar loguearse.
    error_id = almacen.validar_tenant_id(tenant_id)
    if error_id:
        return {"ok": False, "error": error_id}

    res = await _configurar_credenciales(tenant_id, moodle_user, moodle_pass, url,
                                         activeia_user, activeia_pass)
    if not res["ok"]:
        return res

    try:
        almacen.registrar_tenant(tenant_id, nombre, (url or _BASE_DEFAULT).rstrip("/"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    # Sembrar el catálogo del tenant nuevo. Que esto falle no revierte el registro:
    # el campus ya quedó configurado y usable, sólo faltaría correr el descubrimiento
    # a mano si esto no anduvo.
    aviso = None
    try:
        import datetime as _dt

        cursos = await ws_api.descubrir_cursos(_cli(tenant_id))
        hoy = _dt.date.today()
        mes = hoy.month - 1 + 6
        vigente_hasta = f"{hoy.year + mes // 12:04d}-{mes % 12 + 1:02d}"
        aulas_out = Path(almacen.tenant_dir(tenant_id)) / "aulas.json"
        aulas_out.write_text(
            json.dumps({
                "cohorte": f"Descubierto {hoy.isoformat()}",
                "vigente_hasta": vigente_hasta,
                "materias": [{"materia": c.get("nombre"), "course_id": c.get("course_id")}
                            for c in cursos],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8")
        comisiones_out = Path(almacen.tenant_dir(tenant_id)) / "comisiones.json"
        materias_com = []
        for c in cursos:
            try:
                grupos = await ws_api.descubrir_comisiones(_cli(tenant_id), c.get("course_id"))
            except Exception:  # noqa: BLE001
                grupos = []
            # Sólo los grupos tipo "comisión" (no regionales ni "otro") — mismo
            # criterio que el catálogo curado a mano. Sin "tutor": el descubrimiento
            # automático no conoce el reparto tutor->comisión, y `mi_comision` (y
            # cualquier otro lector) trata su ausencia como "todavía sin asignar",
            # no como un error.
            comisiones = [
                {"comision": g.get("nombre"), "nombre_campus": g.get("nombre"),
                 "group_id": g.get("group_id")}
                for g in grupos if _es_comision(g.get("nombre") or "")
            ]
            materias_com.append({"materia": c.get("nombre"),
                                 "course_id": c.get("course_id"), "comisiones": comisiones})
        comisiones_out.write_text(
            json.dumps({
                "cohorte": f"Descubierto {hoy.isoformat()}",
                "vigente_hasta": vigente_hasta,
                "materias": materias_com,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        aviso = (f"El campus quedó registrado y con login OK, pero no pude sembrar su "
                 f"catálogo ({type(e).__name__}: {str(e)[:150]}). Corré "
                 "descubrir_cursos/descubrir_comisiones a mano con usar_campus.")

    # "Mis datos" del campus nuevo: sus materias y las comisiones que el tutor tiene
    # asignadas, detectadas solas — así no arranca vacío ni hay que pedirle nada más.
    mapeo = None
    try:
        mapeo = await _mapear_tenant(tenant_id)
        if not mapeo.get("ok"):
            aviso = ((aviso + " ") if aviso else "") + f"No pude mapear tus materias y comisiones: {mapeo.get('error')}"
    except Exception as e:  # noqa: BLE001
        aviso = ((aviso + " ") if aviso else "") + (
            f"No pude mapear tus materias y comisiones ({type(e).__name__}). "
            "Corré mapear_mis_datos con este campus activo.")

    salida = {"ok": True, "tenant_id": tenant_id, "cursos": len(res["cursos"]),
              "comisiones_asignadas": (mapeo or {}).get("comisiones", 0),
              "mensaje": f"Campus '{tenant_id}' registrado y validado. "
                        f"Veo {len(res['cursos'])} cursos. Usá usar_campus('{tenant_id}') "
                        "para operar contra él."}
    if aviso:
        salida["aviso"] = aviso
    return salida


# ---------- VERSIÓN Y ACTUALIZACIÓN ----------
@mcp.tool()
async def version_skill(forzar: bool = False) -> dict:
    """Versión instalada de la skill y si hay una nueva publicada en GitHub.

    La skill vive en un clon local en la máquina de cada tutor: sin esto, quien la
    instaló hace dos meses sigue con los bugs de hace dos meses sin manera de enterarse.
    El chequeo se cachea 24 h para no pegarle a GitHub en cada consulta (`forzar=true`
    lo saltea).

    `disponible` tiene TRES valores: true (hay una nueva), false (estás al día) y null
    (no se pudo averiguar, p. ej. sin red). Null NO significa que estés al día."""
    return await version.chequear(forzar=forzar)


@mcp.tool()
async def actualizar_skill() -> dict:
    """Actualiza la skill a la última versión publicada (`git pull --ff-only`).

    Si el tutor tiene cambios sin commitear NO toca nada y avisa: pisar trabajo ajeno es
    peor que quedarse desactualizado. Después de actualizar HAY QUE REINICIAR Claude Code
    — el MCP se carga al arrancar la sesión, así que hasta entonces sigue corriendo la
    versión vieja."""
    return await version.actualizar()


# ---------- MIS DATOS (config de la cohorte: la fuente de verdad de los IDs) ----------
def _sin_acentos(txt: str) -> str:
    """Minúsculas y sin tildes: 'Matemática-Agosto 2026' -> 'matematica-agosto 2026'.
    El catálogo y el nombre del curso en el campus no siempre acentúan igual."""
    s = unicodedata.normalize("NFKD", str(txt or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _raices_de_materias() -> list[str]:
    """Raíz de cada materia del catálogo ('Programación I' -> 'programac'), para
    reconocer el curso sin depender de cómo lo tituló la cátedra ese cuatrimestre
    ('Programación I - Agosto 2026', 'Matemática-Agosto 2026').

    Lista vacía si el catálogo no se puede leer. El que llama NO debe avisar nada en
    ese caso: no sabemos, y un aviso disparado por una lectura fallida afirma algo
    que nadie verificó.
    """
    try:
        cat = json.loads(_AULAS_PATH_REPO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raices = []
    for m in cat.get("materias", []):
        primera = _sin_acentos(m.get("materia", "")).split()
        if primera and len(primera[0]) >= 5:
            raices.append(primera[0][:9])
    return sorted(set(raices))


def _avisos_config_incompleta(cursos: list[dict]) -> list[str]:
    """Huecos en "Mis datos" que NO son un archivo vacío pero igual dejan al tutor a
    ciegas: un curso con comisiones pero sin `tareas` (el panel lo muestra vacío/
    degradado sin que nadie lo note), o ninguna materia guardada que figure en el
    catálogo de aulas. No bloquea nada — sólo avisos para que el
    agente se los diga al tutor y ofrezca remapear. Reusada por `mis_datos` (al leer) y
    `guardar_mis_datos` (al guardar, ahí la de `tareas` además rechaza el guardado)."""
    avisos = []
    for curso in cursos:
        if curso.get("comisiones_del_tutor") and not curso.get("tareas"):
            avisos.append(
                f"El curso '{curso.get('nombre')}' (course_id {curso.get('course_id')}) "
                "tiene comisiones pero no tiene 'tareas' guardadas — el panel lo va a "
                "mostrar degradado. Corré listar_tareas(course_id) y volvé a guardar con "
                "guardar_mis_datos."
            )
    # Las materias que cubre la skill salen del CATÁLOGO, no de una palabra clavada.
    # Cuando esto preguntaba sólo por "program", un docente de Matemática —materia
    # mapeada, catálogo al día, todo funcionando— recibía en cada arranque un aviso
    # diciéndole que su materia no era el alcance de la skill. El aviso existe para
    # el que se equivocó de curso, no para el que sumó una materia nueva.
    # Si el catálogo no se puede leer NO se avisa nada: un aviso que se dispara
    # porque falló la lectura es peor que no avisar (afirma algo que nadie verificó).
    raices = _raices_de_materias()
    nombres = [str(c.get("nombre") or "") for c in cursos]
    if nombres and raices and not any(
        any(r in _sin_acentos(n) for r in raices) for n in nombres
    ):
        avisos.append(
            "Ningún curso guardado coincide con las materias que cubre esta skill. "
            f"Cursos guardados: {', '.join(nombres)}. ¿Le faltó mapear su materia real?"
        )
    return avisos


@mcp.tool()
async def mis_datos() -> dict:
    """Configuración vigente del tutor ("Mis datos"): cursos, comisiones (group_id) y
    tareas (assign_id) mapeadas. CONSULTALA PRIMERO para resolver IDs en vez de asumir
    valores. Si viene vacía o la cohorte cambió, corré el descubrimiento (descubrir_cursos
    -> descubrir_comisiones -> listar_tareas), mostrale el mapeo al tutor y guardá con
    guardar_mis_datos.

    Si la respuesta trae `config_incompleta`, la config NO está vacía pero tiene huecos
    (un curso sin tareas, o ninguna materia que parezca Programación) — decíselo al
    tutor ANTES de seguir como si todo estuviera bien, y ofrecé rehacer el mapeo de ese
    curso puntual."""
    await almacen.init_db()
    datos = await almacen.get_mis_datos()

    if not datos:
        # Campus con credenciales guardadas pero sin mapeo (p. ej. dado de alta con una
        # versión anterior): se detecta solo, sin pedirle nada al tutor.
        try:
            if _credenciales_de(almacen.tenant_activo()).get("MOODLE_USER"):
                if (await _mapear_tenant(almacen.tenant_activo())).get("ok"):
                    datos = await almacen.get_mis_datos()
        except Exception as e:  # noqa: BLE001
            log.warning("Auto-mapeo de mis_datos falló: %s: %s", type(e).__name__, e)

    # Aviso de versión acá y no en una tool aparte: SKILL.md manda consultar `mis_datos`
    # primero, así que es el único lugar por el que todos los tutores pasan sí o sí. Va
    # cacheado 24 h y nunca rompe esta tool: si el chequeo falla, se sigue sin él.
    aviso_version = None
    try:
        v = await version.chequear()
        if v.get("disponible"):
            aviso_version = v["aviso"]
    except Exception as e:  # noqa: BLE001
        log.warning("Chequeo de versión falló: %s: %s", type(e).__name__, e)

    if not datos:
        salida = {
            "vacio": True,
            "aviso": "Sin datos guardados. Corré descubrir_cursos / descubrir_comisiones / "
                     "listar_tareas, confirmá el mapeo con el tutor y guardalo con guardar_mis_datos.",
        }
    else:
        salida = {"actualizado_at": await almacen.mis_datos_actualizada(), "datos": datos}
        avisos_config = _avisos_config_incompleta(datos.get("cursos", []))
        if avisos_config:
            salida["config_incompleta"] = avisos_config
    if aviso_version:
        salida["actualizacion_disponible"] = aviso_version
    return salida


_AULAS_PATH_REPO = Path(__file__).parent / "aulas.json"


def _ruta_catalogo(nombre_archivo: str, path_repo: Path) -> Path | None:
    """Resuelve qué archivo de catálogo leer para el tenant ACTIVO, en orden:
    1. El propio del tenant (`tenant_dir()/<nombre_archivo>`), si existe.
    2. El repo-shipped (`mcp/<nombre_archivo>`), SOLO si el tenant activo es `tup`
       (el default histórico, con catálogo curado a mano en el repo).
    3. Ninguno (`None`) — un tenant no-tup sin descubrimiento propio todavía."""
    propio = Path(almacen.tenant_dir()) / nombre_archivo
    if propio.exists():
        return propio
    if almacen.tenant_activo() == "tup" and path_repo.exists():
        return path_repo
    return None


@mcp.tool()
async def aulas() -> dict:
    """Aulas (materia → curso) de la cohorte vigente, del catálogo, VALIDADAS contra los
    cursos reales del tutor. Usá esto PRIMERO al mapear: en vez de que el tutor descubra
    cursos, mostrale estas materias y que elija la suya. Devuelve las materias del catálogo
    que el tutor efectivamente tiene (course_id confirmado en su cuenta).

    Red de seguridad (la lección de no confiar en IDs fijos): si un course_id del catálogo
    ya NO existe en la cuenta del tutor, o el catálogo venció, se avisa y hay que caer a
    `descubrir_cursos` en vivo. NUNCA se mapea un aula que el tutor no tiene."""
    import datetime
    ruta = _ruta_catalogo("aulas.json", _AULAS_PATH_REPO)
    if ruta is None:
        return {"error": "Todavía no hay catálogo de aulas para este campus. Usá "
                         "descubrir_cursos para mapear en vivo."}
    try:
        cat = json.loads(ruta.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"error": f"No pude leer el catálogo de aulas: {e}. Usá descubrir_cursos."}

    reales = {c["course_id"]: c["nombre"] for c in await ws_api.descubrir_cursos(_cli())}
    vigentes, faltantes = [], []
    for m in cat.get("materias", []):
        cid = m.get("course_id")
        if cid in reales:
            vigentes.append({"materia": m["materia"], "course_id": cid,
                             "nombre_campus": reales[cid]})
        else:
            faltantes.append(m)

    vencido = False
    vh = cat.get("vigente_hasta", "")
    try:
        vencido = datetime.date.today().strftime("%Y-%m") > vh
    except Exception:  # noqa: BLE001
        pass

    out = {"cohorte": cat.get("cohorte"), "materias": vigentes}
    if vencido:
        out["aviso"] = (f"El catálogo de aulas venció ({vh}). Toca el mantenimiento de "
                        "6 meses: actualizá mcp/aulas.json. Mientras, usá descubrir_cursos.")
    if faltantes:
        out["faltan_en_tu_cuenta"] = [m["materia"] for m in faltantes]
    if not vigentes:
        out["aviso"] = (out.get("aviso", "") + " Ninguna aula del catálogo está en tu "
                        "cuenta: mapeá con descubrir_cursos en vivo.").strip()
    return out


_APRENDIZAJES_PATH = Path(__file__).parent / "aprendizajes.json"
_ESTADOS_APRENDIZAJE = ("confirmado", "dicho", "descartado")


def _leer_aprendizajes() -> dict:
    try:
        return json.loads(_APRENDIZAJES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@mcp.tool()
async def aprendizajes_materia(course_id: int = 0) -> dict:
    """Lo que los DOCENTES de una materia le enseñaron a la skill sobre cómo funciona
    su materia de verdad. Viaja en el repo: lo que un profesor corrige una vez le sirve
    a los otros docentes y a quien toque el código dentro de seis meses.

    **LEELA AL EMPEZAR a trabajar en una materia, no cuando algo ya salió mal.** Sobre
    todo en las que no son Programación: ahí es donde la skill sabe menos y donde el
    docente sabe más.

    Cada entrada trae `estado` y los tres valores NO se pueden mezclar al hablarle al
    usuario:
      - `confirmado`  → se corroboró contra el campus, y `verificacion` dice cómo.
      - `dicho`       → lo afirmó un docente y NADIE lo verificó todavía.
      - `descartado`  → se verificó y era falso (queda escrito: saber qué se probó y no
                        era ahorra probarlo de nuevo).
    Presentar un `dicho` como hecho es exactamente el error que esta skill existe para no
    cometer. Decí siempre de cuál de los tres viene lo que estás afirmando.

    Y si una entrada `confirmado` contradice lo que ves EN VIVO hoy, **gana lo que ves en
    vivo**: estas reglas envejecen igual que un catálogo. Volvé a anotar con la fecha nueva.

    `course_id=0` devuelve todas las materias. READ-ONLY."""
    cat = _leer_aprendizajes()
    if not cat:
        return {"error": "No pude leer mcp/aprendizajes.json.", "materias": []}
    mats = cat.get("materias", [])
    if course_id:
        mats = [m for m in mats if m.get("course_id") == course_id]
        if not mats:
            return {"course_id": course_id, "aprendizajes": [], "aviso":
                    "Todavía nadie le enseñó nada a la skill sobre esta materia. Si el "
                    "docente te corrige algo, anotalo con `anotar_aprendizaje`."}
    return {"materias": mats, "lectura": cat.get("_regla_dura", "")}


@mcp.tool()
async def anotar_aprendizaje(course_id: int, regla: str, quien: str,
                             estado: str = "dicho", verificacion: str = "",
                             afecta: list[str] | None = None) -> dict:
    """Anota algo que un DOCENTE le enseñó a la skill sobre su materia. Escribe en
    `mcp/aprendizajes.json`, que **viaja en el repo compartido**.

    CUÁNDO: cuando el docente corrige un supuesto ("esa entrega no cuenta", "el TP2 se
    califica distinto", "la U5 tiene tres semanas porque la partimos"). NO para guardar
    la conversación: el log crudo no lo relee nadie y envejece sin avisar. Lo que se
    guarda es la REGLA, en una frase que se entienda sola dentro de seis meses.

    **DECÍSELO ANTES DE ANOTAR, no después.** El docente tiene que saber que queda
    registrado y que el archivo lo ven los demás docentes de la materia. No se graba a
    nadie sin que lo sepa.

    NUNCA anotes acá datos de alumnos (nombres, mails, notas), credenciales, ni nada que
    no sea sobre cómo funciona la materia.

    `estado`:
      - `dicho` (por defecto)  → lo afirmó el docente y todavía no se verificó. Es el
        valor honesto cuando acabás de escucharlo. **No pongas `confirmado` porque suene
        creíble**: ponelo sólo si lo corroboraste contra el campus en esta misma sesión.
      - `confirmado`  → verificado en vivo. `verificacion` es OBLIGATORIA y tiene que
        decir CÓMO (qué tool, qué campo, qué devolvió).
      - `descartado`  → se verificó y era falso. También se guarda.

    `afecta`: qué tools o módulos cambian de comportamiento si esta regla es cierta.
    Sirve para saber qué hay que revisar cuando alguien la confirme o la tire abajo."""
    import datetime

    estado = (estado or "dicho").strip().lower()
    if estado not in _ESTADOS_APRENDIZAJE:
        return {"error": f"`estado` tiene que ser uno de {_ESTADOS_APRENDIZAJE}. "
                         f"Recibí {estado!r}."}
    if estado == "confirmado" and not verificacion.strip():
        return {"error": "Para `confirmado` hace falta `verificacion`: con qué tool y "
                         "contra qué campo se corroboró. Sin eso no es confirmado, es "
                         "`dicho` — y la diferencia es todo el punto de este archivo."}
    if not regla.strip() or not quien.strip():
        return {"error": "`regla` y `quien` no pueden ir vacíos."}

    cat = _leer_aprendizajes()
    if not cat:
        return {"error": "No pude leer mcp/aprendizajes.json — no piso el archivo a "
                         "ciegas. Revisá que exista y sea JSON válido."}

    materia = next((m for m in cat.get("materias", []) if m.get("course_id") == course_id), None)
    if materia is None:
        cat_aulas = json.loads(_AULAS_PATH_REPO.read_text(encoding="utf-8"))
        nombre = next((a["materia"] for a in cat_aulas.get("materias", [])
                       if a.get("course_id") == course_id), f"(course {course_id})")
        materia = {"materia": nombre, "course_id": course_id, "aprendizajes": []}
        cat.setdefault("materias", []).append(materia)

    entrada = {
        "fecha": datetime.date.today().isoformat(),
        "quien": quien.strip(),
        "regla": regla.strip(),
        "estado": estado,
        "verificacion": verificacion.strip(),
        "afecta": afecta or [],
    }
    materia.setdefault("aprendizajes", []).append(entrada)
    _APRENDIZAJES_PATH.write_text(
        json.dumps(cat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "ok": True,
        "materia": materia["materia"],
        "anotado": entrada,
        "total_de_la_materia": len(materia["aprendizajes"]),
        "aviso": ("Queda en mcp/aprendizajes.json, que viaja en el repo compartido. "
                  "Decile al docente que quedó registrado si todavía no se lo dijiste. "
                  "Para que le llegue a los demás hay que commitear y pushear."),
    }


_COMISIONES_PATH_REPO = Path(__file__).parent / "comisiones.json"


def _clave_nombre(texto: str) -> str:
    """Normaliza un nombre para comparar: sin acentos, sin dobles espacios, minúsculas.
    Así 'Tomás García' matchea con 'tomas garcia' escrito a las apuradas."""
    import unicodedata
    sin_tildes = "".join(c for c in unicodedata.normalize("NFD", texto)
                         if unicodedata.category(c) != "Mn")
    return " ".join(sin_tildes.lower().split())


@mcp.tool()
async def mi_comision(nombre: str) -> dict:
    """Resuelve, a partir del NOMBRE del tutor, qué comisión le toca en cada materia y
    qué actividades de cursada tiene para corregir (con su cmid). Es el atajo del mapeo:
    el tutor dice su nombre y no hace falta que descubra cursos, grupos ni tareas.

    Devuelve, por materia: course_id, comisión (nombre en el campus + group_id) y la
    lista de actividades de cursada (cierres de unidad + integradores/TPs). NO incluye
    parciales ni recuperatorios: esos tienen calendario propio, pedilos con listar_tareas.

    Matchea por nombre completo o por parte (apellido, nombre de pila), sin distinguir
    acentos ni mayúsculas. Si el nombre es ambiguo devuelve los candidatos para que el
    tutor elija — nunca elige por él.

    VALIDACIÓN (la regla de la skill: verificar en vivo, nunca inventar): cada group_id
    del catálogo se coteja contra los grupos reales del curso antes de devolverlo. Si el
    catálogo quedó viejo, lo dice y manda a descubrir_comisiones en vivo."""
    ruta = _ruta_catalogo("comisiones.json", _COMISIONES_PATH_REPO)
    if ruta is None:
        return {"error": "Todavía no hay catálogo de comisiones para este campus. "
                         "Mapeá en vivo con descubrir_comisiones + listar_tareas."}
    try:
        cat = json.loads(ruta.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"error": f"No pude leer el catálogo de comisiones: {e}. "
                         "Mapeá en vivo con descubrir_comisiones + listar_tareas."}

    buscado = _clave_nombre(nombre)
    if not buscado:
        return {"error": "Decime el nombre del tutor para buscar su comisión."}

    # Exacto primero; si no hay, por coincidencia parcial (apellido o nombre suelto).
    exactos, parciales = [], []
    for m in cat.get("materias", []):
        for c in m.get("comisiones", []):
            clave = _clave_nombre(c.get("tutor", ""))
            if not clave:
                continue
            fila = {"materia": m["materia"], "course_id": m["course_id"], **c}
            if clave == buscado:
                exactos.append(fila)
            elif buscado in clave or clave in buscado:
                parciales.append(fila)

    encontradas = exactos or parciales
    if not encontradas:
        # `.get("tutor")` y no `c["tutor"]`: una comisión recién descubierta por
        # `agregar_campus` (sin reparto tutor->comisión todavía) no trae la clave
        # "tutor" en absoluto — antes esto tiraba KeyError acá (confirmado en el
        # review). Ausente/vacío se trata como "todavía sin asignar", no como error,
        # y no entra en la lista de tutores conocidos.
        tutores = sorted({c["tutor"] for m in cat.get("materias", [])
                          for c in m.get("comisiones", []) if c.get("tutor")})
        return {"sin_resultado": True,
                "aviso": f"No encontré a '{nombre}' en el reparto de {cat.get('cohorte')}. "
                         "Puede que la comisión no esté asignada todavía, o que el nombre "
                         "esté escrito distinto en el catálogo.",
                "tutores_del_catalogo": tutores}

    # Si el parcial matcheó a más de una persona distinta, no adivinamos: preguntamos.
    distintos = {_clave_nombre(f["tutor"]) for f in encontradas}
    if not exactos and len(distintos) > 1:
        return {"ambiguo": True,
                "aviso": f"'{nombre}' matchea con más de un tutor. Decime cuál es.",
                "candidatos": sorted({f["tutor"] for f in encontradas})}

    # Validación en vivo: el group_id del catálogo tiene que existir en el curso real.
    asignaciones, desfasadas = [], []
    grupos_por_curso: dict[int, dict[int, str]] = {}
    for f in encontradas:
        cid = f["course_id"]
        if cid not in grupos_por_curso:
            try:
                gs = await ws_api.descubrir_comisiones(_cli(), cid)
                grupos_por_curso[cid] = {g["group_id"]: g["nombre"] for g in gs}
            except Exception:  # noqa: BLE001
                grupos_por_curso[cid] = {}
        reales = grupos_por_curso[cid]
        if reales and f["group_id"] not in reales:
            desfasadas.append({"materia": f["materia"], "comision": f["nombre_campus"],
                               "group_id_catalogo": f["group_id"]})
            continue
        materia = next(m for m in cat["materias"] if m["course_id"] == cid)
        asignaciones.append({
            "materia": f["materia"],
            "course_id": cid,
            "comision": f["comision"],
            "nombre_campus": reales.get(f["group_id"], f["nombre_campus"]),
            "group_id": f["group_id"],
            "actividades_cursada": materia.get("actividades_cursada", []),
        })

    out = {"tutor": encontradas[0]["tutor"], "cohorte": cat.get("cohorte"),
           "asignaciones": asignaciones}
    if desfasadas:
        out["aviso"] = ("Estas comisiones del catálogo ya no existen en el campus — quedó "
                        "viejo. Mapealas en vivo con descubrir_comisiones.")
        out["desfasadas"] = desfasadas
    return out


# ---------- MENSAJERÍA PRIVADA ----------

@mcp.tool()
async def mensajes_pendientes(limite: int = 50) -> dict:
    """Mensajes privados de alumnos que esperan respuesta del tutor: aquellos cuya última
    palabra la tuvo el alumno. Es el "qué me falta contestar" de la mensajería.

    Separa `sin_leer` (el tutor ni las abrió — lo más urgente) de `leidas_sin_responder`
    (las vio y quedaron colgadas). Cada una trae `conversacion_id`: abrila con
    `leer_conversacion` para ver el hilo antes de contestar."""
    return await ws_api.mensajes_pendientes(_cli(), limite)


@mcp.tool()
async def leer_mensajes(limite: int = 15) -> dict:
    """Conversaciones privadas recientes: con quién, cuántos sin leer, el último mensaje
    y quién lo escribió. Vista de bandeja. Para el hilo completo, `leer_conversacion`."""
    return {"conversaciones": await ws_api.leer_mensajes(_cli(), limite)}


@mcp.tool()
async def leer_conversacion(conversacion_id: int, limite: int = 30) -> dict:
    """Mensajes de una conversación en orden cronológico. LEELA ANTES DE CONTESTAR:
    `leer_mensajes` solo trae el último mensaje, y responder sin el hilo lleva a repetir
    lo ya dicho o a contestar otra cosa. El `conversacion_id` sale de `leer_mensajes` o
    de `mensajes_pendientes`."""
    return await ws_api.leer_conversacion(_cli(), conversacion_id, limite)


@mcp.tool()
async def responder_mensaje(alumno: str, texto: str, confirmado: bool = False) -> dict:
    """Manda un mensaje privado a un alumno. `alumno` puede ser su email (exacto) o parte
    de su nombre, que se busca entre las conversaciones del tutor.

    ESCRITURA — le llega al alumno y no se puede borrar. Llamala primero SIN `confirmado`:
    devuelve un preview con el texto. Mostráselo al tutor y recién con su OK explícito
    repetí con `confirmado=true`. Nunca mandes un mensaje sin ese OK.

    OJO: por nombre solo encuentra a quien YA tiene conversación con el tutor. Para
    escribirle por primera vez a alguien, pasá su email (lo conseguís con
    `buscar_alumno`)."""
    return await ws_api.responder_mensaje(_cli(), alumno, texto, confirmado)


# ---------- FOROS ----------

@mcp.tool()
async def foros_pendientes(course_id: int, group_ids: list[int] | None = None,
                           solo_consultas: bool = True, incluir_avisos: bool = False) -> dict:
    """Consultas de foro del curso que el tutor TODAVÍA NO respondió. Es el "qué me falta
    contestar", el equivalente en foros de `pendientes_por_corregir`.

    Devuelve dos listas separadas, porque no son la misma urgencia:
      - `sin_responder`: nadie contestó (0 réplicas). Trae `responder_a_post` y un extracto
        del texto, así podés encadenar directo con `responder_foro`.
      - `respondio_otro`: contestó alguien más (un compañero), pero vos no.

    "Respondida por vos" = hay un post tuyo en la discusión. No se infiere por rol: el WS
    de foros no devuelve roles y adivinar quién es docente sería inventar.

    FILTRO POR COMISIÓN: los foros son de todo el curso (Prog I tiene 27 comisiones), así
    que sin filtrar ves los hilos de los alumnos de los demás tutores. Si no pasás
    `group_ids`, se toman los de `mis_datos` para ese curso; si tampoco hay, se revisa el
    curso entero y se avisa.

    QUÉ FOROS MIRA: por defecto solo los de consultas/dudas. Los de avisos son de una vía
    (`incluir_avisos=true` para verlos) y el de "buscar dupla/compañero" es entre alumnos:
    tiene cientos de hilos que ningún tutor debe contestar. `solo_consultas=false` mira
    todos. La lista de lo salteado vuelve en `foros_salteados`, para que no haya recortes
    silenciosos."""
    if not group_ids:
        await almacen.init_db()
        datos = await almacen.get_mis_datos() or {}
        for c in datos.get("cursos", []):
            if c.get("course_id") == course_id:
                group_ids = [x["group_id"] for x in c.get("comisiones_del_tutor", [])]
                break
    return await ws_api.foros_pendientes(_cli(), course_id, group_ids, solo_consultas,
                                         incluir_avisos)


@mcp.tool()
async def listar_foros(course_id: int) -> dict:
    """Foros del curso con `forum_id`, `cmid`, nombre, tipo y cuántas discusiones tiene
    cada uno. OJO: `forum_id` es lo que pide `leer_foro`; `cmid` es el módulo en el aula.
    No son intercambiables. (API REST.)"""
    return await ws_api.listar_foros(_cli(), course_id)


@mcp.tool()
async def leer_foro(forum_id: int, limite: int = 25) -> dict:
    """Discusiones de un foro (título, autor, cuántas réplicas, si podés responder).
    El `forum_id` sale de `listar_foros`. Para leer los mensajes de una discusión,
    seguí con `leer_discusion`. (API REST.)"""
    return await ws_api.leer_foro(_cli(), forum_id, limite)


@mcp.tool()
async def leer_discusion(discussion_id: int) -> dict:
    """Mensajes de una discusión, en orden cronológico: el primero es la consulta original
    y después las respuestas. Cada post trae su `post_id` — ese es el que necesitás para
    contestar con `responder_foro`. (API REST.)"""
    return await ws_api.leer_discusion(_cli(), discussion_id)


@mcp.tool()
async def responder_foro(post_id: int, mensaje: str, asunto: str | None = None,
                         confirmado: bool = False) -> dict:
    """Publica una respuesta en un foro, colgando del post `post_id` (sale de
    `leer_discusion` o del `responder_a_post` que da `foros_pendientes`).

    ESCRITURA — va al campus y lo ven los alumnos. Llamala primero SIN `confirmado`:
    devuelve un preview. Mostráselo al tutor y recién con su OK explícito repetí la
    llamada con `confirmado=true`. Nunca publiques sin ese OK."""
    return await ws_api.responder_foro(_cli(), post_id, mensaje, asunto, confirmado)


@mcp.tool()
async def crear_discusion(forum_id: int, asunto: str, mensaje: str,
                          group_id: int | None = None, confirmado: bool = False) -> dict:
    """Abre un tema NUEVO en un foro (una bienvenida, un aviso). Distinto de
    `responder_foro`, que cuelga de un post que ya existe: usá esta cuando no hay hilo
    del que colgarse.

    EL `group_id` DECIDE QUIÉN LO VE. En los foros de "Avisos de la comisión" el aviso
    llega SOLO a esa comisión; con `group_id=0` se publica para el curso entero —
    cientos de alumnos ajenos, y **no se puede borrar desde la API**. Sacá el id de
    `mis_datos` o `descubrir_comisiones`, nunca inventado. Para avisos de comisión NO uses
    "Avisos generales": ese foro no tiene grupos y siempre va al curso completo.

    Si no pasás `group_id` en un foro con grupos, o si no se puede determinar el alcance,
    la tool **se niega a publicar** y te dice por qué. No insistas mandando `group_id=0`
    para saltar el error: ese valor significa "quiero que lo vea el curso entero" y hay
    que preguntárselo al tutor primero.

    ESCRITURA — llamala primero SIN `confirmado`: devuelve un preview que dice a qué
    grupo va y **a cuántos alumnos llega**, verificado en vivo. Mostráselo al tutor —
    sobre todo ese número — y recién con su OK explícito repetí con `confirmado=true`."""
    return await ws_api.crear_discusion(_cli(), forum_id, asunto, mensaje, group_id, confirmado)


# ---------- ENCUENTROS ----------

@mcp.tool()
async def material_encuentro(course_id: int, url: str | None = None) -> dict:
    """El MATERIAL del encuentro vigente, para contestarle una duda a un alumno.

    En la modalidad de Encuentros el alumno mira cápsulas de video a su ritmo y pregunta
    en el foro durante la ventana horaria. Esta tool trae el **apunte interactivo** de esa
    unidad —escrito video por video, con los mismos ejemplos y gotchas que dio el
    docente— para que la respuesta salga del material y no de Python genérico.

    LLAMALA ANTES DE CONTESTAR, no después. Una respuesta de memoria en ese foro la ven
    las 27 comisiones y va firmada por el tutor.

    LA REGLA: se contesta con lo que está en el apunte. Si la duda cae afuera, decilo y
    pasásela al tutor — no la completes con lo que sabés de Python. Que suene bien no lo
    hace lo que el docente enseñó, y contradecirlo confunde más que no contestar.

    La URL del apunte NO está hardcodeada: sale del label de la sección de Encuentros del
    propio campus, y cambia con cada unidad. Si no puede determinar cuál es, devuelve
    `ok: false` con los candidatos — nunca cae al apunte de la unidad pasada.

    Pasá `url` sólo para forzar un apunte puntual (típico: el equipo todavía no actualizó
    el link en el campus y el encuentro de hoy es de otra unidad). Mirá `apunte.titulo`:
    ahí se ve de qué unidad es lo que bajaste.

    Read-only: no escribe nada en el campus. Para publicar la respuesta, `responder_foro`
    (que pide tu OK, como siempre)."""
    # Tenant-aware: antes leía MOODLE_URL de os.environ (proceso global), así que
    # después de un usar_campus seguía armando la URL del campus VIEJO. Misma
    # resolución que `_cli()`: el .env propio del tenant activo.
    base = (_credenciales_de(almacen.tenant_activo()).get("MOODLE_URL")
            or _BASE_DEFAULT).rstrip("/")
    return await encuentros.material_encuentro(_cli(), base, course_id, url)


@mcp.tool()
async def descubrir_cursos() -> list[dict]:
    """Descubre EN VIVO los cursos del campus donde el tutor está matriculado
    (course_id + nombre). Fallback de `aulas` si el catálogo no sirve. (API REST.)"""
    return await ws_api.descubrir_cursos(_cli())


@mcp.tool()
async def descubrir_comisiones(course_id: int) -> list[dict]:
    """Descubre EN VIVO los grupos de un curso, con su TIPO: `comision`, `regional` u `otro`.

    ⚠️ **Los grupos de un curso NO son todas comisiones.** Prog II devuelve 32 y sólo 15 lo
    son: las otras 17 son las regionales `R-*`, y además hay auxiliares (Grupo_2,
    Entrego_1er_examen…). Contarlos juntos da un número que parece del padrón y no lo es.
    Para trabajar con comisiones, filtrá `tipo == "comision"`.

    Paso 2 del mapeo. Los group_id que devuelve son los ÚNICOS válidos para
    guardar_mis_datos (no inventes números). (API REST.)"""
    return await ws_api.descubrir_comisiones(_cli(), course_id)


@mcp.tool()
async def listar_tareas(course_id: int) -> list[dict]:
    """Lista las tareas del curso (TPs, parciales, integrador) con su cmid (=assign_id) y
    título. Paso 3 del mapeo de "Mis datos" (descubre los assign_id). (API REST.)"""
    return await ws_api.listar_tareas(_cli(), course_id)


@mcp.tool()
async def guardar_mis_datos(datos: dict) -> dict:
    """Guarda "Mis datos" (la config que usan el snapshot y los tableros). Estructura:
    {"tutor": {"nombre": str}, "cursos": [{"course_id": int, "nombre": str,
    "comisiones_del_tutor": [{"comision": str, "group_id": int}],
    "tareas": [{"assign_id": str, "titulo": str}]}]}.

    Es una acción de CONFIGURACIÓN: mostrale el mapeo al tutor y guardá recién tras su OK.

    VALIDACIÓN (lección aprendida): cada group_id se coteja EN VIVO contra
    descubrir_comisiones del curso — así el modelo no puede inventar group_ids. Si alguno
    no existe en el curso real, NO se guarda nada y se devuelve el detalle de los inválidos
    con la lista de grupos reales para que corrijas.

    VALIDACIÓN 2 (lección aprendida, 2026-08-31): un curso con `comisiones_del_tutor` pero
    sin `tareas` se guarda igual pero deja al panel (dia.py/comision.py) mostrando esa
    comisión completamente VACÍA y sin ningún aviso — indistinguible de "al día". Por eso
    esta tool ahora EXIGE `tareas` no vacía en todo curso que tenga comisiones: si falta,
    NO guarda nada y pide correr `listar_tareas` primero.

    Si ningún curso guardado menciona "Programación" (el alcance de esta skill), se
    guarda igual pero se devuelve un `aviso` — puede ser una materia nueva legítima, o
    puede ser que al tutor se le haya escapado mapear su materia real (visto en vivo con
    un tutor de Bases de Datos al que nunca se le guardó su comisión de Programación).

    Si el tutor ya tiene un ID de ClickUp guardado (`guardar_clickup_id`), se conserva
    automáticamente al re-guardar Mis datos — esta tool nunca lo pisa."""
    if not isinstance(datos, dict) or not datos.get("cursos"):
        return {"error": "Estructura inválida: se espera un dict con al menos 'cursos'."}

    # Cotejar cada group_id contra la lista REAL de grupos del curso (API REST).
    invalidos: list[dict] = []
    for curso in datos.get("cursos", []):
        cid = curso.get("course_id")
        if cid is None:
            continue
        reales = await ws_api.descubrir_comisiones(_cli(), int(cid))
        if reales and isinstance(reales[0], dict) and reales[0].get("error"):
            return {"error": f"No pude validar el curso {cid}: {reales[0]['error']}"}
        ids_reales = {g.get("group_id") for g in reales}
        for c in curso.get("comisiones_del_tutor", []):
            gid = c.get("group_id")
            if gid is not None and gid not in ids_reales:
                invalidos.append({
                    "course_id": cid, "comision": c.get("comision"), "group_id": gid,
                    "grupos_reales": sorted(g for g in ids_reales if g is not None),
                })
    if invalidos:
        return {
            "error": "Hay group_id que no existen en el curso real; no guardé nada.",
            "invalidos": invalidos,
            "aviso": "Corregí los group_id usando SOLO los que devuelve descubrir_comisiones.",
        }

    # Un curso con comisiones pero sin tareas rompe el panel en silencio (ver docstring).
    sin_tareas = [
        {"course_id": curso.get("course_id"), "nombre": curso.get("nombre")}
        for curso in datos.get("cursos", [])
        if curso.get("comisiones_del_tutor") and not curso.get("tareas")
    ]
    if sin_tareas:
        return {
            "error": "Hay cursos con comisiones pero sin 'tareas'; no guardé nada.",
            "sin_tareas": sin_tareas,
            "aviso": "Corré listar_tareas(course_id) para cada uno de estos cursos y "
                     "sumá el resultado a 'tareas' antes de guardar — si no, esas "
                     "comisiones van a aparecer vacías en el panel, sin ningún aviso.",
        }

    # No pisar un link de ClickUp ya guardado: esta tool solo administra tutor/cursos,
    # "clickup" lo administra guardar_clickup_id aparte.
    if "clickup" not in datos:
        existentes = await almacen.get_mis_datos()
        if existentes and "clickup" in existentes:
            datos["clickup"] = existentes["clickup"]

    await almacen.set_mis_datos(datos)
    salida = {"ok": True, "cursos": len(datos.get("cursos", []))}

    # Aviso suave (no bloquea, ya se guardó): mismo chequeo de scope que usa `mis_datos`
    # al leer. Puede ser legítimo (materia nueva); puede ser que falte mapear la materia
    # real del tutor.
    avisos_config = _avisos_config_incompleta(datos.get("cursos", []))
    if avisos_config:
        salida["aviso"] = " ".join(avisos_config)
    return salida


@mcp.tool()
async def guardar_clickup_id(clickup_user_id: str, nombre_clickup: str, email: str = "") -> dict:
    """Guarda el ID numérico de ClickUp del tutor dentro de "Mis datos", bajo la clave
    "clickup". Llamala UNA VEZ, después de resolver quién es y de que el TUTOR CONFIRME
    que es él/ella (nunca lo des por sentado por el nombre solo: puede haber tocayos en
    el workspace, y el nombre de cátedra no siempre coincide con el de ClickUp — ej.
    "Neyén Bianchi Medina" en comisiones.json vs. "Neyén Bianchi" en ClickUp).

    Para resolver: `mcp__clickup__clickup_find_member_by_name` NO hace matching difuso
    (verificado: "Neyén Bianchi Medina" devuelve null, solo el nombre EXACTO de ClickUp
    encuentra algo) — si devuelve null, caé a `mcp__clickup__clickup_get_workspace_members`
    y buscá la coincidencia parcial vos mismo antes de confirmar con el tutor.

    Requiere que ya exista "Mis datos" (Paso 0 de Moodle corrido antes): si no hay
    nada guardado todavía, no hay dónde anexar el ID y devuelve error.

    A diferencia de `guardar_mis_datos`, esta tool NO valida nada contra Moodle —
    el ID de ClickUp no tiene relación con group_id ni curso. Solo agrega/actualiza
    la clave "clickup" sin tocar "tutor" ni "cursos"."""
    import datetime

    datos = await almacen.get_mis_datos()
    if not datos:
        return {"error": "Todavía no hay 'Mis datos' guardado. Corré primero el Paso 0 "
                          "de Moodle (mi_comision o el mapeo manual)."}
    datos["clickup"] = {
        "user_id": str(clickup_user_id),
        "nombre_clickup": nombre_clickup,
        "email": email,
        "resuelto_at": datetime.datetime.now().isoformat(),
    }
    await almacen.set_mis_datos(datos)
    return {"ok": True, "clickup": datos["clickup"]}


# ---------- REFRESCO DE TABLEROS (snapshot on-demand) ----------
@mcp.tool()
async def actualizar_tableros() -> dict:
    """Actualiza AHORA los tableros/caché corriendo el snapshot de TODOS los cursos
    mapeados en "Mis datos". LEE del campus (API REST) + ESCRIBE la caché LOCAL que usa
    buscar_alumno: NO toca el campus de Moodle.

    Cuándo: justo después de un guardar_mis_datos exitoso, o cuando el tutor pide
    'refrescá mis datos / fijate cómo vengo / verificá mis pendientes'.

    PUEDE TARDAR (varios requests): avisale al tutor antes. Devuelve un resumen
    (comisiones, entregas, alumnos, pendientes de corregir). Si todavía no mapeó
    comisiones, devuelve omitido=True. Si excede el techo de tiempo, timeout=True."""
    await almacen.init_db()
    try:
        res = await asyncio.wait_for(
            snapshot.tomar_snapshot(_cli()), timeout=_REFRESCO_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        return {
            "error": True, "timeout": True,
            "mensaje": f"El refresco tardó más de {_REFRESCO_TIMEOUT_S}s y se cortó.",
            "que_quedo": "Se guarda por CURSO, no por tarea: desde que el relevamiento es "
                         "paralelo, las filas de un curso se escriben recién cuando "
                         "terminan TODAS sus tareas. Los cursos que alcanzaron a terminar "
                         "quedaron guardados enteros; el que estaba a mitad de camino "
                         "cuando se cortó no guardó nada. El padrón de alumnos y las "
                         "entregas se escriben al final de todo, así que ESOS siguen "
                         "mostrando la corrida anterior. No los leas como si fueran de hoy.",
            "que_hacer": "Reintentá: la segunda corrida suele entrar. Si vuelve a cortarse, "
                         "bajá la concurrencia con SNAPSHOT_CONCURRENCIA=3 (el campus puede "
                         "estar rechazando requests) o subí el techo con "
                         "REFRESCO_TIMEOUT_S=600. Y si sólo necesitás saber quién entregó "
                         "una tarea puntual, usá entregas_tarea, que responde en segundos.",
        }
    if res.get("omitido"):
        return {
            "ok": True, "omitido": True,
            "mensaje": "No hay comisiones mapeadas en 'Mis datos' todavía: corré el mapeo "
                       "(descubrir_cursos -> descubrir_comisiones -> listar_tareas -> "
                       "guardar_mis_datos) antes de refrescar los tableros.",
        }
    filas = await almacen.ultimo_snapshot()
    pendientes = sum(int(f.get("pendientes") or 0) for f in filas)
    comisiones = len({f.get("comision") for f in filas if f.get("comision")})
    return {
        "ok": True, "fecha": res.get("fecha"), "comisiones": comisiones,
        "tareas_relevadas": res.get("filas"), "entregas": res.get("entregas"),
        "alumnos": res.get("alumnos"), "pendientes_por_corregir": pendientes,
        "mensaje": f"Tableros actualizados: {comisiones} comisiones, "
                   f"{res.get('entregas')} entregas, {res.get('alumnos')} alumnos, "
                   f"{pendientes} pendientes de corregir.",
    }


# ---------- LECTURA ----------
@mcp.tool()
async def sumario(assign_id: str, group_id: int = 0) -> dict:
    """Conteo OFICIAL de Moodle de una tarea: participantes / enviados / pendientes por
    calificar. Es el número CONFIABLE para 'cuántos me faltan corregir'. group_id=0 = todo
    el curso; si no, el group_id de la comisión (el de descubrir_comisiones / "Mis datos",
    no un número inventado). Liviano: una request. (API REST.)"""
    return await ws_api.sumario(_cli(), assign_id, group_id)


@mcp.tool()
async def pendientes_por_corregir(assign_id: str, group_id: int = 0) -> dict:
    """Alumnos que ENTREGARON una tarea y siguen SIN NOTA. group_id=0 = todo el curso.

    Devuelve DOS motivos distintos, y el segundo no lo muestra ninguna otra vista:
      - `requiere_correccion`: la cola normal de corrección (`requiregrading`).
      - `calificado_sin_nota`: se guardó la devolución pero la calificación quedó vacía.
        Moodle los da por corregidos, así que salen de la cola SIN nota y nadie los
        espera. Si aparece alguno, hay que cargarle la nota a mano.
    (API REST: mod_assign_list_participants + mod_assign_get_grades.)"""
    return await ws_api.pendientes_tarea(_cli(), assign_id, group_id)


@mcp.tool()
async def entregas_tarea(assign_id: str, group_id: int = 0) -> dict:
    """Padrón COMPLETO de una tarea EN VIVO: TODOS los alumnos con nombre, email, estado
    ("Sin entrega" / "Enviado para calificar" / "Calificado") y nota. group_id=0 = todo el
    curso; si no, el group_id de la comisión (el de "Mis datos", no un número inventado).

    Usá ESTA cuando el tutor pregunte "quiénes entregaron / quiénes me deben la tarea X":
    son dos requests y responde en segundos. NO corras actualizar_tableros para eso — el
    snapshot recorre todas tus comisiones × todas tus tareas y tarda minutos.

    Devuelve `sin_entrega` aparte de `pendientes_por_corregir` a propósito: el que no
    entregó nada tiene 0 para corregir y NO está al día, es el que más debe. Lee la nota
    por texto de la escala (respeta el Aprobado/Desaprobado invertido). (API REST.)"""
    return await ws_api.entregas_tarea(_cli(), assign_id, group_id)


@mcp.tool()
async def buscar_alumno(texto: str, traza: bool = False) -> dict:
    """Busca un alumno por NOMBRE (o email) EN VIVO en las comisiones del tutor y devuelve
    quién es: comisión, email, userid y hace cuántos días que no entra al campus.

    NO necesita snapshot previo ni caché: una request por comisión (~1 s), siempre fresco.
    Insensible a mayúsculas y acentos. Si el nombre matchea a varias personas las devuelve
    todas, para que el tutor elija en vez de que se adivine.

    `traza=True` agrega qué entregó y qué nota sacó en CADA tarea del curso. Eso son dos
    requests por tarea, así que tarda bastante más: pedilo sólo cuando haga falta la
    situación académica completa, y sólo resuelve si la búsqueda dio UNA sola persona.
    (API REST: core_enrol_get_enrolled_users, filtrado a rol alumno.)"""
    await almacen.init_db()
    datos = await almacen.get_mis_datos()
    if not datos:
        return {
            "error": "No tengo tus cursos mapeados, así que no sé en qué comisiones buscar.",
            "siguiente_paso": "Corré mi_comision(tu nombre) y guardá con guardar_mis_datos.",
        }

    cursos = datos.get("cursos", [])
    res = await ws_api.buscar_alumnos(_cli(), texto, cursos)
    hallados = res.get("coincidencias") or []
    if not traza or not hallados:
        return res

    if len(hallados) > 1:
        # Misma disciplina que mi_comision: con varios candidatos no se adivina.
        res["aviso_traza"] = (
            f"'{texto}' matcheó a {len(hallados)} personas: no traigo la traza de todas. "
            "Repetí la búsqueda con el nombre completo o el email de la que te interesa."
        )
        return res

    alumno = hallados[0]
    tareas = next(
        (c.get("tareas", []) for c in cursos if c.get("course_id") == alumno.get("course_id")),
        [],
    )
    if not tareas:
        res["aviso_traza"] = (
            f"No tengo tareas mapeadas para {alumno.get('curso')}, así que no puedo armar "
            "la traza. Corré listar_tareas y sumalas con guardar_mis_datos."
        )
        return res

    alumno["traza"] = await ws_api.traza_alumno(_cli(), alumno, tareas)
    return res


@mcp.tool()
async def alumnos_en_riesgo(course_id: int | None = None, group_id: int = 0,
                            dias_alerta: int = 14, dias_aviso: int = 7) -> dict:
    """Quiénes están abandonando, ANTES de que se note. Cruza dos señales que ya estaban
    en el campus y que ninguna vista junta: hace cuántos días que no abre ESTA materia +
    cuántas tareas seguidas dejó de entregar.

    **Los días son SIN ABRIR ESTA MATERIA**, no sin entrar al campus. Son dos relojes
    distintos y hasta la v1.13.0 esto leía el equivocado: el alumno que entra todos los días
    para otra materia y nunca abrió la tuya daba 0 días → verde → y los verdes no se
    devuelven, así que era invisible. Cada fila trae ahora los dos relojes
    (`dias_sin_abrir_la_materia`, `dias_sin_entrar_al_campus`) más `estado_aula`.

    🔴 rojo: 2+ tareas SEGUIDAS sin entregar · o >`dias_aviso` días sin abrir la materia
       entrando al campus igual (**eligió no entrar**: la señal más fuerte) · o
       >`dias_alerta` días sin abrirla con al menos una sin entregar · o nunca la abrió y
       encima dejó de entregar o tampoco pisa el campus.
    🟡 amarillo: >`dias_aviso` días sin abrir la materia · o la última tarea sin entregar ·
       o nunca la abrió pero todavía no venció nada (puede haberse matriculado recién: NO lo
       presentes como abandono confirmado).
    ⚪ sin_datos: no se pudo leer su acceso a la materia. **No es verde**, es "no sabemos".
    (Los verdes no se devuelven: la lista es para actuar, no para leer 30 nombres.)

    Sin `course_id` toma el primero de "Mis datos"; con `group_id=0` recorre TODAS tus
    comisiones de ese curso. Va en vivo, sin depender del snapshot.

    La racha se cuenta desde la ÚLTIMA tarea, no sobre el total: quien no entregó el TP1
    hace tres meses pero viene entregando los últimos cinco no está abandonando; quien
    dejó de entregar los dos últimos, sí.

    OJO con la racha en cursos SIN fecha de entrega: si las actividades no tienen `duedate`
    (medido en Prog I: las 10 de cierre no la tienen), "sin entrega" no se puede distinguir
    de "todavía no vencía", y la racha marca a TODO el padrón — 94 de 94 alumnos en rojo. En
    ese caso la señal que sirve es el reloj de la materia, y conviene mirar
    `sin_entrar_al_aula`. Si ves el padrón entero en rojo por racha, decíselo al tutor en vez
    de presentarlo como que todos están abandonando."""
    await almacen.init_db()
    datos = await almacen.get_mis_datos()
    if not datos:
        return {"error": "No tengo tus cursos mapeados.",
                "siguiente_paso": "Corré mi_comision(tu nombre) y guardá con guardar_mis_datos."}

    cursos = datos.get("cursos", [])
    curso = (next((c for c in cursos if c.get("course_id") == course_id), None)
             if course_id else (cursos[0] if cursos else None))
    if curso is None:
        return {"error": f"No encontré el curso {course_id} en tus datos.",
                "cursos_disponibles": [c.get("course_id") for c in cursos]}

    # Sólo las actividades de cierre: el Integrador y los parciales tienen otra dinámica y
    # contarlos en la racha marcaba a media comisión sin que hubiera pasado nada.
    todas = curso.get("tareas", [])
    tareas = [t for t in todas if ws_api.es_actividad_de_cierre(t.get("titulo", ""))]
    excluidas = [t.get("titulo", "") for t in todas if t not in tareas]
    if not tareas:
        return {"error": "No encontré actividades de cierre de unidad en este curso.",
                "tareas_mapeadas": [t.get("titulo") for t in todas],
                "siguiente_paso": "Revisá el mapeo con listar_tareas / guardar_mis_datos."}

    grupos = ([{"comision": "(pedida)", "group_id": group_id}] if group_id
              else curso.get("comisiones_del_tutor", []))
    if not grupos:
        return {"error": f"No tenés comisiones mapeadas en {curso.get('nombre')}."}

    por_comision, avisos = [], []
    for g in grupos:
        r = await ws_api.alumnos_en_riesgo(
            _cli(), curso["course_id"], g["group_id"], tareas, dias_alerta, dias_aviso)
        if r.get("error"):
            avisos.append(f"{g['comision']}: {r['error']}")
            continue
        avisos.extend(r.get("_meta", {}).get("avisos", []))
        por_comision.append({"comision": g["comision"], **r})

    total_rojo = sum(c["rojo"] for c in por_comision)
    total_amarillo = sum(c["amarillo"] for c in por_comision)
    total_sin_datos = sum(c.get("sin_datos", 0) for c in por_comision)
    salida = {
        "ok": True,
        "curso": curso.get("nombre"),
        "course_id": curso["course_id"],
        "rojo": total_rojo,
        "amarillo": total_amarillo,
        "sin_datos": total_sin_datos,
        "comisiones": por_comision,
        "_meta": {
            "fuente": "vivo",
            "tareas_consideradas": [t.get("titulo") for t in tareas],
            "tareas_excluidas": excluidas,
            "nota_criterio": ("La racha se cuenta sólo sobre actividades de cierre de "
                              "unidad. El Integrador y los parciales quedan afuera: son "
                              "otra dinámica y contarlos marcaba a media comisión."),
            "degradado": bool(avisos),
            "avisos": avisos,
        },
    }
    con_alumnos = [c for c in por_comision if c.get("alumnos_totales")]
    if not con_alumnos:
        # Arranque de cuatrimestre: ninguna comisión tiene matriculados todavía.
        vacias = ", ".join(c["comision"] for c in por_comision) or "(ninguna)"
        salida["sin_alumnos"] = True
        salida["resumen"] = (f"Ninguna de tus comisiones ({vacias}) tiene alumnos "
                             "matriculados todavía. Eso NO es 'están todos al día': no hay "
                             "a quién evaluar. Volvé a correrlo cuando arranque la cursada.")
    elif not total_rojo and not total_amarillo and not total_sin_datos:
        n = sum(c["alumnos_totales"] for c in con_alumnos)
        salida["resumen"] = (f"Nadie en riesgo: los {n} alumnos vienen abriendo la materia y "
                             "entregando.")
    else:
        n = sum(c["alumnos_totales"] for c in con_alumnos)
        salida["resumen"] = (f"{total_rojo} en rojo y {total_amarillo} en amarillo. "
                             "Los rojos son los que hay que contactar esta semana.")
        if total_sin_datos:
            # Un `sin_datos` que no se nombra es un alumno que desaparece de la lista.
            salida["resumen"] += (f" Y {total_sin_datos} sin datos: de ésos no se pudo leer el "
                                  "acceso a la materia, no es que estén al día.")
        if total_rojo >= n:
            # El padrón entero en rojo no es información: es una alarma saturada. Suele pasar
            # cuando las actividades no tienen fecha de entrega y la racha cuenta como
            # abandono lo que todavía no vencía (medido en Prog I: 94 de 94).
            salida["alarma_saturada"] = True
            salida["resumen"] += (f" OJO: están los {n} alumnos en rojo, o sea TODOS. Eso no "
                                  "distingue a nadie. Revisá si las actividades tienen fecha "
                                  "de entrega: sin `duedate`, la racha cuenta como abandono lo "
                                  "que todavía no vencía. Para este caso mirá "
                                  "`sin_entrar_al_aula`, que no depende de vencimientos.")
    return salida


@mcp.tool()
async def sin_entrar_al_aula(course_id: int | None = None, group_id: int = 0,
                             dias_desenganche: int = 7) -> dict:
    """Quién dejó de abrir ESTA materia, ordenado por hace cuánto. Desenganche por materia.

    OJO con la diferencia, que es todo el punto de esta tool: `dias_sin_entrar` (de
    `buscar_alumno` y `alumnos_en_riesgo`) son días sin entrar al CAMPUS. Acá son días sin
    abrir ESTA materia. Son dos relojes distintos y el campus no avisa cuál estás mirando:
    el que entra todos los días para otra materia y nunca abre la tuya figura con
    `dias_sin_entrar: 0` — o sea, al día — estando desaparecido.

    Cada fila trae los DOS relojes y una frase (`detalle`). Leé el `detalle`, no el número
    pelado. El campo que decide a quién escribir es
    `entra_al_campus_sin_abrir_la_materia`: entró al campus en los últimos
    `dias_desenganche` días y hace `dias_desenganche`+ que no abre esta materia. Ése no
    perdió la contraseña, eligió no entrar. Presentá ésos primero.

    `estado_aula` tiene TRES valores y no se pueden confundir:
    - `abrio` → hay dato: `dias_sin_abrir_la_materia`.
    - `nunca_abrio` → nunca la abrió. NO lo presentes como abandono confirmado: puede
      haberse matriculado esta semana, y esto no ve la fecha de matriculación.
    - `sin_dato` → no se pudo leer. NO es "nunca abrió". Si aparece, el relevamiento está
      incompleto y hay que decirlo.

    El que hace 40 días que no abre la materia Y tampoco pisa el campus sale en la lista
    (arriba, por días) pero NO marcado para contactar: ése no eligió otra materia, no está
    en ninguna parte. Es otro problema y no se mezcla.

    Sin `course_id` toma el primero de "Mis datos"; con `group_id=0` recorre TODAS tus
    comisiones de ese curso. Va en vivo, una request por comisión.

    Por qué existe además de `alumnos_en_riesgo`: aquélla cuenta rachas de actividades sin
    entregar, y al principio del cuatrimestre —cuando no venció nada— marca al padrón entero.
    El reloj del curso sirve desde el día uno."""
    await almacen.init_db()
    datos = await almacen.get_mis_datos()
    if not datos:
        return {"error": "No tengo tus cursos mapeados.",
                "siguiente_paso": "Corré mi_comision(tu nombre) y guardá con guardar_mis_datos."}

    cursos = datos.get("cursos", [])
    curso = (next((c for c in cursos if c.get("course_id") == course_id), None)
             if course_id else (cursos[0] if cursos else None))
    if curso is None:
        return {"error": f"No encontré el curso {course_id} en tus datos.",
                "cursos_disponibles": [c.get("course_id") for c in cursos]}

    grupos = ([{"comision": "(pedida)", "group_id": group_id}] if group_id
              else curso.get("comisiones_del_tutor", []))
    if not grupos:
        return {"error": f"No tenés comisiones mapeadas en {curso.get('nombre')}."}

    por_comision, avisos = [], []
    for g in grupos:
        r = await ws_api.sin_entrar_al_aula(_cli(), curso["course_id"], g["group_id"],
                                            dias_desenganche)
        if r.get("error"):
            avisos.append(f"{g['comision']}: {r['error']}")
            continue
        avisos.extend(r.get("_meta", {}).get("avisos", []))
        por_comision.append({"comision": g["comision"], **r})

    if not por_comision:
        # Todas las comisiones fallaron: no hay nada que mostrar y hay que decirlo así,
        # nunca devolver una lista vacía que se lea como "no hay desenganchados".
        return {"error": "No pude relevar ninguna de tus comisiones, así que no sé quién "
                         "dejó de abrir la materia.",
                "curso": curso.get("nombre"), "course_id": curso["course_id"],
                "_meta": {"fuente": "vivo", "degradado": True, "avisos": avisos}}

    activos = sum(c["entran_al_campus_sin_abrir_la_materia"] for c in por_comision)
    desenganchados = sum(c["desenganchados"] for c in por_comision)
    nunca = sum(c["nunca_abrieron"] for c in por_comision)
    sin_dato = sum(c["sin_dato"] for c in por_comision)
    relevados = sum(c["alumnos_totales"] for c in por_comision)

    salida = {
        "ok": True,
        "curso": curso.get("nombre"),
        "course_id": curso["course_id"],
        "alumnos_relevados": relevados,
        "entran_al_campus_sin_abrir_la_materia": activos,
        "desenganchados": desenganchados,
        "nunca_abrieron": nunca,
        "sin_dato": sin_dato,
        "comisiones": por_comision,
        "_meta": {
            "fuente": "vivo",
            "dias_desenganche": dias_desenganche,
            "comisiones_relevadas": len(por_comision),
            "comisiones_pedidas": len(grupos),
            "nota_reloj": ("`dias_sin_abrir_la_materia` sale de `lastcourseaccess` (reloj "
                           "del curso). `dias_sin_entrar_al_campus` sale de `lastaccess` "
                           "(reloj del sitio). Moodle actualiza los dos con bandas muertas "
                           "de 60 s, así que diferencias de segundos son normales: la brecha "
                           "se cuenta en días enteros."),
            "degradado": bool(avisos) or len(por_comision) < len(grupos),
            "avisos": avisos,
        },
    }
    con_alumnos = [c for c in por_comision if c.get("alumnos_totales")]
    if not con_alumnos:
        vacias = ", ".join(c["comision"] for c in por_comision) or "(ninguna)"
        salida["sin_alumnos"] = True
        salida["resumen"] = (f"Ninguna de tus comisiones ({vacias}) tiene alumnos "
                             "matriculados todavía. Eso NO es 'están todos entrando': no hay "
                             "a quién medir.")
    elif activos:
        salida["resumen"] = (f"{activos} de {relevados} alumnos entran al campus pero hace "
                             f"{dias_desenganche}+ días que NO abren esta materia. Son los "
                             "que hay que contactar, y son los que `dias_sin_entrar` mostraba "
                             "como si estuvieran al día.")
    elif desenganchados:
        salida["resumen"] = (f"Nadie está entrando al campus sin abrir la materia. Quedan "
                             f"{desenganchados} desenganchados ({nunca} nunca la abrieron), "
                             "pero tampoco pisan el campus: es otro problema — o se les "
                             "cayó el acceso, o recién se matricularon.")
    else:
        # `relevados - sin_dato`: el que vino sin el dato no se relevó, y contarlo acá diría
        # "está todo al día" sobre alguien de quien no sabemos nada.
        salida["resumen"] = (f"Los {relevados - sin_dato} alumnos relevados abrieron la "
                             f"materia hace menos de {dias_desenganche} días. Nadie "
                             "desenganchado.")
        if sin_dato:
            salida["resumen"] += (f" OJO: {sin_dato} de {relevados} quedaron sin relevar: "
                                  "esto no cubre a todo el padrón.")
    if sin_dato:
        salida["aviso"] = (f"{sin_dato} alumno(s) vinieron sin el dato de último acceso a la "
                           "materia: quedaron como `sin_dato`, NO como 'nunca abrió'. El "
                           "relevamiento está incompleto.")
    return salida


@mcp.tool()
async def ver_entrega(assign_id: str, email: str, max_chars: int = 20000) -> dict:
    """Muestra QUÉ entregó un alumno: baja la entrega y devuelve su contenido.

    ES EL PASO PREVIO OBLIGATORIO A CALIFICAR A MANO. Sin esto sólo se podía cargar una
    nota sin haber visto el trabajo, que es exactamente lo que la regla de "verificar en
    vivo, nunca inventar" prohíbe — pero aplicada a lo que más importa: el legajo de una
    persona.

    Descomprime los .zip (la forma en que se entrega en la TUP) y devuelve el texto de los
    archivos de código. Los binarios (PDF, imágenes) se bajan igual y viene la `ruta` local
    para abrirlos aparte. `max_chars` reparte el presupuesto de texto entre los archivos.

    Read-only: no escribe nada en el campus. Para corregir con IA en vez de a mano está
    `corregir_con_active_ia` (usa la rúbrica oficial, si la unidad tiene una cargada).
    """
    destino = str(Path(almacen.salidas_dir()) / "entregas" / str(assign_id))
    return await ws_api.leer_entrega(_cli(), assign_id, email, destino, max_chars)


# ---------- AUDITORÍA DE AULA (read-only, presencia/ausencia) ----------
@mcp.tool()
async def auditar_aula(course_id: int, materia: str = "", evaluador: str = "",
                       rol: str = "", con_navegador: bool = False,
                       unidad: int | None = None) -> dict:
    """Audita cómo está ARMADA un aula (no "quién entregó qué"): releva el curso por API
    REST, testea los links (rotos / piden login / a otro campus / con espacio), arma la
    matriz de unidades × 9 componentes en modo PRESENCIA/AUSENCIA y detecta hallazgos
    (componente faltante sistemático, hueco en un patrón, instancia extraordinaria visible,
    fechas de ciclos viejos). Escribe un worksheet .md en salidas/ y devuelve un resumen.

    PREGUNTÁ QUÉ UNIDAD antes de correr: un tutor audita SU unidad, no las 10. Ofrecé el
    número de unidad (1-10) o "todo el aula". Pasá `unidad=N` para auditar solo esa unidad
    (más rápido: testea solo sus links y cuestionarios). Sin `unidad`, releva el aula entera.
    Si la unidad no existe, devuelve `unidades_disponibles` para reintentar.

    REGLA (la misma de la skill): el agente verifica presencia/ausencia/consistencia, NO
    calidad. Puntaje 0=ausente, 3=presente, vacío=sin dato (no se infiere). La calidad y
    los puntajes finos los pone el evaluador humano sobre el borrador. La hoja EQUIPO se
    deja vacía a propósito: un agente no evalúa personas.

    `con_navegador=True` suma el PASO 2 (Playwright): loguea por navegador, cuenta las
    preguntas de cada cuestionario (mini=4 / autoeval=10 según la planilla) y clasifica las
    apps Google (NotebookLM/Colab) en abren / NO verificables (caen en login: eso no prueba
    que existan, un enlace borrado da la misma pantalla). Requiere Playwright instalado
    (`pip install playwright && playwright install chromium`); si no está, se saltea con
    aviso y la auditoría por API igual corre. Tarda más (abre una página por cuestionario).

    Es READ-ONLY sobre Moodle: no escribe nada en el campus, solo el worksheet local.
    `course_id` sale de `aulas` / `descubrir_cursos`. `materia` es el nombre para el
    encabezado (ej. 'Programación 2'); `evaluador`/`rol` son opcionales (dejá `evaluador`
    vacío para firmar como Celda de Control de Calidad)."""
    return await auditoria.auditar_aula(
        _cli(), course_id, almacen.salidas_dir(), materia=materia, evaluador=evaluador,
        rol=rol, con_navegador=con_navegador, unidad=unidad,
        tenant_id=almacen.tenant_activo())


# ---------- VISTA DEL PROFESOR (todas las comisiones a la vez) ----------

async def _cmids_del_curso(course_id: int, cmids: list[str] | None) -> list[str]:
    """Tareas a mirar: las que se pidieron, o TODAS las del curso descubiertas en vivo.

    En vivo y no de "Mis datos" a propósito: el profesor mira un curso entero, y el mapeo
    local es el de SUS comisiones como tutor — usarlo acá le escondería tareas del curso
    que él no tiene mapeadas. `listar_tareas` sale del `_assign_map`, que es una request.
    """
    if cmids:
        return [str(c) for c in cmids]
    return [str(t["id"]) for t in await ws_api.listar_tareas(_cli(), course_id)]


@mcp.tool()
async def reporte_coordinacion(course_id: int, cmids: list[str] | None = None,
                              incluir_foros: bool = True, pdf: bool = True,
                              anexo: bool = False,
                              dias_desenganche: int = 7,
                              unidades: str | None = None) -> dict:
    """La vista del PROFESOR sobre el TRABAJO DE CORRECCIÓN del curso entero, cortada de tres
    maneras: por comisión, por tutor y por actividad. Con `pdf=True` (por defecto) escribe el
    PDF de coordinación y devuelve la ruta en `pdf.archivo`.

    **El PDF son TRES páginas** y está pensado para leerse todos los días en dos minutos: KPIs
    y puntos de atención, el desglose por comisión y por tutor, y los alumnos que dejaron de
    abrir la materia. El detalle largo —una fila por cada entrega esperando, por actividad,
    hilo por hilo— sale con `anexo=True`; en un curso movido esas listas solas se comen tres
    páginas y el informe deja de leerse. El dict SIEMPRE trae todo, tenga anexo o no.

    La tabla por tutor incluye una columna **Nota** que lee la cola por la ESPERA y no por el
    volumen ("cola fresca", "espera de 3,1 d, priorizar"). Es la única interpretación del
    informe y la regla es pareja para todos: describe el estado de una cola, no a la persona.

    **`unidades` agrega la columna RETRASO y hay que PREGUNTÁRSELO AL TUTOR** (`"3-5"`, o `"3"`
    para una sola). Es lo que separa dos casos que en la tabla se ven idénticos: el alumno que no
    entra Y no entregó nada, y el que no entra **porque ya entregó todo** — a ése no hay que
    llamarlo. El campus NO dice qué unidad se cursa (verificado: ninguna actividad de cierre
    tiene fecha de apertura), así que sin el rango la columna no sale y se declara por qué.

    El bloque de alumnos usa el reloj de LA MATERIA (`dias_desenganche`, 7 días por defecto),
    nunca el del campus — ver `informes_nexos` para por qué el corte por campus pierde a la
    mayoría. Va la lista de los 25 más urgentes y el total; el listado completo con mails y el
    nexo de cada sede sigue siendo `informes_nexos`, y este informe no lleva mails.

    Es el complemento de `informes_nexos`, que habla sólo de ALUMNOS. Están separados a
    propósito: juntos en un mismo documento, un tutor leía la lista de alumnos desenganchados
    como parte de su evaluación. Si te piden "quién no entra a la materia", NO es esta tool.

    Tres cortes de la misma información:
    - **por comisión** (`filas`): tutor, alumnos, entregadas, corregidas, sin corregir,
      calificado sin nota, espera máxima, demora mediana, consultas de foro sin responder.
    - **por tutor** (`por_tutor`): la carga sumando SUS comisiones, porque varios llevan dos y
      su cola real no está en ninguna fila. Ordenado por la espera más antigua.
    - **por actividad** (`por_actividad`): la misma cola cortada al revés. Cuando una actividad
      se atrasa en varias comisiones a la vez el problema suele ser de la consigna o del
      calendario, y por comisión eso no se ve.

    **Espera máx es lo accionable; volumen NO es atraso.** Una cola de 15 entregas de ayer está
    al día; una sola entrega esperando tres semanas no. Al presentarlo, ordená por espera.

    **Se audita el TRABAJO, nunca se califica a la PERSONA.** Nombrar al tutor es ruteo —a quién
    llamar— y va. Un ranking, un podio o un puntaje de tutores NO va, ni en la tabla ni en cómo
    lo contás: las comisiones no son comparables entre sí (distinto tamaño, consigna y cohorte),
    así que comparar personas convierte un hecho en un juicio. Lo mismo con los veredictos: no
    escribas "estado general: sano" ni equivalentes.

    **Leé `sin_dato` de cada fila antes de concluir.** Distingue "0 porque está al día" de "0
    porque la comisión está vacía", "porque nadie entregó todavía" o "porque no pude leer".

    `actividades_sin_fecha_de_entrega` importa: sin `duedate` no se puede distinguir "no
    entregó" de "todavía no vencía" (en Prog I no la tiene ninguna). Si eso está, cuidado con
    leer los faltantes como abandono.

    Sin `cmids` mira todas las tareas del curso. READ-ONLY sobre el campus: lo único que escribe
    es el PDF, local, en `salidas/`."""
    from datetime import date

    datos = await panorama.reporte_coordinacion(
        _cli(), course_id, await _cmids_del_curso(course_id, cmids), incluir_foros,
        dias_desenganche=dias_desenganche, unidades=unidades)
    if datos.get("error") or not pdf:
        return datos
    try:
        nombre = await panorama._nombre_del_curso(_cli(), course_id)
        datos["pdf"] = informes.reporte_coordinacion_pdf(
            datos, str(Path(almacen.salidas_dir()) / "informes"),
            materia=nombre or "", fecha=date.today().isoformat(), anexo=anexo)
    except Exception as e:  # noqa: BLE001
        # Que falle el render no puede tirar el relevamiento del curso entero.
        datos["pdf"] = {"error": f"No pude escribir el PDF: {type(e).__name__}: {e}"}
    return datos


@mcp.tool()
async def demora_correccion(course_id: int, cmids: list[str] | None = None) -> dict:
    """Cuánto ESPERA un alumno desde que entrega hasta que le cargan la nota, por comisión.
    Es la pregunta que el conteo de pendientes no contesta: 20 entregas de ayer están bien,
    3 esperando hace tres semanas están mal. El conteo no las distingue; esto sí.

    Devuelve dos bloques que no hay que mezclar:
      - `demora_*`  → sobre entregas YA corregidas. Es historia.
      - `espera_*`  → sobre las que siguen sin corregir, contra hoy. Es lo accionable.

    Misma regla de lectura que `reporte_coordinacion`: son hechos por comisión, no un puntaje
    del tutor. `sin_dato` avisa cuándo no hay nada medible — que no es lo mismo que estar
    al día. Sin `cmids` mira todas las tareas del curso. READ-ONLY."""
    return await panorama.demora_correccion(
        _cli(), course_id, await _cmids_del_curso(course_id, cmids))


@mcp.tool()
async def informes_nexos(course_id: int, dias_desenganche: int = 7,
                         pdf: bool = True, emails: bool = True,
                         unidades: str | None = None) -> dict:
    """EL informe para los TUTORES NEXO: los alumnos que dejaron de abrir la materia,
    agrupados por REGIONAL, con el nexo de cada sede y su mail. Con `pdf=True` (por defecto)
    escribe además el PDF listo para mandar y devuelve la ruta en `pdf.archivo`.

    Uno por materia: pasás el `course_id` y sale el de ese curso.

    **Habla de ALUMNOS y de nadie más.** No trae ni una columna del trabajo de corrección de
    los tutores — eso es `reporte_coordinacion`, y va a coordinación. Estaban juntos en un mismo
    PDF y el costo no era de formato: un tutor que abre un documento donde su comisión aparece
    medida al lado de una lista de alumnos lo lee como una evaluación suya. Si te piden "el
    rendimiento de los tutores", NO es esta tool.

    Los días son **sin abrir ESTA materia**, no sin entrar al campus. Cada fila trae los dos
    relojes y el `caso`, que es lo que decide cómo hablarle a cada uno:
    - **está en el campus** → entra a Moodle y no abre la materia. Eligió no entrar: es el más
      recuperable y el que un corte por "días sin entrar al campus" no encuentra. Va primero.
    - **no aparece** → tampoco pisa el campus. Otra conversación: acceso perdido, o dejó.
    - **sin dato** → no se pudo leer. NO digas que no la abrió.

    `estado_aula = nunca_abrio` **no es abandono confirmado**: puede haberse matriculado esta
    semana, y esto no ve la fecha de matriculación. Presentalo así.

    El nexo de cada regional sale de `mcp/nexos.json`, que viaja con la skill. Si una regional
    no está en el catálogo, el bloque sale SIN contacto y se declara en `_meta.sin_dato` — nunca
    se le adjudica a alguien una sede que no es suya.

    También cuadra el padrón contra el total del curso y lista a los alumnos que están
    matriculados pero **en ninguna comisión**: a ésos no los ve ningún tutor, porque todas las
    vistas del campus trabajan por comisión.

    **No emite veredicto y vos tampoco**: nada de "estado general: sano". El informe que se
    venía armando a mano abría así sobre un curso con 60 alumnos que no abrían la materia,
    porque cortaba por el reloj del campus. Contá los hechos, leé `_meta.sin_dato` ANTES de los
    números, y dejá la conclusión a quien lee.

    **`unidades` agrega la columna RETRASO y hay que PREGUNTÁRSELO AL TUTOR.** Es el rango de
    unidades ya exigibles a la fecha (`"3-5"`, o `"3"` para una sola): un alumno figura retrasado
    si le falta al menos una de esas actividades de cierre. **El campus NO dice qué unidad se
    está cursando** —verificado: ninguna actividad de cierre tiene fecha de apertura— así que sin
    el rango la columna no sale, y el informe lo declara en vez de estimarla. Preguntá cuál es la
    última unidad ya cerrada, o el rango; `unidades_disponibles` trae las que hay en el aula para
    ofrecerlas. Ojo que el rango depende de la materia: en Prog I las unidades 1 y 2 son
    optativas, así que cursando la 4 se exige sólo la 3.

    `emails=True` incluye el mail de cada alumno (es lo que lo hace accionable). Son mails
    personales: cuando pases la ruta, avisá que el PDF no va a un repo ni a una nota compartida.
    READ-ONLY sobre el campus; lo único que escribe es el PDF, local, en `salidas/`."""
    from datetime import date

    datos = await panorama.informe_nexos(_cli(), course_id, dias_desenganche, unidades)
    if datos.get("error") or not pdf:
        return datos
    try:
        datos["pdf"] = informes.informe_nexos_pdf(
            datos, str(Path(almacen.salidas_dir()) / "informes"),
            materia=datos.get("curso") or "", fecha=date.today().isoformat(),
            emails=emails)
    except Exception as e:  # noqa: BLE001
        # Que falle el render NO puede tirar los datos: se relevó el curso entero y eso vale.
        datos["pdf"] = {"error": f"No pude escribir el PDF: {type(e).__name__}: {e}"}
        datos["_meta"]["degradado"] = True
    return datos


# ---------- INFORME (PDF) ----------
@mcp.tool()
async def armar_informe(course_id: int | None = None, group_id: int = 0) -> dict:
    """Genera un PDF de correcciones pendientes del curso (API REST + reportlab). Si no
    pasás course_id, usa el primer curso de tus "Mis datos". group_id=0 = todo el curso.
    Devuelve la ruta del PDF generado."""
    await almacen.init_db()
    cid = course_id
    if cid is None:
        datos = await almacen.get_mis_datos()
        for cu in (datos or {}).get("cursos", []):
            if cu.get("course_id") is not None:
                cid = int(cu["course_id"])
                break
    if not cid:
        return {"error": True, "mensaje": "No sé de qué curso armar el informe: pasá "
                "course_id o mapeá tus datos primero (descubrir_cursos -> guardar_mis_datos)."}
    return await informes.informe_pendientes(_cli(), cid, almacen.salidas_dir(), group_id=group_id)


@mcp.tool()
async def informe_alumnos(course_id: int, group_id: int = 0, pdf: bool = True,
                          detalle: bool | None = None) -> dict:
    """Qué hizo CADA ALUMNO y con qué nota, comisión por comisión y con el tutor a cargo.
    Con `pdf=True` (por defecto) escribe **un PDF por comisión** —el que se le manda a
    cada tutor— y devuelve las rutas en `pdf`.

    Lee el LIBRO DE CALIFICACIONES, no las entregas, y por eso ve lo que el resto de la
    skill no ve. En Matemática `mod_assign` está en CERO ABSOLUTO (549 participantes, 0
    enviados en las 15 actividades, verificado en vivo): la cursada pasa por videos
    interactivos H5P, lecciones y autoevaluaciones. Con la vista de entregas el padrón
    entero sale en blanco y eso se lee como "esta comisión no arrancó", que es falso.

    Devuelve, por comisión: el TUTOR a cargo, sus alumnos, y de cada alumno cuántas
    actividades hizo por tipo (`video` / `leccion` / `autoevaluacion` / `entrega`) y por
    unidad, más **la última vez que abrió LA MATERIA** — no el campus: el que entra todos
    los días para otra materia y hace un mes que no abre ésta figura al día si se mira el
    reloj equivocado. `detalle` agrega la nota actividad por actividad; por defecto se
    prende sola cuando pedís UNA comisión (`group_id`) y se apaga cuando pedís el curso
    entero, donde 550 alumnos x 81 actividades no entran en una respuesta.

    **De dónde sale la unidad de cada video.** De la ESTRUCTURA del curso, no del título:
    los títulos del calificador dicen "Video 2 Semana 1 SN" y nunca la unidad. La sección
    numerada ("2- Sistema binario") abre la unidad y los bloques que siguen (Videos,
    Lecciones, Trabajo Práctico, Autoevaluaciones) la heredan. Se verifica solo: las 13
    tareas `ENTREGA U{n}S{m}` de Matemática caen 13 de 13 en la unidad que dice su propio
    título. Lo que queda fuera de un bloque de unidad (coloquios, integradores, video de
    bienvenida) sale agrupado aparte y NO se le adjudica una unidad inventada.

    **El tutor se resuelve en vivo y no de un reparto escrito.** El padrón de cada
    comisión trae al tutor Y al profesor del curso; se toma al que aparece en MENOS
    comisiones, porque el que cubre cinco es la cátedra. Contrastado contra el reparto
    oficial de Matemática: 15 de 15.

    **Con el curso entero (sin `group_id`) suma la vista del COORDINADOR**, que es otro
    documento y otro destinatario: `coordinacion_calificador_curso«N».pdf` pone las
    comisiones lado a lado con su tutor, y trae dos cortes que ninguna vista por comisión
    da. `huecos_de_calificacion`: actividades que andan en el curso y están en CERO en
    alguna comisión — relevado en PyE, el foro calificado de la semana 1 tenía 31 notas en
    una comisión y NINGUNA en otras dos; por comisión eso se lee como "participan menos" y
    manda a llamar a la gente equivocada. Y `alumnos_sin_comision`, con nombre y mail: no
    los ve ningún tutor porque toda la skill trabaja por comisión.

    **Se audita el TRABAJO, nunca se califica a la PERSONA.** Nombrar al tutor es ruteo —a
    quién llamar— y va. Un ranking o un puntaje de tutores NO va, ni en la tabla ni en cómo
    lo contás: las comisiones no son comparables entre sí (distinto tamaño, cohorte y
    consigna), así que comparar personas convierte un hecho en un juicio.

    `sin_dato_de_acceso` y `avisos` antes de concluir nada: un 0 porque nadie hizo nada y
    un 0 porque no se pudo leer el calificador son cosas distintas. READ-ONLY sobre el
    campus: lo único que escribe son los PDF, locales, en `salidas/informes/`.
    """
    from datetime import date

    comisiones, ignorados, err = await panorama._comisiones_del_curso(_cli(), course_id)
    if err:
        return {"error": err}
    if not comisiones:
        return {"error": f"El curso {course_id} no tiene comisiones de tutoría "
                         f"reconocibles. Grupos que se ignoraron: {ignorados[:10]}"}
    if group_id and not any(c["group_id"] == group_id for c in comisiones):
        return {"error": f"El group_id {group_id} no es una comisión de tutoría del "
                         f"curso {course_id}."}

    # El padrón se baja para TODAS las comisiones aunque se haya pedido una sola, y no es
    # desperdicio: "quién es el tutor de esta comisión" se contesta contando en cuántas
    # comisiones del CURSO aparece cada docente, así que con una sola comisión a la vista
    # todos aparecen una vez y la regla no puede opinar. Pedir una comisión devolvería el
    # nombre del profesor de cátedra con la misma cara de dato bueno.
    padrones, avisos_padron = await panorama._padrones(_cli(), course_id, comisiones)
    tutores = panorama.elegir_tutor(padrones, comisiones)

    if group_id:
        comisiones = [c for c in comisiones if c["group_id"] == group_id]
    if detalle is None:
        detalle = len(comisiones) == 1

    for c in comisiones:
        elegido = tutores.get(c["group_id"]) or {}
        c["tutor"] = elegido.get("tutor")
        c["avisos"] = list(elegido.get("avisos") or [])
        c["course_id"] = course_id

    datos = await calificador.informe(_cli(), course_id, comisiones, padrones)
    if datos.get("error"):
        return datos
    datos["avisos"] = list(avisos_padron) + list(datos.get("avisos") or [])
    if ignorados:
        datos["grupos_ignorados"] = ignorados

    # --- Los tres cortes de la vista del coordinador. Salen GRATIS: los datos ya se
    # bajaron para los PDF por comisión, y son puros. ---
    hasta = datos.get("hasta_donde_llego")
    por_comision = calificador.panorama_por_comision(
        datos["comisiones"], datos["catalogo"], hasta)
    por_actividad = calificador.panorama_por_actividad(
        datos["comisiones"], datos["catalogo"])
    huecos = calificador.huecos_de_calificacion(por_actividad)

    # Quién está matriculado en el curso y en ninguna comisión. Cuesta una consulta más y
    # sólo se paga cuando se mira el curso entero: pedir una comisión no necesita saberlo.
    sin_comision = []
    if not group_id:
        padron_curso = await panorama._padron_del_curso(_cli(), course_id)
        if padron_curso.get("error"):
            datos["avisos"].append(
                f"No pude leer el padrón completo del curso: {padron_curso['error']}. No "
                "puedo decir si hay alumnos fuera de toda comisión — que no es lo mismo "
                "que decir que no los hay.")
        else:
            uids = {uid for gid in padrones
                    for uid in (padrones[gid].get("alumnos") or {})}
            sin_comision = calificador.alumnos_sin_comision(padron_curso, uids)
            if sin_comision:
                datos["avisos"].append(
                    f"{len(sin_comision)} alumno(s) están matriculados en el curso y en "
                    "NINGUNA comisión: no los ve ningún tutor. Van con nombre en "
                    "`alumnos_sin_comision` y en el PDF de coordinación.")

    if pdf:
        try:
            nombre = await panorama._nombre_del_curso(_cli(), course_id)
        except Exception:  # noqa: BLE001
            nombre = ""
        destino = str(Path(almacen.salidas_dir()) / "informes")
        hechos, fallados = [], []
        for b in datos["comisiones"]:
            try:
                hechos.append(informes.informe_comision_pdf(
                    b, datos["catalogo"], destino, materia=nombre or "",
                    fecha=date.today().isoformat(),
                    hasta_donde_llego=datos.get("hasta_donde_llego")))
            except Exception as e:  # noqa: BLE001
                # Que falle un render no puede tirar el relevamiento del curso entero.
                fallados.append({"comision": b.get("comision"),
                                 "motivo": f"{type(e).__name__}: {e}"})
        coordinacion = None
        if not group_id:
            try:
                coordinacion = informes.informe_curso_pdf(
                    datos, destino, materia=nombre or "", fecha=date.today().isoformat(),
                    por_comision=por_comision, por_actividad=por_actividad,
                    huecos=huecos, sin_comision=sin_comision)
            except Exception as e:  # noqa: BLE001
                coordinacion = {"error": f"No pude escribir el PDF de coordinación: "
                                         f"{type(e).__name__}: {e}"}
        datos["pdf"] = {"archivos": hechos, "fallaron": fallados, "carpeta": destino,
                        "coordinacion": coordinacion}
        if fallados:
            datos["avisos"].append(
                f"{len(fallados)} PDF no se pudieron escribir: los datos de esas "
                "comisiones están igual en la respuesta, pero no hay documento.")

    # La respuesta se poda ACÁ y no en `calificador`: el dict completo es el que alimenta
    # los PDF y tiene que estar entero. Lo que se recorta es lo que viaja al chat, y hace
    # falta: el curso entero con una fila por alumno son 325.000 caracteres — no entra en
    # una respuesta y no lo lee nadie. Con varias comisiones se devuelve el índice (tutor,
    # números y ruta del PDF) más los alumnos que no hicieron NADA, que son los únicos
    # nombres accionables sin abrir el documento. El detalle sale pidiendo una comisión.
    curso_entero = len(datos["comisiones"]) > 1
    salida_com = []
    for b in datos["comisiones"]:
        alumnos = []
        for a in ([] if curso_entero else (b.get("alumnos") or [])):
            fila = {k: a[k] for k in ("userid", "nombre", "actividades_con_nota",
                                      "sin_actividad", "estado_aula",
                                      "dias_sin_abrir_la_materia",
                                      "ultimo_acceso_aula_ts")}
            fila["por_tipo"] = {t: {"hechas": v["hechas"], "total": v["total"],
                                    "promedio_pct": v["promedio_pct"]}
                                for t, v in a["por_tipo"].items()}
            fila["por_naturaleza"] = {n: {"hechas": v["hechas"], "total": v["total"]}
                                      for n, v in a["por_naturaleza"].items()}
            # Sólo donde hizo algo. La unidad/semana ausente = no hizo nada ahí; cuáles
            # existen en el curso está arriba, en `unidades`/`semanas`.
            fila["ultima_con_actividad"] = a["ultima_con_actividad"]
            fila[f"por_{a['eje']}"] = {v: b2["hechas"] for v, b2 in a["por_eje"].items()
                                       if b2["hechas"]}
            if detalle:
                fila["notas"] = sorted(
                    ({"nro": n["nro"], "actividad": n["titulo"], "tipo": n["tipo"],
                      "naturaleza": n["naturaleza"], "unidad": n["unidad"],
                      "semana": n["semana"], "nota": n["nota"], "sobre": n["sobre"]}
                     for n in a["notas"].values()), key=lambda n: n["nro"])
            alumnos.append(fila)
        fila_com = ({k: b[k] for k in ("comision", "group_id", "nombre") if k in b}
                    | {"tutor": b.get("tutor"), "resumen": b.get("resumen"),
                       "avisos": b.get("avisos") or []}
                    | ({"error": b["error"]} if b.get("error") else {}))
        if curso_entero:
            fila_com["alumnos_sin_ninguna_actividad"] = [
                a["nombre"] for a in (b.get("alumnos") or []) if a["sin_actividad"]]
        else:
            fila_com["alumnos"] = alumnos
        salida_com.append(fila_com)

    return {
        "ok": True, "course_id": course_id, "detalle": detalle,
        "actividades_del_curso": [
            {"nro": it["nro"], "actividad": it["titulo"], "tipo": it["tipo"],
             "naturaleza": it["naturaleza"], "unidad": it["unidad"],
             "semana": it["semana"], "cmid": it["cmid"]}
            for it in datos["catalogo"]["items"]],
        "eje": datos["catalogo"]["eje"],
        "unidades": datos["catalogo"]["unidades"],
        "semanas": datos["catalogo"].get("semanas") or [],
        "hasta_donde_llego": datos.get("hasta_donde_llego"),
        "comisiones": salida_com,
        "por_comision": por_comision,
        "huecos_de_calificacion": [
            {k: h[k] for k in ("nro", "actividad", "tipo", "naturaleza", "unidad",
                               "semana", "notas_en_el_curso", "comisiones_en_cero")}
            for h in huecos],
        **({"alumnos_sin_comision": sin_comision} if sin_comision else {}),
        **({"como_ver_el_detalle":
            "Esta respuesta trae el índice del curso: tutor, números y la ruta del PDF de "
            "cada comisión, más los alumnos que no hicieron NADA. El alumno por alumno "
            "está en el PDF; para tenerlo también acá pedí UNA comisión con su group_id."}
           if curso_entero else {}),
        "avisos": datos["avisos"],
        "grupos_ignorados": datos.get("grupos_ignorados", []),
        **({"pdf": datos["pdf"]} if pdf else {}),
    }


# ---------- CORRECCIÓN AUTOMÁTICA (Active-IA / Gemini) ----------
@mcp.tool()
async def activeia_pendientes() -> dict:
    """Mapa Moodle<->Active-IA: materias->unidades (con `cmid`=assign_id de Moodle y la
    `rubrica_id` inferida por título)->comisiones (con `comision_id` de Active-IA y
    `group_id` de Moodle). Sirve para resolver a mano comision_id/rubrica_id antes de
    corregir.

    ⚠️ Sus contadores `espera`/`corregidos` son del ESTADO EN MOODLE, no de Active-IA:
    `corregidos: 0` quiere decir "sin nota cargada en el campus", NO "Active-IA no corrigió".
    Para saber qué corrigió Active-IA y con qué nota, usá `activeia_correcciones`."""
    return await active_ia.activeia_pendientes()


@mcp.tool()
async def activeia_correcciones(comision_id: int, solo_corregidas: bool = True) -> dict:
    """QUÉ CORRIGIÓ Active-IA y con qué nota, por comisión. La vista que `activeia_pendientes`
    NO da (esa lee el estado de Moodle).

    Usala sobre todo después de un error `GEMINI_OVERLOADED`: ese error significa que la
    respuesta no llegó a tiempo, NO que la corrección se perdió — muchas terminan bien
    minutos después. Antes de reintentar o de corregir a mano, mirá acá.

    El `comision_id` sale de `activeia_resolver`. Devuelve `{comision_id, total,
    correcciones:[{entrega_id, alumno, estado, nota, correccion_id, rubrica_id}]}`.
    Que una entrega figure con nota acá NO significa que esté cargada en el campus: para
    eso está `cargar_nota`, que es un paso aparte."""
    return await active_ia.activeia_correcciones(comision_id, solo_corregidas)


@mcp.tool()
async def activeia_resolver(assign_id: str, group_id: int) -> dict:
    """A partir del `cmid` (assign_id) + `group_id` de Moodle devuelve
    `{comision_id, rubrica_id, unidad_titulo, moodle_grader_url}` cruzando
    /pendientes/moodle y /rubricas de Active-IA. Es el paso previo a
    `corregir_con_active_ia`. Si no puede inferir la rúbrica, devuelve el comision_id
    igual y avisa que pases rubrica_id a mano. (API REST de Active-IA.)"""
    return await active_ia.activeia_resolver(assign_id, group_id)


@mcp.tool()
async def corregir_con_active_ia(
    assign_id: str,
    email: str,
    comision_id: int,
    rubrica_id: int,
    alumno_nombre: str | None = None,
    moodle_url: str | None = None,
    timeout_s: int = 180,
    confirmado: bool = False,
) -> dict:
    """Corrige la entrega de un alumno con Active-IA (Gemini) de punta a punta: baja el
    archivo de Moodle (API REST, sin navegador), lo sube a Active-IA, dispara la
    corrección, espera el resultado y DESCARGA LOCAL el PDF de devolución.

    NO carga la nota en Moodle. Deja la nota sugerida y el PDF de devolución bajado a
    disco; escribir en el campus es un paso APARTE con `cargar_nota`, que tiene su propia
    confirmación. Aun así es una ESCRITURA (crea la entrega y la corrección en Active-IA):
    llamá primero con confirmado=false para previsualizar; recién tras el OK del tutor,
    confirmado=true. Antes conseguí comision_id/rubrica_id con
    `activeia_resolver(assign_id, group_id)`.

    Devuelve `{ok, nota, correccion_id, entrega_id, devolucion_pdf_url,
    devolucion_pdf_local, estado}`. `devolucion_pdf_local` es la ruta del PDF de
    devolución bajado a `$MOODLE_SKILL_HOME/salidas`. Casos que devuelve como dict (no
    rompe): `conflicto=True` si ya existe la entrega; `error` con "timeout del servicio
    de IA" si Gemini se satura (reintentá más tarde)."""
    if not confirmado:
        return {
            "preview": {
                "accion": "corregir_con_active_ia",
                "alumno": alumno_nombre or email,
                "email": email,
                "assign_id": assign_id,
                "comision_id": comision_id,
                "rubrica_id": rubrica_id,
            },
            "aviso": "Esto baja la entrega y la corrige con Active-IA (Gemini), y deja el "
                     "PDF de devolución en disco. NO escribe la nota en Moodle: para eso "
                     "hace falta después cargar_nota, que se confirma aparte. Revisalo y "
                     "volvé a llamar con confirmado=true para ejecutar.",
        }
    return await active_ia.corregir_con_active_ia(
        _cli(), assign_id, email, comision_id, rubrica_id,
        alumno_nombre=alumno_nombre, moodle_url=moodle_url, timeout_s=timeout_s,
    )


@mcp.tool()
async def ver_correccion(correccion_id: int) -> dict:
    """Estado ACTUAL de una corrección de Active-IA (nota, criterios, fortalezas,
    recomendaciones, comentario). Read-only, sin gate.

    Usala ANTES de `actualizar_correccion`, para tener el "antes" a mano y compararlo
    contra `ver_entrega` — nunca edites una corrección sin haber mirado primero si la
    devolución de Gemini coincide con lo que el alumno entregó de verdad. Caso real que
    motivó esto (2026-08-31): Active-IA marcó como ausentes clases CSS que sí estaban en
    el CSS real del alumno (correccion_id 24794), sugiriendo 16/100 sobre una entrega que
    valía 100/100."""
    return await active_ia.ver_correccion(correccion_id)


@mcp.tool()
async def actualizar_correccion(
    correccion_id: int,
    nota: float | None = None,
    criterios: list[dict] | None = None,
    fortalezas: list[str] | None = None,
    recomendaciones: list[str] | None = None,
    comentario_general: str | None = None,
    confirmado: bool = False,
    regenerar_pdf: bool = True,
) -> dict:
    """Edita a mano una corrección YA HECHA de Active-IA (nota, criterios, fortalezas,
    recomendaciones, comentario general). Todos los campos de contenido son opcionales
    (update parcial: mandá sólo lo que cambia).

    NO carga la nota en Moodle -- eso sigue siendo `cargar_nota`, aparte. Es una
    ESCRITURA: llamá primero con confirmado=false para previsualizar el cambio; recién
    tras el OK del tutor, confirmado=true.

    **Nunca la uses a ciegas.** Antes de llamarla: (1) `ver_correccion(correccion_id)`
    para ver lo que Active-IA generó, (2) comparalo contra `ver_entrega` de lo que el
    alumno mandó de verdad. Sólo si NO coinciden tiene sentido editar. Referencia: el
    caso Molinari (correccion_id 24794, Prog III com2, "Práctica - Actividad III - CSS")
    — Gemini dio 16/100 marcando ausentes clases CSS que sí estaban en el código real; la
    nota corregida a mano fue 100/100.

    Marca `editado_manualmente=True` del lado de Active-IA (auditoría). Por default
    regenera el PDF de devolución con los datos corregidos (`regenerar_pdf=True`) para
    que quede listo para `cargar_nota`, igual que deja el PDF `corregir_con_active_ia`."""
    cambios = {
        k: v for k, v in {
            "nota": nota, "criterios": criterios, "fortalezas": fortalezas,
            "recomendaciones": recomendaciones, "comentario_general": comentario_general,
        }.items() if v is not None
    }
    if not confirmado:
        return {
            "preview": {
                "accion": "actualizar_correccion",
                "correccion_id": correccion_id,
                "cambios_propuestos": cambios,
            },
            "aviso": "Esto edita a mano una corrección ya hecha de Active-IA y marca "
                     "editado_manualmente=True. NO escribe la nota en Moodle: para eso "
                     "hace falta después cargar_nota, que se confirma aparte. Revisalo y "
                     "volvé a llamar con confirmado=true para ejecutar.",
        }
    return await active_ia.actualizar_correccion(
        correccion_id, nota=nota, criterios=criterios, fortalezas=fortalezas,
        recomendaciones=recomendaciones, comentario_general=comentario_general,
        regenerar_pdf=regenerar_pdf,
    )


# ---------- ESCRITURA (con confirmación) ----------
@mcp.tool()
async def cargar_nota(assign_id: str, email: str, nota: str, mensaje: str,
                      confirmado: bool = False, etiquetas: list[str] | None = None,
                      adjunto: str | None = None) -> dict:
    """Escribe nota + devolución en Moodle. Llamá primero con confirmado=false para
    previsualizar; recién tras el OK del tutor, confirmado=true.

    La nota depende del TIPO de calificación de la tarea:
    - **TPs (ESCALA)**: pasá el texto EXACTO 'Aprobado' o 'Desaprobado' (NO un número).
      Si no coincide, devuelve es_escala=true + la lista de opciones válidas.
    - **Integrador (TIO) / numéricas**: pasá el número (coma decimal, ej. '9,85').
    Devuelve `verificado` (relee la nota por API para confirmar el guardado). (API REST:
    mod_assign_save_grade — sin navegador.)

    `etiquetas`: los TEMAS que se le marcaron al alumno, en kebab-case y reutilizables
    entre alumnos (ej. ["perimetro-circulo", "conversion-unidades", "operador-mayor-igual"]).
    Poné una por error real corregido; si el trabajo estaba impecable, dejalas vacías.
    Se guardan en la bitácora local y son lo que después alimenta `errores_frecuentes`:
    sin ellas, ese dato NO se puede reconstruir después. Usá el MISMO nombre de tema para
    el mismo error en distintos alumnos — ahí está toda la gracia.

    `adjunto`: ruta local de un archivo para ADJUNTAR a la devolución (típicamente el PDF
    que dejó `corregir_con_active_ia` en `salidas/`). Sin esto la devolución es sólo texto:
    si el mensaje dice "te adjunto el PDF" y no se pasa `adjunto`, el alumno lee que hay un
    archivo que no existe. Si la subida falla, la nota se carga igual y volvés
    `adjunto_aviso` explicando por qué no se adjuntó."""
    res = await ws_api.cargar_nota(_cli(), assign_id, email, nota, mensaje, confirmado,
                                   adjunto=adjunto)

    # Bitácora: sólo si la nota efectivamente quedó escrita. Registrar un preview o una
    # escritura fallida contaminaría las estadísticas con correcciones que no existieron.
    if confirmado and res.get("ok"):
        await _registrar_bitacora(res, assign_id, email, mensaje, etiquetas)
    return res


async def _contexto_tarea(assign_id: str) -> tuple:
    """(course_id, titulo, comision) de una tarea, desde "Mis datos". Todo None si no está."""
    datos = await almacen.get_mis_datos() or {}
    for c in datos.get("cursos", []):
        for t in c.get("tareas", []):
            if str(t.get("assign_id")) == str(assign_id):
                coms = c.get("comisiones_del_tutor", [])
                return (c.get("course_id"), t.get("titulo"),
                        coms[0].get("comision") if len(coms) == 1 else None)
    return (None, None, None)


async def _registrar_bitacora(res: dict, assign_id: str, email: str, mensaje: str,
                              etiquetas: list | None, comision: str | None = None) -> None:
    """Deja la corrección en la bitácora. NUNCA rompe la carga: si falla, la nota ya está
    escrita en Moodle y lo único que se pierde es la estadística, así que se avisa y sigue."""
    try:
        await almacen.init_db()
        curso, tarea, com = await _contexto_tarea(assign_id)
        await almacen.guardar_correccion({
            "course_id": curso, "assign_id": assign_id, "tarea": tarea,
            "comision": comision or com, "email": email, "alumno": res.get("alumno"),
            "nota": res.get("nota"), "devolucion": mensaje, "etiquetas": etiquetas or [],
        })
        res["registrado_en_bitacora"] = True
    except Exception as e:  # noqa: BLE001
        log.warning("No pude registrar la corrección: %s: %s", type(e).__name__, e)
        res["registrado_en_bitacora"] = False
        res["aviso_bitacora"] = ("La nota se cargó bien, pero no se pudo registrar en la "
                                 "bitácora local: este caso no va a figurar en "
                                 "errores_frecuentes.")


# ---------- SESIÓN DE CORRECCIÓN EN LOTE ----------
@mcp.tool()
async def preparar_correccion(assign_id: str, group_id: int,
                              reemplazar: bool = False) -> dict:
    """Arma la cola para corregir una tarea entera de una comisión, de a un alumno por vez.

    Corregir 15 TPs de a uno son 15 idas y vueltas completas. Con la cola vas resolviendo
    alumno por alumno SIN tocar Moodle, y al final `confirmar_cola` escribe todo junto con
    una sola confirmación — pero mostrándote antes las 15 notas juntas, así el OK es
    informado y no a ciegas.

    La cola es PERSISTENTE: si cortás a la mitad, al volver seguís donde estabas y lo ya
    anotado no se pierde. `reemplazar=true` la descarta y arranca de nuevo.

    Se encolan sólo los que entregaron y no tienen nota (incluidos los "calificados sin
    nota", que no salen en ninguna otra cola)."""
    await almacen.init_db()
    pend = await ws_api.pendientes_tarea(_cli(), assign_id, group_id)
    if pend.get("error"):
        return pend
    alumnos = [{"email": a.get("email"), "nombre": a.get("name")}
               for a in pend.get("alumnos", [])]
    if not alumnos:
        return {"ok": True, "en_cola": 0,
                "aviso": "No hay entregas pendientes de corrección en esta tarea/comisión."}
    _, titulo, comision = await _contexto_tarea(assign_id)
    r = await almacen.cola_abrir(assign_id, titulo, group_id, comision, alumnos, reemplazar)
    return {"ok": True, "assign_id": str(assign_id), "group_id": group_id, "tarea": titulo,
            **r,
            "siguiente_paso": "Llamá `siguiente_para_corregir` para arrancar. Nada se "
                              "escribe en Moodle hasta `confirmar_cola`."}


@mcp.tool()
async def siguiente_para_corregir(assign_id: str | None = None,
                                  group_id: int | None = None,
                                  max_chars: int = 20000) -> dict:
    """El próximo alumno de la cola, CON su entrega ya bajada y lista para leer.

    Devuelve el contenido del trabajo para que se pueda corregir sin pasos intermedios.
    Después de decidir, se anota con `anotar_correccion` y se vuelve a llamar a esta."""
    await almacen.init_db()
    fila = await almacen.cola_siguiente(assign_id, group_id)
    if not fila:
        restan = await almacen.cola_listar(assign_id, group_id, estados=("anotado",))
        return {"ok": True, "quedan_pendientes": 0,
                "anotados_sin_escribir": len(restan),
                "aviso": ("No queda nadie por corregir en la cola. "
                          + (f"Tenés {len(restan)} anotados: confirmá con `confirmar_cola`."
                             if restan else "La cola está vacía."))}
    entrega = await ws_api.leer_entrega(
        _cli(), fila["assign_id"], fila["email"],
        str(Path(almacen.salidas_dir()) / "entregas" / str(fila["assign_id"])), max_chars)
    faltan = await almacen.cola_listar(fila["assign_id"], fila["group_id"],
                                       estados=("pendiente",))
    return {"ok": True, "alumno": fila["alumno"], "email": fila["email"],
            "assign_id": fila["assign_id"], "group_id": fila["group_id"],
            "tarea": fila["tarea"], "quedan_pendientes": len(faltan), "entrega": entrega,
            "siguiente_paso": "Corregila y guardá con `anotar_correccion`. No se escribe "
                              "nada en Moodle todavía."}


@mcp.tool()
async def anotar_correccion(assign_id: str, group_id: int, email: str, nota: str,
                            mensaje: str, etiquetas: list[str] | None = None) -> dict:
    """Guarda la nota y la devolución de UN alumno en la cola. NO escribe en Moodle.

    Es el paso intermedio del lote: se acumula y recién `confirmar_cola` lo manda todo.
    `etiquetas`: los temas marcados, en kebab-case y reutilizables entre alumnos — son las
    que después alimentan `errores_frecuentes`."""
    await almacen.init_db()
    ok = await almacen.cola_anotar(assign_id, group_id, email, nota, mensaje, etiquetas or [])
    if not ok:
        # Se aceptan pendiente/anotado/error/salteado; si llegó acá, o no está en la cola o
        # ya se escribió (esas no se reabren: para cambiar una nota cargada va cargar_nota).
        return {"error": f"No pude anotar a {email} en la cola de esta tarea/comisión.",
                "posibles_motivos": [
                    "no está en la cola (¿corriste `preparar_correccion`?)",
                    "su nota YA se escribió en Moodle — para cambiarla usá `cargar_nota`",
                ]}
    faltan = await almacen.cola_listar(assign_id, group_id, estados=("pendiente",))
    listos = await almacen.cola_listar(assign_id, group_id, estados=("anotado",))
    return {"ok": True, "anotado": email, "nota": nota,
            "quedan_pendientes": len(faltan), "anotados": len(listos),
            "siguiente_paso": ("Seguí con `siguiente_para_corregir`." if faltan
                               else "No queda nadie: revisá todo con `confirmar_cola`.")}


@mcp.tool()
async def saltear_en_cola(assign_id: str, group_id: int, email: str,
                          motivo: str) -> dict:
    """Saca a un alumno de la cola SIN calificarlo, dejando registrado por qué.

    No todo lo que está pendiente se puede corregir. El caso que motivó esto: un alumno
    subió los apuntes de la cátedra en vez de su TP — no merece Aprobado ni Desaprobado,
    necesita que le avisen que suba el archivo correcto. Sin esta salida, la cola devolvía
    siempre a la misma persona y la única forma de avanzar era ponerle una nota falsa.

    Usalo también con entregas ilegibles, archivos corruptos o cualquier caso donde haga
    falta hablar con el alumno antes de poner nota. El `motivo` queda guardado y aparece
    en el resumen de `confirmar_cola`."""
    await almacen.init_db()
    ok = await almacen.cola_saltear(assign_id, group_id, email, motivo)
    if not ok:
        return {"error": f"{email} no está pendiente en la cola de esta tarea/comisión."}
    faltan = await almacen.cola_listar(assign_id, group_id, estados=("pendiente",))
    return {"ok": True, "salteado": email, "motivo": motivo,
            "quedan_pendientes": len(faltan),
            "recordatorio": "Salteado NO es calificado: este alumno sigue sin nota en "
                            "Moodle. Si hay que avisarle, usá `responder_mensaje`."}


@mcp.tool()
async def confirmar_cola(assign_id: str | None = None, group_id: int | None = None,
                         confirmado: bool = False) -> dict:
    """Escribe en Moodle TODAS las correcciones anotadas en la cola, de una.

    Con `confirmado=false` (default) devuelve el detalle completo de lo que se va a
    escribir —alumno por alumno, con su nota y su devolución— para que el OK sea informado
    y no a ciegas. Recién con `confirmado=true` se escribe.

    Cada nota se escribe y se VERIFICA por separado: si una falla, las demás siguen y el
    reporte dice exactamente cuál y por qué. Las que fallan quedan en la cola para
    reintentar; las que salen bien se registran en la bitácora."""
    await almacen.init_db()
    anotados = await almacen.cola_listar(assign_id, group_id, estados=("anotado",))
    if not anotados:
        pend = await almacen.cola_listar(assign_id, group_id, estados=("pendiente",))
        return {"ok": True, "a_escribir": 0,
                "aviso": (f"No hay nada anotado para escribir. Quedan {len(pend)} sin "
                          "corregir en la cola." if pend else "La cola está vacía.")}

    if not confirmado:
        return {
            "requiere_confirmacion": True,
            "a_escribir": len(anotados),
            "previews": [{"alumno": f["alumno"], "email": f["email"], "nota": f["nota"],
                          "etiquetas": f["etiquetas"], "devolucion": f["devolucion"]}
                         for f in anotados],
            "aviso": (f"Se van a escribir {len(anotados)} notas en Moodle. Revisalas y "
                      "volvé a llamar con confirmado=true."),
        }

    escritas, fallidas = [], []
    for f in anotados:
        res = await ws_api.cargar_nota(_cli(), f["assign_id"], f["email"], f["nota"],
                                       f["devolucion"], True)
        if res.get("ok"):
            await _registrar_bitacora(res, f["assign_id"], f["email"], f["devolucion"],
                                      f["etiquetas"], f.get("comision"))
            await almacen.cola_marcar(f["id"], "escrito")
            escritas.append({"alumno": f["alumno"], "nota": res.get("nota"),
                             "verificado": res.get("verificado")})
        else:
            motivo = res.get("error") or "no se pudo escribir"
            await almacen.cola_marcar(f["id"], "error", motivo)
            fallidas.append({"alumno": f["alumno"], "email": f["email"], "motivo": motivo})

    salida = {"ok": not fallidas, "escritas": len(escritas), "fallidas": len(fallidas),
              "detalle_escritas": escritas}
    if fallidas:
        salida["detalle_fallidas"] = fallidas
        salida["aviso"] = (f"{len(escritas)} se escribieron bien y {len(fallidas)} fallaron. "
                           "Las fallidas quedaron en la cola: arreglá el motivo y volvé a "
                           "confirmar, no se van a duplicar las que ya salieron.")
    else:
        salida["resumen"] = (f"Listo: {len(escritas)} notas escritas y verificadas. "
                             "Mirá `errores_frecuentes` para ver qué falló toda la comisión.")

    # Los salteados NO se escribieron y siguen sin nota: hay que recordarlo o se pierden.
    salteados = await almacen.cola_listar(assign_id, group_id, estados=("salteado",))
    if salteados:
        salida["salteados"] = [{"alumno": s["alumno"], "email": s["email"],
                                "motivo": s.get("resultado")} for s in salteados]
        salida["aviso_salteados"] = (
            f"⚠️ {len(salteados)} alumno(s) quedaron SALTEADOS: no se les escribió nota y "
            "siguen pendientes en Moodle. Revisá el motivo de cada uno y avisales.")
    return salida


@mcp.tool()
async def errores_frecuentes(course_id: int | None = None, assign_id: str | None = None,
                             comision: str | None = None) -> dict:
    """En qué se está equivocando TU comisión, agregado sobre las correcciones ya hechas.

    Deja de ser "qué le pasó a este alumno" y pasa a ser "qué no quedó bien explicado":
    cuando el mismo error aparece en más del 40% de los corregidos se marca `sistemico`,
    porque a esa altura el problema ya no es de los alumnos — es del material o de cómo se
    dio el tema.

    Se alimenta de las `etiquetas` que se pasan al `cargar_nota`. Si no se etiqueta al
    corregir, acá no hay nada que mostrar: **el dato no se puede reconstruir después**,
    porque exigiría releer todas las entregas de nuevo.

    Sin filtros toma toda la bitácora; se puede acotar por curso, tarea o comisión."""
    await almacen.init_db()
    r = await almacen.errores_frecuentes(course_id=course_id, assign_id=assign_id,
                                         comision=comision)
    n = r["correcciones_registradas"]
    if not n:
        return {**r, "aviso": (
            "Todavía no hay correcciones registradas con etiquetas. Se van cargando solas a "
            "medida que corregís con `cargar_nota(..., etiquetas=[...])`. Arranca vacío a "
            "propósito: es un histórico, no una foto.")}
    sistemicos = [t for t in r["temas"] if t["sistemico"]]
    salida = {**r, "temas_sistemicos": len(sistemicos)}
    if not r.get("muestra_suficiente"):
        # Con pocas correcciones el porcentaje engaña: 1 de 2 da 50%. Se muestran los temas
        # igual (sirven para ir viendo), pero sin sacar conclusiones sobre la comisión.
        top = ", ".join(f"{t['tema']} ({t['alumnos_afectados']})" for t in r["temas"][:5])
        salida["resumen"] = (
            f"Todavía son pocas correcciones ({n}, hacen falta {r['muestra_minima']}) como "
            "para hablar de la comisión: un porcentaje sobre esta cantidad engaña. "
            + (f"Por ahora aparecieron: {top}." if top else "Sin temas registrados aún.")
            + " Seguí corrigiendo y el dato se vuelve confiable solo.")
    elif sistemicos:
        cuales = ", ".join(f"{t['tema']} ({t['porcentaje']}%)" for t in sistemicos[:5])
        salida["resumen"] = (
            f"Sobre {n} corrección/es: {cuales}. Con esa proporción no es un problema "
            "individual — conviene reforzar el tema con toda la comisión.")
    else:
        salida["resumen"] = (f"Sobre {n} corrección/es no hay ningún error que se repita en "
                             "más del 40%: los desvíos vienen siendo individuales.")
    return salida


@mcp.tool()
async def abrir_panel(puerto: int = 8787) -> dict:
    """Abre el panel local en el navegador: chat con el agente + estado de las comisiones.

    El panel corre en la máquina del tutor y escucha SÓLO en 127.0.0.1, porque
    trabaja con sus credenciales del campus y puede escribir en él.

    Si ya estaba levantado, no arranca otro: devuelve la URL del que corre.
    """
    import importlib.util
    import shutil
    import subprocess
    import sys
    import urllib.error
    import urllib.request
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent
    url = f"http://127.0.0.1:{puerto}"

    def _vivo() -> bool:
        try:
            with urllib.request.urlopen(f"{url}/api/salud", timeout=1.5) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    if not (raiz / "panel" / "web" / "dist" / "index.html").exists():
        return {
            "ok": False,
            "motivo": "El panel no está compilado en esta instalación.",
            "como_arreglar": "Actualizá la skill: el build viaja en el repo.",
        }

    # El panel suma dependencias que el core de la skill NO usa, así que una
    # instalación vieja las tiene todas menos éstas. Sin este chequeo, el tutor
    # que actualiza ve la tool en el menú y se come un ImportError adentro de un
    # subproceso que ni siquiera puede leer: el panel arranca, muere, y `_vivo()`
    # devuelve False sin decir por qué.
    faltan = []
    for modulo, paquete in (
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn[standard]"),
        ("claude_agent_sdk", "claude-agent-sdk"),
    ):
        if importlib.util.find_spec(modulo) is None:
            faltan.append(paquete)
    if faltan:
        return {
            "ok": False,
            "motivo": f"Al panel le faltan dependencias: {', '.join(faltan)}.",
            "como_arreglar": (
                f"Instalalas una sola vez con:  cd {raiz} && "
                ".venv/bin/pip install -r panel/requirements.txt"
            ),
            "por_que": "El core de la skill no las usa, así que no vienen instaladas.",
        }

    ya_estaba = _vivo()
    if not ya_estaba:
        # No se ramifica por sistema operativo: Git Bash sobre Windows reporta
        # `msys` y WSL reporta `linux`, corren el MISMO script y tienen layouts
        # de venv distintos. Se le pregunta al filesystem cuál existe, que no se
        # equivoca nunca.
        candidatos = [
            raiz / ".venv" / "bin" / "python",
            raiz / ".venv" / "Scripts" / "python.exe",
            raiz / ".venv" / "Scripts" / "python",
        ]
        python = next((c for c in candidatos if c.exists()), Path(sys.executable))
        try:
            subprocess.Popen(
                [str(python), "-m", "panel.backend.app"],
                cwd=str(raiz),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                # Se despega de esta sesión de Claude Code: el panel tiene que
                # seguir vivo cuando el tutor cierre la terminal.
                start_new_session=True,
            )
        except Exception as exc:
            return {"ok": False, "motivo": f"No se pudo arrancar el panel: {exc}"}

        for _ in range(30):
            await asyncio.sleep(0.4)
            if _vivo():
                break
        else:
            return {
                "ok": False,
                "motivo": "El panel no respondió a tiempo.",
                "como_arreglar": (
                    f"Probá a mano: cd {raiz} && .venv/bin/pip install -r "
                    "panel/requirements.txt && .venv/bin/python -m panel.backend.app"
                ),
            }

    # Mismo criterio que arriba: se busca el que exista, no el que "debería"
    # existir según el sistema. `wslview` va antes que `xdg-open` a propósito —
    # en WSL los dos están, y sólo el primero abre el navegador de Windows.
    abridor = next(
        (c for c in ("wslview", "xdg-open", "open") if shutil.which(c)), None
    )
    abierto = False
    if abridor:
        subprocess.Popen(
            [abridor, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        abierto = True
    elif hasattr(os, "startfile"):  # Windows nativo
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            abierto = True
        except OSError:
            pass

    return {
        "ok": True,
        "url": url,
        "ya_estaba": ya_estaba,
        "navegador_abierto": abierto,
        "nota": "El panel escucha sólo en 127.0.0.1. Se cierra con: pkill -f panel.backend.app",
    }


if __name__ == "__main__":
    # Transport stdio: lo lanza el propio Claude Code del tutor como MCP local.
    mcp.run()
