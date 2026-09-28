"""Tests de la parte multi-campus de `mcp/server.py`: `agregar_campus` rechazando un id
duplicado SIN pegarle a la red, `usar_campus` rechazando un tenant no registrado sin
tocar el activo, y el orden de resolución de `aulas()`/`mi_comision()` (propio del
tenant -> repo-shipped SOLO para `tup` -> vacío con guía).

Todo corre contra un `MOODLE_SKILL_HOME` temporal (nunca toca `~/.moodle-skill` real) y
mockeando `ws_api`/`MobileWSClient` donde haría falta red — ninguno de estos tests habla
con un campus real. Eso se declara acá porque los otros tests del repo (`test_aprendizajes.py`)
SÍ leen archivos reales del repo (`mcp/aprendizajes.json`) a propósito; estos no.

Correr:  python -m unittest discover -s tests -v
"""

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

# IMPORTANTE — orden de import: `server.py` corre `almacen.migrar_legacy_a_tup()` como
# side-effect AL IMPORTAR (ver su docstring de módulo), y `almacen.HOME` se fija UNA
# SOLA VEZ al importar. Si `MOODLE_SKILL_HOME` no está seteado ANTES de este
# `import server`, la migración automática corre contra el `~/.moodle-skill` REAL de
# quien ejecuta los tests — esto pasó de verdad en una máquina de desarrollo (inofensivo:
# la migración sólo COPIA y se verificó byte-idéntica, pero no debe volver a pasar). Ver
# tests/_env_setup.py para el detalle completo de por qué esto tiene que ser el PRIMER
# import del archivo que pueda llegar a `almacen`/`server`, sin depender del orden en que
# `discover` importe los demás archivos de test.
import _env_setup  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))

from moodle import almacen  # noqa: E402
import server  # noqa: E402


def correr(coro):
    import asyncio
    return asyncio.run(coro)


