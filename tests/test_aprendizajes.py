"""Tests de `anotar_aprendizaje` / `aprendizajes_materia`.

Lo que se testea acá es lo único que hace que el archivo sirva: **que no se pueda marcar
algo como `confirmado` sin decir cómo se confirmó**. Sin esa barrera, "me lo dijo el
profesor" y "lo verifiqué contra el campus" terminan escritos igual, y ahí el archivo deja
de ser conocimiento y pasa a ser una segunda fuente de verdad que vence sin avisar — el
problema que el reparto de tutores en `comisiones.json` ya tiene.

Ninguno de estos casos llega a escribir: todos cortan en la validación.

Correr:  .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import atexit
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "mcp"))

# IMPORTANTE — orden de import: `server.py` corre `almacen.migrar_legacy_a_tup()` al
# importarse (side-effect de módulo). Sin fijar `MOODLE_SKILL_HOME` a un temp ANTES de
# este `import server`, la migración automática corre contra el `~/.moodle-skill` REAL
# de quien corre los tests (ver el mismo fix en tests/test_server_multicampus.py).
_HOME_IMPORT_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("MOODLE_SKILL_HOME", _HOME_IMPORT_TMP.name)
atexit.register(_HOME_IMPORT_TMP.cleanup)

import server  # noqa: E402


def correr(coro):
    return asyncio.run(coro)


class Validaciones(unittest.TestCase):
    """Todos rechazan ANTES de tocar el disco."""

    def test_estado_inventado_se_rechaza(self):
        r = correr(server.anotar_aprendizaje(77, "algo", "Mut", estado="probable"))
        self.assertIn("error", r)

    def test_confirmado_sin_verificacion_se_rechaza(self):
        """El caso que justifica el archivo: `confirmado` sin decir CÓMO no es
        confirmado, es `dicho` con mejor letra."""
        r = correr(server.anotar_aprendizaje(77, "algo", "Mut", estado="confirmado"))
        self.assertIn("error", r)
        self.assertIn("verificacion", r["error"])

    def test_dicho_no_necesita_verificacion(self):
        """`dicho` es el valor honesto al escuchar algo, y no debe exigir nada: si
        exigiera, el atajo sería mentir poniendo `confirmado`."""
        self.assertIn("dicho", server._ESTADOS_APRENDIZAJE)

    def test_regla_vacia_se_rechaza(self):
        self.assertIn("error", correr(server.anotar_aprendizaje(77, "   ", "Mut")))

    def test_quien_vacio_se_rechaza(self):
        self.assertIn("error", correr(server.anotar_aprendizaje(77, "algo", "")))


class Lectura(unittest.TestCase):
    def test_materia_sin_aprendizajes_avisa_en_vez_de_mentir(self):
        r = correr(server.aprendizajes_materia(999999))
        self.assertEqual(r["aprendizajes"], [])
        self.assertIn("aviso", r)

    def test_matematica_trae_sus_entradas(self):
        r = correr(server.aprendizajes_materia(77))
        self.assertTrue(r["materias"][0]["aprendizajes"])

    def test_toda_entrada_declara_estado_valido(self):
        cat = json.loads((RAIZ / "mcp" / "aprendizajes.json").read_text(encoding="utf-8"))
        for m in cat["materias"]:
            for a in m["aprendizajes"]:
                self.assertIn(a["estado"], server._ESTADOS_APRENDIZAJE,
                              f"{m['materia']}: {a['regla'][:40]}")

    def test_lo_confirmado_dice_como(self):
        """Una entrada `confirmado` sin `verificacion` es indistinguible de un `dicho`
        para quien la lea en seis meses."""
        cat = json.loads((RAIZ / "mcp" / "aprendizajes.json").read_text(encoding="utf-8"))
        for m in cat["materias"]:
            for a in m["aprendizajes"]:
                if a["estado"] == "confirmado":
                    self.assertTrue(a.get("verificacion", "").strip(),
                                    f"{m['materia']}: {a['regla'][:40]}")


if __name__ == "__main__":
    unittest.main()
