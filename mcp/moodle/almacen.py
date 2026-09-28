"""Persistencia LOCAL y de-un-tutor de la Skill (reemplaza el `db` multi-tenant del
copiloto).

El copiloto guardaba todo en un SQLite compartido con scoping por `tutor_id` (vault de
credenciales, sesiones del SDK, conversaciones, roles…). Nada de eso aplica a la Skill:
acá corre UN tutor con SUS credenciales de env vars. Así que este módulo se queda con lo
mínimo que el snapshot + las tools necesitan y lo persiste local:

- `mis_datos.json`  -> la config del tutor (cursos/comisiones/tareas). Human-editable.
- `datos.db`        -> SQLite con snapshots, caché de entregas y caché de alumnos.

Ubicación: `$MOODLE_SKILL_HOME` (default `~/.moodle-skill`). Se corre cada query en un
thread (asyncio.to_thread) para no bloquear el loop, igual que el `db` original.
"""

import asyncio
import datetime
import json
import os
import re
import shutil
import sqlite3
from typing import Any

# Raíz de datos de la Skill (MÁQUINA/skill-wide, no por-campus). Configurable por env
# para no clavarla en $HOME (tests, CI). Acá viven `tenants.json`, `estado.json`, los
# datos LEGACY sin tenant (migración, nunca se borran) y el cache de version.py — todo
# lo demás vive bajo `HOME/<tenant_id>/`.
HOME = os.path.expanduser(os.environ.get("MOODLE_SKILL_HOME", "~/.moodle-skill"))

_TENANT_DEFAULT_ID = "tup"
_TENANT_DEFAULT_URL = "https://tup.sied.utn.edu.ar"
_ESTADO_PATH = os.path.join(HOME, "estado.json")
_TENANTS_PATH = os.path.join(HOME, "tenants.json")


def _ahora() -> str:
    return datetime.datetime.now().isoformat()


# --- Identidad del tenant (campus) activo ---
# Lectura con default gracioso y SIN escritura: un `tenant_activo()` en una instalación
# nueva no debe crear nada — sólo `set_tenant_activo` escribe.

