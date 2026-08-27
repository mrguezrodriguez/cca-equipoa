"""
AGENTIC LOOP + TOOL USE — implementación de referencia
=======================================================

Todo lo de la sesión 1 en un archivo ejecutable. El dominio es el capstone:
un agente que ayuda a instructores de The Bridge a revisar proyectos finales.

Mapa del archivo:
    PARTE 1  Definir las tools (el schema que ve el modelo)
    PARTE 2  Implementar las tools (el código que ejecutas tú)
    PARTE 3  Ejecutar los bloques tool_use y construir los tool_result
    PARTE 4  El loop
    PARTE 5  Utilidades (extraer texto, contar tokens)
    PARTE 6  Ejemplo de uso

Para ejecutarlo:
    pip install anthropic
    export ANTHROPIC_API_KEY="tu-key"
    python agentic_loop.py
"""

import anthropic

client = anthropic.Anthropic()  # lee ANTHROPIC_API_KEY del entorno

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048

# Red de seguridad, NO el mecanismo de parada. La parada real es stop_reason.
# Esto solo existe para que un bug en una tool no te deje girando para siempre.
MAX_ITERACIONES = 15


# ============================================================================
# PARTE 1 — DEFINIR LAS TOOLS
# ============================================================================
# Esto es lo unico que el modelo ve de tus tools. No ve tu codigo, no ve tu
# base de datos: ve este diccionario. Por eso la description no es documentacion
# decorativa, es el prompt que decide si te llama a ti o a la tool de al lado.
#
# Una description buena responde a cuatro cosas:
#   1. Que hace
#   2. Que formato tienen los inputs (con ejemplo)
#   3. Casos limite
#   4. Cuando usar ESTA y no la parecida
#
# Fijate en get_student vs search_students: se parecen mucho, y sin el punto 4
# el modelo las confunde. Ese es el "misrouting" del examen, y la respuesta
# canonica es siempre mejorar la description (no meter few-shot, no consolidar
# las dos tools en una, no poner un router antes del LLM).

TOOLS = [
    {
        "name": "get_student",
        "description": (
            "Recupera la ficha completa de UN alumno a partir de su ID exacto. "
            "El student_id tiene el formato 'STU_' seguido de tres digitos, "
            "por ejemplo 'STU_001'. "
            "Si el ID no existe devuelve un error, no una ficha vacia. "
            "Usa esta tool solo cuando ya tengas el ID exacto; si lo que tienes "
            "es un nombre, un apellido o una cohorte, usa search_students primero."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "student_id": {
                    "type": "string",
                    "description": "ID exacto del alumno, formato STU_XXX",
                }
            },
            "required": ["student_id"],
        },
    },
    {
        "name": "search_students",
        "description": (
            "Busca alumnos por nombre parcial o por cohorte y devuelve una lista "
            "de coincidencias con su ID. La busqueda no distingue mayusculas. "
            "Puede devolver cero, uno o varios resultados. "
            "Usa esta tool cuando NO tengas el ID; para obtener la ficha completa, "
            "llama despues a get_student con el ID que te devuelva esta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Nombre parcial o nombre de cohorte",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_project_grade",
        "description": (
            "Devuelve la nota y los comentarios del proyecto final de un alumno. "
            "Requiere el student_id exacto. "
            "Si el alumno aun no ha entregado, devuelve estado 'pendiente' sin nota."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "student_id": {"type": "string", "description": "ID exacto, formato STU_XXX"}
            },
            "required": ["student_id"],
        },
    },
]


# ============================================================================
# PARTE 2 — IMPLEMENTAR LAS TOOLS
# ============================================================================
# Datos falsos para que el archivo funcione sin base de datos. En el capstone
# aqui iria una consulta real a Supabase, HubSpot o lo que toque.

ALUMNOS = {
    "STU_001": {"nombre": "Lucia Fernandez", "cohorte": "FS-2026-01", "asistencia": "92%"},
    "STU_002": {"nombre": "Marc Oliveras", "cohorte": "FS-2026-01", "asistencia": "78%"},
    "STU_003": {"nombre": "Ines Roldan", "cohorte": "DS-2026-02", "asistencia": "95%"},
}

NOTAS = {
    "STU_001": {"estado": "entregado", "nota": 8.5, "comentario": "Buen backend, front justo"},
    "STU_002": {"estado": "pendiente"},
    "STU_003": {"estado": "entregado", "nota": 9.2, "comentario": "Excelente EDA y documentacion"},
}


