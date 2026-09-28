"""Tests de la lógica PURA de la skill: sin red, sin Moodle, sin credenciales.

Por qué acá y no en las funciones que consultan el campus: estas son las que deciden
**qué significa** un dato — si una nota es Aprobado, si un alumno está abandonando, si hay
una versión nueva. Todos los bugs serios encontrados hasta ahora vivían justo acá, y todos
se podían haber cazado sin tocar la red.

Correr:  .venv/bin/python -m unittest discover -s tests -v
Se usa `unittest` (stdlib) y no pytest a propósito: no agrega una dependencia que el tutor
tendría que instalar para poder verificar su propia herramienta.
"""

import json
import sys
import unittest
from pathlib import Path

# `moodle.active_ia` (importado más abajo) llega a `moodle.almacen`, cuyo `HOME` se
# fija UNA SOLA VEZ al importar. Este import tiene que ir antes de ese, en todo
# archivo que lo alcance. Ver tests/_env_setup.py para el detalle completo.
import _env_setup  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))

from moodle.cliente import MoodleWSError  # noqa: E402
from moodle.ws_api import (  # noqa: E402
    _a_html,
    _clasificar_riesgo,
    _fila_aula,
    _lectura_aula,
    _orden_aula,
    _racha_final_sin_entregar,
    _vencidas,
    _resolver_valor_nota,
    clasificar_mensaje_entrante,
    crear_discusion,
    es_actividad_de_cierre,
    nota_display,
    sin_entrar_al_aula,
)
from moodle import active_ia  # noqa: E402
from moodle import panorama  # noqa: E402
from moodle import ws_api  # noqa: E402
from moodle import informes  # noqa: E402
from moodle.active_ia import _nota_de_entrega  # noqa: E402
from moodle.version import _tupla, hay_novedad  # noqa: E402


class TestEscalaInvertida(unittest.TestCase):
    """La escala 5 de la TUP va INVERTIDA: 1=Aprobado, 2=Desaprobado.

    Es la trampa central del dominio: un número suelto se lee al revés de lo que significa.
    """

    def test_lee_la_escala_por_su_mapa(self):
        self.assertEqual(nota_display(-5, "1.00000"), "Aprobado")
        self.assertEqual(nota_display(-5, "2.00000"), "Desaprobado")

    def test_escala_desconocida_NO_devuelve_el_numero_crudo(self):
        # El bug: devolvía "1", que en cualquier otra escala se lee como la peor nota.
        r = nota_display(-7, "1.00000")
        self.assertNotIn(r.strip(), ("1", "1.0", "1.00000"))
        self.assertIn("desconocida", r.lower())

    def test_valor_fuera_de_la_escala_avisa(self):
        r = nota_display(-5, "9.00000")
        self.assertIn("9", r)
        self.assertIn("no está en la escala", r)

    def test_valor_ilegible_avisa(self):
        self.assertIn("ilegible", nota_display(-5, "chau").lower())

    def test_sin_nota_es_None_y_no_texto(self):
        # -1 es el "sin calificar" de Moodle. Confundirlo con una nota fue el bug que dejó
        # a tres alumnos como "calificados" sin calificación.
        for v in (None, "", "-1", "-1.00000"):
            self.assertIsNone(nota_display(-5, v), f"con {v!r}")

    def test_numerica_se_muestra_limpia(self):
        self.assertEqual(nota_display(100, "8.9"), "8.9")
        self.assertEqual(nota_display(100, "10.00000"), "10")


class TestResolverValorNota(unittest.TestCase):
    """Texto -> valor que espera save_grade. Es la puerta de la ESCRITURA de notas."""

    def test_acepta_el_texto_de_la_escala_sin_importar_mayusculas(self):
        for texto in ("Aprobado", "aprobado", "APROBADO"):
            valor, etiqueta, _ = _resolver_valor_nota(-5, texto)
            self.assertEqual(valor, 1.0)
            self.assertEqual(etiqueta, "Aprobado")

    def test_rechaza_una_nota_numerica_en_tarea_de_escala(self):
        # Mandar "10" a una tarea Aprobado/Desaprobado tiene que ser rechazado ANTES de
        # escribir: si llegara al campus, la nota queda sin calificar y el alumno figura
        # como corregido sin nota.
        valor, _, opciones = _resolver_valor_nota(-5, "10")
        self.assertIsNone(valor)
        self.assertIn("Aprobado", opciones)

    def test_numerica_acepta_coma_y_punto(self):
        self.assertEqual(_resolver_valor_nota(100, "9,85")[0], 9.85)
        self.assertEqual(_resolver_valor_nota(100, "9.85")[0], 9.85)


class TestRachaSinEntregar(unittest.TestCase):
    """La racha se cuenta desde la ÚLTIMA tarea, no sobre el total."""

    def test_cuenta_solo_las_del_final(self):
        estados = ["Sin entrega", "Calificado", "Calificado", "Sin entrega", "Sin entrega"]
        self.assertEqual(_racha_final_sin_entregar(estados), 2)

    def test_quien_se_puso_al_dia_no_arrastra_la_deuda_vieja(self):
        # No entregó las dos primeras y después entregó todo: NO está abandonando.
        estados = ["Sin entrega", "Sin entrega", "Calificado", "Calificado"]
        self.assertEqual(_racha_final_sin_entregar(estados), 0)

    def test_todo_sin_entregar(self):
        self.assertEqual(_racha_final_sin_entregar(["Sin entrega"] * 4), 4)

    def test_lista_vacia(self):
        self.assertEqual(_racha_final_sin_entregar([]), 0)

    def test_enviado_sin_corregir_cuenta_como_entregado(self):
        # Que el tutor no haya corregido no es culpa del alumno.
        self.assertEqual(
            _racha_final_sin_entregar(["Sin entrega", "Enviado para calificar"]), 0)


class TestClasificarRiesgo(unittest.TestCase):
    """El reloj que manda es el de la MATERIA. `dias_campus` entra sólo para distinguir al
    que elige no entrar del que no está en ninguna parte.
    """

    def test_al_dia_es_verde(self):
        nivel, _ = _clasificar_riesgo("abrio", 1, racha=0, dias_campus=1)
        self.assertEqual(nivel, "verde")

    def test_dos_tareas_seguidas_sin_entregar_es_rojo_aunque_entre_todos_los_dias(self):
        # La señal fuerte: entrar y abrir la materia sin entregar no es estar al día.
        nivel, motivos = _clasificar_riesgo("abrio", 0, racha=2, dias_campus=0)
        self.assertEqual(nivel, "rojo")
        self.assertTrue(any("seguidas" in m for m in motivos))

    def test_muchos_dias_sin_abrir_la_materia_pero_al_dia_con_las_tareas_es_amarillo(self):
        # No abre la materia hace 20 días y tampoco pisa el campus: puede estar trabajando o
        # sin acceso. No es abandono todavía, y no es "eligió no entrar".
        nivel, _ = _clasificar_riesgo("abrio", 20, racha=0, dias_campus=20)
        self.assertEqual(nivel, "amarillo")

    def test_muchos_dias_sin_abrir_mas_una_sin_entregar_es_rojo(self):
        nivel, _ = _clasificar_riesgo("abrio", 20, racha=1, dias_campus=20)
        self.assertEqual(nivel, "rojo")

    def test_una_semana_sin_abrir_la_materia_es_amarillo(self):
        self.assertEqual(
            _clasificar_riesgo("abrio", 8, racha=0, dias_campus=8)[0], "amarillo")

    def test_nunca_entro_a_nada_es_rojo_y_no_verde(self):
        # Nunca abrió la materia y nunca entró al campus: el único caso donde la frase
        # "nunca entró al campus" es verdadera.
        nivel, motivos = _clasificar_riesgo("nunca_abrio", None, racha=0, dias_campus=None)
        self.assertEqual(nivel, "rojo")
        self.assertTrue(any("nunca entró al campus" in m for m in motivos))

    def test_los_umbrales_son_configurables(self):
        self.assertEqual(
            _clasificar_riesgo("abrio", 10, 0, 10, dias_alerta=30, dias_aviso=20)[0], "verde")
        self.assertEqual(
            _clasificar_riesgo("abrio", 10, 0, 10, dias_alerta=5, dias_aviso=3)[0], "amarillo")

    # --- Los casos REALES que el código viejo daba verde (relevamiento de un tutor, 13/08) ---
    #
    # Corrido contra HEAD, que sólo veía el reloj del sitio: Carina, Cristian, Alochis, Astrid
    # y Aguirre daban **verde**, y los verdes NO se devuelven — eran invisibles. Petrozzelli
    # era el único que salía, como "9 días sin entrar", ocultando que nunca abrió la materia.

    def test_carina_entro_al_campus_hoy_y_nunca_abrio_la_materia(self):
        # El código viejo veía "0 días sin entrar, racha 0" -> verde -> no se devolvía.
        nivel, motivos = _clasificar_riesgo("nunca_abrio", None, racha=0, dias_campus=0)
        self.assertNotEqual(nivel, "verde")
        self.assertEqual(nivel, "amarillo")  # no rojo: puede haberse matriculado esta semana
        self.assertTrue(any("está cursando otra cosa" in m for m in motivos))

    def test_alochis_entro_al_campus_hoy_y_hace_10_dias_que_no_abre_la_materia(self):
        # Viejo: "0 días sin entrar" -> verde. Éste es el caso más claro de "eligió no entrar".
        nivel, motivos = _clasificar_riesgo("abrio", 10, racha=0, dias_campus=0)
        self.assertEqual(nivel, "rojo")
        self.assertTrue(any("10 días sin abrir la materia" in m for m in motivos))
        self.assertTrue(any("no perdió el acceso" in m for m in motivos))

    def test_petrozzelli_nunca_abrio_la_materia_y_hace_9_dias_que_no_pisa_el_campus(self):
        # Viejo: salía "9 días sin entrar" (amarillo) y se perdía el hecho más importante.
        # Tampoco es "eligió no entrar": hace 9 días que no aparece por Moodle.
        nivel, motivos = _clasificar_riesgo("nunca_abrio", None, racha=0, dias_campus=9)
        self.assertEqual(nivel, "amarillo")
        self.assertTrue(any("nunca abrió la materia" in m for m in motivos))
        self.assertTrue(any("tampoco" in m for m in motivos))
        self.assertFalse(any("cursando otra cosa" in m for m in motivos))

    def test_nunca_abrio_la_materia_y_encima_dejo_de_entregar_es_rojo(self):
        # Acá ya no hay duda de matriculación reciente: hubo algo que venció y no entregó.
        nivel, _ = _clasificar_riesgo("nunca_abrio", None, racha=1, dias_campus=0)
        self.assertEqual(nivel, "rojo")

    def test_nunca_abrio_y_hace_un_mes_que_no_pisa_el_campus_es_rojo(self):
        nivel, _ = _clasificar_riesgo("nunca_abrio", None, racha=0, dias_campus=30)
        self.assertEqual(nivel, "rojo")

    def test_sin_dato_de_la_materia_no_es_verde_nunca(self):
        # El cero mudo: si no se pudo leer, no se dice "está al día".
        nivel, motivos = _clasificar_riesgo("sin_dato", None, racha=0, dias_campus=0)
        self.assertEqual(nivel, "sin_datos")
        self.assertTrue(any("no se sabe" in m for m in motivos))

    def test_sin_dato_de_la_materia_pero_con_racha_sigue_siendo_rojo(self):
        # Que falte un reloj no borra la otra señal.
        self.assertEqual(
            _clasificar_riesgo("sin_dato", None, racha=2, dias_campus=0)[0], "rojo")

    def test_abrir_la_materia_hace_dos_dias_entrando_al_campus_hoy_sigue_siendo_verde(self):
        # El umbral importa: sin él, cualquier brecha marcaba a medio padrón.
        self.assertEqual(_clasificar_riesgo("abrio", 2, racha=0, dias_campus=0)[0], "verde")


