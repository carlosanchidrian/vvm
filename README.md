# VVM — Volatile Virtual Machines

CLI que arranca una VM con QEMU/KVM cuya **RAM, disco y medio de arranque viven
solo en memoria**. El disco del guest y la imagen ISO son `memfd` que se pasan
como descriptores de fichero: no hay ruta en disco ni entrada en `/dev/shm`. Al
cerrar la sesión, la RAM, el disco y el medio del guest desaparecen con ella.

Sin dependencias externas en el CLI: solo biblioteca estándar de Python 3.11+ y
QEMU/KVM para una sesión real. La TUI es un extra opcional
(`textual>=0.80` y `pyte>=0.8`): sin él, `check`, `run`, `audit`, `ps` y
`profiles` funcionan igual.

## Requisitos

- Linux con QEMU (`qemu-system-x86_64`) y `/dev/kvm` accesible para una sesión real.
- Python 3.11 o superior.
- No hace falta instalar nada: `python3 -m vvm …` funciona desde el repo.
  Opcionalmente, `pip install .` instala el comando `vvm`.
- Para la TUI: `pip install -e '.[tui]'`.

## Uso

```bash
python3 -m vvm --version   # vvm 0.1.0
```

### `vvm check` — pre-chequeo del anfitrión

```bash
python3 -m vvm check [--image ISO] [--mem 512] [--disk 256] [--json]
```

Ejecuta las diez comprobaciones del anfitrión y sale con código 1 si alguna
**bloqueante** falla. Con `--image`, el chequeo de memoria cuenta también el
tamaño del ISO —se queda entero en RAM— y avisa (sin bloquear) si `--mem` es
menor de 2048 MB. `--json` devuelve la lista de resultados para consumo
mecánico. Los avisos (hibernación, core dumps, KSM y THP en shmem) se muestran
pero no bloquean; el remedio de cada uno va en la salida. Cada entrada muestra
el nombre del chequeo y, en una terminal, sale en verde (OK), amarillo (aviso)
o rojo (falla); el color se desactiva solo si la salida no es un TTY o si
defines `NO_COLOR`. La salida `--json` y el informe conservan además un
identificador interno por comprobación.

### `vvm profiles` — perfiles de escenario

```bash
python3 -m vvm profiles [--json]
```

Lee `$XDG_CONFIG_HOME/vvm/profiles.toml` (por defecto
`~/.config/vvm/profiles.toml`) y lista cada perfil con su fuente (`ISO` o
`kernel`), RAM, disco y red (siempre `none`). Las rutas del TOML expanden `~` y
las relativas se resuelven respecto al directorio del propio fichero. Una clave
desconocida es un error: se listan las válidas y el fichero, sin ignorar erratas.
La validación de combinaciones se delega en la misma que usa `run`.

```toml
default = "kali"

[profiles.kali]
image    = "~/isos/kali-linux-2026.3-live.iso"
mem_mb   = 4096
disk_mb  = 256
cpus     = 2
wipe     = true

[profiles.alpine-min]
kernel     = "~/developing/vvm/poc/vmlinuz-virt"
initrd     = "~/developing/vvm/poc/initramfs-virt"
mem_mb     = 512
disk_mb    = 256
headless   = false
serial_log = "~/vvm-logs/alpine.log"
```

`vvm run --profile NOMBRE` y `vvm check --profile NOMBRE` usan ese perfil. Los
flags `--mem`, `--disk`, `--cpus` y `--wipe` sobrescriben el perfil; `--image`,
`--kernel` o `--initrd` junto a `--profile` son un error (la fuente la decide el
perfil). Un perfil inexistente o un TOML ausente salen con código 2 y explican
qué ruta o qué nombres se esperaban. La TUI permite crear, editar y eliminar
perfiles con `n`, `e` y `d`; guarda solo el bloque afectado, así que conserva
los comentarios y el orden del resto del fichero, y las rutas bajo `$HOME` como
`~/`. Al borrar el `default`,
pasa a serlo el primer perfil alfabético que quede; si el fichero no tenía
`default`, no se inventa, y un `default` roto se deja como está para que
`default_warning` lo declare.

### `vvm ps` — sesiones vivas

```bash
python3 -m vvm ps [--json]
```

Lee el registro que cada sesión escribe en
`$XDG_RUNTIME_DIR/vvm/sessions/<id>/live.json` (o el temporal del sistema) y
lista solo las sesiones cuyo PID sigue vivo **y** es un QEMU VVM. Un PID
reciclado no se presenta como VM: la entrada se poda de forma best-effort. La
tabla muestra id, perfil, PID, RAM, disco, estado y tipo de consola; `--json`
devuelve una lista ordenada por id, con claves ordenadas y sin datos volátiles
como el uptime. Sin sesiones imprime `no hay sesiones vivas` y sale con 0.

