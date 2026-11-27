# RotativaUSB — Preparador de pendrives para exámenes (Ubuntu 24.04)

Aplicación que, tras pulsar **Activar replicación**, vigila los eventos de
udev y prepara automáticamente cada pendrive USB que se conecta.

## Decisiones tomadas

| Aspecto | Decisión |
|---|---|
| GUI | GTK4 (PyGObject) → dependencias apt, `.deb` sencillo |
| Equipos de examen | Linux → `ext4` es el sistema de archivos por defecto |
| "Borrado completo" | Formateo rápido (`wipefs` + nueva tabla + `mkfs`), **sin** sobrescribir sectores |
| Privilegios | Demonio root en el bus del sistema + polkit (una sola autorización al activar), bajo demanda |
| Etiqueta del volumen | editable en la GUI (por defecto `EXAMEN`, máx. 11 caracteres; se sanea antes de aplicarla) |

## Estructura

```
rotativausb/
├── src/rotativausb/
│   ├── common.py     constantes, opciones, XML de la interfaz D-Bus
│   ├── diskops.py    wipefs / parted / mkfs / mount / rsync / umount
│   ├── daemon.py     monitor udev, cola, máquina de estados, D-Bus, polkit
│   └── gui.py        aplicación GTK4
├── src/bin/          scripts de entrada (GUI y demonio)
├── share/            política polkit y fichero .desktop
└── debian/           empaquetado y unidad systemd
```

## Modos de confirmación

- **Nunca** – aplica sin diálogo.
- **Solo la primera vez si los demás son iguales** – pregunta en el primero;
  **"iguales" = misma capacidad** (se comparan los sectores exactos de
  `/sys/block/<dev>/size`; se ignoran modelo, VID:PID y número de serie). Si
  aparece otra capacidad, vuelve a preguntar y esa pasa a ser la nueva
  referencia.
- **Solo la primera vez aunque sean diferentes** – un único diálogo en toda la sesión.
- **En cada pendrive detectado** – diálogo siempre.

Si la GUI desaparece mientras espera una confirmación, el demonio aplica un
timeout de 180 s y omite ese dispositivo en lugar de quedarse colgado.

## Finalizar replicación

El botón **Finalizar replicación** hace una parada *graceful*:

1. Deja de aceptar dispositivos nuevos.
2. Descarta los pendrives que estuvieran esperando en la cola.
3. Canciona cualquier diálogo de confirmación abierto (se cierra en la GUI y
   el daemon despierta al hilo worker en lugar de esperar 180 s).
4. **Termina por completo el dispositivo que se esté formateando/copiando.**
   No se mata el `mkfs`/`rsync` en marcha: hacerlo dejaría una tabla de
   particiones o un sistema de archivos a medias. La GUI muestra
   *"FINALIZANDO — se completará el pendrive actual antes de parar…"* y vuelve
   al estado *desactivado* cuando el trabajo termina.

No se puede reactivar mientras está en fase de finalización.

## Verificación del contenido