class TestDesengancheDeLaMateria(unittest.TestCase):
    """El reloj del CURSO vs. el reloj del SITIO. Son dos y el campus no avisa cuál mirás.

    El bug que motivó esto: `dias_sin_entrar` es del sitio, así que el alumno que entra
    todos los días para otra materia y nunca abre la propia figuraba con `0` — al día para
    la herramienta, desaparecido en la realidad. Verificado en vivo el 2026-08-13 sobre 119
    alumnos: 10 entran al campus sin haber abierto NUNCA la materia.
    """

    @staticmethod
    def _hace(dias: float) -> int:
        import time
        return int(time.time() - dias * 86400)

    def test_sin_el_campo_es_sin_dato_y_NO_nunca_abrio(self):
        # La distinción que más importa: "no se pudo leer" no es "no la abrió". Confundirlas
        # es lo que hace ilegible el "Nunca" de la página de participantes.
        estado, dias = _lectura_aula({"lastaccess": self._hace(1)})
        self.assertEqual(estado, "sin_dato")
        self.assertIsNone(dias)

    def test_campo_en_cero_es_nunca_abrio_sin_numero_gigante(self):
        estado, dias = _lectura_aula({"lastcourseaccess": 0})
        self.assertEqual(estado, "nunca_abrio")
        # Nada de 20.000 días: el estado lo dice, el número no se inventa.
        self.assertIsNone(dias)

    def test_campo_basura_es_sin_dato_no_nunca_abrio(self):
        self.assertEqual(_lectura_aula({"lastcourseaccess": "ayer"})[0], "sin_dato")

    def test_con_timestamp_devuelve_los_dias(self):
        estado, dias = _lectura_aula({"lastcourseaccess": self._hace(10.7)})
        self.assertEqual(estado, "abrio")
        self.assertEqual(dias, 10)

    def test_entra_al_campus_y_nunca_abrio_la_materia_se_marca(self):
        # El caso estrella: para `dias_sin_entrar` estaba perfecto.
        f = _fila_aula({"lastaccess": self._hace(0.1), "lastcourseaccess": 0})
        self.assertTrue(f["entra_al_campus_sin_abrir_la_materia"])
        self.assertIn("está cursando otra cosa", f["detalle"])

    def test_entra_al_campus_pero_no_abre_la_materia_hace_dias(self):
        f = _fila_aula({"lastaccess": self._hace(0.1), "lastcourseaccess": self._hace(10.7)})
        self.assertTrue(f["entra_al_campus_sin_abrir_la_materia"])
        self.assertEqual(f["dias_sin_abrir_la_materia"], 10)
        self.assertEqual(f["dias_sin_entrar_al_campus"], 0)

    def test_el_que_no_aparece_por_ningun_lado_no_se_marca_como_activo(self):
        # No abrió la materia hace 20 días y tampoco entró al campus: es otro problema
        # (puede haberse quedado sin acceso), no "eligió no entrar".
        f = _fila_aula({"lastaccess": self._hace(20), "lastcourseaccess": self._hace(20)})
        self.assertFalse(f["entra_al_campus_sin_abrir_la_materia"])

    def test_nunca_abrio_pero_hace_41_dias_que_no_pisa_el_campus_no_es_elegir_otra_materia(self):
        # Caso real de com6 (ADRIAN FREI). La primera versión lo marcaba "está cursando otra
        # cosa" por tener el reloj del sitio más fresco que el del curso — y era falso: ése
        # no está en ninguna parte. Sigue en la lista, pero no en la de contactar.
        f = _fila_aula({"lastaccess": self._hace(41), "lastcourseaccess": 0})
        self.assertTrue(f["desenganchado_de_la_materia"])
        self.assertFalse(f["entra_al_campus_sin_abrir_la_materia"])
        self.assertIn("no aparece por ningún lado", f["detalle"])

    def test_abrir_la_materia_dos_veces_por_semana_no_es_desenganche(self):
        # Sin umbral, la sola brecha entre los dos relojes marcaba 18 de 36 alumnos de com6:
        # media comisión. Una lista así no la mira nadie — es el error de `alumnos_en_riesgo`
        # devolviendo 69/69 en rojo, repetido.
        f = _fila_aula({"lastaccess": self._hace(0), "lastcourseaccess": self._hace(4)})
        self.assertFalse(f["desenganchado_de_la_materia"])
        self.assertFalse(f["entra_al_campus_sin_abrir_la_materia"])

    def test_el_umbral_es_configurable(self):
        u = {"lastaccess": self._hace(0), "lastcourseaccess": self._hace(4)}
        self.assertTrue(_fila_aula(u, dias_desenganche=3)["entra_al_campus_sin_abrir_la_materia"])
        self.assertFalse(_fila_aula(u, dias_desenganche=10)["entra_al_campus_sin_abrir_la_materia"])

    def test_el_desfasaje_de_segundos_entre_los_dos_relojes_no_es_una_brecha(self):
        # Moodle actualiza los dos con bandas muertas de 60 s (LASTACCESS_UPDATE_SECS), así
        # que el del curso puede quedar hasta un minuto adelantado. Visto en vivo: 46 s.
        ahora = self._hace(0)
        f = _fila_aula({"lastaccess": ahora - 46, "lastcourseaccess": ahora})
        self.assertFalse(f["entra_al_campus_sin_abrir_la_materia"])
        self.assertEqual(f["dias_sin_abrir_la_materia"], 0)  # nunca negativo

    def test_nunca_entro_ni_al_campus_se_dice_completo(self):
        f = _fila_aula({"lastaccess": 0, "lastcourseaccess": 0})
        self.assertEqual(f["estado_aula"], "nunca_abrio")
        self.assertIsNone(f["dias_sin_entrar_al_campus"])
        self.assertFalse(f["entra_al_campus_sin_abrir_la_materia"])
        self.assertIn("nunca entró al campus", f["detalle"])

    def test_el_orden_pone_los_que_nunca_abrieron_arriba_y_los_sin_dato_al_final(self):
        filas = [
            _fila_aula({"lastaccess": self._hace(1), "lastcourseaccess": self._hace(3)}),
            _fila_aula({"lastaccess": self._hace(1)}),                                  # sin dato
            _fila_aula({"lastaccess": self._hace(1), "lastcourseaccess": self._hace(30)}),
            _fila_aula({"lastaccess": self._hace(2), "lastcourseaccess": 0}),           # nunca
        ]
        orden = [f["estado_aula"] for f in sorted(filas, key=_orden_aula)]
        self.assertEqual(orden, ["nunca_abrio", "abrio", "abrio", "sin_dato"])

    def test_entre_los_que_abrieron_el_mas_atrasado_va_primero(self):
        filas = [
            _fila_aula({"lastaccess": self._hace(1), "lastcourseaccess": self._hace(3)}),
            _fila_aula({"lastaccess": self._hace(1), "lastcourseaccess": self._hace(30)}),
            _fila_aula({"lastaccess": self._hace(1), "lastcourseaccess": self._hace(9)}),
        ]
        dias = [f["dias_sin_abrir_la_materia"] for f in sorted(filas, key=_orden_aula)]
        self.assertEqual(dias, [30, 9, 3])

    def test_entre_los_que_nunca_abrieron_va_primero_el_mas_activo_en_el_campus(self):
        # El que entró hoy y nunca abrió la materia es el caso más accionable de todos.
        filas = [
            _fila_aula({"lastaccess": self._hace(12), "lastcourseaccess": 0}),
            _fila_aula({"lastaccess": 0, "lastcourseaccess": 0}),
            _fila_aula({"lastaccess": self._hace(0.1), "lastcourseaccess": 0}),
        ]
        dias = [f["dias_sin_entrar_al_campus"] for f in sorted(filas, key=_orden_aula)]
        self.assertEqual(dias, [0, 12, None])


class TestQueCuentaParaLaRacha(unittest.TestCase):
    """Sólo las actividades de cierre de unidad marcan la cadencia semanal.

    Medido en vivo: 13 de 16 alumnos de una comisión no habían entregado el Integrador
    (grupal, una sola entrega al final). Contarlo en la racha marcaba en amarillo a gente
    que venía entrando todos los días y entregando todo — ruido que hace que después nadie
    mire el tablero.
    """

    def test_las_actividades_de_cierre_cuentan(self):
        for t in ("Actividad de cierre unidad 1 - Estructuras Secuenciales 🎯🏁",
                  "Actividad de cierre de la unidad 4 - Git 🎯🏁",
                  "Actividad de cierre unidad 10 - Recursividad 🎯🏁"):
            self.assertTrue(es_actividad_de_cierre(t), t)

    def test_el_integrador_y_los_parciales_NO_cuentan(self):
        for t in ("📤 Entrega del Trabajo Integrador 1",
                  "Recuperatorio Entrega del Trabajo Integrador",
                  "Parcial 2 - Programación 1: Sistema de Control de Inventario",
                  "Entrega - Extraordinaria Segundo Examen Parcial"):
            self.assertFalse(es_actividad_de_cierre(t), t)

    def test_tolera_acentos_y_mayusculas(self):
        self.assertTrue(es_actividad_de_cierre("ACTIVIDAD DE CIERRE UNIDAD 3"))
        self.assertTrue(es_actividad_de_cierre("actividad de cierre unidad 3"))


class TestClasificarMensaje(unittest.TestCase):
    """Qué mensaje espera respuesta de verdad. Textos reales de la bandeja del tutor.

    Antes se contaba como pendiente cualquier conversación donde el último en hablar fuera
    el alumno: 18 "esperando respuesta" cuando las reales eran ~3. Un contador que exagera
    seis veces se deja de mirar, y ahí se pierden las consultas que sí importan.
    """

    def test_agradecimientos_no_esperan_respuesta(self):
        for t in ("Muchas gracias 😊", "gracias!!", "okey muchas gracias Juan.",
                  "Genial, gracias!",
                  "Muchas gracias Juan!! Y gracias por tu ayuda durante toda la cursada! Saludos"):
            self.assertEqual(clasificar_mensaje_entrante(t), "cortesia", t)

    def test_agradecimiento_largo_con_saludo_adelante_igual_es_cortesia(self):
        # El saludo va primero y la cortesía después: exigir que arranque agradeciendo
        # dejaba este mensaje clasificado como consulta.
        t = ("Buenas tardes Juan! muchas gracias por todo, por el asesoramiento y estar "
             "siempre atento a lo que podia necesitar! La verdad que disfrute mucho la "
             "cursada, asique esperemos que programación II nos encuentre con la misma "
             "dinámica. Gracias nuevamente y abrazo grande!!")
        self.assertEqual(clasificar_mensaje_entrante(t), "cortesia")

    def test_gracias_SEGUIDO_DE_UNA_DUDA_es_pregunta(self):
        # El orden de evaluación importa: primero se busca si pide algo. Al revés, este
        # alumno se quedaba sin respuesta.
        self.assertEqual(
            clasificar_mensaje_entrante("muchas gracias! pero me quedó una duda con el TP"),
            "pregunta")

    def test_consultas_reales_son_pregunta(self):
        for t in ("Hola Juan buenas tardes, queria consultar si está la posibilidad de volver",
                  "Como hago para entregar el TP?",
                  "Buenas tardes Juan!! voy a rendir el final y te queria consultar la modalidad"):
            self.assertEqual(clasificar_mensaje_entrante(t), "pregunta", t)

    def test_anuncios_masivos_no_son_consultas(self):
        # Llegan por privado y figuran como mensaje "del alumno", pero nadie espera que el
        # tutor los conteste.
        for t in ("📢 Estimados estudiantes de Programación 2: Les recordamos que hoy vence",
                  "Hola! Les escribimos para recordarles las condiciones obligatorias"):
            self.assertEqual(clasificar_mensaje_entrante(t), "difusion", t)

    def test_ante_la_duda_entra_a_la_lista(self):
        # Default conservador: perder una consulta es peor que mostrar un mensaje de más.
        for t in ("llegue", "", "ya que virtual se me dificulta"):
            self.assertEqual(clasificar_mensaje_entrante(t), "pregunta", repr(t))