def get_student(student_id):
    """Devuelve la ficha de un alumno. Lanza ValueError si no existe."""
    if student_id not in ALUMNOS:
        # Lanzamos la excepcion a proposito: la capturamos mas arriba y la
        # convertimos en un tool_result con is_error. Ver PARTE 3.
        raise ValueError(f"No existe ningun alumno con el ID {student_id}")
    return ALUMNOS[student_id]


def search_students(query):
    """Busca por nombre parcial o cohorte. Devuelve lista (puede estar vacia)."""
    q = query.lower()
    resultados = []
    for sid, datos in ALUMNOS.items():
        if q in datos["nombre"].lower() or q in datos["cohorte"].lower():
            resultados.append({"student_id": sid, "nombre": datos["nombre"]})
    return resultados


def get_project_grade(student_id):
    """Devuelve la nota del proyecto final."""
    if student_id not in NOTAS:
        raise ValueError(f"No hay registro de proyecto para {student_id}")
    return NOTAS[student_id]


# El modelo te da el nombre de la tool como string. Este diccionario traduce
# ese string a la funcion de Python. Sin esto tendrias un if/elif gigante.
IMPLEMENTACIONES = {
    "get_student": get_student,
    "search_students": search_students,
    "get_project_grade": get_project_grade,
}


# ============================================================================
# PARTE 3 — EJECUTAR LOS BLOQUES tool_use
# ============================================================================

def ejecutar_tools(bloques):
    """
    Recibe response.content y devuelve la lista de tool_result.

    Tres cosas que hay que hacer bien aqui:

    1. RECORRER TODOS LOS BLOQUES. El modelo puede pedir varias tools de golpe
       (parallel tool use). Si devuelves solo el primer resultado, la API te
       responde con un 400: "tool_use ids were found without tool_result blocks".

    2. DEVOLVER EL tool_use_id. Es como la API empareja tu resultado con la
       peticion. Sin el, el modelo no sabe cual de las tres respuestas es cual.

    3. NO DEJAR QUE UNA EXCEPCION ROMPA EL LOOP. Si la tool falla, se lo cuentas
       al modelo con is_error=True y el decide que hacer: reintentar con otro
       argumento, probar otra tool, o rendirse y explicarselo al usuario.
       Un script se cae; un agente se recupera. La diferencia es este try/except.
    """
    resultados = []

    for bloque in bloques:
        # response.content mezcla tipos: text, tool_use, y a veces thinking o
        # server_tool_use. Solo nos interesan los tool_use.
        if bloque.type != "tool_use":
            continue

        funcion = IMPLEMENTACIONES.get(bloque.name)

        try:
            if funcion is None:
                # El modelo se ha inventado un nombre de tool. Pasa poco, pero pasa.
                raise ValueError(f"Tool desconocida: {bloque.name}")

            # bloque.input es un dict con los argumentos. El ** lo desempaqueta
            # como parametros con nombre: {"student_id": "STU_001"} -> student_id="STU_001"
            salida = funcion(**bloque.input)

            resultados.append({
                "type": "tool_result",
                "tool_use_id": bloque.id,     # obligatorio
                "content": str(salida),       # tambien acepta lista de bloques
            })

        except Exception as e:
            print(f"   [tool fallida] {bloque.name}: {e}")
            resultados.append({
                "type": "tool_result",
                "tool_use_id": bloque.id,
                "content": f"Error: {e}",
                "is_error": True,             # <-- la key que todo el mundo olvida
            })

    return resultados


# ============================================================================
# PARTE 4 — EL LOOP
# ============================================================================

