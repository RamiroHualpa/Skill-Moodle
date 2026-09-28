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


if __name__ == "__main__":
    unittest.main()
