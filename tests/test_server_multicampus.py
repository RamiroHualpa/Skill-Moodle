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