El id tiene la forma `<perfil>-<4 hex>` (el CLI usa `cli`). El runtime de cada
sesión es un directorio `0700` con su socket serie y su `live.json`; el estado
guardado va a `$XDG_STATE_HOME/vvm/sessions/<id>.json`.

### `vvm run` — la sesión

```bash
python3 -m vvm run \
  (--image live.iso | --kernel vmlinuz-virt --initrd initramfs-virt | --profile NOMBRE) \
  [--mem 512] [--disk 256] [--cpus 1] [--wipe] \
  [--headless --log /tmp/vvm-serial.log] \
  [--report informe.txt] [--qemu qemu-system-x86_64] \
  [--dry-run] [--force]
```

La fuente de arranque es **una y solo una**: `--image`, o `--kernel` junto con
`--initrd`. Con `--image` el ISO se copia en bloques a un `memfd` anónimo y se
hereda como `/dev/fd/N`; nunca se pasa por ruta. La sesión con `--image` abre
siempre una ventana (`-display gtk`): el cargador del ISO pinta su menú en la
consola VGA y sin pantalla se quedaría parada ahí, así que `--headless` solo
existe para el modo kernel.

Secuencia:

1. Valida la fuente de arranque y el tamaño del ISO (solo `stat`): el ISO
   existe, o kernel e initrd existen y no están vacíos.
2. Ejecuta el pre-chequeo del anfitrión sobre una línea de QEMU con
   descriptores simulados: los chequeos solo miran su forma. Si hay bloqueos,
   no arranca y no copia nada (`--force` los ignora).
3. Toma la línea base (Shmem, MemAvailable, `/dev/shm`, memfds y QEMU vivos),
   antes de reservar memoria para los medios.
4. Crea el disco del guest en un `memfd` (256 MiB por defecto; `--disk 0` lo
   desactiva) y, con `--image`, copia el ISO a otro `memfd`.
5. Construye la línea de QEMU definitiva con los descriptores reales.
6. Instala el guard de señales (SIGTERM, SIGHUP y SIGINT) y lanza QEMU
   heredando los fds del disco y del ISO.
7. Al salir —normal, error, Ctrl-C, SIGTERM o SIGHUP— cierra el ISO, borra el
   disco si se pidió `--wipe`, cierra el disco, toma la instantánea final,
   imprime el informe (con el pre-chequeo en la sección 0) y guarda la sesión
   en `$XDG_STATE_HOME/vvm/sessions/<id>.json`, además de conservar
   `$XDG_STATE_HOME/vvm/last-session.json` (por defecto
   `~/.local/state/vvm/last-session.json`) para `vvm audit` sin argumentos.
8. Restaura los manejadores de señal.

El orden importa: el ISO se copia entero en RAM, así que la comprobación de memoria mide la que
hay de verdad antes de esa copia, y la línea base se toma antes de que el
`memfd` del ISO (ni el del disco) exista; si no, el delta de Shmem del informe
descontaría el tamaño del propio ISO.

Detalles:

- `--dry-run` imprime la línea final con `<fd>` en lugar de cada descriptor, no
  crea ningún fd, no lee el ISO y no ejecuta QEMU ni el pre-chequeo.
- `--disk 0` desactiva el disco de datos, lo habitual con un live.
- `--wipe` sobrescribe con ceros el disco de datos antes de cerrarlo, también si
  la sesión se interrumpe. Con `--disk 0` no es un error: el informe declara
  «sin disco de datos». El ISO no se borra: se libera al cerrar su `memfd`.
- `--headless` exige `--log` y solo vale para `--kernel/--initrd`: sin fichero
  de salida se perdería la consola del guest.
- `--log` no aplica con `--image`: el live se pilota en la ventana y su consola
  va a la VGA, así que la combinación se rechaza (salida 2) en vez de ignorarse.
- `--cpus`, `--mem` y `--disk` se validan antes de copiar el ISO: un valor
  inválido sale con código 2 y sin haber tocado la memoria.
- `--report RUTA` escribe el informe en un fichero en vez de por pantalla
  (crea el directorio si falta).
- Ctrl-C, SIGTERM y SIGHUP cierran la sesión de forma ordenada: se reenvía
  SIGTERM a QEMU, se espera hasta 5 s y, si no atiende, se le manda SIGKILL (el
  informe lo declara). El CLI sale con 130, 143 o 129 respectivamente.
- Las señales cubren todo el ciclo, no solo la vida de QEMU: durante el
  teardown (wipe, instantánea final, informe y guardado) solo se registran y el
  cierre termina; una señal que no inició el cierre sale como `128+señal` y el
  informe dice `Cierre: SIGTERM`, `SIGHUP` o `Ctrl-C` si llegó antes de
  construirlo. Si ya no consta cómo cerró la sesión (por ejemplo, un fallo
  antes de lanzar QEMU o una sesión antigua), el informe dice
  `Cierre: desconocido`.
