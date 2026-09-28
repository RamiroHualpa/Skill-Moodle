"""Fija `MOODLE_SKILL_HOME` a un directorio temporal ANTES de que ningun test file
importe `moodle.almacen` (o `server`, que lo importa).

Por que hace falta esto ademas de `tests/__init__.py`: el comando documentado en
`tests/README-dev.md` es `python -m unittest discover -s tests -v` (SIN `-t`). Bajo
esa invocacion exacta, `unittest discover` importa cada `test_*.py` como modulo de
TOP-LEVEL (p.ej. `test_almacen`), NO como miembro del paquete (`tests.test_almacen`)
— confirmado: tras el discovery, `"tests" in sys.modules` es `False` y
`"test_almacen" in sys.modules` es `True`. Un `__init__.py` de paquete solo se
ejecuta cuando el paquete en si se importa, lo cual NO pasa en este modo — asi que
`tests/__init__.py` es una red de seguridad extra (util si alguna vez se corre con
`-t .`, o via pytest), pero NO alcanza por si solo bajo el comando documentado.

La fuente de la verdad es este modulo: cada test file que llega a `moodle.almacen`
(directa o transitivamente, p.ej. via `server` o `moodle.active_ia`) debe hacer
`import _env_setup` como su PRIMERA linea de import, antes de cualquier import que
llegue a `almacen`/`server`. `almacen.HOME` es una constante de modulo calculada UNA
SOLA VEZ al importar (`os.path.expanduser(os.environ.get("MOODLE_SKILL_HOME", ...))`)
y Python cachea modulos ya importados en `sys.modules` — el PRIMER import de
`almacen` en todo el proceso gana, sin importar que haga despues cualquier otro
archivo con su propia env var. Por eso no alcanza con que cada archivo la setee
"en algun momento": tiene que ser antes de su propio import de `almacen`/`server`,
y no se puede confiar en que discover importe los archivos en un orden particular.

Este modulo NO debe importar `almacen`, `server` ni nada que llegue a ellos —
solo setea la env var — para que sea seguro importarlo primero desde cualquier
test file, sin importar cual sea el que `discover` cargue primero.

Usa `setdefault`: si quien corre la suite ya exporto `MOODLE_SKILL_HOME` a mano
(para apuntar a un fixture propio), esa eleccion se respeta.
"""

import os
import tempfile

if "MOODLE_SKILL_HOME" not in os.environ:
    os.environ["MOODLE_SKILL_HOME"] = tempfile.mkdtemp(prefix="moodle-skill-test-")
