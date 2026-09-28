"""Fija `MOODLE_SKILL_HOME` a un directorio temporal ANTES de que `unittest discover`
importe cualquier archivo de test del paquete.

Por qué hace falta esto y no alcanza con que cada test file setee la env var antes de
su propio `import server` / `from moodle import almacen`: Python cachea los módulos ya
importados en `sys.modules`. Si `test_almacen.py` es el primero que `discover` importa
(orden alfabético u otro), su `from moodle import almacen` FIJA `almacen.HOME`
(constante de módulo, leída de `MOODLE_SKILL_HOME` al importar) — y aunque ese archivo
después recargue `almacen` con `importlib.reload` dentro de cada test (aislamiento
por-test vía `_ConHomeTemporal`), su `tearDown` lo deja reloadeado DE NUEVO con el HOME
REAL al terminar (restaura la env var previa, que si nadie la exportó a mano es
`None`). Un archivo de test posterior que haga `import server` (que a su vez hace
`from moodle import almacen`) recibe el MISMO objeto de módulo ya cacheado en
`sys.modules`, con `HOME` fijado al real — sin importar qué env var haya seteado ESE
archivo ANTES de su propio import, porque un `import`/`from ... import` que ya está en
caché no vuelve a ejecutar el módulo. Esto es justo lo que hacía que correr la suite
completa con `python -m unittest discover -s tests` (sin exportar `MOODLE_SKILL_HOME`
a mano primero) terminara creando `~/.moodle-skill/tup/` en la máquina real, aun
después de que la ronda anterior ordenara los imports DENTRO de cada archivo.

Un `tests/__init__.py` es el primer módulo que Python importa al entrar al paquete
`tests` (antes que cualquier `test_*.py` que `discover` encuentre adentro), así que
fijar acá la env var garantiza que la PRIMERA vez que `moodle.almacen` se importe en
todo el proceso de test, ya vea un `MOODLE_SKILL_HOME` temporal — pase lo que pase con
el orden en que `discover` recorra los archivos después. Sólo actúa si el que corre la
suite no exportó ya la variable a mano (`setdefault`): un tutor que SÍ la exportó (para
apuntar a un fixture propio) mantiene el control."""

import atexit
import os
import tempfile

if "MOODLE_SKILL_HOME" not in os.environ:
    _tmp_home = tempfile.TemporaryDirectory(prefix="moodle-skill-tests-")
    os.environ["MOODLE_SKILL_HOME"] = _tmp_home.name
    atexit.register(_tmp_home.cleanup)
