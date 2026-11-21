"""Demonio root: monitoriza udev, decide confirmaciones y ejecuta el formateo.

Se comunica con la GUI por D-Bus (bus del sistema). La GUI NO necesita
privilegios: aquí es donde se pide autorización a polkit una sola vez,
al pulsar "Activar replicación".
"""

import json
import queue
import subprocess
import threading
import time
import uuid

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
import pyudev

from . import common, diskops

IDLE_TIMEOUT = 600  # segundos sin actividad antes de apagarse


class Daemon(dbus.service.Object):
    def __init__(self, bus):
        bus_name = dbus.service.BusName(common.BUS_NAME, bus=bus)
        super().__init__(bus_name, common.OBJECT_PATH)
        self.bus = bus
        # IMPORTANTE: hay que usar dbus.Interface con la interfaz explícita.
        # Si se llama al método sobre el proxy sin interfaz, dbus-daemon
        # rechaza la llamada (interface="(unset)") y no podemos obtener el PID.
        self._dbus = dbus.Interface(
            bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus"),
            "org.freedesktop.DBus",
        )

        self.active = False
        self.finishing = False  # parada graceful: queda una operación en curso
        self._busy = False
        self.config = None
        self.jobs = queue.Queue()
        self.pending = {}           # request_id -> (Event, {"approved": bool})
        self.ref_signature = None   # para el modo first_if_same
        self.asked_once = False     # para el modo first_always
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._setup_logging()

        # Activación bajo demanda: este proceso se inicia con la primera
        # llamada a Activate() y se apaga cuando ya no queda trabajo.
        self.loop = None
        self._client = None
        self._quit_scheduled = False
        self._inactive_since = time.monotonic()
        self.bus.add_signal_receiver(
            self._on_name_owner_changed,
            signal_name="NameOwnerChanged",
            dbus_interface="org.freedesktop.DBus",
            bus_name="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
        )

        self.ctx = pyudev.Context()
        self.monitor = pyudev.Monitor.from_netlink(self.ctx)
        self.monitor.filter_by(subsystem="block")

        threading.Thread(target=self._watch_loop, daemon=True).start()
        threading.Thread(target=self._worker_loop, daemon=True).start()
        self._emit_state()

    # ------------------------------------------------------------------ D-Bus

    @dbus.service.method(common.IFACE, in_signature="s", out_signature="b",
                         sender_keyword="sender")
    def Activate(self, config_json, sender):
        if not self._authorized(sender):
            raise dbus.exceptions.DBusException(
                "Autorización denegada por polkit",
                name="es.rotativausb.Denied",
            )
        cfg = json.loads(config_json)
        if cfg.get("clone_mode") == "prepare" and not cfg.get("source"):
            raise dbus.exceptions.DBusException(
                "Falta la carpeta de origen", name="es.rotativausb.BadConfig"
            )
        with self._lock:
            self.config = cfg
            self.ref_signature = None
            self.asked_once = False
            self.finishing = False
            self.active = True
            self._client = str(sender)
        self._touch()
        self._log("Replicación activada: %s" % json.dumps(cfg, ensure_ascii=False))
        self._emit_state()
        return True

    @dbus.service.method(common.IFACE, in_signature="")
    def Deactivate(self):
        """Finaliza la replicación SIN abortar la operación en marcha.

        Se descartan los dispositivos que aún estaban en cola y se cancela
        cualquier diálogo de confirmación pendiente. El pendrive que se está
        formateando/copiando se termina por completo: interrumpir un mkfs o un
        rsync a mitad dejaría el dispositivo en un estado inconsistente.
        """
        with self._lock:
            was_active = self.active
            self.active = False
            self.finishing = was_active and self._busy

        # 1) descartar trabajos pendientes
        dropped = 0
        while True:
            try:
                self.jobs.get_nowait()
                self.jobs.task_done()
                dropped += 1
            except queue.Empty:
                break

        # 2) despertar al worker si estaba esperando una confirmación
        with self._lock:
            pending = list(self.pending.items())
            self.pending.clear()
        for _rid, (event, box) in pending:
            box["approved"] = False
            box["aborted"] = True
            event.set()

        self._touch()
        if self.finishing:
            self._log("Finalizando… se terminará el dispositivo actual antes de parar")
        else:
            self.finishing = False
            self._log("Replicación finalizada")
            self._schedule_quit(3)   # el daemon se apaga; D-Bus lo revive si hace falta
        if dropped:
            self._log("Se descartaron %d dispositivos en cola" % dropped)
        self._emit_state()

    @dbus.service.method(common.IFACE, in_signature="sb")
    def Respond(self, request_id, approved):
        with self._lock:
            item = self.pending.get(str(request_id))
        if item:
            item[1]["approved"] = bool(approved)
            item[0].set()

    @dbus.service.method(common.IFACE, out_signature="s")
    def GetStatus(self):
        return json.dumps(self._state())

    @dbus.service.method(common.IFACE, out_signature="s")
    def CheckUdisks2(self):
        report = diskops.udisks2_report()
        line = "Comprobación UDisks2: " + json.dumps(report, ensure_ascii=False)
        self._log(line)
        return json.dumps(report, ensure_ascii=False)

    @dbus.service.signal(common.IFACE, signature="s")
    def ConfirmationRequired(self, request_json):
        pass

    @dbus.service.signal(common.IFACE, signature="s")
    def DeviceDetected(self, info_json):
        pass

    @dbus.service.signal(common.IFACE, signature="ssis")
    def Progress(self, device_id, stage, percent, message):
        pass

    @dbus.service.signal(common.IFACE, signature="sbs")
    def DeviceFinished(self, info_json, success, message):
        pass

    @dbus.service.signal(common.IFACE, signature="s")
    def StateChanged(self, state_json):
        pass

    # -------------------------------------------------------------- utilidades

    def _emit(self, signal, *args):
        # Las señales se emiten desde hilos; se marshal al main loop de GLib.
        GLib.idle_add(lambda: signal(*args))

    def _setup_logging(self):
        """Envía los registros a journald con systemd-cat (etiqueta USB)."""
        self._journal = None
        try:
            self._journal = subprocess.Popen(
                ["systemd-cat", "-t", "rotativausb"],
                stdin=subprocess.PIPE, text=True, bufsize=1,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass  # fuera de systemd: se usa stdout

    def _log(self, message):
        line = "[rotativausb] " + message
        if self._journal is not None and self._journal.stdin is not None:
            try:
                self._journal.stdin.write(line + "\n")
                self._journal.stdin.flush()
                return
            except (BrokenPipeError, OSError, ValueError):
                self._journal = None
        print(line, flush=True)  # fallback: journald captura stdout del service

    def _audit(self, action, info, result, detail=""):
        """Línea estructurada para auditoría: journalctl -t rotativausb."""
        fields = [
            "action=%s" % action,
            "device=%s" % info.get("node", ""),
            "model=%s" % info.get("model", ""),
            "serial=%s" % info.get("id", ""),
            "capacity=%s" % info.get("size", ""),
            "result=%s" % result,
            "detail=%s" % str(detail).replace("\n", " ")[:300],
        ]
        self._log("AUDIT " + " ".join(fields))

    # ------------------------------------------------- ciclo de vida (apagado)

    def _touch(self):
        """Registra actividad y cancela un apagado ya programado."""
        with self._lock:
            self._inactive_since = time.monotonic()
            self._quit_scheduled = False

    def _schedule_quit(self, delay=3):
        with self._lock:
            if self._quit_scheduled:
                return
            self._quit_scheduled = True
        GLib.timeout_add_seconds(delay, self._do_quit)

    def _do_quit(self):
        with self._lock:
            # Doble comprobación en el momento de salir: aunque se programara
            # un apagado, nunca se apaga con trabajo en curso o en cola.
            if (not self._quit_scheduled or self.active or self._busy or
                    self.pending or not self.jobs.empty()):
                self._quit_scheduled = False
                return False
        self.active = False
        self.StateChanged(json.dumps(self._state(), ensure_ascii=False))
        self._log("Sin trabajo pendiente: apagando (D-Bus lo reiniciará bajo demanda)")
        if self.loop is not None:
            self.loop.quit()
        return False

    def _idle_check(self):
        """Se ejecuta cada minuto mientras no haya trabajo."""
        with self._lock:
            idle = (not self.active and self.jobs.empty() and
                    not self._busy and not self.pending)
            elapsed = time.monotonic() - self._inactive_since
        if idle and elapsed >= IDLE_TIMEOUT:
            self._schedule_quit(1)
            return False
        return True

    def _on_name_owner_changed(self, name, old_owner, new_owner):
        """Si la GUI que activó la replicación se cierra, paramos y salimos."""
        if self._client and str(name) == self._client and not str(new_owner):
            self._log("La GUI se desconectó; finalizando la replicación")
            self._client = None
            self.Deactivate()

    def _state(self):
        return {
            "active": self.active,
            "finishing": self.finishing,
            "busy": self._busy,
            "pending_jobs": self.jobs.qsize(),
            "config": self.config,
        }

    def _emit_state(self):
        self._emit(self.StateChanged, json.dumps(self._state(), ensure_ascii=False))

    def _authorized(self, sender):
        """pkcheck delega en polkit la autorización del proceso GUI."""
        try:
            pid = int(self._dbus.GetConnectionUnixProcessID(sender))
        except Exception as exc:
            self._log("polkit: no se pudo obtener el PID de %s: %s" % (sender, exc))
            return False
        try:
            proc = subprocess.run(
                ["pkcheck", "--action-id", common.POLKIT_ACTION,
                 "--process", str(pid), "--allow-user-interaction"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            if proc.returncode:
                self._log("polkit: denegado para pid=%d (%s)" %
                          (pid, (proc.stdout or "").strip()))
            return proc.returncode == 0
        except FileNotFoundError:
            self._log("AVISO: pkcheck no encontrado; se permite por defecto")
            return True  # SOLO para desarrollo; quitar en producción

    # --------------------------------------------------------------- detección

    def _watch_loop(self):
        while not self._stop.is_set():
            dev = self.monitor.poll(1)
            if dev is None or dev.action != "add":
                continue
            if self.active:
                self._log("udev add: %s DEVTYPE=%s ID_BUS=%s USB_DRIVER=%s" % (
                    dev.device_node, dev.get("DEVTYPE"), dev.get("ID_BUS"),
                    dev.get("ID_USB_DRIVER")))
            if dev.get("DEVTYPE") != "disk":
                continue
            if not (dev.get("ID_BUS") == "usb" or dev.get("ID_USB_DRIVER")):
                continue
            # Los atributos pueden no estar completos justo en el evento 'add'.
            time.sleep(0.4)
            try:
                dev = self.ctx.device_from_sys_path(dev.sys_path)
            except Exception:
                pass
            if not self.active or not self.config:
                continue
            info = diskops.device_info(dev)
            if not info["node"] or diskops.is_system_disk(info["node"]):
                continue
            self._emit(self.DeviceDetected, json.dumps(info, ensure_ascii=False))
            self._log("Detectado: %s %s (%s)" % (info["model"], info["size"], info["node"]))
            self._audit("detect", info, "ok")
            self.jobs.put(info)
            self._touch()

    # ----------------------------------------------------------- confirmación

    def _needs_confirmation(self, info):
        mode = self.config["confirm_mode"]
        if mode == "never":
            return False
        if mode == "each":
            return True
        if mode == "first_always":
            return not self.asked_once
        if mode == "first_if_same":
            return self.ref_signature is None or \
                diskops.signature(info) != self.ref_signature
        return True

    def _mark_confirmed(self, info):
        mode = self.config["confirm_mode"]
        with self._lock:
            if mode == "first_always":
                self.asked_once = True
            elif mode == "first_if_same":
                # Si aparece otra capacidad y se aprueba, pasa a ser la nueva
                # referencia para los siguientes.
                self.ref_signature = diskops.signature(info)

    def _ask(self, info):
        """Devuelve (aprobado, abortado_por_finalización)."""
        rid = uuid.uuid4().hex
        event = threading.Event()
        box = {"approved": False, "aborted": False}
        with self._lock:
            self.pending[rid] = (event, box)
        self._emit(self.ConfirmationRequired,
                   json.dumps({"request_id": rid, "device": info}, ensure_ascii=False))
        answered = event.wait(180)   # timeout: si la GUI muere, no se cuelga
        with self._lock:
            self.pending.pop(rid, None)
        return (answered and box["approved"]), box["aborted"]

    # ------------------------------------------------------------------ trabajo

    def _worker_loop(self):
        while not self._stop.is_set():
            info = self.jobs.get()
            with self._lock:
                if not self.active:
                    self.jobs.task_done()
                    continue
                self._busy = True
            self._touch()
            try:
                approved, aborted = True, False
                if self._needs_confirmation(info):
                    approved, aborted = self._ask(info)
                if not approved:
                    reason = ("Omitido: replicación finalizada" if aborted else
                              "Omitido: confirmación cancelada o agotado el tiempo")
                    self._audit("prepare", info, "skipped", reason)
                    self._emit(self.DeviceFinished,
                               json.dumps(info, ensure_ascii=False), False, reason)
                    continue
                self._mark_confirmed(info)

                def progress(pct, stage, msg):
                    self._emit(self.Progress, info["id"], stage, int(pct), msg)

                def log(msg):
                    self._log("%s: %s" % (info["name"], msg))

                message = diskops.prepare_device(info, self.config, log, progress)
                self._audit("prepare", info, "ok", message)
                self._emit(self.DeviceFinished,
                           json.dumps(info, ensure_ascii=False), True, message)
            except Exception as exc:
                self._log("ERROR: %s" % exc)
                self._audit("prepare", info, "error", str(exc))
                self._emit(self.DeviceFinished,
                           json.dumps(info, ensure_ascii=False), False, str(exc))
            finally:
                with self._lock:
                    self._busy = False
                    if not self.active:
                        self.finishing = False
                    no_work = not self.active and self.jobs.empty()
                self.jobs.task_done()
                self._emit_state()
                if no_work:
                    self._schedule_quit(3)


def main():
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    daemon = Daemon(bus)
    loop = GLib.MainLoop()
    daemon.loop = loop
    GLib.timeout_add_seconds(60, daemon._idle_check)
    daemon._log("Demonio iniciado bajo demanda (esperando Activate)")
    try:
        loop.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
