"""Check whether a VM's 3D acceleration can work and whether it came up.

The stored definition only says what was asked for. This looks at what the
host has (a GPU render node, the QEMU modules for virgl and the display),
what QEMU was actually started with, and what QEMU wrote to its log, so a
3D setting that silently did nothing says why.

Every check is a Line: ok is True, False, or None when it could not be
told; label is short, detail says what was seen.
"""
import glob
import os
import re
from collections import namedtuple

Line = namedtuple("Line", "ok label detail")

GL_DEVICES = ("virtio-vga-gl", "virtio-gpu-gl")
# QEMU builds that split their parts into modules (Arch, Debian, Fedora)
# keep them in one of these folders; a build without them has them inside.
MODULE_DIRS = ("/usr/lib/qemu", "/usr/lib64/qemu",
               "/usr/lib/x86_64-linux-gnu/qemu",
               "/usr/lib/aarch64-linux-gnu/qemu")
# The modules each part of the 3D path needs, by what the definition uses.
MODULES = {
    "virgl": ("hw-display-virtio-gpu-gl", "ui-opengl"),
    "virtio-vga-gl": ("hw-display-virtio-vga-gl",),
    "sdl": ("ui-sdl",),
    "gtk": ("ui-gtk",),
    "egl-headless": ("ui-egl-headless",),
    "spice": ("ui-spice-core",),
}
# Words in QEMU's log that point at a 3D or display failure.
TROUBLE = re.compile(
    r"egl|virgl|opengl|gl=|render ?node|not a valid device|"
    r"could not|failed|error", re.I)


def module_dir(dirs=MODULE_DIRS):
    for directory in dirs:
        if glob.glob(os.path.join(directory, "*.so")):
            return directory
    return None


def missing_modules(needed, directory):
    """The needed QEMU modules not in directory; none when it is None."""
    if directory is None:
        return []
    return [m for m in needed
            if not os.path.exists(os.path.join(directory, m + ".so"))]


def qemu_cmdline(name, proc="/proc"):
    """The running QEMU's arguments for the VM named name, or None."""
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return None
    wanted = "guest=%s" % name
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as handle:
                args = handle.read().decode(errors="replace").split("\0")
        except OSError:
            continue
        if not args or "qemu" not in os.path.basename(args[0]):
            continue
        for index, arg in enumerate(args[:-1]):
            if arg == "-name" and (args[index + 1] == name or
                                   args[index + 1].split(",")[0] == wanted):
                return [a for a in args if a]
    return None


def started_with_gl(args):
    """(device, display) as QEMU was started: the GL video device and the
    display that gives it an OpenGL context, each None when absent."""
    text = " ".join(args)
    device = next((d for d in GL_DEVICES if d in text), None)
    display = None
    for index, arg in enumerate(args[:-1]):
        value = args[index + 1]
        if arg == "-display" and ("gl=on" in value or "gl=core" in value
                                  or value.startswith("egl-headless")):
            display = value
        elif arg == "-spice" and "gl=on" in value:
            display = "spice with OpenGL"
    return device, display


def log_path(name, session, home=None):
    if session:
        home = home or os.path.expanduser("~")
        return os.path.join(home, ".cache", "libvirt", "qemu", "log",
                            name + ".log")
    return os.path.join("/var/log/libvirt/qemu", name + ".log")


def last_start(text):
    """The part of a QEMU log written since the VM last started."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if "starting up" in line]
    return lines[starts[-1]:] if starts else lines


def trouble(lines):
    """Lines after the command line itself that point at a problem."""
    found = []
    for line in lines:
        if line.startswith(("LC_ALL=", "/usr/", "-")) or "starting up" in line:
            continue
        if TROUBLE.search(line):
            found.append(line.strip())
    return found


def check(state, uses, vm_name, session, running, render,
          proc="/proc", home=None, dirs=MODULE_DIRS):
    """Every line of the 3D check, in the order a person would fix them.

    state is the read_state of the stored definition; uses is the set of
    MODULES keys the definition needs; render is manager.render_nodes.
    Returns (verdict, lines): verdict is "working", "not working",
    "off" or "unknown".
    """
    lines = []
    asked = state["accel3d"]["value"] == "on"
    lines.append(Line(asked or None, "3D in the settings",
                      "set" if asked else "off; nothing to check"))
    if not asked:
        return "off", lines

    usable = [r for r in render or () if r["usable"]]
    if render is None:
        lines.append(Line(None, "GPU render node", "not checked"))
    elif not render:
        lines.append(Line(False, "GPU render node",
                          "this host has none in /dev/dri"))
    elif not usable:
        lines.append(Line(False, "GPU render node",
                          "%s cannot be opened by this user"
                          % render[0]["node"]))
    else:
        lines.append(Line(True, "GPU render node", "%s (%s)" % (
            usable[0]["node"], usable[0]["driver"])))

    directory = module_dir(dirs)
    needed = [m for key in sorted(uses) for m in MODULES.get(key, ())]
    missing = missing_modules(needed, directory)
    if directory is None:
        lines.append(Line(None, "QEMU modules",
                          "no module folder found; QEMU may have them built in"))
    elif missing:
        lines.append(Line(False, "QEMU modules", "missing in %s: %s" % (
            directory, ", ".join(missing))))
    else:
        lines.append(Line(True, "QEMU modules", "all present in " + directory))

    if not running:
        lines.append(Line(None, "QEMU", "not running; start the VM to check"))
        verdict = "unknown"
        if any(line.ok is False for line in lines):
            verdict = "not working"
        return verdict, lines

    args = qemu_cmdline(vm_name, proc)
    if args is None:
        lines.append(Line(None, "QEMU", "its process was not found"))
    else:
        device, display = started_with_gl(args)
        lines.append(Line(bool(device), "QEMU's video device",
                          device or "started without a GL device"))
        lines.append(Line(bool(display), "QEMU's OpenGL display",
                          display or "started without an OpenGL display"))

    path = log_path(vm_name, session, home)
    try:
        with open(path, errors="replace") as handle:
            found = trouble(last_start(handle.read()))
        if found:
            lines.append(Line(False, "QEMU's log", " | ".join(found[-4:])))
        else:
            lines.append(Line(True, "QEMU's log", "nothing about 3D since start"))
    except PermissionError:
        lines.append(Line(None, "QEMU's log",
                          "only root can read it: sudo tail -30 " + path))
    except OSError:
        lines.append(Line(None, "QEMU's log", "not found at " + path))

    if any(line.ok is False for line in lines):
        verdict = "not working"
    elif any(line.ok is None for line in lines[1:]):
        verdict = "unknown"
    else:
        verdict = "working"
    return verdict, lines


def report(vm_label, verdict, lines):
    """The check as plain text, for the details window and the saved file."""
    marks = {True: "ok  ", False: "FAIL", None: "?   "}
    text = ["3D check for %s: %s" % (vm_label, verdict), ""]
    for line in lines:
        text.append("%s %-24s %s" % (marks[line.ok], line.label, line.detail))
    text += ["", "Inside a Linux guest, the kernel log confirms it:",
             "  journalctl -k | grep virgl    shows '+virgl' when it took"]
    return "\n".join(text) + "\n"
