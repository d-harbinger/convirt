"""Command line: open the window, or read and flip switches from a shell.

    convirt                       open the window
    convirt list                  list the VMs
    convirt show VM               show every switch of one VM
    convirt set VM SWITCH VALUE   change one switch
    convirt undo VM               restore the config before the last change
    convirt slots VM              list the saved copies of a VM's config
    convirt save VM SLOT          save the current config under a name
    convirt restore VM SLOT       put a saved config back

Switches: mic, sound, accel3d, clipboard, network take on or off; display
takes sdl, gtk or spice. A change is stored at once and takes effect at the next
full shut off and start of the VM.
"""
import sys

from . import core
from .manager import Files, Libvirt, Manager


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)

    # Test options: a directory of XML files instead of libvirt, a
    # separate backup directory, and a PNG of the window.
    backend, backups, snapshot = Libvirt(), None, None
    while args and args[0] in ("--files", "--backups", "--snapshot"):
        option, value = args[0], args[1]
        del args[:2]
        if option == "--files":
            backend = Files(value)
        elif option == "--backups":
            backups = value
        else:
            snapshot = value
    manager = Manager(backend, backups)

    try:
        if not args:
            try:
                from . import ui
            except (ImportError, ValueError):
                print("The window needs GTK 3 for Python (PyGObject).")
                print("The commands still work: convirt list")
                return 1
            ui.run(manager, snapshot)
        elif args == ["list"]:
            for vm in manager.list():
                print("%-24s %s" % (vm.label,
                                    "running" if vm.running else "shut off"))
        elif len(args) == 2 and args[0] == "show":
            vm = manager.find(args[1])
            state = manager.state(vm)
            for name in core.SWITCHES:
                print("%-16s %-6s %s" % (core.TITLES[name],
                                         state[name]["value"],
                                         state[name]["meaning"]))
            attached = core.exposures(manager.stored(vm))
            if attached:
                print("also attached: " + ", ".join(attached))
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