- Ventana del cierre: si la señal llega **después** de construir el informe
  (mientras se escribe el fichero o se guarda la sesión), el informe puede
  declarar el cierre anterior —normal o la señal previa— porque ya estaba
  compuesto; el código de salida sigue siendo `128+señal`. El fichero es una
  foto del cierre, no una promesa sobre los últimos milisegundos.
- Para salir de una sesión interactiva: **Ctrl-A x**.

### `vvm tui` — centro de control multi-VM

```bash
pip install -e '.[tui]'
python3 -m vvm tui [--profile NOMBRE]
```

Abre la TUI de Textual. Es un front-end: no abre `/proc` ni lanza procesos;
usa `session`, `supervisor`, `console`, `host_check`, `audit`, `images` y `vm`
(solo para construir la línea que inspecciona el pre-chequeo). Sin terminal
interactivo o sin el extra, sale con código 2 y explica qué falta; el resto de
subcomandos siguen funcionando sin Textual ni pyte.

Pantallas y teclas:

- **Perfiles**: `enter` arrancar, `c` pre-chequeo sin arrancar, `n` crear un
  perfil, `e` editar el seleccionado, `d` eliminarlo (con confirmación),
  `a` último informe y `q` salir. Si el TOML no existe, la pantalla se abre
  vacía y `n` permite crear el primero; el formulario valida las mismas reglas
  que `run` y guarda los cambios de forma atómica. Un nombre duplicado se
  rechaza sin sobrescribir y el nombre no se cambia al editar.
- **Pre-chequeo**: P1-P10 con estado, evidencia y remedio; `enter` seguir,
  `f` forzar, `r` repetir y `esc` volver.
- **Centro de control**: una pestaña por VM. La cabecera muestra `● id · perfil ·
  estado · uptime · RAM · PID`; debajo, el panel de I/O y memoria, la consola
  serie interactiva y el aviso de ventana GTK, wipe y consola. La etiqueta pasa
  por `●` viva, `◐` cerrando y `○` terminada. Al terminar una VM la pestaña no
  se cierra: sustituye la consola por su informe.
- **Informe**: el texto de `build_report()` tal cual, con desplazamiento;
  `g` guardar, `y` copiar y `esc` volver.

Consola serie: QEMU escucha en un socket Unix dentro del runtime de la sesión y
la TUI lo conecta a una pantalla VT100 con pyte. `enter` con el panel enfocado
activa la captura: las teclas van al guest (incluidas `q`, `s` y `Ctrl-C`) y el
borde se pone verde. `Ctrl-G` es la única salida: abre el menú de la VM activa.

| Tecla | Dónde | Acción |
|---|---|---|
| `Ctrl-G` | siempre | menú de la VM activa (sale de la captura) |
| `n` | menú / centro | nueva VM |
| `s` | menú | parar esa VM (SIGTERM) y mostrar su informe |
| `w` | menú | alternar wipe de esa VM |
| `r` | menú | ver el informe de la pestaña |
| `←` / `→` | menú | pestaña anterior / siguiente |
| `q` | sin captura / menú | cerrar **solo** la VM activa |
| `c` | menú | enviar `Ctrl-C` al guest |
| `Ctrl-Q` | siempre | **todas** con wipe, con confirmación, y salir |
| `enter` | panel | entrar en la consola (captura) |

La barra superior tiene dos iconos: `●` a la izquierda (tooltip «Menú de VMs»)
abre la lista de perfiles sin parar ninguna VM, y `✕` a la derecha (tooltip
«Cerrar todo + wipe») abre la confirmación que recuerda que se pondrá a cero el
disco de cada VM: `enter` confirma, `esc` cancela y el foco inicial está en
Cancelar.
Los cierres corren en un hilo para no congelar la pantalla. Si el cierre lo
inicia SIGTERM, SIGHUP o SIGINT, la app sale con 128+señal. `q` en la última
pestaña vuelve a la lista de perfiles sin cerrar la aplicación. Si hay VMs
vivas y `q` se pulsa en perfiles, pre-chequeo o informe, se vuelve al centro de
control; no se cierra nada sin el modal de cierre total.

La app no se cierra cuando una VM termina. Hay dos cadencias: el temporizador
de métricas toma una instantánea de memoria por tick y la comparte entre las
pestañas, mientras que un temporizador de consola a 10 fps repinta solo la
consola activa. El eco de una tecla se ve en el siguiente frame de consola
(≤ 0,2 s), sin esperar al tick de métricas.

### `vvm audit` — informe de una sesión

```bash
python3 -m vvm audit [ID] [--session RUTA]
```