Antes de desmontar y expulsar cada pendrive se puede verificar que contiene
**exactamente** lo esperado (casilla *"Verificar el contenido antes de
expulsar el pendrive"*, marcada por defecto):

- Se compara el árbol completo de rutas relativas con la carpeta de origen:
  archivos, directorios y enlaces simbólicos.
- El contenido de cada archivo se compara por **hash SHA-256**, no solo por
  tamaño.
- Se comprueba el tipo de sistema de archivos (`vfat` para FAT32) y se avisa
  si la etiqueta no coincide (algunos sistemas la normalizan, por lo que la
  etiqueta no hace fallar la verificación).
- En el modo *borrado completo* se monta el volumen solo de lectura y se
  exige que esté vacío. Se ignora `lost+found`, que crea `mke2fs` en ext3/ext4.

### Propietario y permisos (ext3/ext4)

`rsync -a` conserva el UID/GID de la carpeta de origen. Si el equipo de examen
tiene otros UIDs, esos archivos podrían no ser legibles/escribibles. Con la
casilla *"Normalizar propietario y permisos (ext3/ext4)"* (marcada por
defecto) se aplica `chown -R 0:0` y `chmod -R u+rwX,go+rX`: directorios 755,
archivos 644 y se conserva el bit ejecutable donde ya existía. Cualquier
usuario puede leer/ejecutar; solo root puede modificar el contenido, que es
lo deseable en un examen.

No se usa `777`/`666`: permitiría que cualquier alumno altere el material del
examen, y además esos permisos no existen en exFAT/FAT32/NTFS. Si necesitas
una carpeta donde los alumnos **sí** puedan escribir respuestas, añádela al
origen y se le puede dar permiso de escritura aparte.

> **exFAT/FAT32/NTFS:** estos sistemas no admiten propietario, grupo ni
> permisos Unix. `rsync` se ejecuta con `--no-owner --no-group --no-perms`
> para esos casos; si se conservaran, `chown` fallaría (*Operation not
> permitted*) y rsync terminaría con código 23, abortando la replicación.
> ext3/ext4 sí conservan esos atributos. Tampoco se comparan permisos en la
> verificación: esta se basa en rutas y hashes SHA-256 del contenido.

Si falta algo, sobra algo o difiere el contenido de un archivo, se lanza un
error, **no se expulsa el dispositivo** y el registro `AUDIT` queda como
`result=error` con el detalle (`missing`, `extra`, `different`).

## Expulsión del dispositivo

Al terminar cada pendrive (y también al finalizar la replicación con uno en
curso) se hace `sync` y, si está activada la casilla **"Expulsar el pendrive al
terminar (power-off)"** —marcada por defecto—, se llama a:

```bash
udisksctl power-off -b /dev/disk/by-id/usb-...
```

Esto corta la alimentación lógica del dispositivo para que se pueda retirar con
seguridad sin esperar a "extraer de forma segura". Si el equipo no admite
power-off o falta `udisks2`, se registra un aviso y se indica en el registro
que hay que retirarlo manualmente; el resto del proceso no falla.

## Registro y auditoría (journald)

El demonio envía sus registros a journald con la etiqueta `rotativausb`
mediante `systemd-cat -t rotativausb`. Si no está disponible (ejecución
fuera de systemd), cae a stdout, que el propio servicio también captura.

```bash
journalctl -t rotativausb -f              # en directo
journalctl -t rotativausb --since today   # historial
```

Cada detección y cada resultado deja una línea estructurada:

```
AUDIT action=detect device=/dev/... model=... serial=... capacity=14 GiB result=ok
AUDIT action=prepare device=/dev/... model=... serial=... capacity=14 GiB result=ok detail=...
```

Los valores de `result` son `ok`, `error` o `skipped` (confirmación cancelada o
replicación finalizada). El campo `detail` incluye el mensaje de formateo/copia.

## Backend de formateo: mkfs directo o UDisks2

Por defecto se llama directamente a `mkfs.exfat` / `mkntfs`. Si el servicio
systemd con `ProtectSystem=full` diera problemas de permisos con esos binarios,
marca la casilla **"Usar UDisks2 para formatear exFAT/NTFS"**: en ese caso se
usa `org.freedesktop.UDisks2.Filesystem.CreateFilesystem` a través de D-Bus y
polkit. El botón **"Comprobar UDisks2…"** muestra qué herramientas y sistemas
de archivos están disponibles antes de empezar:

```
udisksctl: /usr/bin/udisksctl | mkfs.exfat: /usr/sbin/mkfs.exfat | mkntfs: /usr/sbin/mkntfs
Sistemas soportados por UDisks2: ext4, ext3, vfat, exfat, ntfs, ...
```

Si UDisks2 falla por cualquier motivo, se registrará el aviso y se hará un
**fallback automático a `mkfs` directo**, de modo que la replicación no se
detiene. La opción solo afecta a exFAT y NTFS; ext3/ext4/FAT32 siguen usando
`mkfs` directo.

## Si un pendrive ext4 se monta solo de lectura

Dos causas habituales, ambas cubiertas:

1. **Desmontaje fallido.** Si el volumen seguía montado al expulsarlo, ext4
   queda "sucio" y el siguiente equipo lo monta en modo solo lectura. Ahora
   `umount` se ejecuta con verificación estricta: si falla, se aborta y **no**
   se llama a `udisksctl power-off`, con el error en el registro `AUDIT`.
2. **Características no soportadas por el kernel del otro equipo**.
   `mkfs.ext4` se ejecuta con `-O ^metadata_csum,^64bit` para que incluso
   kernels antiguos puedan montar el volumen en lectura/escritura (en un
   pendrive no se pierde capacidad: el límite sin `64bit` es 16 TiB).

Si aun así se monta en solo lectura, comprueba en el otro equipo:

```bash
dmesg | grep -i ext4 | tail -20
mount | grep -i sdX1
```

Un mensaje `couldn't mount RDWR because of unsupported optional features`
indica una característica del sistema de archivos; `EXT4-fs error` o
`Filesystem has unsupported feature` indica corrupción o desmontaje sucio.

## Seguridad

- Solo se aceptan dispositivos de bloque con `DEVTYPE=disk` y `ID_BUS=usb`.
- Se rechaza explícitamente el disco del sistema (comparando `st_rdev` con el
  disco donde está montado `/`).
- Las rutas usadas son `/dev/disk/by-id/usb-...` (resueltas en
  `diskops.device_info()`); nunca `/dev/sdX`, cuyo nombre puede cambiar entre
  eventos.
- La GUI corre como usuario normal; el demonio es el único proceso root.
- Todas las órdenes se lanzan con `subprocess` en modo lista (jamás `shell=True`).

## Construcción e instalación

```bash
sudo apt install build-essential debhelper-compat dh-python python3-all
cd rotativausb
# Por si quedaron __pycache__ de ejecuciones anteriores como root:
sudo find . -type d -name __pycache__ -prune -exec rm -rf {} +
dpkg-buildpackage -us -uc -b
sudo apt install ../rotativausb_0.1.0_all.deb
```

El `postinst` **no habilita el servicio en el arranque**: el daemon se inicia
bajo demanda cuando la GUI lo necesita (ver sección siguiente). Comprueba con:

```bash
systemctl status rotativausb      # inactive (dead) cuando no se está usando
rotativausb
```

## Por qué hay dos ejecutables

Tras instalar el `.deb` se instalan dos programas con roles distintos:

| Ejecutable | Usuario | Función |
|---|---|---|
| `/usr/bin/rotativausb` | usuario normal | Interfaz GTK4: opciones, diálogos de confirmación, progreso y registro. **No tiene privilegios**. |
| `/usr/sbin/rotativausb-daemon` | `root` (lo arranca systemd) | Vigila udev, decide las confirmaciones, particiona, formatea, copia y expulsa. **No tiene interfaz**. |

Se comunican por D-Bus en el **bus del sistema** (`es.rotativausb.Daemon`,
objeto `/es/rotativausb/Daemon`). La GUI es solo un cliente del daemon.

### ¿Por qué no un único ejecutable?

- Si todo corriera **como usuario**, no se podría particionar, montar ni
  formatear: hacen falta capacidades de root (`CAP_SYS_ADMIN` para `mount`).
- Si todo corriera **como root**, la GUI (selector de archivos, GTK, diálogos)
  sería root: cualquier fallo sería catastrófico y polkit no podría identificar
  al usuario que autoriza la acción.
- Usar `pkexec` por cada operación provocaría una contraseña por pendrive,
  incompatible con el modo *"Nunca"* y con la detección automática.

La separación permite además que el estado (cola, modos de confirmación,
dispositivo en curso) viva en un único proceso aunque la GUI se cierre.

### Ciclo de vida al usar la aplicación instalada

1. Al instalar **no queda ningún daemon residente**. El servicio existe, pero
   no está habilitado en el arranque.
2. Abres **RotativaUSB** desde el menú de aplicaciones (o `rotativausb`).
   La GUI se conecta al daemon por D-Bus; si no responde, lo indica en el
   registro y desactiva el botón de arranque.
3. Configuras GPT/MBR, sistema de archivos, modo de confirmación, modo de
   clonado y carpeta, y pulsas **Activar replicación**.
4. Esa llamada hace que **dbus-daemon arranque el demonio bajo demanda** (a
   través de la unidad systemd). El daemon comprueba con `pkcheck`
   la autorización polkit **del proceso GUI**; aparece la contraseña una sola
   vez y, si se aprueba, el daemon entra en estado activo.
5. El hilo monitor del daemon detecta el evento udev `add` de un USB, aplica
   los filtros (disco USB, no el disco del sistema) y lo encola. Emite
   `DeviceDetected` y la GUI lo añade a la lista.
6. El hilo worker aplica el modo de confirmación. Si toca preguntar, emite
   `ConfirmationRequired`; la GUI muestra el diálogo y responde con `Respond`.
   Luego ejecuta: desmontar → `wipefs` → tabla GPT/MBR → partición → `mkfs`
   (o UDisks2 para exFAT/NTFS) → opcionalmente `rsync` → `sync` → expulsar.
7. Los eventos `Progress` mueven la barra de la GUI y `DeviceFinished` escriben
   el resultado. Todo queda registrado como `AUDIT` en journald.
8. **Finalizar replicación** hace una parada graceful: termina el dispositivo
   en curso, descarta la cola y deja de aceptar otros nuevos.

### Órdenes útiles

```bash
systemctl status rotativausb      # estado del daemon
sudo systemctl restart rotativausb
sudo systemctl stop rotativausb   # parada total del servicio
journalctl -t rotativausb -f      # registro y auditoría en directo
```

## Arranque bajo demanda y apagado automático

No hay un servicio funcionando continuamente. El fichero
`/usr/share/dbus-1/system-services/es.rotativausb.Daemon.service` declara:

```ini
[D-BUS Service]
Name=es.rotativausb.Daemon
Exec=/usr/sbin/rotativausb-daemon
User=root
SystemdService=rotativausb.service
```

Cuando la GUI llama a `Activate()`, dbus-daemon inicia la unidad systemd (sin
habilitarla en el boot). El daemon se apaga solo cuando:

- se pulsa **Finalizar replicación** y ya no hay dispositivo en curso (3 s), o
- se termina el pendrive que estaba en marcha durante la finalización, o
- **se cierra la GUI**: el daemon vigila `NameOwnerChanged` del bus y, si el
  cliente que activó la replicación desaparece, finaliza y sale, o
- lleva 10 minutos inactivo (por ejemplo, si se activó pero nunca se conectó
  ningún USB).

La próxima vez que se pulse *Activar replicación*, D-Bus lo volverá a arrancar.

> Si conectas varios pendrives seguidos, se encolan y se procesan **de uno en
> uno**. El daemon no se apaga entre ellos: la guarda de salida comprueba que
> no haya trabajo en curso, en cola ni confirmaciones pendientes. Ten en cuenta
> que, si cierras la GUI, la replicación se da por finalizada y **se descartan
> los pendrives que aún estuvieran en cola**; solo se termina el que se esté
> formateando/copiando en ese momento.

```bash
systemctl status rotativausb      # inactive cuando no está en uso
sudo systemctl start rotativausb  # arranque manual para depuración
journalctl -t rotativausb -f      # incluye los arranques y apagados
```

> Si necesitas que el equipo **reaccione a pendrives sin tener la GUI abierta**,
> entonces sí debes mantener el daemon arrancado (`sudo systemctl enable --now
> rotativausb`). Es el único caso en el que tiene sentido el servicio
> residente: alguien debe escuchar udev en cuanto se conecta un USB.

## Ejecutar desde el código fuente (sin empaquetar)

Útil para desarrollar o probar antes de generar el `.deb`. Necesitas dos
terminales: el daemon como root y la GUI como usuario normal.

### 1. Dependencias de sistema

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 python3-dbus python3-pyudev \
                 dosfstools exfatprogs ntfs-3g e2fsprogs parted gdisk \
                 rsync util-linux udisks2 polkitd
```

### 2. Instalar la política de polkit (solo una vez)

Aunque no empaquetes, hay que registrar dos cosas: la acción polkit (para que
`pkcheck` la conozca) y la política D-Bus (para que root pueda **poseer** el
nombre `es.rotativausb.Daemon` en el bus del sistema; sin esto da
`AccessDenied` al arrancar el daemon):

```bash
cd "/home/administrador/Documentos/Proyecto predeterminado/rotativausb"
sudo cp share/es.rotativausb.policy /usr/share/polkit-1/actions/
sudo chmod 644 /usr/share/polkit-1/actions/es.rotativausb.policy
sudo cp share/es.rotativausb.conf /etc/dbus-1/system.d/
sudo chmod 644 /etc/dbus-1/system.d/es.rotativausb.conf
sudo systemctl reload dbus
```

Si antes instalaste el `.deb`, para primero el servicio para que no haya dos
daemons compitiendo por el mismo nombre D-Bus:

```bash
sudo systemctl stop rotativausb
```

### 3. Permisos de ejecución

```bash
chmod +x src/bin/rotativausb-daemon src/bin/rotativausb
```

**Forma recomendada:** usa los scripts de entrada; ellos añaden `src/` al
`PYTHONPATH` automáticamente, así que no dependes de variables de entorno:

```bash
./src/bin/rotativausb              # GUI (usuario normal)
sudo ./src/bin/rotativausb-daemon  # daemon (root)
```

Si prefieres lanzar los módulos con `python3 -m`, **necesitas** exportar
`PYTHONPATH` apuntando al directorio actual:

```bash
export PYTHONPATH="$PWD/src:$PYTHONPATH"
python3 -m rotativausb.gui
sudo -E PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 python3 -m rotativausb.daemon
```

> Si ejecutas el daemon como root con `python3 -m`, usa
> `PYTHONDONTWRITEBYTECODE=1`: de lo contrario se crean `__pycache__`
> propiedad de root y `dh_clean` no puede borrarlos al construir el `.deb`
> con un usuario normal. Los scripts `src/bin/` ya lo evitan.

> Si vienes de la versión anterior, revisa tu `PYTHONPATH`: puede seguir
> apuntando a `.../usb-replicator/src`, que ya no existe, y entonces
> `python3 -m rotativausb.gui` dará `ModuleNotFoundError`. Corrígelo con el
> `export` de arriba o, mejor, usa los scripts de entrada. Tampoco existe ya
> `python3 -m usbduplicator.gui`.

### 4. Terminal 1 – daemon (root)

```bash
cd "/home/administrador/Documentos/Proyecto predeterminado/rotativausb"
sudo ./src/bin/rotativausb-daemon
```

Déjalo abierto. Los eventos y las líneas `AUDIT` se envían a journald con
`systemd-cat` (si no está disponible, aparecen en pantalla). Para verlos:

```bash
journalctl -t rotativausb -f
```

`Ctrl+C` en la terminal 1 detiene el daemon limpiamente.

### 5. Terminal 2 – GUI (usuario normal)

```bash
cd "/home/administrador/Documentos/Proyecto predeterminado/rotativausb"
./src/bin/rotativausb
```

Solo puede haber **una instancia de la GUI**: si lanzas de nuevo el programa,
Gtk.Application reenvía el evento a la instancia ya abierta, que se limita a
traer su ventana al frente (no se crea una segunda ventana). Al cerrar la
ventana el proceso termina.

Pulsa **Comprobar UDisks2…** si vas a usar exFAT/NTFS, configura las opciones
y pulsa **Activar replicación**: polkit pedirá la contraseña una sola vez.
A partir de ahí, conecta un pendrive y observa el progreso en la GUI y el
registro en la terminal 1.

### Resolución de problemas en modo fuente

- *"No se pudo conectar con el demonio"* → el daemon no está arrancado o no ha
  conseguido el nombre en el bus del sistema. Comprueba:
  ```bash
  dbus-send --system --dest=org.freedesktop.DBus --print-reply \
    /org/freedesktop/DBus org.freedesktop.DBus.ListNames | grep rotativausb
  ```
- `org.freedesktop.DBus.Error.AccessDenied: Connection ... is not allowed to
  own the service "es.rotativausb.Daemon"` → falta copiar
  `share/es.rotativausb.conf` a `/etc/dbus-1/system.d/` y recargar dbus
  (paso 2). En Ubuntu 24.04, **ni siquiera root** puede registrar un nombre
  arbitrario en el bus del sistema sin esta política.
- *"Autorización denegada por polkit"* → falta el paso 2 o se ejecutó la GUI
  con `sudo` (debe ejecutarse como usuario normal).
- *"Conecto un pendrive y no hace nada"* → con el daemon activo, mira
  `journalctl -t rotativausb -f` al insertarlo. Debe aparecer una línea
  `udev add: /dev/sdX DEVTYPE=disk ID_BUS=usb ...` seguida de `Detectado: ...`.
  Si aparece la primera pero no la segunda, revisa los filtros de
  `_watch_loop()` y `is_system_disk()` (el disco del sistema se resuelve con
  `findmnt` + `lsblk /dev/<nombre>`).
- Errores de permiso con `mkfs.exfat`/`mkntfs` → marca la casilla de UDisks2.

## Pruebas sin hardware (loopback)

El daemon solo reacciona a dispositivos con `ID_BUS=usb`, así que un
`/dev/loopX` **no** disparará la replicación. Para probar `diskops` directamente:

```bash
cd "/home/administrador/Documentos/Proyecto predeterminado/rotativausb"
truncate -s 1G /tmp/disco.img
LOOP=$(sudo losetup -f --show /tmp/disco.img)

sudo PYTHONPATH=src python3 - "$LOOP" <<'PY'
import sys
from rotativausb import diskops

dev = sys.argv[1]
info = {"node": dev, "name": dev.split("/")[-1], "id": "prueba",
        "model": "Loopback", "size": "1 GiB", "sectors": 2097152}
cfg = {"table": "gpt", "filesystem": "ext4", "clone_mode": "wipe",
       "source": "", "label": "EXAMEN", "power_off": False,
       "verify": True, "use_udisks2": False}
print(diskops.prepare_device(info, cfg, print,
      lambda pct, stage, msg: print("%3d%% %s: %s" % (pct, stage, msg))))
PY

sudo losetup -d "$LOOP"
```

Para probar el modo *preparar contenido*, cambia `"clone_mode": "prepare"` y
pone en `"source"` la ruta absoluta de una carpeta con archivos de prueba.

## Recomendaciones antes de usar en un aula real

1. Pulsa **"Comprobar UDisks2…"** en el equipo real y, si exFAT/NTFS figuran
   como soportados, decide si prefieres dejar marcada la casilla de UDisks2.
2. Haz una pasada con un pendrive de prueba de cada capacidad que vayas a
   usar, y verifica el resultado con `journalctl -t rotativausb`.
3. Confirma que `udisksctl power-off` funciona en tus equipos (algunos
   dispositivos no lo admiten y habrá que retirarlos manualmente).
