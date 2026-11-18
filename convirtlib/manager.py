"""Talk to libvirt, keep backups, and apply switch changes.

Two backends share one interface: Libvirt runs virsh, and Files treats a
directory of XML files as the set of VMs so the rest of the program can
be tested and drawn without libvirt.
"""
import json
import os
import shutil
import subprocess
import time

from . import check, core

URIS = ("qemu:///session", "qemu:///system")
KEEP_BACKUPS = 20
# The slot that holds a VM's definition from before convirt first changed it.
ORIGINAL = "original"


class Vm:
    def __init__(self, uri, name, running):
        self.uri = uri
        self.name = name
        self.running = running

    @property
    def session(self):
        return self.uri.endswith("session")

    @property
    def label(self):
        if self.session:
            return self.name
        return "%s (system)" % self.name


class Libvirt:
    def _virsh(self, uri, *args, readonly=False, stdin=None):
        command = ["virsh", "-c", uri]
        if readonly:
            command.insert(1, "-r")
        try:
            return subprocess.run(command + list(args), input=stdin,
                                  capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            raise RuntimeError("virsh is not installed")
        except subprocess.TimeoutExpired:
            raise RuntimeError("libvirt did not answer")

    def list(self):
        vms = []
        for uri in URIS:
            everything = self._virsh(uri, "list", "--all", "--name",
                                     readonly=True)
            if everything.returncode != 0:
                continue
            running = self._virsh(uri, "list", "--name", readonly=True)
            up = set(running.stdout.split("\n"))
            for name in everything.stdout.split("\n"):
                if name.strip():
                    vms.append(Vm(uri, name.strip(), name in up))
        return vms

    def xml(self, vm, live=False):
        args = ["dumpxml", vm.name] if live \
            else ["dumpxml", "--inactive", vm.name]
        result = self._virsh(vm.uri, *args, readonly=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return result.stdout

    def define(self, vm, text):
        result = self._virsh(vm.uri, "define", "/dev/stdin", stdin=text)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())

    def network_xml(self, vm, name):
        """The definition of one libvirt network, or None if unreadable."""
        result = self._virsh(vm.uri, "net-dumpxml", name, readonly=True)
        return result.stdout if result.returncode == 0 else None

    def render_nodes(self, vm):
        return render_nodes(system=not vm.session)

    def power(self, vm, action):
        """start, shutdown (as the power button would) or destroy (off)."""
        result = self._virsh(vm.uri, action, vm.name)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())

    def is_running(self, vm):
        result = self._virsh(vm.uri, "domstate", vm.name, readonly=True)
        return result.stdout.strip() == "running"

    def network_names(self, vm):
        result = self._virsh(vm.uri, "net-list", "--all", "--name",
                             readonly=True)
        if result.returncode != 0:
            return []
        return [n.strip() for n in result.stdout.split("\n") if n.strip()]

    def host_interfaces(self, vm):
        return host_interfaces()

    def audio_devices(self, vm):
        return audio_devices()

    def audio_types(self, vm):
        return audio_types()