class TestVersionSemver(unittest.TestCase):
    def test_compara_por_numero_y_no_por_texto(self):
        # Como texto, "1.9" > "1.10". Es el bug clásico de comparar versiones.
        self.assertTrue(hay_novedad("1.9.0", "1.10.0"))
        self.assertFalse(hay_novedad("1.10.0", "1.9.0"))

    def test_misma_version_no_es_novedad(self):
        self.assertFalse(hay_novedad("1.2.0", "1.2.0"))

    def test_sin_dato_remoto_no_inventa_una_novedad(self):
        for remota in ("", "desconocida"):
            self.assertFalse(hay_novedad("1.0.0", remota))

    def test_tupla_tolera_basura(self):
        self.assertEqual(_tupla("1.2.3"), (1, 2, 3))
        self.assertEqual(_tupla("1.2.x"), (1, 2, 0))


class TestErroresFrecuentes(unittest.TestCase):
    """Bitácora de correcciones: de "este alumno se equivocó" a "esto no se explicó bien".

    Usa una base temporal (`MOODLE_SKILL_HOME`), así que no toca los datos del tutor ni
    necesita red.
    """

    @classmethod
    def setUpClass(cls):
        import asyncio
        import importlib
        import os
        import tempfile

        cls._tmp = tempfile.mkdtemp()
        cls._home_previo = os.environ.get("MOODLE_SKILL_HOME")
        os.environ["MOODLE_SKILL_HOME"] = cls._tmp
        from moodle import almacen
        importlib.reload(almacen)          # relee HOME/DB_PATH del entorno nuevo
        cls.almacen = almacen
        # OJO: el helper NO puede llamarse `run` — ese es el método con el que unittest
        # ejecuta cada test, y pisarlo rompe la corrida entera.
        cls.correr = staticmethod(asyncio.run)
        asyncio.run(almacen.init_db())

    @classmethod
    def tearDownClass(cls):
        import os
        if cls._home_previo is None:
            os.environ.pop("MOODLE_SKILL_HOME", None)
        else:
            os.environ["MOODLE_SKILL_HOME"] = cls._home_previo

    def _corregir(self, alumno, etiquetas):
        self.correr(self.almacen.guardar_correccion({
            "course_id": 74, "assign_id": "17703", "tarea": "u1", "comision": "com6",
            "email": f"{alumno}@x.com", "alumno": alumno, "nota": "Aprobado",
            "devolucion": "…", "etiquetas": etiquetas}))

    def test_arranca_vacio_sin_inventar_nada(self):
        # Agosto empieza de cero: sin correcciones no hay temas, y eso es correcto.
        r = self.correr(self.almacen.errores_frecuentes(assign_id="no-existe"))
        self.assertEqual(r["correcciones_registradas"], 0)
        self.assertEqual(r["temas"], [])

    def test_marca_sistemico_lo_que_le_pasa_a_muchos(self):
        for a, e in (("ana", ["perimetro"]), ("beto", ["perimetro"]),
                     ("caro", ["perimetro"]), ("dani", []), ("eve", ["otro-tema"])):
            self._corregir(a, e)
        r = self.correr(self.almacen.errores_frecuentes(course_id=74))
        temas = {t["tema"]: t for t in r["temas"]}
        # 3 de 5 = 60% -> el problema dejó de ser individual
        self.assertEqual(temas["perimetro"]["alumnos_afectados"], 3)
        self.assertEqual(temas["perimetro"]["porcentaje"], 60)
        self.assertTrue(temas["perimetro"]["sistemico"])
        # 1 de 5 = 20% -> caso puntual
        self.assertFalse(temas["otro-tema"]["sistemico"])

    def test_con_muestra_chica_NO_marca_sistemico(self):
        # Apareció corrigiendo de verdad: con 2 corregidos, 1 error da 50% y la tool decía
        # "conviene reforzar el tema con toda la comisión" por UNA sola persona. Un
        # porcentaje sobre 2 casos no significa nada.
        import tempfile
        from moodle import almacen as alm
        prev_db_path = alm.db_path
        db_temp = f"{tempfile.mkdtemp()}/chica.db"
        alm.db_path = lambda tenant_id=None: db_temp
        try:
            self.correr(alm.init_db())
            for a in ("uno", "dos"):
                self.correr(alm.guardar_correccion({
                    "course_id": 99, "assign_id": "x", "comision": "c", "email": f"{a}@x",
                    "alumno": a, "nota": "Aprobado", "devolucion": "",
                    "etiquetas": ["tema-x"] if a == "uno" else []}))
            r = self.correr(alm.errores_frecuentes(course_id=99))
            self.assertEqual(r["correcciones_registradas"], 2)
            self.assertFalse(r["muestra_suficiente"])
            self.assertEqual(r["temas"][0]["porcentaje"], 50)      # el % se calcula igual
            self.assertFalse(r["temas"][0]["sistemico"])           # pero NO concluye
        finally:
            alm.db_path = prev_db_path

    def test_el_porcentaje_es_sobre_los_CORREGIDOS_no_sobre_los_afectados(self):
        # Lo que importa no es "3 se equivocaron" sino "3 de 5": sin el denominador el
        # número no dice si hay que rehacer la clase o hablar con una persona.
        r = self.correr(self.almacen.errores_frecuentes(course_id=74))
        for t in r["temas"]:
            self.assertEqual(t["de_corregidos"], r["correcciones_registradas"])

    def test_recargar_una_nota_NO_cuenta_al_alumno_dos_veces(self):
        # Recargar una nota —para arreglar la devolución, o tras un fallo— deja OTRA fila
        # en la bitácora. Contando filas, 6 alumnos cargados dos veces informaban "12
        # correcciones". Lo grave no es el número inflado: al superar así la muestra
        # mínima, la tool marcaba `sistemico` sin evidencia — y ese es el dato con el que
        # se decide rehacer material de cátedra.
        import tempfile
        from moodle import almacen as alm
        prev_db_path = alm.db_path
        db_temp = f"{tempfile.mkdtemp()}/dedup.db"
        alm.db_path = lambda tenant_id=None: db_temp
        try:
            self.correr(alm.init_db())
            for _ in range(2):                     # cada alumno, cargado DOS veces
                for a in ("ana", "beto", "caro", "dani", "eve", "fer"):
                    self.correr(alm.guardar_correccion({
                        "course_id": 7, "assign_id": "u1", "comision": "c",
                        "email": f"{a}@x", "alumno": a, "nota": "Aprobado",
                        "devolucion": "", "etiquetas": ["tema-x"] if a == "ana" else []}))
            r = self.correr(alm.errores_frecuentes(course_id=7))
            self.assertEqual(r["correcciones_registradas"], 6)   # 6 alumnos, no 12 filas
            temas = {t["tema"]: t for t in r["temas"]}
            self.assertEqual(temas["tema-x"]["alumnos_afectados"], 1)   # 1 persona, no 2
            self.assertFalse(temas["tema-x"]["sistemico"])
        finally:
            alm.db_path = prev_db_path


class TestEscala3(unittest.TestCase):
    """Escala 3 (Prog IV): No satisfactorio / Satisfactorio / Supera lo esperado.

    Relevada del `<select>` del grader real el 2026-08-04. Antes devolvía "escala
    desconocida" y `cargar_nota` pedía un número para un desplegable de tres opciones.
    """

    def test_lee_las_tres_etiquetas(self):
        self.assertEqual(nota_display(-3, "1.00000"), "No satisfactorio")
        self.assertEqual(nota_display(-3, "2.00000"), "Satisfactorio")
        self.assertEqual(nota_display(-3, "3.00000"), "Supera lo esperado")

    def test_satisfactorio_NO_matchea_dentro_de_no_satisfactorio(self):
        # "satisfactorio" es substring de "no satisfactorio". Si el match fuera por
        # inclusión en vez de exacto, un "No satisfactorio" se guardaría como Satisfactorio
        # — el alumno aprobado por un substring. Este test fija que el match es exacto.
        self.assertEqual(_resolver_valor_nota(-3, "No satisfactorio")[:2],
                         (1.0, "No satisfactorio"))
        self.assertEqual(_resolver_valor_nota(-3, "satisfactorio")[:2],
                         (2.0, "Satisfactorio"))

    def test_esta_escala_NO_va_invertida_y_la_5_si(self):
        # Las dos conviven en el mismo campus, así que no hay una regla general: cada
        # escala se releva del grader. Hardcodear "1 es la peor" rompe la 5.
        self.assertEqual(nota_display(-3, "1.00000"), "No satisfactorio")   # 1 = la peor
        self.assertEqual(nota_display(-5, "1.00000"), "Aprobado")           # 1 = la mejor


class TestNoSeSiEsEscalaONumerica(unittest.TestCase):
    """Ante la duda NO se adivina.

    Antes, un `grade_cfg` ilegible caía a `0` = numérica — y numérica es justamente el modo
    que corrompe: en la escala 5 un "1" pensado como la peor nota guarda APROBADO.
    """

    def test_no_resuelve_la_nota_si_no_sabe_el_tipo(self):
        for gc in (None, "", "chau"):
            self.assertIsNone(_resolver_valor_nota(gc, "Aprobado")[0],
                              f"con grade_cfg={gc!r} resolvió igual")

    def test_tampoco_acepta_un_numero_a_ciegas(self):
        # El que importa: antes, con el tipo ilegible, un 9.5 se escribía como numérica sin
        # chistar. Si la tarea era de escala, eso queda mal en el legajo de una persona.
        self.assertIsNone(_resolver_valor_nota(None, "9.5")[0])

    def test_nota_display_avisa_en_vez_de_inventar(self):
        r = nota_display(None, "1.00000")
        self.assertNotIn(r.strip(), ("1", "1.0", "1.00000"))
        self.assertIn("no sé", r.lower())


class TestNotaDeEntregaActiveIA(unittest.TestCase):
    """Una nota 0 es una nota.

    `/entregas/` devuelve la nota bajo tres nombres posibles según el caso. Encadenarlos
    con `or` hacía que un 0 —entrega vacía, plagio— cayera hasta `None` y la entrega
    desapareciera de `activeia_correcciones` como si nunca se hubiera corregido.
    """

    def test_cero_no_desaparece(self):
        self.assertEqual(_nota_de_entrega({"nota": 0}), 0)
        self.assertEqual(_nota_de_entrega({"puntaje": 0.0}), 0.0)

    def test_el_primero_que_EXISTE_gana_aunque_sea_cero(self):
        # Con el `or` encadenado esto devolvía 99: el 0 real se perdía y se reportaba la
        # nota de otro campo.
        self.assertEqual(_nota_de_entrega({"nota": 0, "calificacion": 99}), 0)

    def test_cae_al_campo_siguiente_solo_si_el_anterior_FALTA(self):
        self.assertEqual(_nota_de_entrega({"calificacion": 87}), 87)
        self.assertEqual(_nota_de_entrega({"nota": None, "puntaje": 70}), 70)

    def test_sin_ningun_campo_es_None(self):
        self.assertIsNone(_nota_de_entrega({"alumno_nombre": "x"}))


class TestAvisoAHtml(unittest.TestCase):
    """El aviso del foro se guarda con messageformat=1, o sea HTML."""

    def test_cada_parrafo_en_su_p_y_el_salto_simple_es_br(self):
        # Sin esto el campus se come los saltos y el aviso queda como un ladrillo.
        self.assertEqual(_a_html("uno\n\ndos"), "<p>uno</p><p>dos</p>")
        self.assertEqual(_a_html("uno\ndos"), "<p>uno<br>dos</p>")

    def test_escapa_el_codigo(self):
        # Es un foro de PROGRAMACIÓN: sin escapar, un aviso que diga "if (a < b)" o
        # "List<int>" se renderiza mutilado y el tutor se entera cuando ya está publicado.
        self.assertEqual(_a_html("if (a < b)"), "<p>if (a &lt; b)</p>")
        self.assertIn("&amp;&amp;", _a_html("a && b"))
        self.assertNotIn("<int>", _a_html("List<int>"))

    def test_vacio_no_rompe(self):
        for v in ("", "   ", "\n\n", None):
            self.assertEqual(_a_html(v), "", f"con {v!r}")