class _ConHomeTemporal(unittest.TestCase):
    """Aísla cada test en su propio `MOODLE_SKILL_HOME`: recarga `almacen` y `server`
    para que sus constantes/pool de clientes arranquen limpios."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env_previo = os.environ.get("MOODLE_SKILL_HOME")
        os.environ["MOODLE_SKILL_HOME"] = self._tmp.name
        importlib.reload(almacen)
        importlib.reload(server)

    def tearDown(self):
        if self._env_previo is None:
            os.environ.pop("MOODLE_SKILL_HOME", None)
        else:
            os.environ["MOODLE_SKILL_HOME"] = self._env_previo
        importlib.reload(almacen)
        importlib.reload(server)
        self._tmp.cleanup()


class TestAgregarCampusIdDuplicado(_ConHomeTemporal):
    def test_rechaza_id_ya_registrado_sin_tocar_la_red(self):
        almacen.registrar_tenant("foo", "Foo Campus", "https://foo.example")
        # ws_api.descubrir_cursos NUNCA debe llamarse: el rechazo es antes de la red.
        with patch.object(server.ws_api, "descubrir_cursos",
                          new=AsyncMock(side_effect=AssertionError("no debía llamarse"))):
            r = correr(server.agregar_campus(
                tenant_id="foo", nombre="Otro nombre", url="https://otra.example",
                moodle_user="u", moodle_pass="p"))
        self.assertFalse(r["ok"])
        self.assertIn("error", r)
        # Y no se pisó el registro existente.
        entradas = [t for t in almacen.tenants() if t["id"] == "foo"]
        self.assertEqual(len(entradas), 1)
        self.assertEqual(entradas[0]["nombre"], "Foo Campus")

    def test_login_invalido_no_persiste_nada(self):
        with patch.object(server.ws_api, "descubrir_cursos",
                          new=AsyncMock(side_effect=RuntimeError("401"))):
            r = correr(server.agregar_campus(
                tenant_id="nueva", nombre="Nueva", url="https://nueva.example",
                moodle_user="u", moodle_pass="mala"))
        self.assertFalse(r["ok"])
        ids = [t["id"] for t in almacen.tenants()]
        self.assertNotIn("nueva", ids)
        self.assertFalse((Path(almacen.tenant_dir("nueva")) / ".env").exists())


class TestUsarCampus(_ConHomeTemporal):
    def test_rechaza_tenant_no_registrado_sin_cambiar_el_activo(self):
        antes = correr(server.listar_campus())
        r = correr(server.usar_campus("no-existe"))
        self.assertFalse(r["ok"])
        despues = correr(server.listar_campus())
        self.assertEqual(antes, despues)
        self.assertEqual(despues["activo"], "tup")

    def test_acepta_tenant_registrado_y_cambia_el_activo(self):
        almacen.registrar_tenant("foo", "Foo Campus", "https://foo.example")
        r = correr(server.usar_campus("foo"))
        self.assertTrue(r["ok"])
        self.assertEqual(almacen.tenant_activo(), "foo")


class TestResolucionCatalogos(_ConHomeTemporal):
    """Orden: propio del tenant -> repo-shipped (sólo si activo == tup) -> vacío+guía."""

    def test_tup_sin_override_cae_al_repo_shipped(self):
        ruta = server._ruta_catalogo("aulas.json", server._AULAS_PATH_REPO)
        self.assertEqual(ruta, server._AULAS_PATH_REPO)

    def test_tup_con_override_usa_el_propio(self):
        propio = Path(almacen.tenant_dir("tup")) / "aulas.json"
        propio.parent.mkdir(parents=True, exist_ok=True)
        propio.write_text(json.dumps({"materias": []}), encoding="utf-8")
        ruta = server._ruta_catalogo("aulas.json", server._AULAS_PATH_REPO)
        self.assertEqual(ruta, propio)

    def test_tenant_no_tup_sin_descubrimiento_no_cae_al_repo(self):
        almacen.registrar_tenant("otra", "Otra", "https://otra.example")
        almacen.set_tenant_activo("otra")
        ruta = server._ruta_catalogo("aulas.json", server._AULAS_PATH_REPO)
        self.assertIsNone(ruta)

    def test_tenant_no_tup_con_su_propio_descubrimiento(self):
        almacen.registrar_tenant("otra", "Otra", "https://otra.example")
        almacen.set_tenant_activo("otra")
        propio = Path(almacen.tenant_dir("otra")) / "comisiones.json"
        propio.parent.mkdir(parents=True, exist_ok=True)
        propio.write_text(json.dumps({"materias": []}), encoding="utf-8")
        ruta = server._ruta_catalogo("comisiones.json", server._COMISIONES_PATH_REPO)
        self.assertEqual(ruta, propio)

    def test_aulas_tool_devuelve_guia_cuando_no_hay_catalogo(self):
        almacen.registrar_tenant("otra", "Otra", "https://otra.example")
        almacen.set_tenant_activo("otra")
        r = correr(server.aulas())
        self.assertIn("error", r)
        self.assertIn("descubrir_cursos", r["error"])

    def test_mi_comision_tool_devuelve_guia_cuando_no_hay_catalogo(self):
        almacen.registrar_tenant("otra", "Otra", "https://otra.example")
        almacen.set_tenant_activo("otra")
        r = correr(server.mi_comision("cualquiera"))
        self.assertIn("error", r)


class TestConfigurarSigueImplicito(_ConHomeTemporal):
    """`configurar` no debe pedir tenant_id ni cambiar de forma: sigue operando sobre
    el tenant activo, igual que antes de este cambio (2.5)."""

    def test_configurar_valida_y_guarda_en_el_tenant_activo(self):
        with patch.object(server.ws_api, "descubrir_cursos",
                          new=AsyncMock(return_value=[{"course_id": 1, "nombre": "X"}])):
            r = correr(server.configurar(moodle_user="u", moodle_pass="p"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["cursos"], 1)
        self.assertTrue((Path(almacen.tenant_dir("tup")) / ".env").exists())

    def test_configurar_no_persiste_si_el_login_falla(self):
        with patch.object(server.ws_api, "descubrir_cursos",
                          new=AsyncMock(side_effect=RuntimeError("401"))):
            r = correr(server.configurar(moodle_user="u", moodle_pass="mala"))
        self.assertFalse(r["ok"])
        self.assertFalse((Path(almacen.tenant_dir("tup")) / ".env").exists())


class TestAislamientoEntreTenants(_ConHomeTemporal):
    """4.2 (mockeado, sin login real): dos tenants registrados directo vía
    `almacen.registrar_tenant` (bypaseando el login real de `agregar_campus`, que
    necesita credenciales de verdad) no se pisan datos entre sí al conmutar."""

    def test_switch_no_mezcla_mis_datos_de_los_dos_tenants(self):
        almacen.registrar_tenant("otra", "Otra", "https://otra.example")

        correr(almacen.set_mis_datos({"tutor": {"nombre": "Tup"}, "cursos": []}))
        self.assertEqual(correr(almacen.get_mis_datos()), {"tutor": {"nombre": "Tup"}, "cursos": []})

        correr(server.usar_campus("otra"))
        # Tenant nuevo: sin datos propios todavía (nunca ve los de tup).
        self.assertIsNone(correr(almacen.get_mis_datos()))
        correr(almacen.set_mis_datos({"tutor": {"nombre": "Otra"}, "cursos": []}))

        correr(server.usar_campus("tup"))
        self.assertEqual(correr(almacen.get_mis_datos()), {"tutor": {"nombre": "Tup"}, "cursos": []})

        correr(server.usar_campus("otra"))
        self.assertEqual(correr(almacen.get_mis_datos()), {"tutor": {"nombre": "Otra"}, "cursos": []})

    def test_storage_state_del_navegador_difiere_por_tenant(self):
        """2.7 (path-only, sin browser real): las dos sesiones de navegador de
        distintos tenants apuntan a archivos distintos."""
        from moodle import navegador
        almacen.registrar_tenant("otra", "Otra", "https://otra.example")
        p_tup = navegador._storage_path("tup")
        p_otra = navegador._storage_path("otra")
        self.assertNotEqual(p_tup, p_otra)
        self.assertIn(os.path.join("tup", ".auth"), p_tup)
        self.assertIn(os.path.join("otra", ".auth"), p_otra)


class TestCredencialesNoSeMezclanEntreTenants(_ConHomeTemporal):
    """Bug #1 del review (CRÍTICO), repro exacto: dos tenants con sus propios `.env`
    en disco, y `os.environ` ya "contaminado" con las credenciales del PRIMERO (tal
    cual queda tras `_cargar_env()` al importar, o tras un `configurar` previo en el
    mismo proceso). Antes, `_credenciales_de()` para el tenant ACTIVO leía de
    `os.environ` con `setdefault`, así que el segundo tenant terminaba logueándose
    con las credenciales del primero. Ahora cada `_cli(tenant)` tiene que armarse con
    SUS PROPIAS credenciales, leídas directo de SU `.env`, sin importar qué haya en
    `os.environ`."""

    @staticmethod
    def _restaurar_env(k: str, valor_previo: str | None) -> None:
        if valor_previo is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = valor_previo

    def _escribir_env_tenant(self, tenant_id: str, usuario: str, password: str,
                             url: str) -> None:
        d = Path(almacen.tenant_dir(tenant_id))
        d.mkdir(parents=True, exist_ok=True)
        (d / ".env").write_text(
            f"MOODLE_USER={usuario}\nMOODLE_PASS={password}\nMOODLE_URL={url}\n",
            encoding="utf-8")

    def test_dos_tenants_en_el_mismo_proceso_usan_cada_uno_sus_propias_credenciales(self):
        self._escribir_env_tenant("tup", "user-tup", "pass-tup", "https://tup.example")
        almacen.registrar_tenant("otra", "Otra", "https://otra.example")
        self._escribir_env_tenant("otra", "user-otra", "pass-otra", "https://otra.example")

        # Repro del bug real: os.environ ya viene "contaminado" con credenciales de
        # OTRO tenant/proceso (deliberadamente distintas de "tup" Y de "otra", para
        # que cualquier lectura vía os.environ sea detectable) ANTES de pedir un
        # cliente para cualquiera de los dos.
        for k in ("MOODLE_USER", "MOODLE_PASS", "MOODLE_URL"):
            self.addCleanup(self._restaurar_env, k, os.environ.get(k))
        os.environ["MOODLE_USER"] = "contaminado"
        os.environ["MOODLE_PASS"] = "contaminado"
        os.environ["MOODLE_URL"] = "https://contaminado.example"

        cli_tup = server._cli("tup")
        self.assertEqual(cli_tup._dni, "user-tup")
        self.assertEqual(cli_tup._password, "pass-tup")

        # Conmutar y pedir el cliente de "otra" sin invalidar nada a mano: tiene que
        # traer SUS credenciales, no las de "tup" que siguen pisadas en os.environ.
        correr(server.usar_campus("otra"))
        cli_otra = server._cli("otra")
        self.assertEqual(cli_otra._dni, "user-otra")
        self.assertEqual(cli_otra._password, "pass-otra")

        # Y volver a "tup" (con "otra" ya activo, os.environ sigue diciendo "tup" del
        # primer _cargar_env — el fallback legacy no debe interferir) sigue dando las
        # credenciales correctas de "tup", cacheadas o no.
        correr(server.usar_campus("tup"))
        cli_tup_de_nuevo = server._cli("tup")
        self.assertEqual(cli_tup_de_nuevo._dni, "user-tup")
        self.assertEqual(cli_tup_de_nuevo._password, "pass-tup")

    def test_credenciales_de_no_lee_os_environ_para_un_tenant_que_no_es_el_activo(self):
        self._escribir_env_tenant("tup", "user-tup", "pass-tup", "https://tup.example")
        almacen.registrar_tenant("otra", "Otra", "https://otra.example")
        # "otra" NO tiene .env propio todavía (nunca se configuró). os.environ tiene
        # las credenciales de "tup" (el activo). Pedir credenciales de "otra" -que NO
        # es el activo- no debe devolver las de os.environ (las de tup) — antes de
        # este fix habría heredado tup por el fallback si no se filtraba por tenant
        # activo; ahora tiene que quedar vacío.
        server._cargar_env()
        self.assertEqual(server._credenciales_de("otra"), {})


class TestCredencialesDeSinEnvNoHeredaDeOtroTenant(_ConHomeTemporal):
    """Issue 1, versión chica (repro del reviewer): el tenant ACTIVO no tiene NINGÚN
    `.env` propio (ej. `tenants.json` editado a mano con una entrada sin archivo —
    `agregar_campus` normal siempre escribe uno, así que esto sólo pasa con un
    registro manual/corrupto), pero `os.environ` quedó con las credenciales de OTRO
    tenant que sí se cargó antes en este mismo proceso (`tup`, vía `_cargar_env` o un
    `configurar` previo). `_credenciales_de` no debe heredarlas: un tenant sin `.env`
    propio es "no configurado", no "usá las credenciales de quien sea que haya en
    memoria"."""

    def _escribir_env_tenant(self, tenant_id: str, extra: dict) -> None:
        d = Path(almacen.tenant_dir(tenant_id))
        d.mkdir(parents=True, exist_ok=True)
        (d / ".env").write_text(
            "".join(f"{k}={v}\n" for k, v in extra.items()), encoding="utf-8")

    def test_tenant_activo_sin_env_propio_no_hereda_de_tup_ya_cargado(self):
        self._escribir_env_tenant("tup", {
            "MOODLE_USER": "user-tup", "MOODLE_PASS": "pass-tup",
            "MOODLE_URL": "https://tup.example",
        })
        server._cargar_env()  # simula lo que pasa al importar con tup activo

        # "fantasma": registrado directo (bypass de agregar_campus, que SIEMPRE
        # escribe un .env) para simular un tenants.json tocado a mano sin su archivo.
        almacen.registrar_tenant("fantasma", "Fantasma", "https://fantasma.example")
        correr(server.usar_campus("fantasma"))

        self.assertEqual(server._credenciales_de("fantasma"), {})

    def test_legacy_genuino_sin_ningun_tenant_cargado_sigue_funcionando(self):
        # Nadie llamó nunca a `_cargar_env`/`_escribir_env` con datos de un tenant
        # real en este proceso (recién reloadeado en el setUp): el único contenido de
        # os.environ es lo que un tutor legacy exportó a mano. Ese caso SIGUE
        # funcionando -- no es lo que este fix restringe.
        for k in ("MOODLE_USER", "MOODLE_PASS", "MOODLE_URL"):
            self.addCleanup(TestCredencialesNoSeMezclanEntreTenants._restaurar_env,
                             k, os.environ.get(k))
        os.environ["MOODLE_USER"] = "legacy-a-mano"
        os.environ["MOODLE_PASS"] = "legacy-pass"
        os.environ["MOODLE_URL"] = "https://legacy.example"

        creds = server._credenciales_de("tup")
        self.assertEqual(creds.get("MOODLE_USER"), "legacy-a-mano")
        self.assertEqual(creds.get("MOODLE_PASS"), "legacy-pass")


