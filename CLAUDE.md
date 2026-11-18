# CLAUDE.md — convirt

A desktop tool that shows the microphone, sound output, 3D acceleration, display and network of a libvirt virtual machine as five switches, and changes them through `virsh`. MIT-licensed.

## Project at a glance

- **Stack**: Python 3.9+ standard library, plus GTK 3 through PyGObject for the window. There are no pip dependencies and no virtual environment.
- **Modules**: `convirtlib/core.py` reads and edits a domain definition as an ElementTree and touches neither libvirt nor the screen. `convirtlib/manager.py` runs `virsh`, keeps the backups and applies changes. `convirtlib/ui.py` is the window. `convirtlib/cli.py` parses the command line. `convirt` is the launcher.
- **State**: previous definitions for undo, under `~/.local/state/convirt/`, mode 0600. Nothing else is stored.

## Run, install, test

| Task | Command |
|---|---|
| Run | `./convirt` (window) or `./convirt list` |
| Menu entry | `sh install.sh`, removed with `sh install.sh --remove` |
| Tests | `python3 -m unittest discover -s tests` |

## Verification boundary

The tests cover `core.py` and the change and undo flow on sample definitions through the `Files` backend, which treats a directory of XML files as the set of VMs. Keep `core.py` free of libvirt and GTK imports so it stays testable that way.

Three hidden options exist for checking the rest without a real VM: `--files DIR` swaps libvirt for that directory, `--backups DIR` moves the undo store, and `--snapshot FILE` draws the window to a PNG and exits. `docs/screenshot.png` is made with them.

Whether a display choice shows a picture is a property of the host's graphics driver and is verified on the host, by starting a VM.

## Gotchas

- **A stored change is not a running change.** `virsh define` only affects the next full shut off and start. The window compares the running definition with the stored one and lists the switches that differ.
- **libvirt rewrites what it stores.** It fills in defaults on define, such as the `device` attribute of a virtio video model (libvirt 12.5.0 and newer). `Manager.change` re-reads the stored definition and applies the change once more if a default undid it.
- **virgl needs the `-gl` device.** 3D acceleration is only on when the video device is `virtio-vga-gl` or `virtio-gpu-gl` and a display with OpenGL exists. `accel3d="yes"` alone does not turn it on.
- **The GTK window is a raw QEMU argument.** libvirt has no GTK graphics type, so `display gtk` removes every `<graphics>` element, which makes libvirt pass `-display none`, and adds `-display gtk,gl=on|off` through `<qemu:commandline>`; QEMU uses the last `-display` it is given.
- **An own-window display needs the session's variables.** libvirt starts QEMU with a nearly empty environment, so `WAYLAND_DISPLAY` and `XDG_RUNTIME_DIR` (or `DISPLAY` and `XAUTHORITY` on X11) are passed through `<qemu:env>`. They are read from the environment the tool runs in.
- **SPICE-only devices go with the SPICE display.** A `spicevmc` channel or USB redirection device cannot exist without a SPICE display, so switching to an own window removes them.
- **System VMs cannot use an own window.** QEMU under `qemu:///system` runs as another user and cannot reach the desktop session.

## Where things are documented

- `README.md` — requirements per distribution, usage, what each change does, and the risk of 3D acceleration.
