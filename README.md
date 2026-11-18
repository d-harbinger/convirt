# convirt

![The convirt window: rows grouped under Sound (card, microphone, host sound system, output, input, and latency and sample rate under an expander), Picture and Links to the host; amber caution icons beside the session VM, the microphone and the shared clipboard, grey notes beside host sound and the network; a green line confirms a change of host sound to PipeWire, the changed row is marked to apply at the next start, and an amber line offers Shut down and start](docs/screenshot.png)

convirt shows the sound, display and host-link settings of a libvirt virtual machine as a short list of rows, and changes them without hand-editing XML. Save slots keep exact copies of a configuration.

| Row | Values | What it controls |
|---|---|---|
| Sound card | off, or a model: Intel HDA (ICH9, ICH6), AC97, USB audio, virtio sound, ES1370, SB16 | Which sound card the guest has, if any. Each model says whether it records: USB audio and SB16 play only. |
| Microphone | none, microphone, line in | On an Intel HDA card, the codec: `hda-output` plays only, `hda-micro` gives the guest a microphone, `hda-duplex` (QEMU's default) a line in. Other cards have a fixed input. |
| Host sound | PipeWire, PulseAudio, SPICE, ALSA, SDL, JACK, OSS, D-Bus, none | The host sound system QEMU plays into and records from. SPICE sends the sound to the viewer and needs the virt-manager display. A system whose QEMU module is missing is marked "not installed". |
| Plays to | the host's default output, or one of its outputs | With PipeWire or PulseAudio, which host output the guest plays to. The list holds what the host has. |
| Records from | the host's default input, or one of its inputs | With PipeWire or PulseAudio, which host input the guest's microphone hears. |
| Latency | default, 10 to 200 ms | Under **Latency and sample rate**: the latency asked of PipeWire or PulseAudio. |
| Sample rate | the guest's choice, 44.1 kHz, 48 kHz | Under **Latency and sample rate**: a fixed rate both ways, so QEMU does not convert. |
| 3D acceleration | on, off | Whether the guest renders on the host GPU through virgl. |
| Display | Own window (SDL), Own window (GTK), virt-manager | A QEMU window on the desktop, of either kind, or a SPICE display shown inside virt-manager. |
| Shared clipboard | on, off | Whether host and guest share the clipboard. Only the virt-manager display has one. |
| Network | unplugged, or a connection | What the network card is plugged into: a libvirt network, a host bridge, macvtap on a wired host interface, or user-mode networking. The list holds what the host has. |

Each row states what its current value means, from the definition and from what the host has: the microphone row says what the guest's input hears, and a row that does not apply to the chosen sound system or card is shown fixed with the reason. A change is stored at once and takes effect at the next full shut off and start of the VM. Two lines under the rows hold their place: the first confirms the last change in green (red when it failed) with **Undo**, then fades to grey; the second says whether the running VM matches. While a VM runs, a changed row is marked "at next start" and says what runs now and what the next start brings, and the second line offers **Shut down and start**: convirt asks the guest to shut down, as its power button would, waits up to a minute (then offers Force off), and starts it again. **Start**, **Shut down** and **Open** (the VM's screen in virt-viewer or virt-manager) sit beside the VM's name, so the whole round trip happens in one window.

## Does 3D work?

The 3D row's **Details**, and `./convirt check VM` from a shell, check each step the 3D path needs and say which one fails:

- a GPU render node on the host that QEMU can open, and its driver;
- the QEMU modules for virgl and the chosen display (`hw-display-virtio-gpu-gl`, `ui-opengl`, `ui-sdl` and the like; Arch, Debian and Fedora ship them as separate packages);
- for a running VM, what QEMU was actually started with (a `-gl` video device and an OpenGL display), read from its process;
- what QEMU wrote to its log since the VM last started. A system VM's log is readable by root only, so the check prints the `sudo tail` line instead.

The row itself reads "✓ working in QEMU" or "✗ not working" with the first failing step. Each check is saved to `~/.local/state/convirt/3d-<session|system>-<vm>.txt`. Inside a Linux guest, `journalctl -k | grep virgl` showing `+virgl` is the final word.