class TestActiveIANoMezclaCredencialesEntreTenants(_ConHomeTemporal):
    """Issue 1, repro exacto del reviewer: `tup` activo al arrancar el proceso con
    Active-IA configurado; se agrega "otra" con `.env` PROPIO pero sólo credenciales
    de Moodle (sin Active-IA, el caso típico de `agregar_campus` sin pasar
    `activeia_user`/`activeia_pass`); se conmuta a "otra". El cliente de Active-IA de
    "otra" NO debe armarse con las credenciales de Active-IA de `tup` que quedaron en
    `os.environ`.

    Antes de este fix, el gate en `active_ia._default_client` era "¿al tenant activo
    le faltan las claves ACTIVEIA_*?" -- cierto para "otra" aunque tenga su propio
    `.env` -- así que caía al fallback de `os.environ`, que seguía teniendo las de
    `tup`. Ahora el gate es "¿el tenant activo no tiene NINGÚN `.env` propio Y
    `os.environ` no quedó marcado con el de otro tenant?", que da False para "otra"."""

    def _escribir_env_tenant(self, tenant_id: str, extra: dict) -> None:
        d = Path(almacen.tenant_dir(tenant_id))
        d.mkdir(parents=True, exist_ok=True)
        (d / ".env").write_text(
            "".join(f"{k}={v}\n" for k, v in extra.items()), encoding="utf-8")

    def test_tenant_con_env_propio_sin_activeia_no_hereda_del_activo_al_arrancar(self):
        from moodle import active_ia

        self._escribir_env_tenant("tup", {
            "MOODLE_USER": "user-tup", "MOODLE_PASS": "pass-tup",
            "MOODLE_URL": "https://tup.example",
            "ACTIVEIA_USER": "activeia-tup", "ACTIVEIA_PASS": "activeia-pass-tup",
        })
        server._cargar_env()  # tup activo "al arrancar el proceso"

        almacen.registrar_tenant("otra", "Otra", "https://otra.example")
        self._escribir_env_tenant("otra", {
            "MOODLE_USER": "user-otra", "MOODLE_PASS": "pass-otra",
            "MOODLE_URL": "https://otra.example",
            # Sin ACTIVEIA_USER/ACTIVEIA_PASS a propósito.
        })

        correr(server.usar_campus("otra"))

        cli = active_ia._default_client("otra")
        self.assertEqual(cli._username, "")
        self.assertEqual(cli._password, "")
        self.assertNotEqual(cli._username, "activeia-tup")
        self.assertNotEqual(cli._password, "activeia-pass-tup")

    def test_tenant_activo_con_su_propio_activeia_lo_usa(self):
        """Control: un tenant que SÍ tiene sus propias credenciales de Active-IA en
        su `.env` las usa, sin que este fix las tape."""
        from moodle import active_ia

        self._escribir_env_tenant("tup", {
            "MOODLE_USER": "user-tup", "MOODLE_PASS": "pass-tup",
            "MOODLE_URL": "https://tup.example",
        })
        almacen.registrar_tenant("otra", "Otra", "https://otra.example")
        self._escribir_env_tenant("otra", {
            "MOODLE_USER": "user-otra", "MOODLE_PASS": "pass-otra",
            "MOODLE_URL": "https://otra.example",
            "ACTIVEIA_USER": "activeia-otra", "ACTIVEIA_PASS": "activeia-pass-otra",
        })
        correr(server.usar_campus("otra"))

        cli = active_ia._default_client("otra")
        self.assertEqual(cli._username, "activeia-otra")
        self.assertEqual(cli._password, "activeia-pass-otra")