def tenant_activo() -> str:
    """Id del tenant/campus activo. Default `"tup"` si `estado.json` no existe."""
    try:
        with open(_ESTADO_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        tid = data.get("tenant_activo")
        return tid or _TENANT_DEFAULT_ID
    except (FileNotFoundError, ValueError):
        return _TENANT_DEFAULT_ID


def set_tenant_activo(tenant_id: str) -> None:
    """Marca `tenant_id` como el campus activo (persistido en `estado.json`)."""
    os.makedirs(HOME, exist_ok=True)
    with open(_ESTADO_PATH, "w", encoding="utf-8") as fh:
        json.dump({"tenant_activo": tenant_id}, fh, ensure_ascii=False, indent=2)


def tenants() -> list[dict]:
    """Campus registrados. Default: sólo `tup` si `tenants.json` no existe (no escribe)."""
    try:
        with open(_TENANTS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return [{"id": _TENANT_DEFAULT_ID, "nombre": "TUP (UTN)", "url": _TENANT_DEFAULT_URL}]


# Formato del id de tenant: minúsculas, dígitos y guiones, 1-40 caracteres. Sólo
# minúsculas a propósito — en Windows (NTFS) el filesystem es case-INsensitive, así que
# "TUP" y "tup" son EL MISMO directorio; si se permitiera mayúsculas, registrar "TUP"
# pisaría en silencio el .env real de "tup" con credenciales de otro campus (repro
# confirmado por el review). Comparar todo en minúsculas evita la colisión de raíz en
# vez de andar detectándola caso por caso.
_TENANT_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
# Reservados/peligrosos explícitos, aunque el regex ya los rechazaría salvo por vacío
# (que el regex también rechaza por el {1,40}, pero lo dejamos explícito por claridad
# del mensaje de error).
_TENANT_ID_RESERVADOS = {"", ".", ".."}


def validar_tenant_id(tenant_id: str, existentes: list[dict] | None = None) -> str | None:
    """Valida un `tenant_id` suministrado por el tutor (o por cualquier caller) ANTES
    de usarlo para construir un path o registrarlo. Devuelve un mensaje de error en
    castellano si es inválido, o `None` si está OK para usar.

    Reglas: sólo `[a-z0-9-]`, 1 a 40 caracteres; nunca vacío, `.` ni `..` (esos dos
    escaparían del directorio del tenant o pisarían el HOME); y no puede colisionar,
    comparando SIN importar mayúsculas/minúsculas, con un tenant ya registrado — así
    "TUP" se rechaza como duplicado de "tup" en vez de crear un directorio que en
    Windows termina siendo el mismo que el de "tup" pero con otro `.env` adentro."""
    if tenant_id in _TENANT_ID_RESERVADOS:
        return "El id de campus no puede estar vacío, ni ser '.' o '..'."
    if not _TENANT_ID_RE.match(tenant_id):
        return ("El id de campus sólo puede tener minúsculas, números y guiones "
                "('-'), sin espacios, mayúsculas ni barras (ej: 'otra-utn'). Recibí "
                f"{tenant_id!r}.")
    actuales = tenants() if existentes is None else existentes
    lower = tenant_id.lower()
    for t in actuales:
        if str(t.get("id", "")).lower() == lower:
            return (f"El campus '{tenant_id}' colisiona con el ya registrado "
                     f"'{t.get('id')}' (los ids se comparan sin importar mayúsculas, "
                     "porque en Windows son el mismo directorio en disco).")
    return None


def registrar_tenant(tenant_id: str, nombre: str, url: str) -> dict:
    """Agrega un tenant nuevo al registro. Lanza `ValueError` si el id es inválido
    (ver `validar_tenant_id`) o ya existe (exacto o por colisión de mayúsculas) —
    nunca pisa uno existente. Valida acá TAMBIÉN (no sólo en la tool de server.py):
    este es el punto real de escritura, y confiar sólo en que el caller ya validó es
    lo que un día deja pasar un id malo por un camino que nadie pensó."""
    actuales = tenants()
    error = validar_tenant_id(tenant_id, actuales)
    if error:
        raise ValueError(error)
    entrada = {"id": tenant_id, "nombre": nombre, "url": url}
    actuales.append(entrada)
    os.makedirs(HOME, exist_ok=True)
    with open(_TENANTS_PATH, "w", encoding="utf-8") as fh:
        json.dump(actuales, fh, ensure_ascii=False, indent=2)
    return entrada


# --- Paths por tenant ---

def tenant_dir(tenant_id: str | None = None) -> str:
    """Directorio de datos del tenant (o del activo si no se pasa uno)."""
    return os.path.join(HOME, tenant_id or tenant_activo())


def db_path(tenant_id: str | None = None) -> str:
    return os.path.join(tenant_dir(tenant_id), "datos.db")


def mis_datos_path(tenant_id: str | None = None) -> str:
    return os.path.join(tenant_dir(tenant_id), "mis_datos.json")


def salidas_dir(tenant_id: str | None = None) -> str:
    return os.path.join(tenant_dir(tenant_id), "salidas")


def env_path(tenant_id: str | None = None) -> str:
    return os.path.join(tenant_dir(tenant_id), ".env")


def _leer_env_file(path: str) -> dict[str, str]:
    vals: dict[str, str] = {}
    if not os.path.exists(path):
        return vals
    with open(path, encoding="utf-8") as fh:
        for linea in fh:
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                k, _, v = linea.partition("=")
                vals[k.strip()] = v.strip()
    return vals


def leer_env(tenant_id: str | None = None) -> dict[str, str]:
    """Lee el `.env` de un tenant DIRECTO del archivo — nunca a través de
    `os.environ`, que es compartido por TODO el proceso y no debe ser la fuente de
    verdad de las credenciales de un tenant puntual (mezclaría credenciales entre
    campus tras un `usar_campus`). Fallback: sólo para `tup`, si todavía no tiene su
    propio `.env` (la migración no corrió o no había nada que migrar), lee el `.env`
    plano legacy en la raíz de `HOME`."""
    tid = tenant_id or tenant_activo()
    vals = _leer_env_file(env_path(tid))
    if not vals and tid == _TENANT_DEFAULT_ID:
        vals = _leer_env_file(os.path.join(HOME, ".env"))
    return vals


def tiene_env(tenant_id: str | None = None) -> bool:
    """True si el tenant tiene su PROPIO `.env` (existe el archivo, tenga o no las
    claves que se le pidan). Para `tup` cuenta también el `.env` plano legacy en la
    raíz de `HOME` (mismo fallback que `leer_env`).

    Distingue dos situaciones que `leer_env` por sí sola no separa: "este tenant
    todavía no tiene ningún `.env`" (único caso legítimo para mirar `os.environ` como
    resto legacy de un tutor que exportó variables a mano) de "este tenant SÍ tiene su
    `.env`, sólo que le faltan campos puntuales" (que debe leerse como 'no
    configurado', nunca heredar el valor de otro tenant desde `os.environ`)."""
    tid = tenant_id or tenant_activo()
    if os.path.exists(env_path(tid)):
        return True
    if tid == _TENANT_DEFAULT_ID and os.path.exists(os.path.join(HOME, ".env")):
        return True
    return False


# --- Qué tenant, si alguno, "contamina" os.environ en este proceso ---
# `server.py` puebla `os.environ` con el `.env` del tenant activo al importar
# (`_cargar_env`) y lo vuelve a pisar cada vez que escribe credenciales nuevas
# (`_escribir_env`). Ese `os.environ` queda VIVO para todo el proceso, así que si el
# tutor después conmuta a OTRO tenant (`usar_campus`) que no tiene sus propias
# credenciales, `os.environ` sigue teniendo las del tenant anterior — y un fallback
# ingenuo a `os.environ` se las presta en silencio. Este flag registra de QUIÉN son
# los valores que hay ahora mismo en `os.environ`, para que ese fallback sólo se use
# cuando de verdad es indistinguible del caso legacy genuino (nadie cargó todavía el
# `.env` de NINGÚN tenant ahí adentro).
_tenant_en_os_environ: str | None = None


def marcar_tenant_en_os_environ(tenant_id: str) -> None:
    """Registra que `os.environ` fue poblado (o refrescado) con el `.env` propio de
    `tenant_id`. Llamarlo SÓLO cuando de verdad se acaban de mezclar valores del
    `.env` de ese tenant puntual a `os.environ` (nunca por las dudas)."""
    global _tenant_en_os_environ
    _tenant_en_os_environ = tenant_id


def os_environ_es_de(tenant_id: str) -> bool:
    """True si `os.environ` es una fuente de credenciales segura para `tenant_id`:
    o bien nunca se cargó ahí el `.env` de NINGÚN tenant en este proceso (caso legacy
    genuino: tutor que exportó variables a mano y nunca tocó multi-tenant), o el
    último tenant cuyo `.env` se mezcló ahí es justo éste. Cualquier otro caso
    significa que `os.environ` tiene puestas las credenciales de OTRO tenant."""
    return _tenant_en_os_environ in (None, tenant_id)


# --- Migración legacy (flat) -> `HOME/tup/` ---
# Explícita y llamable sola (para tests contra un HOME temporal) y también invocada una
# vez al importar server.py. Sólo actúa si hay datos flat de verdad (.env, mis_datos.json,
# datos.db o salidas/ en HOME) y es idempotente vía CHEQUEO DE FRESCURA (mtime), no vía
# "¿ya existe el destino?": si el legacy es más nuevo que la copia por-tenant, se vuelve a
# copiar (pisa el destino con la versión más nueva) — así un tutor que siguió usando el
# código single-tenant viejo después de una migración previa no queda con una foto vieja
# para siempre. NUNCA borra ni toca los originales.

def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return -1.0


def _dir_mtime_max(path: str) -> float:
    """mtime más nuevo entre todos los archivos de un directorio (recursivo), o -1 si
    no existe/está vacío. Sirve para decidir frescura de `salidas/`, que es un
    directorio y no tiene un único mtime comparable."""
    mx = -1.0
    if not os.path.isdir(path):
        return mx
    for raiz, _, archivos in os.walk(path):
        for f in archivos:
            mx = max(mx, _mtime(os.path.join(raiz, f)))
    return mx


def _copiar_si_hace_falta(origen: str, destino: str) -> bool:
    """Copia `origen` -> `destino` si el destino no existe todavía, o si `origen`
    tiene mtime más nuevo que `destino` (re-migración por uso del código viejo).
    Nunca copia al revés. Devuelve True si copió algo."""
    if not os.path.exists(origen):
        return False
    if os.path.exists(destino) and _mtime(origen) <= _mtime(destino):
        return False
    shutil.copy2(origen, destino)
    return True


def migrar_legacy_a_tup() -> bool:
    """Copia `.env`, `mis_datos.json`, `datos.db` y `salidas/` del layout flat viejo a
    `HOME/tup/`, archivo por archivo, sólo los que hagan falta (ausentes o legacy más
    nuevo). Devuelve True si copió algo, False si no había nada que migrar o el
    destino ya está al día."""
    env_legacy = os.path.join(HOME, ".env")
    mis_datos_legacy = os.path.join(HOME, "mis_datos.json")
    db_legacy = os.path.join(HOME, "datos.db")
    salidas_legacy = os.path.join(HOME, "salidas")

    # Dispara también si sólo hay `datos.db`/`salidas/` sin `.env` ni `mis_datos.json`
    # — un tutor que operó siempre con env vars exportadas a mano (nunca guardó
    # `mis_datos` por la tool) igual tiene caché/salidas flat que merece migrarse.
    hay_legacy = (os.path.exists(env_legacy) or os.path.exists(mis_datos_legacy)
                  or os.path.exists(db_legacy) or os.path.isdir(salidas_legacy))
    if not hay_legacy:
        return False  # instalación nueva, nada que migrar

    destino = tenant_dir(_TENANT_DEFAULT_ID)
    os.makedirs(destino, exist_ok=True)
    copio_algo = False

    if _copiar_si_hace_falta(env_legacy, os.path.join(destino, ".env")):
        copio_algo = True
    if _copiar_si_hace_falta(mis_datos_legacy, os.path.join(destino, "mis_datos.json")):
        copio_algo = True
    if _copiar_si_hace_falta(db_legacy, os.path.join(destino, "datos.db")):
        copio_algo = True

    if os.path.isdir(salidas_legacy):
        destino_salidas = os.path.join(destino, "salidas")
        if not os.path.isdir(destino_salidas) or \
                _dir_mtime_max(salidas_legacy) > _dir_mtime_max(destino_salidas):
            shutil.copytree(salidas_legacy, destino_salidas, dirs_exist_ok=True)
            copio_algo = True

    return copio_algo


def _conectar(tenant_id: str | None = None) -> sqlite3.Connection:
    d = tenant_dir(tenant_id)
    os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(db_path(tenant_id))
    con.row_factory = sqlite3.Row
    return con


# Esquema mínimo: sin tutor_id, sin tablas de auth/vault/conversaciones (eso era del
# copiloto multi-tenant). Solo lo que alimenta buscar_alumno y los tableros.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha         TEXT,
    comision      TEXT,
    assign_id     TEXT,
    tarea         TEXT,
    participantes INTEGER,
    entregados    INTEGER,
    calificados   INTEGER,
    pendientes    INTEGER,
    datos_json    TEXT
);

CREATE TABLE IF NOT EXISTS alumnos (
    email          TEXT PRIMARY KEY,
    nombre         TEXT,
    comision       TEXT,
    ultimo_acceso  TEXT,
    actualizado_at TEXT
);

CREATE TABLE IF NOT EXISTS entregas (
    email     TEXT,
    comision  TEXT,
    assign_id TEXT,
    tarea     TEXT,
    estado    TEXT,
    nota      TEXT,
    pendiente INTEGER
);

-- Bitácora de correcciones. A diferencia de `entregas` (que es una foto del estado
-- actual y se pisa entera en cada snapshot), esto es HISTÓRICO y sólo crece: cada nota
-- cargada queda con su devolución y los temas que se le marcaron al alumno.
-- Para qué: cuando media comisión falla en lo mismo, el problema no son los alumnos —
-- es que ese tema no quedó bien explicado. Ese dato sólo se puede ver acumulando, y se
-- pierde para siempre si no se guarda en el momento de corregir.
CREATE TABLE IF NOT EXISTS correcciones (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha         TEXT,
    course_id     INTEGER,
    assign_id     TEXT,
    tarea         TEXT,
    comision      TEXT,
    email         TEXT,
    alumno        TEXT,
    nota          TEXT,
    devolucion    TEXT,
    etiquetas     TEXT   -- JSON: ["perimetro-circulo", "conversion-unidades"]
);

-- Cola de una sesión de corrección. Se va llenando alumno por alumno SIN tocar Moodle, y
-- se escribe todo junto al final con una sola confirmación. Es persistente a propósito:
-- corregir 15 TPs no entra en una sentada, y si se corta la sesión el trabajo hecho no se
-- puede perder.
CREATE TABLE IF NOT EXISTS cola_correccion (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    creada_at  TEXT,
    assign_id  TEXT,
    tarea      TEXT,
    group_id   INTEGER,
    comision   TEXT,
    email      TEXT,
    alumno     TEXT,
    nota       TEXT,
    devolucion TEXT,
    etiquetas  TEXT,
    estado     TEXT,   -- pendiente | anotado | escrito | error
    resultado  TEXT,   -- detalle del error si estado = 'error'
    UNIQUE(assign_id, group_id, email)
);

CREATE INDEX IF NOT EXISTS idx_cola_estado ON cola_correccion(estado);
CREATE INDEX IF NOT EXISTS idx_snapshots_fecha ON snapshots(fecha);
CREATE INDEX IF NOT EXISTS idx_snapshots_com_assign ON snapshots(comision, assign_id);
CREATE INDEX IF NOT EXISTS idx_entregas_email ON entregas(email);
CREATE INDEX IF NOT EXISTS idx_correcciones_assign ON correcciones(assign_id, comision);
CREATE INDEX IF NOT EXISTS idx_correcciones_curso ON correcciones(course_id);
"""


def _init_db() -> None:
    os.makedirs(salidas_dir(), exist_ok=True)
    con = _conectar()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()


async def init_db() -> None:
    """Crea el directorio de datos + las tablas si no existen."""
    await asyncio.to_thread(_init_db)


# --- "Mis datos": config del tutor en un JSON local (human-editable) ---

def _get_mis_datos() -> dict | None:
    try:
        with open(mis_datos_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


async def get_mis_datos() -> dict | None:
    return await asyncio.to_thread(_get_mis_datos)


def _set_mis_datos(datos: dict) -> None:
    os.makedirs(tenant_dir(), exist_ok=True)
    with open(mis_datos_path(), "w", encoding="utf-8") as fh:
        json.dump(datos, fh, ensure_ascii=False, indent=2)


async def set_mis_datos(datos: dict) -> None:
    await asyncio.to_thread(_set_mis_datos, datos)


async def mis_datos_actualizada() -> str | None:
    """mtime del mis_datos.json (ISO), o None si no existe."""
    def _stat() -> str | None:
        try:
            ts = os.path.getmtime(mis_datos_path())
        except OSError:
            return None
        return datetime.datetime.fromtimestamp(ts).isoformat()

    return await asyncio.to_thread(_stat)


# --- snapshots ---

def _guardar_snapshot(fila: dict[str, Any]) -> int:
    datos = fila.get("datos_json")
    if datos is not None and not isinstance(datos, str):
        datos = json.dumps(datos, ensure_ascii=False)
    con = _conectar()
    try:
        cur = con.execute(
            "INSERT INTO snapshots "
            "(fecha, comision, assign_id, tarea, participantes, entregados, "
            "calificados, pendientes, datos_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fila.get("fecha") or _ahora(),
                fila.get("comision"),
                fila.get("assign_id"),
                fila.get("tarea"),
                fila.get("participantes"),
                fila.get("entregados"),
                fila.get("calificados"),
                fila.get("pendientes"),
                datos,
            ),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


async def guardar_snapshot(fila: dict[str, Any]) -> int:
    return await asyncio.to_thread(_guardar_snapshot, fila)


def _ultimo_snapshot() -> list[dict]:
    """Último snapshot por (comision, assign_id), tomando el id más reciente."""
    con = _conectar()
    try:
        cur = con.execute(
            "SELECT s.* FROM snapshots s "
            "JOIN (SELECT comision, assign_id, MAX(id) AS mid FROM snapshots "
            "GROUP BY comision, assign_id) u "
            "ON s.id = u.mid "
            "ORDER BY s.comision, s.assign_id"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


async def ultimo_snapshot() -> list[dict]:
    return await asyncio.to_thread(_ultimo_snapshot)


# --- entregas (caché por alumno para la traza, llenada por el snapshot) ---

def _reemplazar_entregas(filas: list[dict]) -> int:
    """Reescribe TODA la caché de entregas en una transacción (delete + insert).
    Single-tenant: es la del único tutor, así que se borra completa y se reinserta."""
    con = _conectar()
    try:
        con.execute("DELETE FROM entregas")
        con.executemany(
            "INSERT INTO entregas "
            "(email, comision, assign_id, tarea, estado, nota, pendiente) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    (f.get("email") or "").lower(),
                    f.get("comision"),
                    f.get("assign_id"),
                    f.get("tarea"),
                    f.get("estado"),
                    f.get("nota"),
                    1 if f.get("pendiente") else 0,
                )
                for f in filas
            ],
        )
        con.commit()
        return len(filas)
    finally:
        con.close()


async def reemplazar_entregas(filas: list[dict]) -> int:
    return await asyncio.to_thread(_reemplazar_entregas, filas)


def _entregas_previas(comision: str, assign_id: str) -> list[dict]:
    """Filas de entregas ya cacheadas de una (comisión, tarea). Sirve para REUSAR el
    dato previo cuando una tarea falla en el snapshot (no perderlo por un timeout)."""
    con = _conectar()
    try:
        rows = con.execute(
            "SELECT email, comision, assign_id, tarea, estado, nota, pendiente "
            "FROM entregas WHERE comision = ? AND assign_id = ?",
            (comision, str(assign_id)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


async def entregas_previas(comision: str, assign_id: str) -> list[dict]:
    return await asyncio.to_thread(_entregas_previas, comision, assign_id)


# --- alumnos ---

def _upsert_alumno(
    email: str,
    nombre: str | None = None,
    comision: str | None = None,
    ultimo_acceso: str | None = None,
) -> None:
    con = _conectar()
    try:
        con.execute(
            "INSERT INTO alumnos (email, nombre, comision, ultimo_acceso, actualizado_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "nombre = COALESCE(excluded.nombre, alumnos.nombre), "
            "comision = COALESCE(excluded.comision, alumnos.comision), "
            "ultimo_acceso = COALESCE(excluded.ultimo_acceso, alumnos.ultimo_acceso), "
            "actualizado_at = excluded.actualizado_at",
            (email.lower(), nombre, comision, ultimo_acceso, _ahora()),
        )
        con.commit()
    finally:
        con.close()


async def upsert_alumno(
    email: str,
    nombre: str | None = None,
    comision: str | None = None,
    ultimo_acceso: str | None = None,
) -> None:
    await asyncio.to_thread(_upsert_alumno, email, nombre, comision, ultimo_acceso)


def _traza_alumno(email: str) -> dict | None:
    """Identidad (de alumnos) + entregas por tarea (de entregas), con el shape que
    consume buscar_alumno: {nombre, comision, ultimo_acceso, entregas[], pendientes[]}."""
    em = email.lower()
    con = _conectar()
    try:
        ident = con.execute("SELECT * FROM alumnos WHERE email = ?", (em,)).fetchone()
        rows = con.execute(
            "SELECT tarea, estado, nota, pendiente FROM entregas "
            "WHERE email = ? ORDER BY rowid",
            (em,),
        ).fetchall()
        if ident is None and not rows:
            return None
        ident = dict(ident) if ident else {}
        return {
            "email": em,
            "nombre": ident.get("nombre") or em,
            "comision": ident.get("comision"),
            "ultimo_acceso": ident.get("ultimo_acceso"),
            "entregas": [
                {"tarea": r["tarea"], "estado": r["estado"], "nota": r["nota"]}
                for r in rows
            ],
            "pendientes": [r["tarea"] for r in rows if r["pendiente"]],
        }
    finally:
        con.close()


async def traza_alumno(email: str) -> dict | None:
    return await asyncio.to_thread(_traza_alumno, email)


# --- cola de corrección (sesión en curso) ---

def _cola_abrir(assign_id: str, tarea: str, group_id: int, comision: str | None,
                alumnos: list[dict], reemplazar: bool) -> dict:
    con = _conectar()
    try:
        if reemplazar:
            con.execute("DELETE FROM cola_correccion WHERE assign_id = ? AND group_id = ?",
                        (str(assign_id), group_id))
        nuevos = 0
        for a in alumnos:
            # INSERT OR IGNORE: si el alumno ya estaba en la cola (sesión retomada) se
            # conserva lo que se le había anotado en vez de pisarlo con 'pendiente'.
            cur = con.execute(
                "INSERT OR IGNORE INTO cola_correccion (creada_at, assign_id, tarea, "
                "group_id, comision, email, alumno, estado) VALUES (?,?,?,?,?,?,?,'pendiente')",
                (_ahora(), str(assign_id), tarea, group_id, comision,
                 (a.get("email") or "").lower(), a.get("nombre")))
            nuevos += cur.rowcount
        con.commit()
        tot = con.execute(
            "SELECT COUNT(*) c FROM cola_correccion WHERE assign_id=? AND group_id=?",
            (str(assign_id), group_id)).fetchone()["c"]
    finally:
        con.close()
    return {"en_cola": tot, "agregados": nuevos}


def _cola_siguiente(assign_id: str | None, group_id: int | None) -> dict | None:
    where = "estado = 'pendiente'"
    params: list = []
    if assign_id:
        where += " AND assign_id = ?"; params.append(str(assign_id))
    if group_id is not None:
        where += " AND group_id = ?"; params.append(group_id)
    con = _conectar()
    try:
        f = con.execute(f"SELECT * FROM cola_correccion WHERE {where} ORDER BY id LIMIT 1",
                        params).fetchone()
        return dict(f) if f else None
    finally:
        con.close()


def _cola_anotar(assign_id: str, group_id: int, email: str, nota: str,
                 devolucion: str, etiquetas: list) -> bool:
    """Anota (o RE-anota) la corrección de un alumno de la cola.

    Acepta también las filas en estado 'error' y 'salteado', y ese detalle no es menor:
    `confirmar_cola` promete "las fallidas quedaron en la cola, arreglá el motivo y volvé a
    confirmar", pero si acá sólo se aceptaran 'pendiente'/'anotado' esa promesa sería
    mentira — la fila fallida quedaría en un estado del que no se puede salir y el tutor no
    tendría forma de corregir el error que la propia herramienta le señaló.
    Las 'escrito' NO se aceptan a propósito: para cambiar una nota ya cargada está
    `cargar_nota`, y reabrirlas acá permitiría duplicar escrituras sin querer."""
    con = _conectar()
    try:
        cur = con.execute(
            "UPDATE cola_correccion SET nota=?, devolucion=?, etiquetas=?, "
            "estado='anotado', resultado=NULL "
            "WHERE assign_id=? AND group_id=? AND email=? "
            "AND estado IN ('pendiente','anotado','error','salteado')",
            (nota, devolucion, json.dumps(etiquetas or [], ensure_ascii=False),
             str(assign_id), group_id, (email or "").lower()))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def _cola_saltear(assign_id: str, group_id: int, email: str, motivo: str) -> bool:
    """Saca a un alumno de la cola SIN calificarlo.

    Hace falta porque no todo lo que está pendiente se puede corregir: alguien que subió
    el archivo equivocado (pasó: un alumno entregó los apuntes de la cátedra en vez de su
    TP) no merece ni Aprobado ni Desaprobado — necesita que le avisen. Sin esta salida, la
    cola devolvía siempre a la misma persona y la única forma de avanzar era ponerle una
    nota que no correspondía."""
    con = _conectar()
    try:
        cur = con.execute(
            "UPDATE cola_correccion SET estado='salteado', resultado=? "
            "WHERE assign_id=? AND group_id=? AND email=? AND estado IN ('pendiente','anotado')",
            (motivo, str(assign_id), group_id, (email or "").lower()))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


async def cola_saltear(assign_id, group_id, email, motivo):
    return await asyncio.to_thread(_cola_saltear, assign_id, group_id, email, motivo)


def _cola_listar(assign_id: str | None = None, group_id: int | None = None,
                 estados: tuple = ("pendiente", "anotado", "escrito", "error")) -> list[dict]:
    where = f"estado IN ({','.join('?' * len(estados))})"
    params: list = list(estados)
    if assign_id:
        where += " AND assign_id = ?"; params.append(str(assign_id))
    if group_id is not None:
        where += " AND group_id = ?"; params.append(group_id)
    con = _conectar()
    try:
        filas = con.execute(
            f"SELECT * FROM cola_correccion WHERE {where} ORDER BY id", params).fetchall()
    finally:
        con.close()
    out = []
    for f in filas:
        d = dict(f)
        try:
            d["etiquetas"] = json.loads(d.get("etiquetas") or "[]")
        except (ValueError, TypeError):
            d["etiquetas"] = []
        out.append(d)
    return out


def _cola_marcar(fila_id: int, estado: str, resultado: str | None = None) -> None:
    con = _conectar()
    try:
        con.execute("UPDATE cola_correccion SET estado=?, resultado=? WHERE id=?",
                    (estado, resultado, fila_id))
        con.commit()
    finally:
        con.close()


def _cola_limpiar(assign_id: str | None = None, group_id: int | None = None) -> int:
    where, params = "1=1", []
    if assign_id:
        where += " AND assign_id = ?"; params.append(str(assign_id))
    if group_id is not None:
        where += " AND group_id = ?"; params.append(group_id)
    con = _conectar()
    try:
        cur = con.execute(f"DELETE FROM cola_correccion WHERE {where}", params)
        con.commit()
        return cur.rowcount
    finally:
        con.close()


async def cola_abrir(assign_id, tarea, group_id, comision, alumnos, reemplazar=False):
    return await asyncio.to_thread(_cola_abrir, assign_id, tarea, group_id, comision,
                                   alumnos, reemplazar)


async def cola_siguiente(assign_id=None, group_id=None):
    return await asyncio.to_thread(_cola_siguiente, assign_id, group_id)


async def cola_anotar(assign_id, group_id, email, nota, devolucion, etiquetas):
    return await asyncio.to_thread(_cola_anotar, assign_id, group_id, email, nota,
                                   devolucion, etiquetas)


async def cola_listar(assign_id=None, group_id=None, estados=("pendiente", "anotado",
                                                              "escrito", "error")):
    return await asyncio.to_thread(_cola_listar, assign_id, group_id, estados)


async def cola_marcar(fila_id, estado, resultado=None):
    await asyncio.to_thread(_cola_marcar, fila_id, estado, resultado)


async def cola_limpiar(assign_id=None, group_id=None):
    return await asyncio.to_thread(_cola_limpiar, assign_id, group_id)


# --- correcciones (bitácora histórica) ---

def _guardar_correccion(reg: dict) -> None:
    con = _conectar()
    try:
        con.execute(
            "INSERT INTO correcciones (fecha, course_id, assign_id, tarea, comision, "
            "email, alumno, nota, devolucion, etiquetas) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_ahora(), reg.get("course_id"), str(reg.get("assign_id") or ""),
             reg.get("tarea"), reg.get("comision"), (reg.get("email") or "").lower(),
             reg.get("alumno"), reg.get("nota"), reg.get("devolucion"),
             json.dumps(reg.get("etiquetas") or [], ensure_ascii=False)),
        )
        con.commit()
    finally:
        con.close()


async def guardar_correccion(reg: dict) -> None:
    await asyncio.to_thread(_guardar_correccion, reg)


# Correcciones mínimas para que un porcentaje signifique algo. Por debajo de esto no se
# marca nada como sistémico: con 2 corregidos, 1 error da 50% y sugeriría rehacer la clase
# por una sola persona.
_MUESTRA_MINIMA = 5


def _errores_frecuentes(course_id: int | None = None, assign_id: str | None = None,
                        comision: str | None = None) -> dict:
    """Agrega las etiquetas de las correcciones ya hechas.

    El porcentaje se calcula sobre los alumnos CORREGIDOS, no sobre los que tienen el
    error: lo que importa pedagógicamente no es "8 alumnos se equivocaron" sino "8 de 12",
    que es cuando deja de ser un problema individual."""
    where, params = [], []
    if course_id:
        where.append("course_id = ?"); params.append(course_id)
    if assign_id:
        where.append("assign_id = ?"); params.append(str(assign_id))
    if comision:
        where.append("comision = ?"); params.append(comision)
    sql = "SELECT tarea, assign_id, comision, alumno, nota, etiquetas FROM correcciones"
    if where:
        sql += " WHERE " + " AND ".join(where)

    con = _conectar()
    try:
        filas = con.execute(sql, params).fetchall()
    finally:
        con.close()

    # DEDUPLICAR por (alumno, tarea): recargar una nota —para corregir la devolución, o
    # tras un fallo -- deja OTRA fila en la bitácora, y contando filas el mismo alumno
    # pesaba doble. Eso inflaba `alumnos_afectados` y hacía ver como "media comisión" lo
    # que era una persona cargada dos veces. Nos quedamos con el registro más reciente.
    ultimas: dict[tuple, object] = {}
    for f in filas:
        ultimas[(f["alumno"], f["assign_id"])] = f
    filas = list(ultimas.values())

    corregidas = len(filas)
    conteo: dict[str, set] = {}
    for f in filas:
        try:
            etiquetas = json.loads(f["etiquetas"] or "[]")
        except (ValueError, TypeError):
            etiquetas = []
        # set: si una etiqueta se repite dentro de la MISMA corrección, es un alumno solo.
        for e in etiquetas:
            conteo.setdefault(str(e), set()).add(f["alumno"])

    items = []
    for etiqueta, alumnos in conteo.items():
        pct = round(100 * len(alumnos) / corregidas) if corregidas else 0
        items.append({
            "tema": etiqueta,
            "alumnos_afectados": len(alumnos),
            "de_corregidos": corregidas,
            "porcentaje": pct,
            # Un porcentaje sobre 2 correcciones no significa nada: 1 de 2 da 50% y
            # marcaría "reforzalo con toda la comisión" porque una persona se equivocó.
            # Recién con MUESTRA_MINIMA el número empieza a decir algo.
            "sistemico": pct >= 40 and corregidas >= _MUESTRA_MINIMA,
            "quienes": sorted(a for a in alumnos if a)[:12],
        })
    items.sort(key=lambda i: -i["alumnos_afectados"])
    return {"correcciones_registradas": corregidas, "temas": items,
            "muestra_suficiente": corregidas >= _MUESTRA_MINIMA,
            "muestra_minima": _MUESTRA_MINIMA}


async def errores_frecuentes(course_id: int | None = None, assign_id: str | None = None,
                             comision: str | None = None) -> dict:
    return await asyncio.to_thread(_errores_frecuentes, course_id, assign_id, comision)


def _buscar_alumnos(texto: str, limite: int = 8) -> list[dict]:
    """Busca alumnos por NOMBRE o email (substring) en el caché y devuelve la traza de
    cada coincidencia. Si el alumno no tiene entregas cacheadas, 'entregas' viene vacío
    pero 'comision' indica en qué comisión está."""
    q = f"%{texto.lower().strip()}%"
    con = _conectar()
    try:
        rows = con.execute(
            "SELECT email FROM alumnos WHERE lower(nombre) LIKE ? OR lower(email) LIKE ? "
            "ORDER BY nombre LIMIT ?",
            (q, q, limite),
        ).fetchall()
    finally:
        con.close()
    res = [_traza_alumno(r["email"]) for r in rows]
    return [t for t in res if t]


async def buscar_alumnos(texto: str, limite: int = 8) -> list[dict]:
    return await asyncio.to_thread(_buscar_alumnos, texto, limite)