def run_agent(pregunta, system_prompt=None):
    """
    El agente entero. Es un while, no hay mas magia.

    La API no guarda estado: cada llamada manda el historial completo. Por eso
    'messages' va creciendo y por eso hay que hacer append de DOS cosas en cada
    vuelta: la respuesta del assistant y luego los tool_result como mensaje user.
    Si te saltas el primero, el modelo recibe el resultado de una tool que, hasta
    donde el sabe, nunca pidio.
    """
    messages = [{"role": "user", "content": pregunta}]

    for vuelta in range(MAX_ITERACIONES):
        print(f"\n--- Vuelta {vuelta + 1} ---")

        kwargs = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "tools": TOOLS,
            "messages": messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        response = client.messages.create(**kwargs)

        print(f"stop_reason: {response.stop_reason}")
        print(f"tokens: {response.usage.input_tokens} in / "
              f"{response.usage.output_tokens} out")

        # Guardamos response.content ENTERO, sin filtrar ni reconstruir.
        # Si activas extended thinking, ahi dentro viene un bloque 'thinking'
        # con su 'signature'; si lo tocas o lo quitas, la API rechaza la
        # siguiente llamada.
        messages.append({"role": "assistant", "content": response.content})

        # ------------------------------------------------------------------
        # La decision. Es stop_reason y solo stop_reason.
        #
        # NO mires si hay texto: una respuesta solo-tool_use no tiene texto y
        # no ha terminado. NO busques "he terminado" en la prosa: el modelo
        # puede decir "voy a mirarlo" y venir con tool_use, o terminar en
        # silencio. NO uses el contador de vueltas: te dice cuando rendirte,
        # no cuando has acabado.
        # ------------------------------------------------------------------

        if response.stop_reason == "tool_use":
            tool_results = ejecutar_tools(response.content)
            # Los tool_result van en un mensaje de role "user", primero en el
            # array de content y justo despues del mensaje del assistant.
            messages.append({"role": "user", "content": tool_results})
            continue  # otra vuelta

        if response.stop_reason == "pause_turn":
            # Solo pasa con server tools (web_search, code_execution): el loop
            # que corre en los servidores de Anthropic ha llegado a su tope de
            # iteraciones. No ha terminado. Se reenvia tal cual y sigue.
            continue

        if response.stop_reason == "max_tokens":
            print("AVISO: respuesta truncada por max_tokens (no es un error, "
                  "esta a medias). Sube MAX_TOKENS o pidele que continue.")
            return extraer_texto(response.content)

        if response.stop_reason == "refusal":
            print("El modelo ha declinado responder.")
            return None

        # Cualquier otro caso (end_turn, stop_sequence,
        # model_context_window_exceeded) = hemos terminado.
        #
        # Fijate en que salimos por defecto en vez de comprobar == "end_turn".
        # Si un dia aparece un stop_reason nuevo, sales del loop en vez de
        # quedarte girando y pagando llamadas.
        return extraer_texto(response.content)

    # Solo se llega aqui si se agotan las iteraciones: hay un bug o la tarea
    # es mas larga de lo previsto.
    print(f"AVISO: alcanzado el limite de {MAX_ITERACIONES} vueltas.")
    return None


# ============================================================================
# PARTE 5 — UTILIDADES
# ============================================================================

def extraer_texto(bloques):
    """
    Junta todos los bloques de texto de una respuesta.

    Ojo: puede haber varios bloques text, o ninguno. Por eso se recorre y se
    filtra en vez de hacer response.content[0].text, que es la linea que
    revienta el dia que el modelo devuelve thinking primero.
    """
    trozos = [b.text for b in bloques if b.type == "text"]
    return "\n".join(trozos)


def calcular_coste(response, precio_in=3.0, precio_out=15.0):
    """
    Coste en dolares de una llamada. Los precios van por millon de tokens y
    cambian segun el modelo: comprueba los actuales antes de fiarte.

    Util para ver lo que cuesta de verdad un loop largo: en cada vuelta vuelves
    a mandar el historial entero, asi que los input_tokens crecen sin parar.
    Diez vueltas no cuestan diez veces la primera, cuestan bastante mas.
    """
    entrada = response.usage.input_tokens / 1_000_000 * precio_in
    salida = response.usage.output_tokens / 1_000_000 * precio_out
    return entrada + salida


# ============================================================================
# PARTE 6 — EJEMPLO DE USO
# ============================================================================

SYSTEM = (
    "Eres un asistente para instructores de The Bridge que revisan proyectos "
    "finales. Responde en espanol, de forma breve y concreta. "
    "Si una tool devuelve un error, explica que ha fallado en vez de inventarte "
    "los datos."
)

if __name__ == "__main__":
    # Caso 1: encadena dos tools (buscar por nombre, luego pedir la nota)
    respuesta = run_agent(
        "Que nota ha sacado Ines en el proyecto final?",
        system_prompt=SYSTEM,
    )
    print("\n=== RESPUESTA ===")
    print(respuesta)

    # Caso 2 (descomenta para probarlo): fuerza un is_error.
    # El alumno STU_999 no existe, asi que get_student lanza ValueError,
    # lo convertimos en tool_result con is_error y el modelo se recupera solo.
    #
    # respuesta = run_agent("Dame la ficha del alumno STU_999", system_prompt=SYSTEM)
    # print(respuesta)