class ClienteFalso:
    """Doble mínimo del cliente Moodle: devuelve lo que le pusiste, por función."""

    def __init__(self, respuestas):
        self.respuestas = respuestas
        self.llamadas = []

    async def ws(self, fn, params=None):
        self.llamadas.append(fn)
        r = self.respuestas.get(fn)
        if isinstance(r, Exception):
            raise r
        return r


class TestSinEntrarAlAulaNoDevuelveCeroMudo(unittest.TestCase):
    """En este proyecto un 0 que significa "no se relevó" es peor que un error: se lee como
    "está todo al día" y el tutor no le escribe a nadie. Estos tests cubren los tres modos de
    mentir callado que tiene esta tool.
    """

    @staticmethod
    def _alumnos(n, con_campo=True, desde=0):
        import time
        ahora = int(time.time())
        return [{"id": 100 + i, "fullname": f"Alumno {i}", "email": f"a{i}@x.com",
                 "lastaccess": ahora, "roles": [{"shortname": "student"}],
                 **({"lastcourseaccess": ahora} if i >= desde and con_campo else {})}
                for i in range(n)]

    def _correr(self, alumnos):
        import asyncio
        cli = ClienteFalso({"core_enrol_get_enrolled_users": alumnos})
        return asyncio.run(sin_entrar_al_aula(cli, 74, 7740))

    def test_si_el_campo_no_viene_para_nadie_es_error_y_no_una_lista_de_ceros(self):
        # El escenario de permisos. Una lista de ceros acá se leería como "todos abrieron la
        # materia hoy", que es lo contrario de lo que sabemos (que no sabemos nada).
        r = self._correr(self._alumnos(3, con_campo=False))
        self.assertIn("error", r)
        self.assertNotIn("alumnos", r)
        self.assertTrue(r["_meta"]["degradado"])
        self.assertIs(r["_meta"]["campo_disponible"], False)

    def test_relevamiento_parcial_no_cuenta_como_relevado_al_que_no_se_pudo_leer(self):
        # 2 con dato y 2 sin. Decir "los 4 abrieron la materia" es el cero mudo con otra cara.
        r = self._correr(self._alumnos(4, desde=2))
        self.assertEqual(r["sin_dato"], 2)
        self.assertTrue(r["_meta"]["degradado"])
        self.assertIn("Los 2 alumnos relevados", r["resumen"])
        self.assertIn("sin relevar", r["resumen"])
        self.assertIn("aviso", r)

    def test_comision_vacia_no_es_estan_todos_entrando(self):
        r = self._correr([])
        self.assertTrue(r["sin_alumnos"])
        self.assertEqual(r["alumnos_totales"], 0)
        self.assertIn("no hay a quién medir", r["aviso"])

    def test_comision_al_dia_lo_dice_con_el_numero_del_peor_no_con_un_cero(self):
        r = self._correr(self._alumnos(5))
        self.assertEqual(r["desenganchados"], 0)
        self.assertFalse(r["_meta"]["degradado"])
        self.assertIn("el más atrasado hace 0 día(s)", r["resumen"])

    def test_un_error_de_la_comision_se_devuelve_no_se_traga(self):
        r = self._correr(MoodleWSError("core_enrol_get_enrolled_users",
                                       {"errorcode": "nopermissions", "message": "no"}))
        self.assertIn("error", r)


class TestCrearDiscusionNoPublicaAlCursoEntero(unittest.TestCase):
    """Publicar un aviso de comisión a cientos de alumnos ajenos NO se deshace desde la
    API. Por eso acá "no sé a cuántos llega" tiene que ser un freno, no un cartel.
    """

    FORO_CON_GRUPOS = {
        "core_course_get_course_module_by_instance": {
            "cm": {"id": 1, "name": "Avisos de la comisión", "course": 74, "groupmode": 1}},
        "mod_forum_can_add_discussion": {"status": True},
        "core_group_get_course_groups": [{"id": 7740, "name": "A26 C1-6"}],
        "core_enrol_get_enrolled_users": [
            {"id": 1, "roles": [{"shortname": "student"}]},
            {"id": 2, "roles": [{"shortname": "student"}]},
            {"id": 3, "roles": [{"shortname": "editingteacher"}]},   # el tutor no cuenta
        ],
        "mod_forum_add_discussion": {"discussionid": 999},
    }

    def _correr(self, respuestas, **kw):
        import asyncio
        cli = ClienteFalso(respuestas)
        r = asyncio.run(crear_discusion(cli, 55, "Aviso", "hola", **kw))
        return r, cli

    def test_foro_con_grupos_sin_group_id_NO_publica_aunque_confirmes(self):
        # EL test. El freno no puede vivir sólo en el preview: si alguien llama directo
        # con confirmado=true, tiene que seguir frenando igual.
        r, cli = self._correr(self.FORO_CON_GRUPOS, confirmado=True)
        self.assertIn("error", r)
        self.assertIn("GRUPOS SEPARADOS", r["error"])
        self.assertNotIn("mod_forum_add_discussion", cli.llamadas)

    def test_si_no_puede_leer_el_foro_NO_publica(self):
        # Antes, si esta llamada fallaba el contexto quedaba vacío, la alerta no se
        # disparaba y la tool publicaba callada. Justo cuando menos se sabe.
        resp = dict(self.FORO_CON_GRUPOS)
        resp["core_course_get_course_module_by_instance"] = MoodleWSError(
            "core_course_get_course_module_by_instance", {"errorcode": "accessexception"})
        r, cli = self._correr(resp, confirmado=True)
        self.assertIn("error", r)
        self.assertNotIn("mod_forum_add_discussion", cli.llamadas)
        self.assertTrue(r["_meta"]["degradado"])

    def test_group_id_inexistente_no_se_publica_en_otro_lado(self):
        r, cli = self._correr(self.FORO_CON_GRUPOS, group_id=9999, confirmado=True)
        self.assertIn("no existe", r["error"])
        self.assertNotIn("mod_forum_add_discussion", cli.llamadas)

    def test_con_group_id_valido_el_preview_dice_a_cuantos_llega(self):
        r, _ = self._correr(self.FORO_CON_GRUPOS, group_id=7740)
        self.assertTrue(r["requiere_confirmacion"])
        destino = r["preview"]["destino"]
        self.assertEqual(destino["grupo_nombre"], "A26 C1-6")
        self.assertEqual(destino["alcance_alumnos"], 2)      # el editingteacher no cuenta
        self.assertEqual(destino["alcance"], "comisión")

    def test_group_id_cero_EXPLICITO_es_una_decision_y_se_permite(self):
        # 0 no es "no me lo dijiste": es "quiero que lo vea el curso entero".
        r, cli = self._correr(self.FORO_CON_GRUPOS, group_id=0, confirmado=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["alcance"], "curso entero")
        self.assertIn("mod_forum_add_discussion", cli.llamadas)

    def test_no_contar_alumnos_no_es_contar_cero(self):
        resp = dict(self.FORO_CON_GRUPOS)
        resp["core_enrol_get_enrolled_users"] = MoodleWSError(
            "core_enrol_get_enrolled_users", {"errorcode": "accessexception"})
        r, _ = self._correr(resp, group_id=7740)
        self.assertIsNone(r["preview"]["destino"]["alcance_alumnos"])
        self.assertTrue(any("desconocido" in a for a in r["_meta"]["avisos"]))


class TestPanoramaClasificaEntregas(unittest.TestCase):
    """La capa que decide QUÉ SIGNIFICA una entrega en la vista del profesor.

    Existe por un bug real de la primera corrida (2026-08-07): `mod_assign_get_grades`
    devuelve una fila con grade `-1.00000` para las entregas que TODAVÍA no se corrigieron.
    Tomar "hay registro de nota" como "está corregida" mandó las 22 pendientes del curso a
    otro balde y el tablero mostró CERO pendientes en las 16 comisiones — le decía al
    profesor que estaba todo al día cuando no lo estaba.
    """

    @staticmethod
    def _subs(*filas):
        """filas: (userid, status, gradingstatus, timemodified)."""
        return {"assignments": [{"submissions": [
            {"userid": u, "status": st, "gradingstatus": gs, "timemodified": tm}
            for u, st, gs, tm in filas]}]}

    @staticmethod
    def _notas(*filas):
        """filas: (userid, grade, timemodified)."""
        return {"assignments": [{"grades": [
            {"userid": u, "grade": g, "timemodified": tm} for u, g, tm in filas]}]}

    def test_el_menos_uno_con_notgraded_es_PENDIENTE_no_corregido(self):
        # EL bug. Hay registro de nota, pero vale -1 y la entrega dice `notgraded`.
        r = panorama.clasificar_entregas(
            self._subs((7, "submitted", "notgraded", 1000)),
            self._notas((7, "-1.00000", 1000)))
        self.assertIn(7, r["pendientes"])
        self.assertNotIn(7, r["calificadas"])
        self.assertNotIn(7, r["sin_nota"])

    def test_los_new_no_son_entregas(self):
        # 21 de los 46 registros de la unidad 1 estaban en `new`: el alumno abrió la tarea
        # y nunca entregó. Contarlos casi duplicaba el trabajo reportado.
        r = panorama.clasificar_entregas(
            self._subs((1, "submitted", "notgraded", 1000), (2, "new", "notgraded", 1000)),
            self._notas())
        self.assertEqual(list(r["entregas"]), [1])

    def test_graded_con_nota_real_es_corregida(self):
        r = panorama.clasificar_entregas(
            self._subs((3, "submitted", "graded", 1000)),
            self._notas((3, "1.00000", 87400)))
        self.assertEqual(r["calificadas"][3], 87400)
        self.assertFalse(r["pendientes"])

    def test_graded_con_calificacion_vacia_es_hallazgo_no_corregida(self):
        # Moodle la saca de la cola, pero el alumno quedó sin nota y nadie lo espera.
        r = panorama.clasificar_entregas(
            self._subs((4, "submitted", "graded", 1000)),
            self._notas((4, "-1.00000", 2000)))
        self.assertIn(4, r["sin_nota"])
        self.assertNotIn(4, r["calificadas"])
        self.assertNotIn(4, r["pendientes"])

    def test_released_cuenta_como_corregida(self):
        r = panorama.clasificar_entregas(
            self._subs((5, "submitted", "released", 1000)),
            self._notas((5, "2.00000", 3000)))
        self.assertIn(5, r["calificadas"])

    def test_estado_intermedio_del_flujo_NO_cuenta_como_corregida(self):
        # La nota existe pero el alumno todavía no la tiene: el trabajo no está cerrado.
        r = panorama.clasificar_entregas(
            self._subs((6, "submitted", "readyforreview", 1000)),
            self._notas((6, "1.00000", 3000)))
        self.assertIn(6, r["pendientes"])
        self.assertNotIn(6, r["calificadas"])

    def test_sin_gradingstatus_no_se_asume_ninguno_de_los_dos(self):
        # "No sé" no se disfraza ni de corregido ni de pendiente: se cuenta aparte.
        r = panorama.clasificar_entregas(
            self._subs((8, "submitted", None, 1000)), self._notas())
        self.assertIn(8, r["sin_clasificar"])
        self.assertFalse(r["pendientes"])
        self.assertFalse(r["calificadas"])

    def test_vale_el_ultimo_intento(self):
        r = panorama.clasificar_entregas(
            self._subs((9, "submitted", "graded", 1000)),
            self._notas((9, "1.00000", 5000), (9, "2.00000", 9000)))
        self.assertEqual(r["calificadas"][9], 9000)

    def test_los_libros_cierran(self):
        # Réplica de la forma real de la unidad 1 de Prog I: 25 entregadas (15 corregidas /
        # 10 pendientes) + 21 `new`. La suma de los baldes tiene que dar las entregas.
        filas_sub, filas_nota = [], []
        for i in range(15):
            filas_sub.append((i, "submitted", "graded", 1000))
            filas_nota.append((i, "1.00000", 2000))
        for i in range(15, 25):
            filas_sub.append((i, "submitted", "notgraded", 1000))
            filas_nota.append((i, "-1.00000", 1000))
        for i in range(25, 46):
            filas_sub.append((i, "new", "notgraded", 1000))
        r = panorama.clasificar_entregas(self._subs(*filas_sub), self._notas(*filas_nota))
        self.assertEqual(len(r["entregas"]), 25)
        self.assertEqual(len(r["calificadas"]), 15)
        self.assertEqual(len(r["pendientes"]), 10)
        self.assertEqual(
            len(r["calificadas"]) + len(r["pendientes"]) + len(r["sin_nota"])
            + len(r["sin_clasificar"]), len(r["entregas"]))


