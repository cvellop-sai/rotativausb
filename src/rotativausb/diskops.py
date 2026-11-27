"""Operaciones de disco. Se ejecutan SIEMPRE desde el demonio (root).

"borrado completo" = formateo rápido: se destruyen firmas y tabla de
particiones y se crea una nueva, sin sobrescribir los sectores con ceros.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import dbus

from . import common


class DiskOpsError(RuntimeError):
    pass


def run(cmd, log=None, check=True):
    """Ejecuta un comando sin shell. Devuelve stdout (stdout+stderr)."""
    if log:
        log("$ " + " ".join(cmd))
    proc = subprocess.run(
        cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    out = proc.stdout or ""
    if log and out.strip():
        log(out.strip())
    if check and proc.returncode:
        raise DiskOpsError("'%s' falló (%d):\n%s" % (" ".join(cmd), proc.returncode, out))
    return out


def run_streaming(cmd, progress_cb=None, log=None):
    """Como run() pero informa de la última '%' de cada línea (rsync)."""
    if log:
        log("$ " + " ".join(cmd))
    proc = subprocess.Popen(
        cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1
    )
    buf = ""
    assert proc.stdout is not None
    while True:
        ch = proc.stdout.read(1)
        if not ch:
            break
        buf += ch
        if ch in "\r\n":
            line = buf.strip()
            buf = ""
            if not line:
                continue
            if log:
                log(line)
            if progress_cb:
                m = re.search(r"(\d{1,3})%", line)
                if m:
                    progress_cb(int(m.group(1)), line)
    proc.wait()
    if proc.returncode:
        raise DiskOpsError("'%s' falló (%d)" % (" ".join(cmd), proc.returncode))


def _human(size_bytes):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size_bytes < 1024:
            return "%.0f %s" % (size_bytes, unit)
        size_bytes /= 1024
    return "%.0f PiB" % size_bytes


def _root_disk_name():
    """Nombre del disco del que arranca el sistema (ej. 'sda' o 'nvme0n1')."""
    try:
        src = run(["findmnt", "-no", "SOURCE", "/"]).strip()
        name = os.path.basename(src)
        if not name or not os.path.exists("/dev/" + name):
            return None
        # lsblk necesita la ruta completa; con el nombre simple falla y
        # devuelve un mensaje de error que antes se tomaba como nombre.
        proc = subprocess.run(
            ["lsblk", "-no", "PKNAME", "/dev/" + name],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        parent = proc.stdout.strip() if proc.returncode == 0 else ""
        return parent or name
    except Exception:
        return None


def is_system_disk(devnode):
    """True si el dispositivo es el disco del sistema. Nunca se debe tocar."""
    root = _root_disk_name()
    if not root:
        return True  # conservador: si no lo sabemos, lo rechazamos
    try:
        return os.stat("/dev/%s" % root).st_rdev == os.stat(devnode).st_rdev
    except OSError:
        return True


def stable_node(devnode):
    """Resuelve /dev/disk/by-id/usb-... equivalente al nodo dado."""
    if not devnode:
        return devnode
    try:
        target = os.path.realpath(devnode)
        base = "/dev/disk/by-id"
        for entry in sorted(os.listdir(base)):
            if entry.startswith("usb-") and \
                    os.path.realpath(os.path.join(base, entry)) == target:
                return os.path.join(base, entry)
    except OSError:
        pass
    return devnode


def device_info(dev):
    """Convierte un pyudev.Device en un diccionario serializable por D-Bus."""
    name = os.path.basename(dev.device_node or "")
    size = ""
    sectors = 0
    try:
        sectors = int(open("/sys/block/%s/size" % name).read().strip())
        size = _human(sectors * 512)
    except Exception:
        pass
    return {
        "node": stable_node(dev.device_node or ""),
        "name": name,
        "id": dev.get("ID_SERIAL") or name,
        "model": dev.get("ID_MODEL", "Desconocido"),
        "vendor": dev.get("ID_VENDOR", ""),
        "vid_pid": "%s:%s" % (dev.get("ID_VENDOR_ID", ""), dev.get("ID_MODEL_ID", "")),
        "size": size,
        "sectors": sectors,   # capacidad exacta, usada por signature()
    }


def signature(info):
    """Dos USB se consideran IGUALES si tienen la MISMA CAPACIDAD.

    Se comparan los sectores exactos de /sys/block/<dev>/size (512 B por
    sector), no el modelo, el VID:PID ni el número de serie.
    """
    return (info.get("sectors", 0),)


def unmount_all(devnode, log=None):
    """Desmonta cualquier partición previa (GNOME auto-monta los USB).

    Si algún desmontaje falla se lanza un error: expulsar un dispositivo con
    un sistema de archivos montado dejaría ext4 en estado sucio y el próximo
    equipo lo montaría en modo solo lectura.
    """
    out = run(["lsblk", "-n", "-o", "MOUNTPOINTS", devnode], log=log, check=False)
    failed = []
    for line in out.splitlines():
        mp = line.strip()
        if not mp:
            continue
        if log:
            log("$ umount " + mp)
        proc = subprocess.run(
            ["umount", mp], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        if proc.returncode:
            failed.append("%s: %s" % (mp, (proc.stdout or "").strip()))
    if failed:
        raise DiskOpsError("No se pudieron desmontar: " + "; ".join(failed))


def _clean_label(label):
    label = re.sub(r"[^A-Za-z0-9_]", "", (label or "EXAMEN").upper())
    return (label or "EXAMEN")[:11]  # el denominador común más restrictivo


def _find_partition(devnode, timeout=15):
    """Espera a que aparezca la partición recién creada."""
    name = os.path.basename(os.path.realpath(devnode))
    candidates = ["/dev/%s1" % name, "/dev/%sp1" % name]
    deadline = time.time() + timeout
    while time.time() < deadline:
        for cand in candidates:
            if os.path.exists(cand):
                return cand
        time.sleep(0.2)
    out = run(["lsblk", "-n", "-o", "NAME", devnode], check=False)
    children = [l.strip() for l in out.splitlines() if l.strip()]
    if len(children) > 1:
        return "/dev/" + children[1]
    raise DiskOpsError("No apareció la partición de %s" % devnode)


MKFS = {
    # ^metadata_csum y ^64bit son características RO_COMPAT: un kernel más
    # antiguo que no las entiende monta el volumen en modo solo lectura.
    # En pendrives (<16 TiB) desactivarlas no tiene coste práctico.
    "ext4": lambda p, l: ["mkfs.ext4", "-F", "-q", "-L", l,
                          "-O", "^metadata_csum,^64bit", p],
    "ext3": lambda p, l: ["mkfs.ext3", "-F", "-q", "-L", l,
                          "-O", "^metadata_csum,^64bit", p],
    "exfat": lambda p, l: ["mkfs.exfat", "-n", l, p],
    "fat32": lambda p, l: ["mkfs.vfat", "-F", "32", "-n", l, p],
    "ntfs": lambda p, l: ["mkfs.ntfs", "-f", "-Q", "-L", l, p],
}

UDISKS_FS = {
    "ext4": "ext4", "ext3": "ext3", "exfat": "exfat",
    "fat32": "vfat", "ntfs": "ntfs",
}


def udisks2_report():
    """Comprueba si UDisks2 puede formatear exFAT/NTFS en este equipo."""
    report = {
        "udisksctl": shutil.which("udisksctl"),
        "mkfs_exfat": shutil.which("mkfs.exfat"),
        "mkntfs": shutil.which("mkntfs"),
        "supported": [],
        "can_format": {},
        "available": False,
        "error": "",
    }
    try:
        bus = dbus.SystemBus()
        obj = bus.get_object("org.freedesktop.UDisks2",
                             "/org/freedesktop/UDisks2/Manager",
                             introspect=False)
        manager = dbus.Interface(obj, "org.freedesktop.UDisks2.Manager")
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        # SupportedFilesystems es una PROPIEDAD, no un método.
        report["supported"] = [
            str(x) for x in props.Get("org.freedesktop.UDisks2.Manager",
                                      "SupportedFilesystems", timeout=10000)
        ]
        for fs in ("exfat", "ntfs"):
            try:
                available, _reason = manager.CanFormat(fs, timeout=10000)
                report["can_format"][fs] = bool(available)
            except Exception:
                pass
        report["available"] = True
    except Exception as exc:
        report["error"] = str(exc)
    if not report["udisksctl"] and not report["error"]:
        report["error"] = "falta udisksctl (paquete udisks2)"
    return report


def format_with_udisks2(part, fs, label, log=None):
    """Formatea una partición usando UDisks2 en vez de mkfs.* directo.

    Útil si el servicio systemd con ProtectSystem=full impide ejecutar los
    binarios mkfs.exfat / mkntfs. UDisks2 hace el formateo desde su propio
    proceso privilegiado mediante polkit.
    """
    log = log or (lambda msg: None)
    name = os.path.basename(os.path.realpath(part))
    objpath = "/org/freedesktop/UDisks2/block_devices/" + name
    bus = dbus.SystemBus()
    fs_type = UDISKS_FS[fs]
    last_error = None

    # Tras parted, UDisks2 puede tardar un instante en ver la partición.
    for _ in range(15):
        try:
            obj = bus.get_object("org.freedesktop.UDisks2", objpath,
                                 introspect=False)
            iface = dbus.Interface(obj, "org.freedesktop.UDisks2.Filesystem")
            iface.CreateFilesystem(
                fs_type, {"label": dbus.String(_clean_label(label))},
                timeout=180000,
            )
            log("Formateo %s realizado por UDisks2 en %s" % (fs_type, name))
            return
        except dbus.exceptions.DBusException as exc:
            last_error = exc
            err = exc.get_name() or ""
            if "UnknownObject" in err or "ServiceUnknown" in err:
                time.sleep(0.3)
                continue
            raise
    raise DiskOpsError("UDisks2 no vio la partición %s: %s" % (name, last_error))


def format_partition(part, fs, label, config, log=None):
    """Elige el backend de formateo con fallback automático a mkfs directo."""
    log = log or (lambda msg: None)
    use_udisks2 = config.get("use_udisks2", False) and fs in ("exfat", "ntfs")
    if use_udisks2:
        try:
            format_with_udisks2(part, fs, label, log)
            return
        except Exception as exc:
            log("AVISO: UDisks2 falló (%s); se usa mkfs.%s directo" % (exc, fs))
    run(MKFS[fs](part, label), log=log)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_snapshot(root):
    """Mapa ruta_relativa -> (tipo, contenido/hash) de un directorio."""
    snapshot = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + list(filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            # mke2fs crea lost+found en ext3/ext4; no es contenido del usuario.
            if rel == "lost+found" or rel.startswith("lost+found/"):
                continue
            if os.path.islink(path):
                snapshot[rel] = ("link", os.readlink(path))
            elif os.path.isdir(path):
                snapshot[rel] = ("dir", "")
            elif os.path.isfile(path):
                snapshot[rel] = ("file", _sha256(path))
            else:
                snapshot[rel] = ("other", "")
    return snapshot


def verify_content(part, mountpoint, source=None, fs=None, label=None):
    """Comprueba que el pendrive contiene exactamente lo esperado."""
    report = {
        "ok": True, "filesystem": "", "label": "",
        "expected": 0, "actual": 0,
        "missing": [], "extra": [], "different": [], "warnings": [],
    }

    if fs:
        expected_fs = "vfat" if fs == "fat32" else fs
        actual_fs = run(["lsblk", "-no", "FSTYPE", part],
                        log=None, check=False).strip()
        report["filesystem"] = actual_fs
        if actual_fs != expected_fs:
            report["ok"] = False
            report["warnings"].append(
                "sistema de archivos: esperado %s, encontrado '%s'"
                % (expected_fs, actual_fs))

    if label:
        actual_label = run(["lsblk", "-no", "LABEL", part],
                           log=None, check=False).strip()
        report["label"] = actual_label
        if actual_label and actual_label.upper() != label.upper():
            report["warnings"].append(
                "etiqueta: esperada %s, encontrada '%s'" % (label, actual_label))

    expected = _tree_snapshot(source) if source else {}
    actual = _tree_snapshot(mountpoint)
    report["expected"] = len(expected)
    report["actual"] = len(actual)
    report["missing"] = sorted(set(expected) - set(actual))
    report["extra"] = sorted(set(actual) - set(expected))
    report["different"] = sorted(
        key for key in set(expected) & set(actual)
        if expected[key] != actual[key]
    )
    if report["missing"] or report["extra"] or report["different"]:
        report["ok"] = False
    return report


def prepare_device(info, config, log=None, progress=None):
    """Formatea el pendrive y, si toca, copia el contenido de la carpeta."""
    log = log or (lambda msg: None)
    progress = progress or (lambda pct, stage, msg: None)

    node = info["node"]
    if is_system_disk(node):
        raise DiskOpsError("Rechazado: %s parece ser el disco del sistema" % node)

    table = "gpt" if config["table"] == "gpt" else "msdos"
    fs = config["filesystem"]
    if fs not in MKFS:
        raise DiskOpsError("Sistema de archivos no soportado: %s" % fs)
    label = _clean_label(config.get("label"))

    progress(2, "unmount", "Desmontando particiones previas…")
    unmount_all(node, log)

    progress(6, "wipefs", "Limpiando firmas existentes…")
    run(["wipefs", "-a", node], log=log)
    run(["sgdisk", "--zap-all", node], log=log, check=False)

    progress(12, "partition", "Creando tabla %s y partición única…" % config["table"])
    run(["parted", "-s", node, "mklabel", table], log=log)
    run(["parted", "-s", node, "mkpart", "primary", "1MiB", "100%"], log=log)
    run(["udevadm", "settle", "--timeout=10"], log=log, check=False)
    part = _find_partition(node)

    progress(45, "format", "Formateando como %s…" % fs)
    format_partition(part, fs, label, config, log)

    mountpoint = None
    source = None
    verification_note = ""
    try:
        if config["clone_mode"] == "prepare":
            source = config.get("source", "")
            if not source or not os.path.isdir(source):
                raise DiskOpsError("La carpeta de origen no existe: %s" % source)
            mountpoint = tempfile.mkdtemp(prefix="rotativausb-")
            progress(55, "mount", "Montando partición…")
            run(["mount", "-o", "rw", part, mountpoint], log=log)
            progress(60, "copy", "Copiando contenido…")
            rsync_opts = ["-a", "--info=progress2"]
            # exFAT/FAT32/NTFS no tienen propietario, grupo ni permisos Unix:
            # conservarlos hace que rsync falle con 'chown: Operation not
            # permitted' y código 23. ext3/ext4 sí los conservan.
            if fs not in ("ext3", "ext4"):
                rsync_opts += ["--no-owner", "--no-group", "--no-perms"]
            run_streaming(
                ["rsync"] + rsync_opts + [source + "/", mountpoint + "/"],
                progress_cb=lambda pct, line: progress(
                    60 + int(pct * 0.35), "copy", line
                ),
                log=log,
            )

            if fs in ("ext3", "ext4") and config.get("normalize_perms", True):
                progress(92, "perms", "Normalizando propietario y permisos…")
                # rsync -a copió el UID del origen. En otro equipo con otro
                # UID los archivos no serían escribibles ni legibles según
                # qué permisos. root:root + 755/644 evita depender de UIDs.
                run(["chown", "-R", "0:0", mountpoint], log=log)
                run(["chmod", "-R", "u+rwX,go+rX", mountpoint], log=log)

        if config.get("verify", True):
            progress(94, "verify", "Verificando contenido del pendrive…")
            check_mount = mountpoint
            if check_mount is None:
                # En modo "borrado completo" no habíamos montado: lo montamos
                # solo de lectura para comprobar que el volumen quedó vacío.
                check_mount = tempfile.mkdtemp(prefix="rotativausb-verify-")
                run(["mount", "-o", "ro", part, check_mount], log=log)
            try:
                report = verify_content(part, check_mount, source, fs, label)
            finally:
                if check_mount != mountpoint:
                    run(["umount", check_mount], log=log, check=False)
                    shutil.rmtree(check_mount, ignore_errors=True)
            if not report["ok"]:
                raise DiskOpsError(
                    "Verificación fallida: %s" %
                    json.dumps(report, ensure_ascii=False))
            for warning in report["warnings"]:
                log("AVISO de verificación: %s" % warning)
            log("Verificación correcta: %d elementos, FS=%s" % (
                report["actual"], report["filesystem"]))
            progress(96, "verify", "Verificado: %d elementos" % report["actual"])
            verification_note = "; verificado (%d elementos)" % report["actual"]
    finally:
        if mountpoint:
            run(["sync"], log=log, check=False)
            # check=True: un umount fallido dejaría ext4 sucio.
            run(["umount", mountpoint], log=log, check=True)
            shutil.rmtree(mountpoint, ignore_errors=True)

    progress(97, "sync", "Sincronizando datos y tabla de particiones…")
    run(["sync"], log=log, check=False)

    message = "Preparado %s (%s, %s) como %s%s" % (
        info["name"], info["size"] or "?", config["table"], fs,
        verification_note
    )

    if config.get("power_off", True):
        progress(99, "eject", "Expulsando el dispositivo…")
        unmount_all(node, log)   # por si el escritorio lo volvió a montar
        try:
            proc = subprocess.run(
                ["udisksctl", "power-off", "-b", node],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            if proc.returncode:
                log("AVISO: no se pudo expulsar %s (%s). Retírelo manualmente."
                    % (node, (proc.stdout or "").strip()))
                message += ". No se pudo expulsar: retírelo manualmente"
            else:
                log("Dispositivo expulsado; puede retirarlo con seguridad.")
                message += ". Dispositivo expulsado"
        except FileNotFoundError:
            log("AVISO: udisksctl no está instalado")
            message += ". No se pudo expulsar (falta udisks2)"

    progress(100, "done", "Listo.")
    return message
