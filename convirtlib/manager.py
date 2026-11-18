"""Talk to libvirt, keep backups, and apply switch changes.

Two backends share one interface: Libvirt runs virsh, and Files treats a
directory of XML files as the set of VMs so the rest of the program can
be tested and drawn without libvirt.
"""
import os
import subprocess
import time

from . import core

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
    render nodes to report, or None to leave the host's GPU unsaid.
    """

    def __init__(self, directory, render=None):
        self.directory = directory
        self.render = render

    def network_xml(self, vm, name):
        path = os.path.join(self.directory, "networks", name + ".xml")
        if not os.path.exists(path):
            return None
        with open(path) as handle:
            return handle.read()

    def render_nodes(self, vm):
        return self.render

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
    def __init__(self, backend, backups=None):
        self.backend = backend
        self.backups = backups or state_dir()

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
        facts = {"networks": networks}
        render = self.backend.render_nodes(vm)
        if render is not None:
            facts["render"] = render
        return facts

    def state(self, vm):
        root = self.stored(vm)
        state = core.read_state(root, self.host_facts(vm, root))
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
        return [name for name in core.SWITCHES if live[name] != stored[name]]

    def change(self, vm, switch, value, facts=None):
        """Apply one switch. Return True if the definition changed."""
        if switch == "display" and value in core.OWN_WINDOW \
                and not vm.session:
            raise RuntimeError("a system VM cannot use its own window")
        original = self.backend.xml(vm)
        root = core.parse(original)
        if core.values(root)[switch] == value:
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
        if core.values(root)[switch] != value:
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
