# VVM — Volatile Virtual Machines

CLI que arranca una VM con QEMU/KVM cuya **RAM, disco y medio de arranque viven
solo en memoria**. El disco del guest y la imagen ISO son `memfd` que se pasan
como descriptores de fichero: no hay ruta en disco ni entrada en `/dev/shm`. Al
cerrar la sesión, la RAM, el disco y el medio del guest desaparecen con ella.

Sin dependencias externas: solo biblioteca estándar de Python 3.11+ y QEMU/KVM
para una sesión real.

## Requisitos

- Linux con QEMU (`qemu-system-x86_64`) y `/dev/kvm` accesible para una sesión real.
- Python 3.11 o superior.
- No hace falta instalar nada: `python3 -m vvm …` funciona desde el repo.
  Opcionalmente, `pip install .` instala el comando `vvm`.

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

### `vvm run` — la sesión

```bash
python3 -m vvm run \
  (--image live.iso | --kernel vmlinuz-virt --initrd initramfs-virt) \
  [--mem 512] [--disk 256] [--cpus 1] \
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
6. Lanza QEMU heredando los fds del disco y del ISO.
7. Al salir —normal, error o Ctrl-C— cierra los fds, imprime el informe (con el
   pre-chequeo en la sección 0) y guarda la sesión en
   `$XDG_STATE_HOME/vvm/last-session.json` (por defecto
   `~/.local/state/vvm/last-session.json`).

El orden importa: el ISO se copia entero en RAM, así que la comprobación de memoria mide la que
hay de verdad antes de esa copia, y la línea base se toma antes de que el
`memfd` del ISO (ni el del disco) exista; si no, el delta de Shmem del informe
descontaría el tamaño del propio ISO.

Detalles:

- `--dry-run` imprime la línea final con `<fd>` en lugar de cada descriptor, no
  crea ningún fd, no lee el ISO y no ejecuta QEMU ni el pre-chequeo.
- `--disk 0` desactiva el disco de datos, lo habitual con un live.
- `--headless` exige `--log` y solo vale para `--kernel/--initrd`: sin fichero
  de salida se perdería la consola del guest.
- `--log` no aplica con `--image`: el live se pilota en la ventana y su consola
  va a la VGA, así que la combinación se rechaza (salida 2) en vez de ignorarse.
- `--cpus`, `--mem` y `--disk` se validan antes de copiar el ISO: un valor
  inválido sale con código 2 y sin haber tocado la memoria.
- `--report RUTA` escribe el informe en un fichero en vez de por pantalla
  (crea el directorio si falta).
- Para salir de una sesión interactiva: **Ctrl-A x**.

### `vvm audit` — informe de una sesión

```bash
python3 -m vvm audit [--session RUTA]
```

Sin `--session`, lee la última sesión guardada por `run`. Imprime el informe
determinista: pre-chequeo de la sesión (sección 0), entradas nuevas en `/dev/shm`,
memfds nuevos comparados con la línea base —los del escritorio
(`displaysurface`, `wayland-cursor`, `pulseaudio`) se declaran como pila del
anfitrión, no del guest—, procesos QEMU de esa sesión vivos al cierre, deltas de
Shmem/MemAvailable, log serie declarado y las exclusiones de alcance. Si se arrancó con `--force` y había
fallos bloqueantes, el veredicto lo dice. La ruta del log no se persiste en la
sesión, así que al reconstruir el informe desde `audit` la sección del log sale
vacía.

## Qué garantiza la línea de QEMU

- `memory-backend-memfd` con `share=off`: otro proceso no puede mapear la RAM del guest.
- `dump-guest-core=off` y `mem-merge=off` en `-machine`.
- `file=/dev/fd/N`: ni el disco ni el ISO tienen ruta ni aparecen en `/dev/shm`.
- `cache=writethrough`: ni `memfd` ni tmpfs soportan I/O directa.
- `-nic none`: sin red.
- `RLIMIT_CORE=0` en el hijo: QEMU no deja core dumps.
- En modo kernel, `-display none` y serial en `stdio` o en fichero.
- Con `--image`, `-nodefaults`, `-vga std`, ratón absoluto por USB, el ISO como
  cdrom de solo lectura por fd, `-boot d` y `-display gtk`.

## Límites honestos

- `Shmem` es un contador global del anfitrión: con un escritorio abierto tiene
  cientos de MB de ruido. El informe no atribuye sus variaciones al guest sin
  una línea base limpia.
- Fuera del alcance del informe: `root`/`ptrace`, volcado en vivo (LiME/AVML),
  DMA/Thunderbolt, cold boot, `journald` y page cache, y la propia herramienta.
- La herramienta no ejecuta `sudo`: si `vvm check` pide `swapoff`, lo propone y
  eres tú quien decide.
- No crea ni borra entradas de `/dev/shm` ajenas.
- La IA no interviene en la ejecución: los veredictos los calcula el código.

## Desarrollo

```bash
python3 -m pytest -q          # suite completa (sin KVM ni QEMU real)
python3 -m vvm check          # contra el host actual
python3 -m vvm run --kernel … --initrd … --dry-run
```

Estructura:

```
vvm/
├── host_check.py   # comprobaciones del anfitrión con lectores sustituibles
├── images.py       # disco memfd + validación de kernel/initrd
├── vm.py           # línea de QEMU y ciclo de vida
├── audit.py        # snapshots, informe y sesión en JSON
├── cli.py          # argparse: check | run | audit
└── __main__.py     # python3 -m vvm
tests/              # 148 tests, sin KVM
```
