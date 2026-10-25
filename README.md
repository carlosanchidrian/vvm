# VVM — Volatile Virtual Machines

CLI que arranca una VM con QEMU/KVM cuya **RAM y disco viven solo en memoria**.
El disco del guest es un `memfd` que se pasa como descriptor de fichero: no hay
ruta en disco ni entrada en `/dev/shm`. Al cerrar la sesión, la RAM y el disco
del guest desaparecen con ella.

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
python3 -m vvm check [--mem 512] [--disk 256] [--json]
```

Ejecuta las diez comprobaciones del anfitrión y sale con código 1 si alguna
**bloqueante** falla. `--json` devuelve la lista de resultados para consumo
mecánico. Los avisos (hibernación, core dumps, KSM y THP en shmem) se muestran
pero no bloquean; el remedio de cada uno va en la salida. Cada entrada muestra
el nombre del chequeo y, en una terminal, sale en verde (OK), amarillo (aviso)
o rojo (falla); el color se desactiva solo si la salida no es un TTY o si
defines `NO_COLOR`. La salida `--json` y el informe conservan además un
identificador interno por comprobación.

### `vvm run` — la sesión

```bash
python3 -m vvm run \
  --kernel vmlinuz-virt --initrd initramfs-virt \
  [--mem 512] [--disk 256] [--cpus 1] \
  [--headless --log /tmp/vvm-serial.log] \
  [--report informe.txt] [--qemu qemu-system-x86_64] \
  [--dry-run] [--force]
```

Secuencia:

1. Valida que kernel e initrd existen y no están vacíos.
2. Crea el disco del guest en un `memfd` (256 MiB por defecto).
3. Construye la línea de QEMU (la necesitan los chequeos de volcado de RAM
   y anonimato).
4. Ejecuta el pre-chequeo del anfitrión. Si hay bloqueos, cierra el fd y no
   arranca (`--force` los ignora).
5. Toma la línea base (Shmem, MemAvailable, `/dev/shm`, QEMU vivos).
6. Lanza QEMU heredando el fd del disco.
7. Al salir —normal, error o Ctrl-C— cierra el fd, imprime el informe (con el
   pre-chequeo en la sección 0) y guarda la sesión en
   `$XDG_STATE_HOME/vvm/last-session.json` (por defecto
   `~/.local/state/vvm/last-session.json`).

Detalles:

- `--dry-run` imprime la línea final y no ejecuta QEMU ni el pre-chequeo.
- `--headless` exige `--log`: sin fichero de salida se perdería la consola del guest.
- `--report RUTA` escribe el informe en un fichero en vez de por pantalla
  (crea el directorio si falta).
- Para salir de una sesión interactiva: **Ctrl-A x**.

### `vvm audit` — informe de una sesión

```bash
python3 -m vvm audit [--session RUTA]
```

Sin `--session`, lee la última sesión guardada por `run`. Imprime el informe
determinista: pre-chequeo de la sesión (sección 0), entradas nuevas en `/dev/shm`,
procesos QEMU de esa sesión vivos al cierre, deltas de Shmem/MemAvailable, log
serie declarado y las exclusiones de alcance. Si se arrancó con `--force` y había
fallos bloqueantes, el veredicto lo dice. La ruta del log no se persiste en la
sesión, así que al reconstruir el informe desde `audit` la sección del log sale
vacía.

## Qué garantiza la línea de QEMU

- `memory-backend-memfd` con `share=off`: otro proceso no puede mapear la RAM del guest.
- `dump-guest-core=off` y `mem-merge=off` en `-machine`.
- `file=/dev/fd/N`: el disco no tiene ruta y no aparece en `/dev/shm`.
- `cache=writethrough`: ni `memfd` ni tmpfs soportan I/O directa.
- `-nic none`: sin red.
- `RLIMIT_CORE=0` en el hijo: QEMU no deja core dumps.
- `-display none` y serial en `stdio` o en fichero.

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
tests/              # 91 tests, sin KVM
```