class TestInvalidarClienteConIdVacio(_ConHomeTemporal):
    """Parte de bug #1/#2: `_invalidar_cliente` con un id vacío/inválido no debe
    tocar el cliente cacheado del tenant realmente activo."""

    def test_invalidar_con_id_vacio_no_afecta_al_activo(self):
        server._clientes["tup"] = object()
        marca = server._clientes["tup"]
        server._invalidar_cliente("")
        self.assertIs(server._clientes.get("tup"), marca)


class TestAgregarCampusValidaTenantId(_ConHomeTemporal):
    """Bug #2 del review (CRÍTICO): `agregar_campus` tiene que rechazar un
    `tenant_id` inválido ANTES de tocar la red o el disco — colisión de mayúsculas
    (grave en Windows, filesystem case-insensitive), vacío, `.`, `..` y path
    traversal."""

    def _agregar(self, tenant_id: str):
        with patch.object(server.ws_api, "descubrir_cursos",
                          new=AsyncMock(side_effect=AssertionError(
                              "no debía intentar loguearse con un tenant_id inválido"))):
            return correr(server.agregar_campus(
                tenant_id=tenant_id, nombre="X", url="https://x.example",
                moodle_user="u", moodle_pass="p"))

    def test_rechaza_colision_de_mayusculas_con_tup(self):
        r = self._agregar("TUP")
        self.assertFalse(r["ok"])
        self.assertNotIn("TUP", [t["id"] for t in almacen.tenants()])

    def test_rechaza_vacio(self):
        r = self._agregar("")
        self.assertFalse(r["ok"])

    def test_rechaza_punto(self):
        r = self._agregar(".")
        self.assertFalse(r["ok"])

    def test_rechaza_puntopunto(self):
        r = self._agregar("..")
        self.assertFalse(r["ok"])

    def test_rechaza_path_traversal(self):
        r = self._agregar("../escape")
        self.assertFalse(r["ok"])
        # No se escribió nada fuera de HOME.
        fuera = Path(almacen.HOME).parent / "escape"
        self.assertFalse(fuera.exists())

    def test_acepta_id_normal_con_login_ok(self):
        with patch.object(server.ws_api, "descubrir_cursos",
                          new=AsyncMock(return_value=[{"course_id": 1, "nombre": "X"}])), \
             patch.object(server.ws_api, "descubrir_comisiones",
                          new=AsyncMock(return_value=[])):
            r = correr(server.agregar_campus(
                tenant_id="otra-facu", nombre="Otra Facu", url="https://otra.example",
                moodle_user="u", moodle_pass="p"))
        self.assertTrue(r["ok"])
        self.assertIn("otra-facu", [t["id"] for t in almacen.tenants()])


