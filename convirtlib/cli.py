"""Command line: open the window, or read and flip switches from a shell.

    convirt                       open the window
    convirt open VM [URI]         open the window on one VM
    convirt list                  list the VMs
    convirt start VM              start a VM
    convirt stop VM               ask a VM to shut down, as its power button
    convirt off VM                power a VM off at once
    convirt check VM              check whether its 3D acceleration works
    convirt show VM               show every switch of one VM
    convirt set VM SWITCH VALUE   change one switch
    convirt undo VM               restore the config before the last change
    convirt slots VM              list the saved copies of a VM's config
    convirt save VM SLOT          save the current config under a name
    convirt restore VM SLOT       put a saved config back

Switches:
  sound      off, on, or a card: ich9 ich6 ac97 usb virtio es1370 sb16
  mic        off, on (line in, hda-duplex) or micro (hda-micro); Intel HDA
  audio      default, pipewire, pulseaudio, spice, alsa, sdl, jack, oss,
             dbus or none: the host sound system
  speaker    default or a PipeWire/PulseAudio output name (pactl list sinks)
  recorder   default or an input name (pactl list sources)
  latency    default or milliseconds
  rate       default or a sample rate in Hz, both ways
  accel3d, clipboard   on or off
  display    sdl, gtk or spice
  screens    1 to 4 (virtio or QXL video)
  network    off, on, network:NAME, bridge:NAME, direct:DEVICE (macvtap)
             or user
A change is stored at once and takes effect at the next full shut off and
start of the VM.
"""
import sys

from . import core
from .manager import Files, Libvirt, Manager


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)

    # Test options: a directory of XML files instead of libvirt, a
    # separate backup directory, a PNG of the window, and a change
    # (SWITCH=VALUE) made in the window before the PNG is drawn.
    backend, backups, snapshot, change = Libvirt(), None, None, None
    while args and args[0] in ("--files", "--backups", "--snapshot",
                               "--change"):
        option, value = args[0], args[1]
        del args[:2]
        if option == "--files":
            backend = Files(value)
        elif option == "--backups":
            backups = value
        elif option == "--change":
            change = tuple(value.split("=", 1))
        else:
            snapshot = value
    manager = Manager(backend, backups)

    try:
        if not args or args[0] == "open" and len(args) in (2, 3):
            try:
                from . import ui
            except (ImportError, ValueError):
                print("The window needs GTK 3 for Python (PyGObject).")
                print("The commands still work: convirt list")
                return 1
            initial = None
            if args:
                initial = (args[1], args[2] if len(args) == 3 else None)
            ui.run(manager, snapshot, change, initial)
        elif len(args) == 2 and args[0] in ("start", "stop", "off"):
            vm = manager.find(args[1])
            {"start": manager.start, "stop": manager.shutdown,
             "off": manager.force_off}[args[0]](vm)
            print({"start": "started", "stop": "asked to shut down",
                   "off": "powered off"}[args[0]])
        elif len(args) == 2 and args[0] == "check":
            verdict, text = manager.check_3d(manager.find(args[1]))
            print(text, end="")
            return 0 if verdict in ("working", "off") else 1
        elif args == ["list"]:
            for vm in manager.list():
                print("%-24s %s" % (vm.label,
                                    "running" if vm.running else "shut off"))
        elif len(args) == 2 and args[0] == "show":
            vm = manager.find(args[1])
            host = manager.host_facts(vm, manager.stored(vm))
            state = manager.state(vm, host)
            cautions = manager.cautions(vm, host)
            marks = {core.CAUTION: "!", core.NOTE: "i"}

            def caution(key):
                if key in cautions:
                    level, text = cautions[key]
                    print("  %s %s" % (marks[level], text))

            print("%s, %s VM" % (vm.label,
                                 "session" if vm.session else "system"))
            caution("vm")
            for name in core.SWITCHES:
                print("%-16s %-16s %s" % (core.TITLES[name],
                                         state[name]["value"],
                                         state[name]["meaning"]))
                caution(name)
            attached = core.exposures(manager.stored(vm))
            if attached:
                print("also attached: " + ", ".join(attached))
                caution("attached")
            pending = manager.pending(vm)
            if pending:
                print("waiting for next full start: " + ", ".join(pending))
        elif len(args) == 4 and args[0] == "set":
            vm = manager.find(args[1])
            changed = manager.change(vm, args[2], args[3])
            print("changed" if changed else "already set")
        elif len(args) == 2 and args[0] == "undo":
            manager.undo(manager.find(args[1]))
            print("restored")
        elif len(args) == 2 and args[0] == "slots":
            for name in manager.slots(manager.find(args[1])):
                print(name)
        elif len(args) == 3 and args[0] == "save":
            manager.save_slot(manager.find(args[1]), args[2])
            print("saved")
        elif len(args) == 3 and args[0] == "restore":
            manager.restore_slot(manager.find(args[1]), args[2])
            print("restored")
        else:
            print(__doc__.strip())
            return 2
    except RuntimeError as error:
        print("error: %s" % error)
        return 1
    return 0