class TestPanoramaEtiquetasYDias(unittest.TestCase):
    def test_reconoce_la_comision_y_descarta_lo_que_no_lo_es(self):
        self.assertEqual(panorama._etiqueta_comision("A26 C1-06"), "com6")
        self.assertEqual(panorama._etiqueta_comision("M26 C2-01"), "com1")
        self.assertEqual(panorama._etiqueta_comision("A25 C3-14"), "com14")
        # Grupos regionales y auxiliares NO son comisiones de tutoría.
        self.assertIsNone(panorama._etiqueta_comision("R-Rosario"))
        self.assertIsNone(panorama._etiqueta_comision("Grupo_2"))
        self.assertIsNone(panorama._etiqueta_comision("Entrego_1er_examen"))
        self.assertIsNone(panorama._etiqueta_comision(""))

    def test_sin_una_de_las_dos_fechas_devuelve_None_y_no_cero(self):
        # Un 0 acá se leería "se corrigió el mismo día". Es "no sé cuándo".
        self.assertIsNone(panorama._dias(0, 90000))
        self.assertIsNone(panorama._dias(90000, 0))
        # Corrección anterior a la entrega: dato incoherente, tampoco se inventa.
        self.assertIsNone(panorama._dias(90000, 1000))
        self.assertEqual(panorama._dias(0 + 86400, 86400 * 3), 2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDesengancheDelCurso(unittest.TestCase):
    """La mitad de la vista del profesor que habla de ALUMNOS, no de tutores.

    Lo que se está protegiendo acá es la lección del informe de coordinación del 13/08: cortaba
    la inactividad por el reloj del CAMPUS y sobre datos medidos eso pierde el ~90% de los
    desenganchados, porque al que entra todos los días para otra materia lo muestra impecable.
    """

    @staticmethod
    def _hace(dias):
        import time
        return int(time.time() - dias * 86400)

    def _padron(self, alumnos, error=None, sin_accesos=False):
        """alumnos: lista de (uid, nombre, dias_campus|None, dias_materia|'nunca'|'sin_dato')."""
        if error:
            return {"error": error}
        accesos = {}
        for fila in alumnos:
            uid, nombre, dc, dm = fila[:4]
            reg = fila[4] if len(fila) > 4 else "Rosario"
            u = {"id": uid, "fullname": nombre, "email": f"{uid}@x.com"}
            if reg:
                u["groups"] = [{"name": f"R-{reg}"}, {"name": "M25 C4-01"}]
            u["lastaccess"] = 0 if dc is None else self._hace(dc)
            if dm == "nunca":
                u["lastcourseaccess"] = 0
            elif dm != "sin_dato":
                u["lastcourseaccess"] = self._hace(dm)
            accesos[uid] = u
        p = {"alumnos": {u[0]: u[1] for u in alumnos}, "docentes": []}
        if not sin_accesos:
            p["accesos"] = accesos
        return p

    COMS = [{"comision": "com1", "group_id": 11}, {"comision": "com2", "group_id": 22}]

    def test_el_que_entra_al_campus_y_no_abre_la_materia_va_PRIMERO(self):
        # Es el más accionable y el más recuperable: no perdió el acceso, eligió no entrar.
        # El de 247 días es más extremo pero es otro problema, y va después.
        padrones = {
            11: self._padron([(1, "Perdido Total", 247, "nunca"),
                              (2, "Elige No Entrar", 0, "nunca")]),
            22: self._padron([(3, "Al Dia", 0, 1)]),
        }
        r = panorama.desenganche_del_curso(padrones, self.COMS)
        self.assertEqual([a["nombre"] for a in r["alumnos"]],
                         ["Elige No Entrar", "Perdido Total"])
        self.assertEqual(r["totales"]["entran_al_campus_sin_abrir_la_materia"], 1)

    def test_no_devuelve_a_los_que_estan_al_dia_pero_los_cuenta(self):
        # El recorte se declara: si no, una lista de 7 sobre 238 se lee como cobertura total.
        padrones = {11: self._padron([(1, "Al Dia", 0, 1), (2, "Tambien", 1, 2)]),
                    22: self._padron([(3, "Deseng", 0, 20)])}
        r = panorama.desenganche_del_curso(padrones, self.COMS)
        self.assertEqual(len(r["alumnos"]), 1)
        self.assertEqual(r["relevados"], 3)
        self.assertEqual(r["al_dia"], 2)
        self.assertIn("1 de 3", r["criterio"]["recorte"])

    def test_una_comision_que_no_se_pudo_leer_queda_SIN_MEDIR_y_se_grita(self):
        # Nunca un 0 mudo: "no pude leer com2" no puede parecerse a "com2 está bien".
        padrones = {11: self._padron([(1, "Deseng", 0, 20)]),
                    22: self._padron([], error="no pude leer el padrón: nopermissions")}
        r = panorama.desenganche_del_curso(padrones, self.COMS)
        self.assertTrue(r["por_comision"]["com2"]["sin_medir"])
        self.assertTrue(any("SIN MEDIR" in s for s in r["sin_dato"]))

    def test_padron_viejo_sin_datos_de_acceso_no_se_lee_como_comision_sana(self):
        # Compatibilidad hacia atrás: un padrón sin la clave `accesos` es "no medido".
        padrones = {11: self._padron([(1, "X", 0, 20)], sin_accesos=True),
                    22: self._padron([(2, "Y", 0, 1)])}
        r = panorama.desenganche_del_curso(padrones, self.COMS)
        self.assertTrue(r["por_comision"]["com1"]["sin_medir"])
        self.assertEqual(r["relevados"], 1)

    def test_sin_dato_del_aula_no_se_cuenta_como_desenganchado_ni_como_al_dia(self):
        # Tres estados, no dos: aparece en la lista para que se lo vea, pero no infla el total.
        padrones = {11: self._padron([(1, "No Se Sabe", 0, "sin_dato")]), 22: self._padron([])}
        r = panorama.desenganche_del_curso(padrones, self.COMS)
        self.assertEqual(r["totales"]["sin_dato"], 1)
        self.assertEqual(r["totales"]["desenganchados"], 0)
        self.assertEqual(r["al_dia"], 0)
        self.assertEqual(len(r["alumnos"]), 1)
        self.assertTrue(any("no cubre" in s for s in r["sin_dato"]))

    def test_cuenta_por_comision_para_ver_donde_se_concentra(self):
        padrones = {11: self._padron([(1, "A", 0, "nunca"), (2, "B", 0, 30), (3, "C", 0, 1)]),
                    22: self._padron([(4, "D", 0, 1)])}
        r = panorama.desenganche_del_curso(padrones, self.COMS)
        self.assertEqual(r["por_comision"]["com1"]["desenganchados"], 2)
        self.assertEqual(r["por_comision"]["com1"]["nunca_abrieron"], 1)
        self.assertEqual(r["por_comision"]["com2"]["desenganchados"], 0)

    def test_agrupa_por_regional_con_las_que_mas_concentran_primero(self):
        # Pedido de coordinación: el seguimiento lo hacen los tutores nexos, que trabajan por
        # sede. Una lista global los obliga a filtrar a ojo.
        padrones = {11: self._padron([(1, "A", 0, "nunca", "Rosario"),
                                      (2, "B", 0, 30, "Rosario"),
                                      (3, "C", 0, 20, "Chubut")]),
                    22: self._padron([(4, "D", 0, 25, "Rosario")])}
        r = panorama.desenganche_del_curso(padrones, self.COMS)
        bloques = r["por_regional_bloques"]
        self.assertEqual([b["regional"] for b in bloques], ["Rosario", "Chubut"])
        self.assertEqual(bloques[0]["desenganchados"], 3)

    def test_dentro_de_la_regional_los_accionables_siguen_arriba(self):
        # Agrupar cambia dónde busca cada nexo, no qué es urgente: el que entra al campus y no
        # abre la materia sigue primero dentro de su bloque.
        padrones = {11: self._padron([(1, "Desaparecido", 40, 40, "Rosario"),
                                      (2, "Elige No Entrar", 0, 20, "Rosario")]),
                    22: self._padron([])}
        r = panorama.desenganche_del_curso(padrones, self.COMS)
        lista = r["por_regional_bloques"][0]["lista"]
        self.assertEqual([a["nombre"] for a in lista], ["Elige No Entrar", "Desaparecido"])

    def test_los_alumnos_sin_comision_se_miden_igual_y_no_quedan_afuera(self):
        # Los 11 de Prog I que están en el curso y en su regional pero en ninguna comisión: no
        # los ve ningún tutor porque toda vista por comisión los saltea.
        padrones = {11: self._padron([(1, "Con Comision", 0, 20, "Rosario")]),
                    22: self._padron([])}
        suelto = self._padron([(9, "Sin Comision", 0, "nunca", "Chubut")])["accesos"][9]
        r = panorama.desenganche_del_curso(padrones, self.COMS, sueltos=[suelto])
        self.assertEqual(r["relevados"], 2)
        self.assertIn("(sin comisión)", r["por_comision"])
        self.assertTrue(any(a["comision"] == "(sin comisión)" for a in r["alumnos"]))

    def test_sin_regional_es_su_propia_categoria_y_no_se_esconde(self):
        padrones = {11: self._padron([(1, "A", 0, 20, None)]), 22: self._padron([])}
        r = panorama.desenganche_del_curso(padrones, self.COMS)
        self.assertEqual(r["por_regional_bloques"][0]["regional"], "(sin regional)")

    def test_el_umbral_de_dias_es_configurable(self):
        padrones = {11: self._padron([(1, "A", 0, 4)]), 22: self._padron([])}
        self.assertEqual(len(panorama.desenganche_del_curso(padrones, self.COMS)["alumnos"]), 0)
        self.assertEqual(
            len(panorama.desenganche_del_curso(padrones, self.COMS, 3)["alumnos"]), 1)


class TestFocosDeAlumnos(unittest.TestCase):
    """Los focos del informe de nexos: calculados de los datos, sin adjetivos.

    En el informe que se venía armando a mano este bloque lo escribía un modelo y salía
    "Estado general: sano" sobre un curso con 60 alumnos que no abrían la materia. Estos tests
    fijan que siga siendo hechos con su cuenta al lado.
    """

    @staticmethod
    def _datos(**kw):
        base = {
            "desenganche": {
                "relevados": 371,
                "dias_desenganche": 7,
                "totales": {"desenganchados": 60,
                            "entran_al_campus_sin_abrir_la_materia": 50,
                            "nunca_abrieron": 14, "nunca_entraron_ni_al_campus": 0,
                            "sin_dato": 0},
                "por_regional_bloques": [
                    {"regional": "Avellaneda", "alumnos": 23, "desenganchados": 6,
                     "lista": [{"entra_al_campus_sin_abrir_la_materia": True}] * 6},
                    {"regional": "Chubut", "alumnos": 21, "desenganchados": 4,
                     "lista": [{"entra_al_campus_sin_abrir_la_materia": True}] * 3
                              + [{"entra_al_campus_sin_abrir_la_materia": False}]},
                ],
            },
            "padron": {"sin_comision": 0},
        }
        for k, v in kw.items():
            base[k] = {**base[k], **v} if isinstance(v, dict) and k in base else v
        return base

    def test_lo_primero_es_el_grupo_recuperable(self):
        f = informes.focos_de_alumnos(self._datos())
        self.assertIn("50 alumnos entran al campus", f[0][0])
        self.assertIn("Avellaneda (6)", f[0][1])

    def test_separa_a_los_que_no_aparecen_por_ningun_lado(self):
        f = informes.focos_de_alumnos(self._datos())
        # 60 desenganchados - 50 que entran al campus = 10 fríos.
        self.assertTrue(any("10 alumnos no aparecen" in t for t, _ in f))

    def test_los_alumnos_sin_comision_son_su_propio_foco(self):
        f = informes.focos_de_alumnos(self._datos(padron={"sin_comision": 11}))
        self.assertTrue(any("11 alumnos sin comisión" in t for t, _ in f))
        self.assertTrue(any("ningún tutor" in d for _, d in f))

    def test_sin_dato_aparece_y_aclara_que_no_es_no_la_abrio(self):
        f = informes.focos_de_alumnos(
            self._datos(desenganche={"totales": {
                "desenganchados": 60, "entran_al_campus_sin_abrir_la_materia": 50,
                "nunca_abrieron": 14, "nunca_entraron_ni_al_campus": 0, "sin_dato": 3}}),
            tope=9)
        texto = " ".join(t + " " + d for t, d in f)
        self.assertIn("3 alumnos no se pudieron medir", texto)
        self.assertIn("no se sabe", texto)

    def test_NUNCA_emite_un_veredicto(self):
        # El test que fija la doctrina: ninguna palabra que cierre el diagnóstico por el lector.
        prohibidas = ("sano", "saludable", "excelente", "crisis", "grave", "todo al día",
                      "estado general", "impecable", "preocupante")
        casos = [self._datos(),
                 self._datos(padron={"sin_comision": 11}),
                 self._datos(desenganche={"totales": {
                     "desenganchados": 0, "entran_al_campus_sin_abrir_la_materia": 0,
                     "nunca_abrieron": 0, "nunca_entraron_ni_al_campus": 0, "sin_dato": 0}})]
        for datos in casos:
            texto = " ".join(t + " " + d
                             for t, d in informes.focos_de_alumnos(datos)).lower()
            for pal in prohibidas:
                self.assertNotIn(pal, texto, f"apareció un veredicto: {pal!r}")

    def test_curso_sin_nada_para_reportar_no_inventa_focos(self):
        d = self._datos(
            desenganche={"totales": {"desenganchados": 0,
                                     "entran_al_campus_sin_abrir_la_materia": 0,
                                     "nunca_abrieron": 0, "nunca_entraron_ni_al_campus": 0,
                                     "sin_dato": 0}},
            padron={"sin_comision": 0})
        self.assertEqual(informes.focos_de_alumnos(d), [])




class TestCatalogoDeNexos(unittest.TestCase):
    """El catálogo de Tutores Nexo que viaja con la skill.

    La clave es el nombre del grupo del campus sin el `R-`. Si no matchea, el bloque de esa
    regional sale SIN contacto — nunca con un nexo inventado, que sería adjudicarle a alguien
    una sede que no es suya.
    """

    def test_el_catalogo_carga_y_tiene_las_17_regionales(self):
        regs, aviso = panorama.nexos_por_regional()
        self.assertIsNone(aviso)
        self.assertEqual(len(regs), 17)

    def test_cada_regional_tiene_nexo_y_al_menos_un_mail(self):
        regs, _ = panorama.nexos_por_regional()
        for nombre, d in regs.items():
            self.assertTrue(d.get("nexos"), f"{nombre} sin nexo")
            self.assertTrue(d.get("mails"), f"{nombre} sin mail")
            for m in d["mails"]:
                self.assertIn("@", m, f"{nombre}: mail raro {m!r}")

    def test_las_claves_son_los_nombres_del_campus_sin_el_prefijo(self):
        # Verificado en vivo el 2026-08-13 contra los 4 cursos: éstas son las 17 tal cual.
        regs, _ = panorama.nexos_por_regional()
        for esperada in ("San Nicolás", "Mar del Plata", "Villa María",
                         "Concepción del Uruguay", "General Pacheco", "Córdoba", "Paraná"):
            self.assertIn(esperada, regs, f"falta {esperada!r} o cambió de nombre")
        # y ninguna clave arrastra el prefijo del grupo
        for k in regs:
            self.assertFalse(k.startswith("R-"), f"la clave {k!r} tiene el prefijo pegado")

    def test_una_regional_que_no_esta_en_el_catalogo_no_inventa_nexo(self):
        regs, _ = panorama.nexos_por_regional()
        self.assertIsNone(regs.get("Regional Inexistente"))


class TestCortesDelTrabajo(unittest.TestCase):
    """Los dos cortes nuevos del panorama: por actividad y por tutor. Ambos puros."""

    @staticmethod
    def _tarea(entregas, calificadas=(), sin_nota=(), pendientes=()):
        return {"entregas": entregas, "calificadas": dict(calificadas),
                "sin_nota": set(sin_nota), "pendientes": set(pendientes)}

    def test_por_actividad_suma_las_comisiones_y_dice_en_cuantas_hay_cola(self):
        # Lo que la vista por comisión NO muestra: 1 pendiente en cada una de 3 comisiones es
        # una actividad con 3 de cola, y eso es un problema de la consigna, no de nadie.
        uid_a_com = {1: "com1", 2: "com2", 3: "com3"}
        datos = {"100": self._tarea({1: 1000, 2: 1000, 3: 1000}, pendientes=(1, 2, 3))}
        meta = {"100": {"titulo": "TP Unidad 4", "duedate": 0}}
        r = panorama.resumen_por_actividad(datos, uid_a_com, meta, ahora=1000 + 3 * 86400)
        self.assertEqual(r[0]["sin_corregir"], 3)
        self.assertEqual(r[0]["comisiones_con_cola"], 3)
        self.assertEqual(r[0]["espera_max_dias"], 3.0)
        self.assertEqual(r[0]["vencimiento"], "sin fecha")

    def test_por_actividad_no_cuenta_alumnos_fuera_de_comision(self):
        # Así los totales cierran con las filas por comisión, que tampoco los cuentan.
        datos = {"100": self._tarea({1: 1000, 99: 1000}, pendientes=(1, 99))}
        r = panorama.resumen_por_actividad(datos, {1: "com1"}, {}, ahora=2000)
        self.assertEqual(r[0]["entregadas"], 1)

    def test_por_actividad_distingue_vencida_de_a_futuro_y_de_sin_fecha(self):
        ahora = 1_000_000
        datos = {str(i): self._tarea({}) for i in (1, 2, 3)}
        meta = {"1": {"titulo": "vieja", "duedate": ahora - 86400},
                "2": {"titulo": "proxima", "duedate": ahora + 86400},
                "3": {"titulo": "sin", "duedate": 0}}
        r = {a["titulo"]: a["vencimiento"]
             for a in panorama.resumen_por_actividad(datos, {}, meta, ahora)}
        self.assertEqual(r, {"vieja": "vencida", "proxima": "a futuro", "sin": "sin fecha"})

    def test_carga_por_tutor_suma_sus_comisiones(self):
        # Seis tutores llevan dos: su cola real no está en ninguna fila.
        filas = [
            {"comision": "com1", "tutor": {"nombre": "Ana"}, "alumnos": 30,
             "sin_corregir": 2, "espera_max_dias": 1.0, "consultas_sin_responder": 0},
            {"comision": "com9", "tutor": {"nombre": "Ana"}, "alumnos": 34,
             "sin_corregir": 3, "espera_max_dias": 4.0, "consultas_sin_responder": 2},
            {"comision": "com5", "tutor": {"nombre": "Beto"}, "alumnos": 20,
             "sin_corregir": 9, "espera_max_dias": 0.5, "consultas_sin_responder": 0},
        ]
        r = panorama.carga_por_tutor(filas)
        ana = next(t for t in r if t["tutor"] == "Ana")
        self.assertEqual(ana["alumnos"], 64)
        self.assertEqual(ana["sin_corregir"], 5)
        self.assertEqual(ana["espera_max_dias"], 4.0)  # el máximo, no la suma
        self.assertEqual(sorted(ana["comisiones"]), ["com1", "com9"])
        # Ordena por espera, no por volumen: Beto tiene más cola pero más fresca.
        self.assertEqual(r[0]["tutor"], "Ana")

    def test_carga_por_tutor_no_esconde_a_la_comision_sin_tutor(self):
        r = panorama.carga_por_tutor([{"comision": "com3", "tutor": None, "alumnos": 40,
                                       "sin_corregir": 1, "espera_max_dias": None}])
        self.assertEqual(r[0]["tutor"], "— sin identificar —")

    def test_saca_los_emoji_pero_deja_los_acentos(self):
        # Reportlab pinta los emoji como cuadraditos negros y en la tabla parecían un dato.
        self.assertEqual(informes.sin_emoji("Actividad de cierre unidad 5 🎯🏁"),
                         "Actividad de cierre unidad 5")
        self.assertEqual(informes.sin_emoji("Programación ñandú"), "Programación ñandú")
        self.assertEqual(informes.sin_emoji(None), "")

    def test_los_focos_de_correccion_NO_emiten_veredicto(self):
        prohibidas = ("sano", "excelente", "crisis", "grave", "estado general", "impecable")
        datos = {
            "tareas_miradas": 11, "actividades_sin_fecha_de_entrega": 10,
            "filas": [{"comision": "com15", "tutor": {"nombre": "T"}, "sin_corregir": 2,
                       "espera_max_dias": 3.0, "calificado_sin_nota": 1}],
            "por_actividad": [{"titulo": "TP4", "sin_corregir": 12, "comisiones_con_cola": 7,
                               "espera_max_dias": 3.0}],
        }
        texto = " ".join(t + " " + d
                         for t, d in informes.focos_de_correccion(datos, tope=9)).lower()
        for p in prohibidas:
            self.assertNotIn(p, texto, f"apareció un veredicto: {p!r}")
        self.assertIn("volumen no es atraso", texto)

    def test_sin_cola_lo_dice_acotado_a_lo_relevado(self):
        f = informes.focos_de_correccion({"tareas_miradas": 5, "filas": [], "por_actividad": []})
        self.assertIn("No hay entregas esperando", f[0][0])
        self.assertIn("5 actividades miradas", f[0][1])


class TestSoloCuentaLoVencido(unittest.TestCase):
    """La racha de abandono NO puede contar lo que todavía no venció.

    Es el bug que marcaba 94 de 94 alumnos en rojo en Prog I: ninguna de sus 10 actividades de
    cierre tiene fecha de entrega, y `duedate = 0` se estaba leyendo como "venció hace mucho".
    """

    AHORA = 1_800_000_000
    AYER = AHORA - 86400
    MANANA = AHORA + 86400

    def test_sin_fecha_de_entrega_NO_es_exigible(self):
        # El caso que rompía: 0 no es "venció en 1970", es "no tiene fecha".
        venc, fuera = _vencidas(["a", "b"], {"a": 0, "b": 0}, self.AHORA)
        self.assertEqual(venc, [])
        self.assertEqual(fuera, ["a", "b"])

    def test_la_que_vence_manana_no_cuenta_y_la_de_ayer_si(self):
        venc, fuera = _vencidas(["vieja", "futura"],
                                {"vieja": self.AYER, "futura": self.MANANA}, self.AHORA)
        self.assertEqual(venc, ["vieja"])
        self.assertEqual(fuera, ["futura"])

    def test_conserva_el_orden_del_curso(self):
        # La racha se lee desde la última, así que un reordenamiento la cambia.
        d = {"u1": self.AYER, "u2": self.AYER, "u3": self.AYER}
        self.assertEqual(_vencidas(["u3", "u1", "u2"], d, self.AHORA)[0], ["u3", "u1", "u2"])

    def test_una_tarea_que_no_esta_en_el_mapa_no_se_asume_vencida(self):
        venc, fuera = _vencidas(["huerfana"], {}, self.AHORA)
        self.assertEqual((venc, fuera), ([], ["huerfana"]))

    def test_el_padron_entero_en_rojo_ya_no_puede_pasar(self):
        # Prog I: 10 actividades de cierre, ninguna con fecha. Antes daba racha 10.
        orden = [f"u{i}" for i in range(1, 11)]
        venc, _ = _vencidas(orden, {a: 0 for a in orden}, self.AHORA)
        self.assertEqual(_racha_final_sin_entregar(["Sin entrega"] * len(venc)), 0)


class TestElPDFNoEsCONDEDeLosHuecos(unittest.TestCase):
    """Un hueco declarado en el dict que el PDF no muestra es un hueco que no existe.

    El PDF es lo que se comparte y muchas veces lo único que alguien lee. `degradado` decide si
    el bloque de avisos se dibuja, así que tiene que salir de la lista COMPLETA de huecos y no
    de una enumeración de fuentes que hay que acordarse de mantener.
    """

    def _datos(self, avisos):
        return {"ok": True, "course_id": 1, "comisiones": 1, "tareas_miradas": 1,
                "tareas_pedidas": 1, "actividades_en_el_curso": 1,
                "actividades_no_habilitadas": 0, "padron": {}, "foros": {},
                "filas": [{"comision": "com1", "tutor": {"nombre": "T"}, "alumnos": 1,
                           "entregados": 1, "corregidos": 1, "sin_corregir": 0,
                           "calificado_sin_nota": 0, "espera_max_dias": None,
                           "demora_mediana_dias": None, "demora_max_dias": None,
                           "consultas_sin_responder": 0, "sin_dato": []}],
                "desenganche": {}, "por_actividad": [], "por_tutor": [],
                "esperando_detalle": [], "sin_nota_detalle": [],
                "consultas_sin_responder_detalle": [],
                "actividades_sin_fecha_de_entrega": 0, "avisos": avisos, "segundos": 1.0}

    def test_con_huecos_el_pdf_sale_marcado_degradado(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            r = informes.reporte_coordinacion_pdf(
                self._datos(["falta algo"]), d, materia="X", fecha="2026-01-01")
            self.assertTrue(r["degradado"], "el PDF no marcó el hueco que el dict declaraba")

    def test_sin_huecos_no_se_marca(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(informes.reporte_coordinacion_pdf(
                self._datos([]), d, materia="X", fecha="2026-01-01")["degradado"])

    def test_el_render_completo_no_se_rompe(self):
        # Humo: los bugs de esta capa (columnas, paginación, emoji) no los ve ningún test puro.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            r = informes.reporte_coordinacion_pdf(
                self._datos(["🎯 título con emoji que Helvetica no dibuja"]), d,
                materia="Programación I", fecha="2026-01-01", anexo=True)
            self.assertTrue(os.path.getsize(r["archivo"]) > 1000)
            self.assertGreaterEqual(r["paginas"], 1)


class TestLaLeyendaDelPieEntra(unittest.TestCase):
    """`canvas.drawString` no recorta ni hace wrap: dibuja ENCIMA.

    La leyenda se montaba sobre el número de página y quedaban ilegibles las dos cosas. Un
    `Paragraph` del flow se acomoda solo; el canvas no. Por eso esto se mide."""

    def test_no_pisa_el_numero_de_pagina(self):
        from reportlab.pdfbase.pdfmetrics import stringWidth
        ancho = stringWidth(informes.LEYENDA_SITUACION, "Helvetica", 6.2)
        self.assertLessEqual(
            ancho, informes.LEYENDA_ANCHO_MAX,
            f"la leyenda mide {ancho:.0f}pt y el tope es {informes.LEYENDA_ANCHO_MAX:.0f}pt: "
            "se va a dibujar encima del número de página")


class TestDegradadoSaleDeTodosLosHuecos(unittest.TestCase):
    """El invariante que se rompió: hay huecos <-> está degradado.

    Se calculaba enumerando las fuentes de huecos una por una y faltaban dos, así que el dict
    los declaraba y el PDF salía sin el aviso. Un hueco que el documento no muestra no existe.
    """

    def test_un_solo_hueco_ya_degrada(self):
        m = panorama.meta_de_huecos(["no pude leer una comisión"])
        self.assertTrue(m["degradado"])
        self.assertEqual(m["sin_dato"], ["no pude leer una comisión"])

    def test_sin_huecos_no_degrada(self):
        self.assertFalse(panorama.meta_de_huecos([])["degradado"])

    def test_ningun_hueco_puede_quedar_afuera_del_flag(self):
        # El bug real: un hueco de una fuente que nadie sumó a la enumeración.
        for n in range(1, 6):
            m = panorama.meta_de_huecos([f"hueco {i}" for i in range(n)])
            self.assertTrue(m["degradado"], f"con {n} huecos no marcó degradado")
            self.assertEqual(len(m["sin_dato"]), n)

    def test_los_extras_NO_pueden_pisar_el_invariante(self):
        # Si alguien pasa degradado=False desde afuera, el aviso se volvería a esconder.
        m = panorama.meta_de_huecos(["x"], fuente="vivo", segundos=1.0,
                                    degradado=False, sin_dato=[])
        self.assertTrue(m["degradado"], "un extra pisó el flag y el hueco se esconde de nuevo")
        self.assertEqual(m["sin_dato"], ["x"])
        self.assertEqual(m["fuente"], "vivo")



# --------------------------------------------------------------------------------------
# El informe de nexos: el cruce con el catálogo de regionales
# --------------------------------------------------------------------------------------

class TestAsignarNexos(unittest.TestCase):
    """`asignar_nexos` — le pega a cada bloque de regional su Tutor Nexo.

    Estos tests existen por un bug que llegó a una versión publicada: `informe_nexos` usaba
    `aviso_cat`, `sin_nexo`, `sin_regional` y `catalogo` sin haberlos definido nunca, así que
    reventaba con `NameError` en el 100% de las corridas. Un tutor se lo comió en medio de su
    circuito diario y quedó sin poder derivar a sus alumnos.

    Los 139 tests que había no lo tocaban porque la función necesita red. Por eso el cruce se
    extrajo a esta función pura: para que se pueda probar sin campus.
    """

    CATALOGO = {
        "Avellaneda": {
            "facultad": "Facultad Regional Avellaneda",
            "nexos": ["Paula Garaventa"],
            "mails": ["tutoriatup@fra.utn.edu.ar"],
        },
        "Mendoza": {"facultad": "Facultad Regional Mendoza", "nexos": ["Nombre Ap"], "mails": []},
    }

    def test_regional_con_nexo_lo_recibe_completo(self):
        bloques = [{"regional": "Avellaneda", "desenganchados": 3}]
        sin_nexo, sin_regional = panorama.asignar_nexos(bloques, self.CATALOGO)
        self.assertEqual(sin_nexo, [])
        self.assertEqual(sin_regional, 0)
        self.assertEqual(bloques[0]["nexo"]["nexos"], ["Paula Garaventa"])
        self.assertEqual(bloques[0]["nexo"]["facultad"], "Facultad Regional Avellaneda")

    def test_regional_que_no_esta_en_el_catalogo_no_inventa_responsable(self):
        """Una regional sin nexo queda en None y SE DECLARA. Adjudicarle esos alumnos a
        cualquier otro nexo sería peor que no nombrar a nadie."""
        bloques = [{"regional": "Regional Que No Existe", "desenganchados": 2}]
        sin_nexo, _ = panorama.asignar_nexos(bloques, self.CATALOGO)
        self.assertEqual(sin_nexo, ["Regional Que No Existe"])
        self.assertIsNone(bloques[0]["nexo"])

    def test_los_sin_regional_se_cuentan_aparte_y_no_son_una_regional_faltante(self):
        """No estar en ningún grupo `R-*` es OTRO problema que una regional sin nexo: no hay
        a quién derivarlos, y mezclarlos haría creer que falta cargar una sede."""
        bloques = [{"regional": panorama._SIN_REGIONAL, "desenganchados": 5}]
        sin_nexo, sin_regional = panorama.asignar_nexos(bloques, self.CATALOGO)
        self.assertEqual(sin_nexo, [])
        self.assertEqual(sin_regional, 5)
        self.assertIsNone(bloques[0]["nexo"])

    def test_catalogo_vacio_no_rompe_y_declara_todas(self):
        """Si el catálogo no se pudo leer, el informe sale igual pero sin contactos y
        diciéndolo. Un catálogo que falla no puede inventar un responsable."""
        bloques = [{"regional": "Avellaneda", "desenganchados": 1},
                   {"regional": "Mendoza", "desenganchados": 2}]
        sin_nexo, _ = panorama.asignar_nexos(bloques, {})
        self.assertEqual(sorted(sin_nexo), ["Avellaneda", "Mendoza"])
        self.assertTrue(all(b["nexo"] is None for b in bloques))

    def test_el_catalogo_real_del_repo_se_lee_y_tiene_las_17_regionales(self):
        """`nexos.json` viaja en el repo A PROPÓSITO: si queda ignorado, la skill anda en la
        máquina del que lo escribió y ningún otro tutor recibe los contactos."""
        catalogo, aviso = panorama.nexos_por_regional()
        self.assertIsNone(aviso, f"el catálogo del repo no se pudo leer: {aviso}")
        self.assertEqual(len(catalogo), 17)


class TestSinNombresIndefinidos(unittest.TestCase):
    """Ningún módulo usa un nombre que no existe.

    Es el test que hubiera cazado el `NameError` de `informe_nexos` el día que se escribió,
    en vez de que lo encontrara un tutor con el informe roto adelante. Cubre TODO el paquete,
    incluidos los caminos que ningún otro test ejercita porque necesitan red — que son
    justamente donde se esconden estos.

    `pyflakes` es una dependencia de DESARROLLO: si no está, el test se saltea en vez de
    fallar. El tutor no tiene por qué instalarla para verificar su propia herramienta.
    """

    def test_pyflakes_no_encuentra_nombres_indefinidos(self):
        try:
            from pyflakes.api import checkPath
            from pyflakes.reporter import Reporter
        except ImportError:
            self.skipTest("pyflakes no instalado (dependencia de desarrollo)")

        import io

        raiz = Path(__file__).resolve().parent.parent / "mcp"
        salida, errores = io.StringIO(), io.StringIO()
        reporter = Reporter(salida, errores)
        for py in sorted(raiz.rglob("*.py")):
            if "__pycache__" in str(py):
                continue
            checkPath(str(py), reporter)

        indefinidos = [l for l in salida.getvalue().splitlines() if "undefined name" in l]
        self.assertEqual(
            indefinidos, [],
            "hay nombres usados y nunca definidos:\n  " + "\n  ".join(indefinidos))


class TestSoloAlumnosActivos(unittest.TestCase):
    """`_es_estudiante` — el filtro compartido de quién cuenta en los informes.

    Aporte de un tutor del equipo (2026-08-17). Una matrícula suspendida es una BAJA del aula:
    el alumno sigue figurando en la lista de participantes, así que sin filtrarlo aparece como
    deudor, pendiente, retrasado y desenganchado a la vez. Infla cuatro informes con gente que
    ya no cursa, y lo hace en silencio — los números quedan plausibles.
    """

    def _p(self, **kw):
        base = {"roles": [{"shortname": "student"}]}
        base.update(kw)
        return base

    def test_alumno_activo_cuenta(self):
        self.assertTrue(ws_api._es_estudiante(self._p(suspended=False)))

    def test_alumno_suspendido_no_cuenta(self):
        self.assertFalse(ws_api._es_estudiante(self._p(suspended=True)))

    def test_suspendido_manda_sobre_el_rol(self):
        """Un suspendido con rol de estudiante sigue siendo una baja."""
        self.assertFalse(
            ws_api._es_estudiante({"suspended": True, "roles": [{"shortname": "student"}]}))

    def test_docente_no_cuenta_como_alumno(self):
        self.assertFalse(
            ws_api._es_estudiante({"roles": [{"shortname": "editingteacher"}]}))

    def test_sin_roles_cuenta(self):
        """El campus a veces devuelve la fila sin roles; se asume alumno, como antes."""
        self.assertTrue(ws_api._es_estudiante({"roles": []}))
        self.assertTrue(ws_api._es_estudiante({}))

    def test_sin_el_campo_suspended_no_se_descarta_a_nadie(self):
        """`core_enrol_get_enrolled_users` NO trae `suspended` — ya viene filtrado con
        `onlyactive:1` en origen. Si la ausencia del campo se leyera como suspendido, este
        filtro vaciaría medio informe."""
        self.assertTrue(ws_api._es_estudiante({"roles": [{"shortname": "student"}]}))


class TestAlumnosFueraDeLaTarea(unittest.TestCase):
    """`alumnos_fuera_de_la_tarea` — quién está matriculado y la tarea no lista.

    Este hueco no lo delata nada: el conteo de la tarea es internamente consistente y se lee
    como completo. Medido el 2026-08-18, dos alumnos en las cuatro comisiones de un tutor, uno
    de ellos ausente de las ONCE actividades de su curso — o sea, invisible para todos los
    informes de corrección a la vez.
    """

    def _correr(self, respuestas, ids_en_tarea):
        import asyncio
        cli = ClienteFalso(respuestas)
        return asyncio.run(
            ws_api.alumnos_fuera_de_la_tarea(cli, 74, 7740, set(ids_en_tarea)))

    def _alumno(self, uid, nombre):
        return {"id": uid, "fullname": nombre, "email": f"{uid}@x.com",
                "roles": [{"shortname": "student"}]}

    def test_detecta_al_que_la_tarea_no_lista(self):
        us = [self._alumno(1, "A"), self._alumno(2, "B"), self._alumno(3, "INVISIBLE")]
        r = self._correr({"core_enrol_get_enrolled_users": us}, {1, 2})
        self.assertEqual([a["userid"] for a in r["invisibles"]], [3])
        self.assertTrue(r["_meta"]["verificado"])

    def test_cuando_cuadra_no_inventa_a_nadie(self):
        us = [self._alumno(1, "A"), self._alumno(2, "B")]
        r = self._correr({"core_enrol_get_enrolled_users": us}, {1, 2})
        self.assertEqual(r["invisibles"], [])

    def test_los_docentes_no_cuentan_de_ninguno_de_los_dos_lados(self):
        """El padrón de la tarea son ALUMNOS. Comparar contra los matriculados totales mete
        al tutor en la cuenta y da una diferencia falsa: en com6 daba «36 de 38» cuando los
        alumnos son 37 y el 38 era el propio tutor."""
        us = [self._alumno(1, "A"), self._alumno(2, "B"),
              {"id": 99, "fullname": "TUTOR", "roles": [{"shortname": "editingteacher"}]}]
        r = self._correr({"core_enrol_get_enrolled_users": us}, {1, 2})
        self.assertEqual(r["invisibles"], [])
        self.assertEqual(r["_meta"]["alumnos_matriculados"], 2)
        self.assertEqual(r["_meta"]["matriculados_totales"], 3)

    def test_si_falla_la_consulta_NO_dice_que_esta_todo_bien(self):
        """«No encontré a nadie afuera» y «no pude chequear» son opuestos. Sin
        `verificado: False`, un fallo de red se leería como padrón cuadrado."""
        r = self._correr(
            {"core_enrol_get_enrolled_users": MoodleWSError("sin permiso", {})}, {1, 2})
        self.assertEqual(r["invisibles"], [])
        self.assertFalse(r["_meta"]["verificado"])
        self.assertIn("motivo", r["_meta"])

    def test_respuesta_con_forma_inesperada_tampoco_tranquiliza(self):
        r = self._correr({"core_enrol_get_enrolled_users": {"exception": "x"}}, {1})
        self.assertFalse(r["_meta"]["verificado"])


class TestTrazaVaciaNoSeAfirmaCompleta(unittest.TestCase):
    """Una traza que no revisó nada NO puede decir `degradado: False`.

    Antes, un alumno que no figuraba en el padrón de ninguna tarea devolvía `entregas: []`
    con `degradado: False` y cero avisos: el relevamiento se afirmaba completo habiendo
    revisado cero. Se lee como «no entregó nada» y la verdad es «no sé nada de él».

    Lo reportó un tutor como «una tarea rota degrada la traza entera». El diagnóstico era
    otro —el código sí saltea la tarea rota— pero el instinto era correcto: ese 0 estaba mal.
    """

    def _traza(self, filas_por_tarea, tareas):
        """filas_por_tarea: por assign_id, la fila del alumno o None si no figura."""
        import asyncio

        async def falsa_entregas_tarea(client, cmid, group_id=0):
            fila = filas_por_tarea.get(str(cmid))
            alumnos = [fila] if fila else []
            return {"tarea": f"T{cmid}", "alumnos": alumnos}

        original = ws_api.entregas_tarea
        ws_api.entregas_tarea = falsa_entregas_tarea
        try:
            return asyncio.run(ws_api.traza_alumno(
                None, {"email": "a@x.com", "group_id": 1}, tareas))
        finally:
            ws_api.entregas_tarea = original

    def _fila(self, estado="Sin entrega"):
        return {"email": "a@x.com", "estado": estado, "nota": None, "pendiente": False}

    def test_alumno_que_no_figura_en_ninguna_tarea_se_declara(self):
        tareas = [{"assign_id": "1", "titulo": "T1"}, {"assign_id": "2", "titulo": "T2"}]
        r = self._traza({}, tareas)
        self.assertEqual(r["entregas"], [])
        self.assertEqual(r["_meta"]["tareas_revisadas"], 0)
        self.assertEqual(r["_meta"]["tareas_donde_no_figura"], 2)
        self.assertTrue(r["_meta"]["degradado"], "una traza que revisó 0 no puede ir sin marca")
        self.assertIn("NINGUNA", r["_meta"]["avisos"][0])

    def test_no_figurar_en_algunas_tambien_se_declara(self):
        tareas = [{"assign_id": "1", "titulo": "T1"}, {"assign_id": "2", "titulo": "T2"}]
        r = self._traza({"1": self._fila()}, tareas)
        self.assertEqual(r["_meta"]["tareas_revisadas"], 1)
        self.assertEqual(r["_meta"]["tareas_donde_no_figura"], 1)
        self.assertTrue(r["_meta"]["degradado"])

    def test_alumno_normal_no_dispara_ninguna_alarma(self):
        """Con el padrón sano la traza va limpia: si esto marcara degradado, la señal se
        volvería ruido y nadie la miraría."""
        tareas = [{"assign_id": "1", "titulo": "T1"}, {"assign_id": "2", "titulo": "T2"}]
        r = self._traza({"1": self._fila(), "2": self._fila("Calificado")}, tareas)
        self.assertEqual(r["_meta"]["tareas_revisadas"], 2)
        self.assertEqual(r["_meta"]["tareas_donde_no_figura"], 0)
        self.assertFalse(r["_meta"]["degradado"])
        self.assertEqual(r["_meta"]["avisos"], [])


class TestDiagnosticoDeErrorActiveIA(unittest.TestCase):
    """`GEMINI_OVERLOADED` significa dos cosas OPUESTAS con el mismo texto.

    Reportado por un tutor con caso control (2026-08-17): 8 correcciones limpias en el mismo
    rato en que 4 entregas viejas seguían fallando. El servicio nunca estuvo caído — lo que
    estaba roto era la entrega, y ésa no se destraba sola nunca.

    Confundirlos cuesta días de reintentos que no pueden funcionar: la tool RETOMA la entrega
    ya subida, así que cada intento vuelve a chocar contra el mismo registro en ERROR.
    """

    _diag = staticmethod(active_ia.diagnosticar_error)

    def test_entrega_nueva_puede_ser_saturacion_y_reintentar_sirve(self):
        r = self._diag({"error": "GEMINI_OVERLOADED", "error_code": "GEMINI_OVERLOADED"},
                       reusada=False)
        self.assertEqual(r["diagnostico"], "servicio_saturado")
        self.assertTrue(r["reintentar_sirve"])

    def test_entrega_retomada_esta_atascada_y_reintentar_NO_sirve(self):
        """La señal que las separa: si vino de un 409 y encima está en ERROR, ese error es
        viejo y persistido, no una saturación de este momento."""
        r = self._diag({"error": "GEMINI_OVERLOADED", "error_code": "GEMINI_OVERLOADED"},
                       reusada=True)
        self.assertEqual(r["diagnostico"], "entrega_atascada")
        self.assertFalse(r["reintentar_sirve"])
        self.assertIn("BORRARLA", r["que_hacer"])

    def test_zip_sobredimensionado_no_se_arregla_subiendo_el_timeout(self):
        r = self._diag({"error": "se pasó", "error_code": "NBN_TIMEOUT"}, reusada=False)
        self.assertEqual(r["diagnostico"], "zip_sobredimensionado")
        self.assertFalse(r["reintentar_sirve"])
        self.assertIn("timeout", r["que_hacer"].lower())

    def test_el_zip_manda_aunque_la_entrega_venga_retomada(self):
        """Un NBN_TIMEOUT sobre una entrega retomada sigue siendo un problema del archivo:
        borrarla de Active-IA no alcanza si el alumno vuelve a subir el mismo ZIP."""
        r = self._diag({"error": "x", "error_code": "NBN_TIMEOUT"}, reusada=True)
        self.assertEqual(r["diagnostico"], "zip_sobredimensionado")

    def test_conserva_los_campos_originales_del_error(self):
        r = self._diag({"error": "algo", "error_code": "X", "entrega_id": 99}, reusada=False)
        self.assertEqual(r["entrega_id"], 99)
        self.assertEqual(r["error"], "algo")


class TestSubmissionsMap(unittest.TestCase):
    """`submissions_map` — la vía por la que se recupera el trabajo de un alumno que la
    tarea no lista.

    `mod_assign_list_participants` y `mod_assign_get_submissions` NO coinciden: hay alumnos
    matriculados y activos que no salen en la lista pero cuya entrega sí está acá, intacta.
    Verificado en vivo: `ver_entrega` abre sin problema los 8 `.java` de un alumno que no
    figura en ningún padrón.
    """

    def _map(self, subs):
        import asyncio
        cli = ClienteFalso({"mod_assign_get_submissions":
                            {"assignments": [{"submissions": subs}]}})
        return asyncio.run(ws_api.submissions_map(cli, 1))

    def test_devuelve_el_status_por_userid(self):
        r = self._map([{"userid": 1, "status": "submitted"},
                       {"userid": 2, "status": "new"}])
        self.assertEqual(r, {1: "submitted", 2: "new"})

    def test_guarda_el_status_en_vez_de_contar_todo_como_entrega(self):
        """`status: new` es "abrió la tarea y no entregó". Contarlo como entrega infla el
        trabajo casi al doble: 46 registros donde el conteo oficial decía 25."""
        r = self._map([{"userid": 1, "status": "new"}])
        self.assertEqual(r[1], "new")
        self.assertNotEqual(r[1], "submitted")

    def test_error_devuelve_vacio_y_no_rompe(self):
        import asyncio
        cli = ClienteFalso({"mod_assign_get_submissions": MoodleWSError("x", {})})
        self.assertEqual(asyncio.run(ws_api.submissions_map(cli, 1)), {})

    def test_respuesta_sin_assignments_no_rompe(self):
        import asyncio
        cli = ClienteFalso({"mod_assign_get_submissions": {"assignments": []}})
        self.assertEqual(asyncio.run(ws_api.submissions_map(cli, 1)), {})


class TestClasificarGrupo(unittest.TestCase):
    """Los grupos de un curso NO son todos comisiones.

    Prog II devuelve 32 y sólo 15 lo son: las otras 17 son regionales. Contarlos juntos
    produjo un conteo de alumnos «invisibles» inflado al doble, hecho por una herramienta
    que hacía exactamente lo que decía el nombre de la tool.
    """

    def test_comisiones(self):
        for n in ("A26 C1-06", "M26 C2-14", "M25 C4-08", "A26 C1-6"):
            self.assertEqual(ws_api.clasificar_grupo(n), "comision", n)

    def test_regionales(self):
        for n in ("R-Avellaneda", "R-San Nicolás", "r-córdoba"):
            self.assertEqual(ws_api.clasificar_grupo(n), "regional", n)

    def test_auxiliares_no_son_ni_una_cosa_ni_la_otra(self):
        for n in ("Grupo_2", "Entrego_1er_examen", "", "Docentes"):
            self.assertEqual(ws_api.clasificar_grupo(n), "otro", n)
