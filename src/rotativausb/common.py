"""Constantes compartidas entre la GUI y el demonio."""

BUS_NAME = "es.rotativausb.Daemon"
OBJECT_PATH = "/es/rotativausb/Daemon"
IFACE = "es.rotativausb.Daemon1"
POLKIT_ACTION = "es.rotativausb.activate"

PARTITION_TABLES = {
    "gpt": "GPT",
    "mbr": "MBR (msdos)",
}

FILESYSTEMS = ("ext4", "ext3", "exfat", "fat32", "ntfs")

CONFIRM_MODES = {
    "never": "Nunca",
    "first_if_same": "Solo la primera vez si los demás son iguales",
    "first_always": "Solo la primera vez aunque sean diferentes",
    "each": "En cada pendrive detectado",
}

CLONE_MODES = {
    "wipe": "Borrado completo (formateo rápido, sin datos)",
    "prepare": "Preparar contenido desde carpeta",
}

DEFAULT_CONFIG = {
    "table": "gpt",
    "filesystem": "ext4",   # los equipos de examen son Linux
    "confirm_mode": "each",
    "clone_mode": "wipe",
    "source": "",
    "label": "EXAMEN",
    "power_off": True,
    "verify": True,         # comprobar el contenido antes de expulsar
    "use_udisks2": False,   # alternativa a mkfs directo para exFAT/NTFS
}

# Se usa para crear el proxy Gio.DBusProxy en la GUI. Debe coincidir con
# los decorators @dbus.service.method/@signal del demonio.
DBUS_XML = """
<node>
  <interface name='es.rotativausb.Daemon1'>
    <method name='Activate'>
      <arg type='s' name='config_json' direction='in'/>
      <arg type='b' name='ok' direction='out'/>
    </method>
    <method name='Deactivate'/>
    <method name='Respond'>
      <arg type='s' name='request_id' direction='in'/>
      <arg type='b' name='approved' direction='in'/>
    </method>
    <method name='GetStatus'>
      <arg type='s' name='status_json' direction='out'/>
    </method>
    <method name='CheckUdisks2'>
      <arg type='s' name='report_json' direction='out'/>
    </method>
    <signal name='ConfirmationRequired'>
      <arg type='s' name='request_json'/>
    </signal>
    <signal name='DeviceDetected'>
      <arg type='s' name='info_json'/>
    </signal>
    <signal name='Progress'>
      <arg type='s' name='device_id'/>
      <arg type='s' name='stage'/>
      <arg type='i' name='percent'/>
      <arg type='s' name='message'/>
    </signal>
    <signal name='DeviceFinished'>
      <arg type='s' name='info_json'/>
      <arg type='b' name='success'/>
      <arg type='s' name='message'/>
    </signal>
    <signal name='StateChanged'>
      <arg type='s' name='state_json'/>
    </signal>
  </interface>
</node>
"""