class TestSeedDeCatalogoYMiComision(_ConHomeTemporal):
    """Bug #5 del review: el catálogo sembrado por `agregar_campus` tiene que traer
    `cohorte`/`vigente_hasta` (si no, `aulas()` siempre avisa "venció"), y una
    comisión sin `tutor` (recién descubierta, sin reparto todavía) no puede tirar
    `mi_comision()` abajo."""

    def test_mi_comision_no_crashea_con_comision_sin_tutor(self):
        almacen.registrar_tenant("otra", "Otra", "https://otra.example")
        correr(server.usar_campus("otra"))
        propio = Path(almacen.tenant_dir("otra")) / "comisiones.json"
        propio.parent.mkdir(parents=True, exist_ok=True)
        propio.write_text(json.dumps({
            "cohorte": "Descubierto 2026-09-28",
            "materias": [{
                "materia": "Programación I", "course_id": 1,
                "comisiones": [
                    {"comision": "A26 C1-01", "nombre_campus": "A26 C1-01",
                     "group_id": 123},
                ],
            }],
        }), encoding="utf-8")
        # No debe tirar KeyError: 'tutor' — antes de este fix, esto rompía.
        r = correr(server.mi_comision("cualquiera"))
        self.assertTrue(r.get("sin_resultado"))
        self.assertEqual(r["tutores_del_catalogo"], [])

    def test_catalogo_sembrado_por_agregar_campus_trae_vigente_hasta_y_cohorte(self):
        with patch.object(server.ws_api, "descubrir_cursos",
                          new=AsyncMock(return_value=[{"course_id": 1, "nombre": "Materia X"}])), \
             patch.object(server.ws_api, "descubrir_comisiones",
                          new=AsyncMock(return_value=[
                              {"group_id": 9, "nombre": "Comisión 1", "tipo": "comision"},
                              {"group_id": 10, "nombre": "R-Ciudad", "tipo": "regional"},
                          ])):
            r = correr(server.agregar_campus(
                tenant_id="otra-facu2", nombre="Otra Facu 2", url="https://otra2.example",
                moodle_user="u", moodle_pass="p"))
        self.assertTrue(r["ok"])

        aulas_cat = json.loads(
            (Path(almacen.tenant_dir("otra-facu2")) / "aulas.json").read_text(encoding="utf-8"))
        self.assertIn("vigente_hasta", aulas_cat)
        self.assertTrue(aulas_cat["vigente_hasta"])
        self.assertIn("cohorte", aulas_cat)

        com_cat = json.loads(
            (Path(almacen.tenant_dir("otra-facu2")) / "comisiones.json").read_text(encoding="utf-8"))
        self.assertIn("vigente_hasta", com_cat)
        comisiones = com_cat["materias"][0]["comisiones"]
        # Sólo el grupo tipo "comision" quedó, el "regional" se filtró.
        self.assertEqual(len(comisiones), 1)
        self.assertEqual(comisiones[0]["group_id"], 9)
        # `aulas()` (activo = "otra-facu2") no debería avisar que el catálogo venció.
        correr(server.usar_campus("otra-facu2"))
        with patch.object(server.ws_api, "descubrir_cursos",
                          new=AsyncMock(return_value=[{"course_id": 1, "nombre": "Materia X"}])):
            r_aulas = correr(server.aulas())
        self.assertNotIn("venció", r_aulas.get("aviso", ""))


