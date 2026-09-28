"""Tests de `moodle/almacen.py`: identidad del tenant activo, registro de campus y la
migración idempotente del layout flat viejo (single-tenant) a `HOME/tup/`.

Por qué existe: el cambio multi-tenant-moodle mueve todos los datos locales de
`~/.moodle-skill/*` a `~/.moodle-skill/<tenant_id>/*`. Sin estos tests, un bug en la
migración pierde silenciosamente los datos de un tutor que ya tenía la skill instalada
(su `.env`, su `mis_datos.json`, su caché de snapshots).

`almacen` calcula `HOME` (y todos los paths derivados) UNA VEZ al importar, leyendo
`MOODLE_SKILL_HOME`. Para aislar cada test en su propio directorio temporal sin que un
test contamine al siguiente, cada uno setea la env var y hace `importlib.reload(almacen)`.

Correr:  python -m unittest discover -s tests -v
"""

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))

from moodle import almacen  # noqa: E402


class _ConHomeTemporal(unittest.TestCase):
    """Base: cada test corre contra un `MOODLE_SKILL_HOME` temporal propio, con
    `almacen` recargado para que sus constantes de módulo apunten ahí."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env_previo = os.environ.get("MOODLE_SKILL_HOME")
        os.environ["MOODLE_SKILL_HOME"] = self._tmp.name
        importlib.reload(almacen)

    def tearDown(self):
        if self._env_previo is None:
            os.environ.pop("MOODLE_SKILL_HOME", None)
        else:
            os.environ["MOODLE_SKILL_HOME"] = self._env_previo
        importlib.reload(almacen)
        self._tmp.cleanup()


class TestTenantActivoDefaults(_ConHomeTemporal):
    def test_home_fresco_sin_archivos_default_tup_sin_escribir(self):
        self.assertEqual(almacen.tenant_activo(), "tup")
        self.assertEqual(almacen.tenants(),
                          [{"id": "tup", "nombre": "TUP (UTN)",
                            "url": "https://tup.sied.utn.edu.ar"}])
        # Leer no debe haber creado ningún archivo.
        self.assertEqual(os.listdir(self._tmp.name), [])

    def test_set_tenant_activo_persiste_en_lectura_fresca(self):
        almacen.set_tenant_activo("foo")
        self.assertEqual(almacen.tenant_activo(), "foo")
        # Y una relectura "fresca" (releyendo el archivo) confirma la persistencia.
        with open(os.path.join(self._tmp.name, "estado.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["tenant_activo"], "foo")


class TestRegistrarTenant(_ConHomeTemporal):
    def test_registrar_tenant_aparece_en_tenants(self):
        almacen.registrar_tenant("foo", "Foo Campus", "https://foo.example")
        ids = [t["id"] for t in almacen.tenants()]
        self.assertIn("tup", ids)
        self.assertIn("foo", ids)

    def test_registrar_tenant_duplicado_no_pisa_ni_duplica(self):
        almacen.registrar_tenant("foo", "Foo Campus", "https://foo.example")
        with self.assertRaises(ValueError):
            almacen.registrar_tenant("foo", "Otro nombre", "https://otra.url")
        entradas = [t for t in almacen.tenants() if t["id"] == "foo"]
        self.assertEqual(len(entradas), 1)
        self.assertEqual(entradas[0]["nombre"], "Foo Campus")


class TestValidarTenantId(_ConHomeTemporal):
    """Bug #2 del review: un `tenant_id` sin validar permite colisión de mayúsculas
    (grave en Windows, filesystem case-insensitive) y path traversal. `tup` ya está
    registrado por default en toda instalación nueva, así que sirve para probar la
    colisión de mayúsculas sin tener que registrar nada primero."""

    def test_rechaza_colision_de_mayusculas_con_tup(self):
        self.assertIsNotNone(almacen.validar_tenant_id("TUP"))
        self.assertIsNotNone(almacen.validar_tenant_id("Tup"))

    def test_rechaza_vacio_punto_y_puntopunto(self):
        for malo in ("", ".", ".."):
            with self.subTest(malo=malo):
                self.assertIsNotNone(almacen.validar_tenant_id(malo))

    def test_rechaza_path_traversal(self):
        self.assertIsNotNone(almacen.validar_tenant_id("../escape"))

    def test_acepta_id_normal(self):
        self.assertIsNone(almacen.validar_tenant_id("otra-facu"))

    def test_registrar_tenant_rechaza_colision_de_mayusculas(self):
        almacen.registrar_tenant("foo", "Foo Campus", "https://foo.example")
        with self.assertRaises(ValueError):
            almacen.registrar_tenant("FOO", "Otro Foo", "https://otro.example")
        # No se creó ni duplicó nada: sigue habiendo un único "foo".
        ids = [t["id"] for t in almacen.tenants()]
        self.assertEqual(ids.count("foo"), 1)
        self.assertNotIn("FOO", ids)

    def test_registrar_tenant_rechaza_path_traversal(self):
        with self.assertRaises(ValueError):
            almacen.registrar_tenant("../escape", "Escape", "https://x.example")
        # No se escribió nada fuera de HOME.
        self.assertFalse(os.path.exists(os.path.join(
            os.path.dirname(self._tmp.name), "escape")))


class TestMigracionLegacy(_ConHomeTemporal):
    def _sembrar_layout_viejo(self):
        home = self._tmp.name
        Path(home, ".env").write_text("MOODLE_USER=viejo\n", encoding="utf-8")
        Path(home, "mis_datos.json").write_text('{"tutor": {"nombre": "Viejo"}}',
                                                  encoding="utf-8")
        Path(home, "datos.db").write_bytes(b"sqlite-fake-bytes")
        salidas = Path(home, "salidas")
        salidas.mkdir()
        (salidas / "informe.pdf").write_bytes(b"pdf-fake-bytes")

    def test_instalacion_fresca_no_hace_nada(self):
        self.assertFalse(almacen.migrar_legacy_a_tup())
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "tup")))

    def test_migra_copiando_sin_borrar_originales(self):
        self._sembrar_layout_viejo()
        self.assertTrue(almacen.migrar_legacy_a_tup())

        home = self._tmp.name
        destino = os.path.join(home, "tup")
        self.assertEqual(Path(destino, ".env").read_text(encoding="utf-8"),
                          "MOODLE_USER=viejo\n")
        self.assertEqual(json.loads(Path(destino, "mis_datos.json").read_text(
            encoding="utf-8")), {"tutor": {"nombre": "Viejo"}})
        self.assertEqual(Path(destino, "datos.db").read_bytes(), b"sqlite-fake-bytes")
        self.assertEqual(Path(destino, "salidas", "informe.pdf").read_bytes(),
                          b"pdf-fake-bytes")

        # Los originales siguen intactos (nunca se borran).
        self.assertEqual(Path(home, ".env").read_text(encoding="utf-8"),
                          "MOODLE_USER=viejo\n")
        self.assertTrue(Path(home, "datos.db").exists())
        self.assertTrue(Path(home, "salidas", "informe.pdf").exists())

    def test_segunda_corrida_es_no_op(self):
        self._sembrar_layout_viejo()
        self.assertTrue(almacen.migrar_legacy_a_tup())
        # Modificar el destino para probar que la segunda corrida NO lo vuelve a tocar.
        destino_env = Path(self._tmp.name, "tup", ".env")
        destino_env.write_text("MOODLE_USER=nuevo-post-migracion\n", encoding="utf-8")

        self.assertFalse(almacen.migrar_legacy_a_tup())
        self.assertEqual(destino_env.read_text(encoding="utf-8"),
                          "MOODLE_USER=nuevo-post-migracion\n")

    def test_legacy_mas_nuevo_que_la_copia_se_re_migra(self):
        """Bug #3 del review: si alguien sigue usando el código viejo single-tenant
        DESPUÉS de una migración previa (escribe en el `.env` plano), una corrida
        posterior de la migración tiene que traer eso — no quedarse con la foto vieja
        para siempre sólo porque `tup/.env` ya "existe"."""
        self._sembrar_layout_viejo()
        self.assertTrue(almacen.migrar_legacy_a_tup())

        home = Path(self._tmp.name)
        destino_env = home / "tup" / ".env"
        contenido_viejo = destino_env.read_text(encoding="utf-8")
        self.assertEqual(contenido_viejo, "MOODLE_USER=viejo\n")

        # El tutor sigue usando el código single-tenant viejo: escribe en el .env
        # plano de nuevo, con contenido DISTINTO y mtime más nuevo que la copia.
        import time
        time.sleep(0.01)
        (home / ".env").write_text("MOODLE_USER=actualizado-post-migracion\n",
                                    encoding="utf-8")

        self.assertTrue(almacen.migrar_legacy_a_tup())
        self.assertEqual(destino_env.read_text(encoding="utf-8"),
                          "MOODLE_USER=actualizado-post-migracion\n")

    def test_dispara_con_solo_datos_db_sin_env_ni_mis_datos(self):
        """Un tutor que sólo usó env vars exportadas a mano (nunca `configurar`, nunca
        `guardar_mis_datos`) puede igual tener `datos.db`/`salidas/` flat de la
        cátedra vieja — la migración tiene que alcanzarlo también, no sólo a quien
        tiene `.env`/`mis_datos.json`."""
        home = Path(self._tmp.name)
        (home / "datos.db").write_bytes(b"solo-db-sin-env-ni-mis-datos")
        self.assertTrue(almacen.migrar_legacy_a_tup())
        self.assertEqual(Path(home, "tup", "datos.db").read_bytes(),
                          b"solo-db-sin-env-ni-mis-datos")


class TestPathsPorTenant(_ConHomeTemporal):
    def test_paths_explicitos_vs_default_al_activo(self):
        home = self._tmp.name
        self.assertEqual(almacen.tenant_dir("foo"), os.path.join(home, "foo"))
        self.assertEqual(almacen.tenant_dir(), os.path.join(home, "tup"))
        almacen.set_tenant_activo("foo")
        self.assertEqual(almacen.tenant_dir(), os.path.join(home, "foo"))
        self.assertEqual(almacen.db_path("foo"), os.path.join(home, "foo", "datos.db"))
        self.assertEqual(almacen.mis_datos_path("foo"),
                          os.path.join(home, "foo", "mis_datos.json"))
        self.assertEqual(almacen.salidas_dir("foo"), os.path.join(home, "foo", "salidas"))


class TestActiveIAPoolPorTenant(_ConHomeTemporal):
    """Bug #1 del review (tercera instancia): `active_ia` tenía un singleton de
    módulo armado UNA vez desde `os.environ`, así que `activeia_user`/
    `activeia_pass` pasados a `agregar_campus` para un campus NUEVO quedaban
    ignorados en silencio tras un `usar_campus` (el singleton ya existía). Ahora es
    un pool por tenant, igual que el cliente Moodle principal — cada tenant lee sus
    propias credenciales ACTIVEIA_* directo de SU `.env`."""

    def setUp(self):
        super().setUp()
        # `active_ia` no se recarga por test (a diferencia de `almacen`): su pool
        # `_clients` es de módulo y sobreviviría entre tests con HOMEs temporales
        # distintos, devolviendo un cliente cacheado de un test anterior en vez de
        # leer el `.env` del HOME de ESTE test. Se limpia a mano acá.
        from moodle import active_ia
        active_ia._clients.clear()

    def test_cada_tenant_tiene_su_propio_cliente_activeia(self):
        from moodle import active_ia

        almacen.registrar_tenant("otra", "Otra", "https://otra.example")
        Path(almacen.tenant_dir("tup")).mkdir(parents=True, exist_ok=True)
        Path(almacen.tenant_dir("tup"), ".env").write_text(
            "ACTIVEIA_USER=user-tup\nACTIVEIA_PASS=pass-tup\n", encoding="utf-8")
        Path(almacen.tenant_dir("otra")).mkdir(parents=True, exist_ok=True)
        Path(almacen.tenant_dir("otra"), ".env").write_text(
            "ACTIVEIA_USER=user-otra\nACTIVEIA_PASS=pass-otra\n", encoding="utf-8")

        cli_tup = active_ia._get_client("tup")
        cli_otra = active_ia._get_client("otra")
        self.assertEqual(cli_tup._username, "user-tup")
        self.assertEqual(cli_otra._username, "user-otra")
        self.assertNotEqual(cli_tup._username, cli_otra._username)

        # Y son cacheados por tenant: pedir el mismo tenant de nuevo da el MISMO
        # objeto (no se reconstruye ni se mezcla).
        self.assertIs(active_ia._get_client("tup"), cli_tup)

    def test_invalidar_cliente_fuerza_reconstruccion_con_credenciales_nuevas(self):
        from moodle import active_ia

        Path(almacen.tenant_dir("tup")).mkdir(parents=True, exist_ok=True)
        env_tup = Path(almacen.tenant_dir("tup"), ".env")
        env_tup.write_text("ACTIVEIA_USER=viejo\nACTIVEIA_PASS=viejo\n", encoding="utf-8")

        cli_viejo = active_ia._get_client("tup")
        self.assertEqual(cli_viejo._username, "viejo")

        env_tup.write_text("ACTIVEIA_USER=nuevo\nACTIVEIA_PASS=nuevo\n", encoding="utf-8")
        # Sin invalidar, sigue cacheado el viejo.
        self.assertIs(active_ia._get_client("tup"), cli_viejo)

        active_ia.invalidar_cliente("tup")
        cli_nuevo = active_ia._get_client("tup")
        self.assertEqual(cli_nuevo._username, "nuevo")
        self.assertIsNot(cli_nuevo, cli_viejo)


if __name__ == "__main__":
    unittest.main()