Sin argumentos lee la última sesión guardada por `run` (o por la TUI). Con un
`ID` lee `$XDG_STATE_HOME/vvm/sessions/<id>.json`; si no existe, sale con
código 2 y lista las sesiones guardadas. `--session RUTA` sigue permitiendo
apuntar a un fichero concreto.

Imprime el informe determinista: pre-chequeo de la sesión (sección 0), entradas
nuevas en `/dev/shm`, memfds nuevos comparados con la línea base —los del
escritorio (`displaysurface`, `wayland-cursor`, `pulseaudio` y los `pipewire-*`)
se declaran como pila del anfitrión, no del guest—, procesos QEMU de esa sesión
vivos al cierre, deltas de Shmem/MemAvailable —con la línea «Otras sesiones
vivas» si había más VMs al arrancar, para no atribuir su actividad al guest—,
contadores de I/O de QEMU leídos con la VM viva, cierre y resultado de `--wipe`,
log serie declarado y las exclusiones de alcance. En una sesión con ventana, si
`write_bytes` no es cero el informe lo declara como I/O del anfitrión con GUI;
con `--disk 256` (el valor por defecto) la frase no afirma que no haya
dispositivo de bloque: dice que la I/O no se puede atribuir solo al anfitrión,
porque el guest tiene un disco virtio. En modo kernel, `write_bytes=0` es el
testigo. Si se arrancó con `--force` y había fallos bloqueantes, el veredicto lo
dice. La ruta y el tamaño del log se guardan con la sesión, así que
`audit <ID>` reproduce la sección del log aunque el fichero se mueva o se
borre después. La salida del informe termina exactamente en el salto de línea
del propio informe, sin una línea en blanco adicional, tanto en `run` como en
`audit`.

## Qué garantiza la línea de QEMU

- `memory-backend-memfd` con `share=off`: otro proceso no puede mapear la RAM del guest.
- `dump-guest-core=off` y `mem-merge=off` en `-machine`.
- `file=/dev/fd/N`: ni el disco ni el ISO tienen ruta ni aparecen en `/dev/shm`.
- `cache=writethrough`: ni `memfd` ni tmpfs soportan I/O directa.
- `-nic none`: sin red.
- `RLIMIT_CORE=0` en el hijo: QEMU no deja core dumps.
- En modo kernel, `-display none`; la consola serie va a `stdio` (CLI), a un
  fichero o a un socket Unix (TUI).
- Con `--image`, `-nodefaults`, `-vga std`, ratón absoluto por USB, el ISO como
  cdrom de solo lectura por fd, `-boot d` y `-display gtk`.
- Con `serial_mode="socket"` (solo TUI): `-monitor none`, `-chardev
  socket,id=ser0,path=…,server=on,wait=off` y `-serial chardev:ser0`. No hay
  QMP ni capturas de pantalla: la pestaña se compone del proceso, la consola y
  la ventana.

## Límites honestos

- `Shmem` es un contador global del anfitrión: con un escritorio abierto tiene
  cientos de MB de ruido. El informe no atribuye sus variaciones al guest sin
  una línea base limpia.
- Fuera del alcance del informe: `root`/`ptrace`, volcado en vivo (LiME/AVML),
  DMA/Thunderbolt, cold boot, `journald` y page cache, y la propia herramienta.
- La herramienta no ejecuta `sudo`: si `vvm check` pide `swapoff`, lo propone y
  eres tú quien decide.
- No crea ni borra entradas de `/dev/shm` ajenas.
- `--wipe` borra el disco de datos, no la RAM del guest (su `memfd` es de QEMU y
  muere con él; la remanencia DRAM queda fuera del modelo de amenaza) ni el ISO
  (copia de una imagen pública que se libera al cerrar).
- La IA no interviene en la ejecución: los veredictos los calcula el código.

## Desarrollo

```bash
python3 -m pytest -q          # suite sin KVM (TUI y consola se saltan sin el extra)
.venv/bin/python -m pytest -q # suite completa con el extra [tui]
python3 -m vvm check          # contra el host actual
python3 -m vvm run --kernel … --initrd … --dry-run
```

Estructura:

```
vvm/
├── host_check.py   # comprobaciones del anfitrión con lectores sustituibles
├── images.py       # disco memfd + validación de kernel/initrd
├── vm.py           # línea de QEMU y ciclo de vida
├── session.py      # start/poll/stop/io/finish reutilizable por CLI y TUI
├── supervisor.py   # ids, runtime, live.json, vvm ps y guardia de grupo
├── console.py      # socket serie -> pantalla pyte -> teclas (único con pyte)
├── profiles.py     # perfiles TOML y validación delegada en vm
├── audit.py        # snapshots, informe y sesión en JSON
├── cli.py          # argparse: check | run | audit | ps | profiles | tui
├── tui/            # Textual opcional: app, pantallas, widgets y CSS
└── __main__.py     # python3 -m vvm
tests/              # 334 tests, sin KVM
```