class TestMigracionAutomaticaAlImportar(unittest.TestCase):
    """4.1 (mockeado, sin login real): en una máquina con instalación flat vieja, el
    `import server` corre la migración solo, y una tool de lectura ve los mismos
    datos que veía antes de este cambio — sin que el tutor haga nada."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env_previo = os.environ.get("MOODLE_SKILL_HOME")
        os.environ["MOODLE_SKILL_HOME"] = self._tmp.name
        # Sembrar el layout flat legacy ANTES de importar/recargar server, para que
        # la migración automática al importar tenga algo que migrar.
        home = Path(self._tmp.name)
        (home / ".env").write_text("MOODLE_USER=viejo\nMOODLE_PASS=x\n", encoding="utf-8")
        (home / "mis_datos.json").write_text(
            json.dumps({"tutor": {"nombre": "Instalación vieja"}, "cursos": []}),
            encoding="utf-8")

    def tearDown(self):
        if self._env_previo is None:
            os.environ.pop("MOODLE_SKILL_HOME", None)
        else:
            os.environ["MOODLE_SKILL_HOME"] = self._env_previo
        importlib.reload(almacen)
        importlib.reload(server)
        self._tmp.cleanup()

    def test_import_migra_solo_y_mis_datos_lee_igual_que_antes(self):
        importlib.reload(almacen)
        importlib.reload(server)  # dispara almacen.migrar_legacy_a_tup() al importar

        self.assertTrue((Path(self._tmp.name, "tup", ".env")).exists())
        self.assertTrue((Path(self._tmp.name, ".env")).exists())  # original intacto

        r = correr(server.mis_datos())
        self.assertEqual(r["datos"]["tutor"]["nombre"], "Instalación vieja")


if __name__ == "__main__":
    unittest.main()