## Requirements

- Linux with libvirt and the `virsh` command.
- Python 3.9 or newer.
- For the window: GTK 3 and its Python bindings. A machine with virt-manager installed already has them.

| Distribution | Packages |
|---|---|
| Arch and derivatives | `python-gobject gtk3 libvirt` |
| Debian, Ubuntu, Mint | `python3-gi gir1.2-gtk-3.0 libvirt-clients` |
| Fedora | `python3-gobject gtk3 libvirt-client` |

The tool installs nothing, needs no root, and runs on the machine that hosts the VMs. For **Open**, virt-viewer or virt-manager.

## Run

```sh
git clone https://github.com/d-harbinger/convirt.git
cd convirt
./convirt
```

To add it to the application menu, and to take it out again:

```sh
sh install.sh
sh install.sh --remove
```

The menu entry points at the cloned folder, so the folder has to stay where it is.

The same switches work from a shell:

```sh
./convirt list
./convirt show workstation
./convirt set workstation mic off
./convirt undo workstation
./convirt stop workstation        # as the power button; `off` powers off at once
./convirt start workstation
./convirt check workstation       # exits 1 when 3D is set and not working
./convirt open workstation        # the window, on that VM
```

`sound` takes `off`, `on` or a model (`ich9`, `ich6`, `ac97`, `usb`, `virtio`, `es1370`, `sb16`). `mic` takes `off`, `on` (line in) or `micro`. `audio` takes `default` (libvirt's choice for the display), `pipewire`, `pulseaudio`, `spice`, `alsa`, `sdl`, `jack`, `oss`, `dbus` or `none`. `speaker` and `recorder` take `default` or a device name as `pactl list sinks` and `pactl list sources` print it. `latency` takes `default` or milliseconds, `rate` takes `default` or a rate in Hz. `accel3d` and `clipboard` take `on` or `off`. `display` takes `sdl`, `gtk` or `spice`. `network` takes `off`, `on` (plug the cable back in), `network:NAME`, `bridge:NAME`, `direct:DEVICE` (macvtap) or `user`; the card keeps its model and MAC address.

## Also attached

Shared folders, host devices passed through to the guest and USB redirection ports also link a VM to its host, and they have no switch. The window and `convirt show` list them under "Also attached" so they are not missed. Remove them in virt-manager for a VM that holds untrusted software.

## Save slots

A slot is an exact, named copy of one VM's configuration, including the parts that have no switch.

```sh
./convirt save workstation "video calls"
./convirt slots workstation
./convirt restore workstation "video calls"
```

The configuration from before convirt first changed a VM is kept in a slot named `original`, which cannot be overwritten. Restoring a slot is itself a change, so undo returns to the configuration it replaced.

## What a change does

Every change reads the stored definition of the VM, edits it, saves the previous definition, and hands the new one to libvirt. If libvirt rejects it, the stored definition stays as it was.

- **Undo.** The previous definitions are kept in `~/.local/state/convirt/`, readable only by the owner, twenty per VM. Undo restores the newest one.
- **Own window.** Both kinds remove the SPICE display and the devices that only exist with it (shared clipboard channel, USB redirection), point audio at PipeWire or PulseAudio, and pass the desktop session's display name to QEMU. Run the tool inside the desktop session so it can read that name.
- **SDL or GTK.** The two kinds differ in how they take the pointer. The SDL window holds the pointer whenever it is inside the window and lets go when it touches the window edge. The GTK window holds it only after a click. In both, Ctrl+Alt+G releases it. libvirt has no GTK display type, so the tool passes it to QEMU as a raw `-display` argument; whether the GTK window shows a picture with 3D on depends on the host's graphics driver.
- **virt-manager.** This display uses a private socket, not a TCP port, and restores the shared clipboard channel.
- **3D acceleration row.** The row says the switch is set and names the kernel driver of the host's GPU render node, or says the host has none or that QEMU could not open it. The definition cannot show whether QEMU turned virgl on; inside a Linux guest, the kernel log line `[drm] features: +virgl` does.
- **Network row.** The row names what each card is plugged into, read from the definition and the libvirt network: a NAT, isolated or routed network, a host bridge, macvtap on a host interface, or user-mode networking. What the guest can reach from there also depends on the host's firewall, so the row does not guess it.
- **3D on a virt-manager display.** The picture is rendered on the GPU and then copied to the viewer, which can tear. The own-window display avoids the copy.
- **System VMs.** A VM under the system connection runs as another user and cannot open a window in the desktop session, so its Display switch is fixed.
- **Sound card models.** Only the Intel HDA cards (`ich6`, `ich9`) take a codec, so only they have a Microphone choice; AC97, ES1370 and virtio sound always have an input, USB audio and SB16 never do. Changing the model keeps the input where the new card can have one and drops the card's PCI address, which libvirt assigns again. Sound card off removes the card.
- **Host sound.** A chosen sound system stays when the display changes, with its output, input and latency, except SPICE sound, which only exists with the SPICE display: switching to an own window replaces it with the desktop session's PipeWire or PulseAudio. PipeWire gets the session's runtime folder (`runtimeDir`). A system VM's QEMU runs as another user and reaches the desktop's PipeWire only if it is allowed to open its socket, which the row says. Changing the sound system drops the options that belong to the old one (device names, latency) and keeps the sample rate.
- **Plays to and Records from.** The lists come from `pactl` on the host, which answers for PipeWire (through pipewire-pulse) and PulseAudio; an output's monitor is not offered as an input.

## What each setting opens to the host

A row whose current value opens something to the host carries an icon; hovering it says what. ⚠ marks a channel that crosses by design or a large body of host code the guest drives; ⓘ marks a smaller exposure, or one the host's own firewall bounds. A closed value (card off, no microphone, 3D off, clipboard off, network isolated or unplugged) carries none. `convirt show VM` prints the same notes, marked `!` and `i`.

| Where | When | What it opens |
|---|---|---|
| Beside the VM | Session VM | ⚠ QEMU runs as the desktop user, so a guest escape reaches what that account can reach. A system VM's QEMU runs as a separate account. |
| 3D acceleration | On | ⚠ The guest's GL command streams are parsed by host code (virglrenderer) and passed to the host GPU driver. |
| Display | Own window | ⓘ QEMU joins the desktop session as a client; ⚠ on X11, where any client can read input to other windows and capture them. |
| Microphone | Input on, with PipeWire or PulseAudio | ⚠ The guest can record from the host input. With SPICE it records only what the viewer sends (ⓘ). |
| Host sound | PipeWire on a system VM | ⚠ The system's QEMU user is let into the session's PipeWire, whose session manager (WirePlumber) gives native clients every node, cameras and screen-share streams included. On a session VM, QEMU has the same access as the user's own apps (ⓘ). |
| Sound card | SB16, ES1370, AC97 | ⓘ Older emulated hardware: more legacy device code than Intel HDA or virtio. |
| Shared clipboard | On | ⚠ Synced both ways while a viewer is open; the same agent channel carries drag-and-drop file transfer. |
| Network | NAT or routed network | ⓘ The guest reaches the LAN through the host, and the host itself on the bridge address; the host firewall decides which host services answer. |
| Network | Isolated network | ⓘ Nothing leaves the host, but the guest still reaches the host itself. |
| Network | Host bridge, macvtap, or a bridged libvirt network | ⚠ The guest is a full peer on the LAN. |
| Network | User-mode | ⚠ Services the host binds only to localhost are reachable from the guest at its gateway address, which SLIRP maps to the host's loopback. |
| Also attached | Shared folders, host devices, USB redirection | ⚠ Host folders the guest can read and write; host hardware handed to the guest. |

What the notes cannot show is the host's own confinement of QEMU: SELinux or AppArmor labels and seccomp, applied by libvirt where the distribution enables them. For a VM that exists to contain untrusted software, the closed values throughout, on a system VM, keep the most between the guest and the host.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests cover the switch logic and the change and undo flow on sample definitions. They do not start a VM.

## License

MIT. See `LICENSE`.
