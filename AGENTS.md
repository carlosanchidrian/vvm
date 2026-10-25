# Reglas del proyecto VVM
- Si algo no está claro, pregunta antes.
- Solo biblioteca estándar de Python 3.11+. Ninguna dependencia externa.
- No borrar ni tocar entradas de /dev/shm que no haya creado el propio programa.
- Mensajes y errores en español, con la causa y la salida cruda.
- Un commit por funcion, con pytest en verde antes de cada commit.

# Contexto del proyecto
- **VVM = Volatile Virtual Machines**: CLI que arranca una VM con QEMU/KVM cuya RAM y disco
  viven solo en memoria (`memfd`), de forma que al cerrar no quede rastro en disco.
- La especificación completa está en `SPEC.md` en la raíz. Está en `.gitignore`, no se versiona:
  si falta en local, pedírsela al usuario antes de tomar decisiones de diseño.
- Módulos: `host_check.py` (P1–P10), `images.py` (kernel/initrd + disco `memfd`),
  `vm.py` (línea de QEMU y ciclo de vida), `audit.py` (línea base e informe),
  `cli.py` (argparse: `check` | `run` | `audit`), `__main__.py` (`python3 -m vvm`).
- La IA no interviene en la ejecución: veredictos deterministas, con medidas citadas.
- Nada de `sudo` automático: se propone el comando y se sale.
- Nunca crear ni borrar entradas de `/dev/shm`; el disco del guest va por `memfd` + fd heredado.
- Código (variables, funciones, APIs) en inglés; mensajes de usuario y errores en español.
- Comprobar siempre con `python3 -m pytest`; el CLI se prueba con `python3 -m vvm <subcomando>`.
- Alcance actual: `check`, `run`, `audit`. Fuera de alcance: TUI, perfiles de escenario,
  red aislada, multi-VM, IA integrada y empaquetado.
- Los tests no requieren KVM ni QEMU real: lectores inyectables y `--qemu /bin/true`.

# Flujo de ramas
- `main` solo recibe integraciones revisadas; no se commitea directamente en ella.
- `dev` es la rama de integración.
- Cada tramo usa una rama `feat/tramo-N-<tema>` creada desde `dev`, con un commit por tramo.
- Antes de abrir el tramo siguiente, el anterior se revisa y se fusiona en `dev` (`git merge --no-ff`).
- El agente principal revisa el diff y ejecuta `python3 -m pytest` antes de cada fusión.
- No se hace push ni se fusiona `dev` en `main` sin que lo pida el usuario.
- `images.py` es módulo propio (decisión validada): disco `memfd` y validación de kernel/initrd.

# Pull requests
- Todo PR de una rama `feat/tramo-N-*` hacia `dev` usa la plantilla `pr-template.md` de la raíz.
- Rellenar con datos reales: recuentos de tests ejecutados, comandos y resultado observable.
- Marcar `[x]` solo lo que se haya ejecutado; lo que dependa del portátil o de KVM queda en `[ ]`.
- Título y cuerpo en español; el resumen en prosa, no en viñetas.
- Quitar las secciones de la plantilla que no aporten; un cambio pequeño no necesita tablas.
- No cerrar ni recrear PRs para reutilizar la plantilla: se actualiza el cuerpo del PR existente.

# Registro de actividad de la IA
- Mantener `registro-ia.md` en la raíz del repo como constancia del trabajo de la IA.
- Ese archivo NO se versiona: está en `.gitignore` y nunca debe subirse.
- Escribir en español, en orden cronológico y en formato de entradas fechadas.
- Cada entrada debe incluir: qué se hizo, qué archivos se tocaron, comandos relevantes y el resultado.
- Añadir la entrada al terminar una tarea, no antes, para que refleje el estado real.

