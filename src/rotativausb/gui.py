"""GUI GTK4. Se ejecuta como usuario normal; habla con el demonio por D-Bus."""

import json

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gio, Gdk, GLib

from . import common


def _combo(items, active=None):
    """Gtk.DropDown con una Gtk.StringList; guardamos las claves aparte."""
    model = Gtk.StringList()
    keys = []
    for key, label in items:
        keys.append(key)
        model.append(label)
    combo = Gtk.DropDown(model=model)
    combo.keys = keys  # atributo auxiliar para recuperar el valor elegido
    if active in keys:
        combo.set_selected(keys.index(active))
    return combo


def _selected(combo):
    index = combo.get_selected()
    return combo.keys[index] if 0 <= index < len(combo.keys) else None


class ReplicatorApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="es.rotativausb.gui")
        self.proxy = None
        self.win = None
        self.confirm_dialogs = []
        self.device_rows = {}   # id del dispositivo -> Gtk.Label de la lista
        self._was_active = False

    def do_activate(self):
        # Gtk.Application ya garantiza un único proceso por application_id,
        # pero un segundo lanzamiento reenvía 'activate' a esta instancia:
        # en ese caso solo mostramos la ventana existente, no creamos otra.
        windows = self.get_windows()
        if windows:
            windows[0].present()
            return

        win = Gtk.ApplicationWindow(application=self, title="RotativaUSB – Preparador de pendrives")
        win.set_default_size(760, 620)
        self.win = win

        # Estilo para los dispositivos ya replicados.
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b".rotativa-finished { color: #9aa0a6; }")
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(14)
        root.set_margin_bottom(14)
        root.set_margin_start(14)
        root.set_margin_end(14)
        win.set_child(root)

        # --- opciones generales -------------------------------------------
        frame = Gtk.Frame(label="Opciones generales")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_bottom(8)
        frame.set_child(box)

        self.cb_table = _combo(
            [(k, v) for k, v in common.PARTITION_TABLES.items()],
            common.DEFAULT_CONFIG["table"],
        )
        self.cb_fs = _combo(
            [(f, f) for f in common.FILESYSTEMS], common.DEFAULT_CONFIG["filesystem"]
        )
        self.cb_confirm = _combo(
            [(k, v) for k, v in common.CONFIRM_MODES.items()],
            common.DEFAULT_CONFIG["confirm_mode"],
        )
        box.append(self._row("Tipo de partición:", self.cb_table))
        box.append(self._row("Sistema de archivos:", self.cb_fs))
        box.append(self._row("Modo de confirmación:", self.cb_confirm))

        self.entry_label = Gtk.Entry()
        self.entry_label.set_text(common.DEFAULT_CONFIG["label"])
        self.entry_label.set_max_length(11)   # denominador común (exFAT/FAT32)
        self.entry_label.set_tooltip_text(
            "Etiqueta del pendrive. Se limpiarán los caracteres no válidos y "
            "se convertirá a mayúsculas (máx. 11 caracteres).")
        box.append(self._row("Etiqueta del volumen:", self.entry_label))
        root.append(frame)

        # --- opciones de clonado ------------------------------------------
        frame2 = Gtk.Frame(label="Opción de clonado")
        box2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box2.set_margin_top(8)
        box2.set_margin_start(10)
        box2.set_margin_end(10)
        box2.set_margin_bottom(8)
        frame2.set_child(box2)

        self.cb_clone = _combo(
            [(k, v) for k, v in common.CLONE_MODES.items()],
            common.DEFAULT_CONFIG["clone_mode"],
        )
        self.cb_clone.connect("notify::selected", self._on_clone_changed)
        self.btn_folder = Gtk.Button(label="Seleccionar carpeta…")
        self.btn_folder.connect("clicked", self._on_pick_folder)
        self.lbl_folder = Gtk.Label(label="(ninguna)")
        self.lbl_folder.set_hexpand(True)
        self.lbl_folder.set_xalign(0)
        box2.append(self._row("Acción:", self.cb_clone))
        folder_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        folder_row.append(Gtk.Label(label="Contenido:"))
        folder_row.append(self.btn_folder)
        folder_row.append(self.lbl_folder)
        box2.append(folder_row)
        self._on_clone_changed(self.cb_clone)  # estado inicial del botón de carpeta
        root.append(frame2)

        self.cb_poweroff = Gtk.CheckButton(
            label="Expulsar el pendrive al terminar (power-off)")
        self.cb_poweroff.set_active(common.DEFAULT_CONFIG["power_off"])
        root.append(self.cb_poweroff)

        self.cb_normalize = Gtk.CheckButton(
            label="Permitir a los alumnos modificar el contenido (ext3/ext4)")
        self.cb_normalize.set_active(common.DEFAULT_CONFIG["normalize_perms"])
        self.cb_normalize.set_tooltip_text(
            "Pone los archivos a root:root con permisos 777/666 (mantiene "
            "la 'x' en ejecutables) para que cualquier alumno pueda crear, "
            "borrar y modificar archivos sin depender de los UID del origen.")
        root.append(self.cb_normalize)

        self.cb_verify = Gtk.CheckButton(
            label="Verificar el contenido antes de expulsar el pendrive")
        self.cb_verify.set_active(common.DEFAULT_CONFIG["verify"])
        self.cb_verify.set_tooltip_text(
            "Compara nombre, tipo y hash SHA-256 de cada archivo con la carpeta "
            "de origen (o comprueba que el volumen quedó vacío). Si algo no "
            "coincide, se aborta y no se expulsa el dispositivo.")
        root.append(self.cb_verify)

        self.cb_udisks2 = Gtk.CheckButton(
            label="Usar UDisks2 para formatear exFAT/NTFS (alternativa a mkfs)")
        self.cb_udisks2.set_active(common.DEFAULT_CONFIG["use_udisks2"])
        root.append(self.cb_udisks2)

        self.btn_check_udisks2 = Gtk.Button(label="Comprobar UDisks2…")
        self.btn_check_udisks2.connect("clicked", self._on_check_udisks2)
        root.append(self.btn_check_udisks2)

        # --- botones -------------------------------------------------------
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_activate = Gtk.Button(label="Activar replicación")
        self.btn_activate.add_css_class("suggested-action")
        self.btn_activate.connect("clicked", self._on_activate)
        self.btn_stop = Gtk.Button(label="Finalizar replicación")
        self.btn_stop.set_sensitive(False)
        self.btn_stop.connect("clicked", self._on_stop)
        actions.append(self.btn_activate)
        actions.append(self.btn_stop)
        root.append(actions)

        self.lbl_status = Gtk.Label(label="Estado: desactivado")
        self.lbl_status.set_xalign(0)
        root.append(self.lbl_status)

        self.bar = Gtk.ProgressBar()
        root.append(self.bar)

        # --- dispositivos y registro --------------------------------------
        root.append(Gtk.Label(label="Dispositivos detectados:"))
        self.devices = Gtk.ListBox()
        scroll1 = Gtk.ScrolledWindow()
        scroll1.set_min_content_height(110)
        scroll1.set_child(self.devices)
        root.append(scroll1)

        root.append(Gtk.Label(label="Registro:"))
        self.logview = Gtk.TextView()
        self.logview.set_editable(False)
        self.logbuffer = self.logview.get_buffer()
        scroll2 = Gtk.ScrolledWindow()
        scroll2.set_min_content_height(130)
        scroll2.set_child(self.logview)
        root.append(scroll2)

        # Al cerrar la ventana se termina el proceso, para que no quede una
        # instancia en segundo plano dueña del application_id.
        win.connect("destroy", lambda *_: self.quit())

        self._connect_dbus()
        win.present()

    # ------------------------------------------------------------------- GUI

    @staticmethod
    def _row(label, widget):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=label)
        lbl.set_size_request(190, -1)
        lbl.set_xalign(0)
        row.append(lbl)
        row.append(widget)
        return row

    def _log(self, text):
        end = self.logbuffer.get_end_iter()
        self.logbuffer.insert(end, text + "\n")
        mark = self.logbuffer.get_insert()
        self.logbuffer.place_cursor(self.logbuffer.get_end_iter())
        self.logview.scroll_to_mark(mark, 0, True, 0, 0)

    def _on_clone_changed(self, combo, _pspec=None):
        # 'notify::selected' pasa (widget, pspec); la llamada inicial del
        # do_activate solo pasa el widget, por eso _pspec es opcional.
        sensitive = _selected(combo) == "prepare"
        self.btn_folder.set_sensitive(sensitive)

    def _on_pick_folder(self, _button):
        dialog = Gtk.FileDialog(title="Seleccionar carpeta de contenido")
        dialog.select_folder(self.win, None, self._on_folder_selected, None)

    def _on_folder_selected(self, dialog, result, _data):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        self.source = folder.get_path()
        self.lbl_folder.set_label(self.source)

    @property
    def source(self):
        return getattr(self, "_source", "")

    @source.setter
    def source(self, value):
        self._source = value

    # ------------------------------------------------------------------ D-Bus

    def _connect_dbus(self):
        try:
            node = Gio.DBusNodeInfo.new_for_xml(common.DBUS_XML)
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM,
                Gio.DBusProxyFlags.NONE,
                node.interfaces[0],
                common.BUS_NAME,
                common.OBJECT_PATH,
                common.IFACE,
                None,
            )
            self.proxy.connect("g-signal::ConfirmationRequired", self._on_confirm)
            self.proxy.connect("g-signal::DeviceDetected", self._on_detected)
            self.proxy.connect("g-signal::Progress", self._on_progress)
            self.proxy.connect("g-signal::DeviceFinished", self._on_finished)
            self.proxy.connect("g-signal::StateChanged", self._on_state)
            self._log("Conectado. El demonio se iniciará al pulsar 'Activar replicación'.")
        except Exception as exc:
            self._log("No se pudo conectar con el demonio: %s" % exc)
            self.btn_activate.set_sensitive(False)

    def _call(self, method, variant, callback=None):
        try:
            self.proxy.call(method, variant, 0, -1, None, callback, None)
        except Exception as exc:
            self._log("Error al llamar a %s: %s" % (method, exc))

    def _on_call_done(self, proxy, result, _data):
        try:
            proxy.call_finish(result)
        except GLib.Error as exc:
            self._log("El demonio rechazó la operación: %s" % exc)

    def _on_activate(self, _button):
        cfg = {
            "table": _selected(self.cb_table),
            "filesystem": _selected(self.cb_fs),
            "confirm_mode": _selected(self.cb_confirm),
            "clone_mode": _selected(self.cb_clone),
            "source": self.source if _selected(self.cb_clone) == "prepare" else "",
            "label": self.entry_label.get_text().strip() or common.DEFAULT_CONFIG["label"],
            "power_off": self.cb_poweroff.get_active(),
            "normalize_perms": self.cb_normalize.get_active(),
            "verify": self.cb_verify.get_active(),
            "use_udisks2": self.cb_udisks2.get_active(),
        }
        self._call("Activate", GLib.Variant("(s)", (json.dumps(cfg),)),
                   self._on_activate_done)

    def _on_activate_done(self, proxy, result, _data):
        try:
            proxy.call_finish(result)
        except GLib.Error as exc:
            self._log("El demonio rechazó la operación: %s" % exc)
            return
        # Por si no llegara la señal StateChanged, limpiamos también aquí.
        if not self._was_active:
            self._clear_session()
            self._was_active = True

    def _on_stop(self, _button):
        self._call("Deactivate", GLib.Variant("()", ()), self._on_call_done)

    def _on_check_udisks2(self, _button):
        self._log("Comprobando UDisks2…")
        self._call("CheckUdisks2", GLib.Variant("()", ()), self._on_check_done)

    def _on_check_done(self, proxy, result, _data):
        try:
            # call_finish devuelve un GLib.Variant con una tupla de Argumentos
            # de salida (incluso cuando solo hay uno): (str,).
            raw = proxy.call_finish(result).unpack()
            if isinstance(raw, tuple):
                raw = raw[0]
            report = json.loads(raw)
        except Exception as exc:
            self._log("Error al comprobar UDisks2: %s" % exc)
            return
        self._log("udisksctl: %s | mkfs.exfat: %s | mkntfs: %s" % (
            report["udisksctl"] or "NO",
            report["mkfs_exfat"] or "NO",
            report["mkntfs"] or "NO",
        ))
        self._log("Sistemas soportados por UDisks2: %s" % (
            ", ".join(report["supported"]) or "(desconocido)"))
        self._log("CanFormat -> exfat: %s | ntfs: %s" % (
            report.get("can_format", {}).get("exfat", "?"),
            report.get("can_format", {}).get("ntfs", "?")))
        if report.get("error"):
            self._log("Aviso: %s" % report["error"])
        elif "exfat" in report["supported"] and "ntfs" in report["supported"]:
            self._log("UDisks2 puede formatear exFAT y NTFS.")
        else:
            self._log("UDisks2 no declara soporte para exFAT o NTFS.")

    # --------------------------------------------------------------- señales

    def _clear_session(self):
        """Limpia dispositivos, registro y barra antes de una nueva tanda."""
        while True:
            row = self.devices.get_row_at_index(0)
            if row is None:
                break
            self.devices.remove(row)
        self.device_rows.clear()
        self.logbuffer.set_text("")
        self.bar.set_fraction(0.0)
        self._log("Replicación iniciada — esperando dispositivos…")

    def _on_state(self, _proxy, _sender, _name, params):
        state = json.loads(params.unpack()[0])
        active = state["active"]
        finishing = state.get("finishing", False)

        # Cada nueva activación empieza con la lista de dispositivos y el
        # registro limpios (solo si la activación fue aceptada por polkit).
        if active and not self._was_active:
            self._clear_session()
        self._was_active = active

        self.btn_activate.set_sensitive(not active and not finishing)
        self.btn_stop.set_sensitive(active and not finishing)
        if finishing:
            self.lbl_status.set_label(
                "Estado: FINALIZANDO — se completará el pendrive actual antes de parar…")
            # El daemon ya canceló la espera; cerramos los diálogos abiertos.
            for dialog in self.confirm_dialogs:
                dialog.destroy()
            self.confirm_dialogs.clear()
        else:
            self.lbl_status.set_label(
                "Estado: %s" % ("ACTIVO" if active else "desactivado"))

    def _on_detected(self, _proxy, _sender, _name, params):
        info = json.loads(params.unpack()[0])
        row = Gtk.Label(label="%s – %s – %s (%s)" % (
            info["model"], info["size"], info["name"], info["node"]))
        row.set_xalign(0)
        self.devices.append(row)
        self.device_rows[info["id"]] = row
        self._log("Detectado %s" % info["name"])

    def _on_progress(self, _proxy, _sender, _name, params):
        _dev, stage, percent, message = params.unpack()
        self.bar.set_fraction(max(0, min(100, percent)) / 100.0)
        self._log("[%s %d%%] %s" % (stage, percent, message))

    def _on_finished(self, _proxy, _sender, _name, params):
        info, success, message = params.unpack()
        info = json.loads(info)
        row = self.device_rows.get(info["id"])
        if row is not None:
            row.add_css_class("rotativa-finished")
        self._log(("OK " if success else "FALLO ") + info["name"] + ": " + message)

    def _on_confirm(self, _proxy, _sender, _name, params):
        data = json.loads(params.unpack()[0])
        info = data["device"]
        rid = data["request_id"]

        # Gtk.Dialog está deprecated en GTK4: se construye una ventana modal.
        dialog = Gtk.Window(title="Confirmar preparación")
        dialog.set_transient_for(self.win)
        dialog.set_modal(True)
        dialog.set_default_size(460, 240)
        dialog._rid = rid
        dialog.connect("close-request", self._on_confirm_close, rid)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)

        label = Gtk.Label(label=(
            "Se va a BORRAR todo el contenido de:\n\n"
            "  Modelo: %s %s\n  Tamaño: %s\n  Dispositivo: %s\n\n"
            "¿Continuar?"
        ) % (info["vendor"], info["model"], info["size"], info["node"]))
        label.set_xalign(0)
        content.append(label)

        # Avisar de que esta confirmación se reutilizará para los siguientes
        # dispositivos en los modos "solo la primera vez".
        note = {
            "first_if_same": (
                "Este diálogo solo aparece la primera vez: los siguientes "
                "pendrives de la MISMA CAPACIDAD se borrarán automáticamente "
                "sin volver a preguntar. Si aparece otra capacidad, se "
                "volverá a mostrar."),
            "first_always": (
                "Este diálogo solo aparece la primera vez: todos los pendrives "
                "que se conecten después se borrarán automáticamente sin "
                "volver a preguntar."),
        }.get(_selected(self.cb_confirm))
        if note:
            note_label = Gtk.Label(label=note)
            note_label.set_xalign(0)
            note_label.set_wrap(True)
            note_label.add_css_class("dim-label")
            content.append(note_label)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancelar")
        cancel.connect("clicked", self._on_confirm_response, False, rid)
        accept = Gtk.Button(label="Preparar este pendrive")
        accept.add_css_class("suggested-action")
        accept.connect("clicked", self._on_confirm_response, True, rid)
        buttons.append(cancel)
        buttons.append(accept)
        content.append(buttons)

        dialog.set_child(content)
        self.confirm_dialogs.append(dialog)
        dialog.present()

    def _on_confirm_response(self, _button, approved, rid):
        self._call("Respond", GLib.Variant("(sb)", (str(rid), bool(approved))))
        self._close_confirm(rid)

    def _on_confirm_close(self, _dialog, rid):
        # Cerrar con la X cuenta como cancelación para no dejar al daemon
        # esperando la respuesta hasta el timeout.
        self._call("Respond", GLib.Variant("(sb)", (str(rid), False)))
        self._close_confirm(rid)
        return True

    def _close_confirm(self, rid):
        for dialog in list(self.confirm_dialogs):
            if getattr(dialog, "_rid", None) == rid:
                self.confirm_dialogs.remove(dialog)
                dialog.destroy()


def main():
    ReplicatorApp().run()


if __name__ == "__main__":
    main()