def audio_devices(run=subprocess.run):
    """This desktop's sound outputs and inputs, as {"sinks": [(name,
    label)], "sources": [...]}, read from PipeWire or PulseAudio through
    pactl; None when neither answers. Monitors of outputs are not inputs."""
    found = {}
    for kind in ("sinks", "sources"):
        try:
            result = run(["pactl", "--format=json", "list", kind],
                         capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        try:
            entries = json.loads(result.stdout)
        except ValueError:
            return None
        devices = []
        for entry in entries:
            properties = entry.get("properties") or {}
            if properties.get("device.class") == "monitor":
                continue
            name = entry.get("name")
            if name:
                devices.append((name, entry.get("description") or name))
        found[kind] = devices
    return found


def audio_types(dirs=check.MODULE_DIRS):
    """The host sound systems this QEMU can use, or None when QEMU is not
    split into modules and so has whatever it was built with."""
    directory = check.module_dir(dirs)
    if directory is None:
        return None
    return {kind for kind, module in core.AUDIO_MODULES.items()
            if not check.missing_modules((module,), directory)}


def host_interfaces(sysfs="/sys/class/net"):
    """This host's bridges, and its wired cards that macvtap can sit on."""
    bridges, wired = [], []
    try:
        names = sorted(os.listdir(sysfs))
    except OSError:
        return {"bridges": bridges, "wired": wired}
    for name in names:
        path = os.path.join(sysfs, name)
        if os.path.isdir(os.path.join(path, "bridge")):
            bridges.append(name)
        elif os.path.exists(os.path.join(path, "device")) \
                and not os.path.isdir(os.path.join(path, "wireless")):
            wired.append(name)
    return {"bridges": bridges, "wired": wired}


def render_nodes(system=False, root="/dev/dri", sysfs="/sys/class/drm"):
    """The GPU render nodes on this host, each with its kernel driver.

    A session VM runs QEMU as this user, so a node counts as usable only
    if this user can open it. libvirt opens one for a system VM itself.
    """
    nodes = []
    try:
        names = sorted(n for n in os.listdir(root) if n.startswith("renderD"))
    except OSError:
        return nodes
    for name in names:
        node = os.path.join(root, name)
        driver = os.path.join(sysfs, name, "device", "driver")
        try:
            driver = os.path.basename(os.readlink(driver))
        except OSError:
            driver = "unknown driver"
        usable = system or os.access(node, os.R_OK | os.W_OK)
        nodes.append({"node": node, "driver": driver, "usable": usable})
    return nodes


def forward_mode(text):
    """A libvirt network's forward mode; "isolated" when it forwards none."""
    if text is None:
        return None
    try:
        forward = core.parse(text).find("forward")
    except core.ET.ParseError:
        return None
    if forward is None:
        return "isolated"
    return forward.get("mode", "nat")


class Files:
    """Each NAME.xml in a directory is a VM; NAME.live.xml means running.

    NAME.xml under networks/ is a libvirt network. render is the list of
    render nodes to report, or None to leave the host's GPU unsaid. An
    audio.json beside the VMs holds the host's sound: "sinks" and "sources"
    as [name, label] pairs and "types", the sound systems QEMU has.
    """

    def __init__(self, directory, render=None, interfaces=None):
        self.directory = directory
        self.render = render
        self.interfaces = interfaces or {"bridges": [], "wired": []}

    def _audio(self):
        path = os.path.join(self.directory, "audio.json")
        if not os.path.exists(path):
            return {}
        with open(path) as handle:
            return json.load(handle)

    def audio_devices(self, vm):
        audio = self._audio()
        if "sinks" not in audio:
            return None
        return {kind: [tuple(d) for d in audio.get(kind, [])]
                for kind in ("sinks", "sources")}

    def audio_types(self, vm):
        types = self._audio().get("types")
        return None if types is None else set(types)

    def network_xml(self, vm, name):
        path = os.path.join(self.directory, "networks", name + ".xml")
        if not os.path.exists(path):
            return None
        with open(path) as handle:
            return handle.read()

    def render_nodes(self, vm):
        return self.render

    def power(self, vm, action):
        """Starting copies the stored definition to the live one; a shut
        down or power off removes it, so the VM reads as shut off."""
        live = self._path(vm, live=True)
        if action == "start":
            with open(self._path(vm)) as source, open(live, "w") as target:
                target.write(source.read())
        elif action in ("shutdown", "destroy"):
            if os.path.exists(live):
                os.remove(live)
        else:
            raise RuntimeError("unknown action: %s" % action)

    def is_running(self, vm):
        return os.path.exists(self._path(vm, live=True))

    def network_names(self, vm):
        directory = os.path.join(self.directory, "networks")
        if not os.path.isdir(directory):
            return []
        return sorted(f[:-4] for f in os.listdir(directory)
                      if f.endswith(".xml"))

    def host_interfaces(self, vm):
        return self.interfaces

    def _path(self, vm, live=False):
        suffix = ".live.xml" if live else ".xml"
        return os.path.join(self.directory, vm.name + suffix)

    def list(self):
        vms = []
        for entry in sorted(os.listdir(self.directory)):
            if entry.endswith(".xml") and not entry.endswith(".live.xml"):
                name = entry[:-4]
                live = os.path.join(self.directory, name + ".live.xml")
                vms.append(Vm("qemu:///session", name, os.path.exists(live)))
        return vms

    def xml(self, vm, live=False):
        with open(self._path(vm, live)) as handle:
            return handle.read()

    def define(self, vm, text):
        core.parse(text)
        with open(self._path(vm), "w") as handle:
            handle.write(text)


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") \
        or os.path.expanduser("~/.local/state")
    return os.path.join(base, "convirt")


class Manager:
    def __init__(self, backend, backups=None, check_paths=None):
        self.backend = backend
        self.backups = backups or state_dir()
        # Where the 3D check looks for processes, the home folder and the
        # QEMU modules; tests point them at a temporary folder.
        self.check_paths = check_paths or {}

    def list(self):
        """Running VMs first, then by name."""
        return sorted(self.backend.list(),
                      key=lambda vm: (not vm.running, vm.name))

    def find(self, name):
        matches = [vm for vm in self.list() if vm.name == name]
        if not matches:
            raise RuntimeError("no VM named %s" % name)
        return matches[0]

    def stored(self, vm):
        return core.parse(self.backend.xml(vm))

    def host_facts(self, vm, root):
        """What the host says about the networks and GPU a VM would use."""
        names = {i.find("source").get("network")
                 for i in root.findall("devices/interface")
                 if i.get("type") == "network" and i.find("source") is not None}
        networks = {}
        for name in sorted(n for n in names if n):
            mode = forward_mode(self.backend.network_xml(vm, name))
            if mode:
                networks[name] = mode
        # The desktop this tool runs in is the one an own window opens on.
        facts = {"networks": networks, "system": not vm.session,
                 "wayland": bool(os.environ.get("WAYLAND_DISPLAY"))}
        render = self.backend.render_nodes(vm)
        if render is not None:
            facts["render"] = render
        devices = self.backend.audio_devices(vm)
        if devices is not None:
            facts["sinks"] = devices["sinks"]
            facts["sources"] = devices["sources"]
        types = self.backend.audio_types(vm)
        if types is not None:
            facts["audio_types"] = types
        return facts

    def cautions(self, vm, host=None):
        """{row: (level, text)} for what the VM's settings open."""
        root = self.stored(vm)
        return core.cautions(root, host or self.host_facts(vm, root))

    def choices(self, vm, host=None):
        """{switch: [(value, label)]} for every switch that is a list."""
        root = self.stored(vm)
        choices = core.sound_choices(root, host or self.host_facts(vm, root))
        choices["display"] = list(core.DISPLAY_LABELS.items())
        choices["network"] = self.network_choices(vm)
        return choices

    def network_choices(self, vm):
        """[(value, label)] for every connection this host offers the VM."""
        modes = {name: forward_mode(self.backend.network_xml(vm, name))
                 for name in self.backend.network_names(vm)}
        keys = ["off"] + ["network:%s" % n for n in sorted(modes)]
        interfaces = self.backend.host_interfaces(vm)
        keys += ["bridge:%s" % b for b in interfaces["bridges"]]
        keys += ["direct:%s" % d for d in interfaces["wired"]]
        if vm.session:
            keys.append("user")
        current = core.values(self.stored(vm))["network"]
        if current not in keys and current != "?":
            keys.append(current)
        return [(k, core.connection_label(k, modes)) for k in keys]

    # --------------------------------------------------------- lifecycle

    def start(self, vm):
        self.backend.power(vm, "start")
        vm.running = True

    def shutdown(self, vm):
        """Ask the guest to shut down, as its power button would."""
        self.backend.power(vm, "shutdown")

    def force_off(self, vm):
        self.backend.power(vm, "destroy")
        vm.running = False

    def refresh_running(self, vm):
        vm.running = self.backend.is_running(vm)
        return vm.running

    def check_3d(self, vm, write=True):
        """(verdict, text) of the 3D check; the text is also saved."""
        root = self.stored(vm)
        state = core.read_state(root)
        render = self.backend.render_nodes(vm)
        verdict, lines = check.check(
            state, core.gl_uses(root), vm.name, vm.session,
            self.refresh_running(vm), render, **self.check_paths)
        text = check.report(vm.label, verdict, lines)
        if write:
            self._write_private(self.check_file(vm), text)
        return verdict, text

    def check_file(self, vm):
        """Where the last 3D check of a VM is saved."""
        safe = "".join(c if c.isalnum() or c in "-_." else "_"
                       for c in vm.name)
        kind = "session" if vm.session else "system"
        return os.path.join(self.backups, "3d-%s-%s.txt" % (kind, safe))

    def open_screen(self, vm):
        """Show a running VM's screen in virt-viewer or virt-manager."""
        for command in (
                ["virt-viewer", "--connect", vm.uri, "--wait", "--", vm.name],
                ["virt-manager", "--connect", vm.uri,
                 "--show-domain-console", vm.name]):
            if shutil.which(command[0]):
                subprocess.Popen(command, start_new_session=True,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return
        raise RuntimeError("install virt-viewer or virt-manager to see it")

    def live_values(self, vm):
        """The running VM's switch values, or None when it is shut off."""
        if not vm.running:
            return None
        return core.values(core.parse(self.backend.xml(vm, live=True)))

    def state(self, vm, host=None):
        """host is host_facts, passed in when a caller already has them,
        so one redraw asks the host once."""
        root = self.stored(vm)
        state = core.read_state(root, host or self.host_facts(vm, root))
        if not vm.session \
                and state["display"]["value"] not in core.OWN_WINDOW:
            # A system VM runs as another user and cannot open a window
            # in this desktop session.
            state["display"]["enabled"] = False
            state["display"]["meaning"] += "; system VM, fixed"
        return state

    def pending(self, vm):
        """Switches whose stored value differs from the running VM."""
        if not vm.running:
            return []
        live = core.values(core.parse(self.backend.xml(vm, live=True)))
        stored = core.values(self.stored(vm))
        pending = [name for name in core.SWITCHES
                   if live[name] != stored[name]]
        if stored["sound"] == "off":
            # Taking the card out is one change; the rows that hang off
            # the card go with it.
            pending = [name for name in pending
                       if name == "sound" or name not in core.SOUND_ROWS]
        return pending

    def change(self, vm, switch, value, facts=None):
        """Apply one switch. Return True if the definition changed."""
        if switch == "display" and value in core.OWN_WINDOW \
                and not vm.session:
            raise RuntimeError("a system VM cannot use its own window")
        if switch == "audio" and value in core.AUDIO_MODULES:
            types = self.backend.audio_types(vm)
            if types is not None and value not in types:
                raise RuntimeError(
                    "QEMU's %s module is not installed (on Arch and "
                    "Fedora, the package qemu-%s)" % (
                        core.AUDIO_MODULES[value], core.AUDIO_MODULES[value]))
        original = self.backend.xml(vm)
        root = core.parse(original)
        if core.same_value(switch, core.values(root)[switch], value):
            return False
        try:
            core.apply(root, switch, value, facts)
        except ValueError as error:
            raise RuntimeError(str(error))
        backup = self._save_backup(vm, original)
        try:
            self.backend.define(vm, core.serialize(root))
        except RuntimeError:
            os.remove(backup)
            raise
        # libvirt fills in defaults when it stores a definition; if one of
        # them undid the change, apply once more on what it stored.
        root = self.stored(vm)
        if not core.same_value(switch, core.values(root)[switch], value):
            core.apply(root, switch, value, facts)
            self.backend.define(vm, core.serialize(root))
        return True

    # ------------------------------------------------------------- slots

    def slots(self, vm):
        """Names of the saved copies of this VM's definition."""
        directory = self._slot_dir(vm)
        if not os.path.isdir(directory):
            return []
        return sorted(f[:-4] for f in os.listdir(directory)
                      if f.endswith(".xml"))

    def save_slot(self, vm, name):
        """Keep an exact copy of the stored definition under a name."""
        name = name.strip()
        allowed = all(c.isalnum() or c in " -_" for c in name)
        if not name or len(name) > 32 or not allowed:
            raise RuntimeError(
                "slot name: 1 to 32 letters, digits, spaces, - or _")
        if name == ORIGINAL and ORIGINAL in self.slots(vm):
            raise RuntimeError("the original slot is kept as it is")
        self._write_private(self._slot_path(vm, name), self.backend.xml(vm))

    def restore_slot(self, vm, name):
        """Put a saved definition back. Undo returns to the one replaced."""
        if name not in self.slots(vm):
            raise RuntimeError("no slot named %s" % name)
        with open(self._slot_path(vm, name)) as handle:
            text = handle.read()
        backup = self._save_backup(vm, self.backend.xml(vm))
        try:
            self.backend.define(vm, text)
        except RuntimeError:
            os.remove(backup)
            raise

    def _slot_dir(self, vm):
        return os.path.join(self._vm_dir(vm), "slots")

    def _slot_path(self, vm, name):
        return os.path.join(self._slot_dir(vm), name + ".xml")

    def _write_private(self, path, text):
        slot_dir = os.path.dirname(path)
        os.makedirs(os.path.dirname(slot_dir), mode=0o700, exist_ok=True)
        os.makedirs(slot_dir, mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)

    def can_undo(self, vm):
        return bool(self._backups(vm))

    def undo(self, vm):
        backups = self._backups(vm)
        if not backups:
            raise RuntimeError("nothing to undo")
        with open(backups[-1]) as handle:
            self.backend.define(vm, handle.read())
        os.remove(backups[-1])

    def _vm_dir(self, vm):
        kind = "session" if vm.session else "system"
        safe = "".join(c if c.isalnum() or c in "-_." else "_"
                       for c in vm.name)
        return os.path.join(self.backups, "%s-%s" % (kind, safe))

    def _backups(self, vm):
        directory = self._vm_dir(vm)
        if not os.path.isdir(directory):
            return []
        return sorted(os.path.join(directory, f)
                      for f in os.listdir(directory) if f.endswith(".xml"))

    def _save_backup(self, vm, text):
        directory = self._vm_dir(vm)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if ORIGINAL not in self.slots(vm):
            self._write_private(self._slot_path(vm, ORIGINAL), text)
        path = os.path.join(directory, "%d.xml" % time.time_ns())
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
        for old in self._backups(vm)[:-KEEP_BACKUPS]:
            os.remove(old)
        return path
