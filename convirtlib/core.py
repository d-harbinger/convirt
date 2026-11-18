"""Read and change the switchable parts of a libvirt domain definition.

Everything here works on an ElementTree of the domain XML and touches
neither libvirt nor the screen, so it can be tested on plain files.

The five switches:

    mic      on | off          the guest has a recording device
    sound    on | off          the guest has a sound card
    accel3d  on | off          the guest renders on the host GPU (virgl)
    display  sdl | gtk | spice own window (two kinds) | virt-manager
    network  on | off          virtual network cable plugged in

The two own-window displays differ in how they take the pointer. The SDL
window grabs it whenever it is inside the window and lets go at the edge.
The GTK window grabs it only after a click. libvirt has no GTK display
type, so that one is passed to QEMU as a raw "-display" argument.
"""
import os
import xml.etree.ElementTree as ET

QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"
ET.register_namespace("qemu", QEMU_NS)
ET.register_namespace(
    "libosinfo", "http://libosinfo.org/xmlns/libvirt/domain/1.0")

SWITCHES = ("mic", "sound", "accel3d", "display", "clipboard", "network")
TITLES = {
    "mic": "Microphone",
    "sound": "Sound output",
    "accel3d": "3D acceleration",
    "display": "Display",
    "clipboard": "Shared clipboard",
    "network": "Network",
}
CLIPBOARD_CHANNEL = "com.redhat.spice.0"
DISPLAY_LABELS = {
    "sdl": "Own window (SDL)",
    "gtk": "Own window (GTK)",
    "spice": "virt-manager",
}
OWN_WINDOW = ("sdl", "gtk")

# Sound models whose codec can be swapped for a playback-only one.
ICH = ("ich6", "ich9")
GL_DEVICE = {"virtio-vga": "virtio-vga-gl", "virtio-gpu": "virtio-gpu-gl"}
PLAIN_DEVICE = {gl: plain for plain, gl in GL_DEVICE.items()}
# Variables handed to QEMU so its window can reach the desktop session.
SESSION_ENV = ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DISPLAY", "XAUTHORITY")
_CMDLINE = "{%s}commandline" % QEMU_NS
_ARG = "{%s}arg" % QEMU_NS
_ENV = "{%s}env" % QEMU_NS


def parse(text):
    return ET.fromstring(text)


def serialize(root):
    return ET.tostring(root, encoding="unicode")


def session_facts(env=None):
    """Describe the desktop session this process runs in."""
    env = os.environ if env is None else env
    runtime = env.get("XDG_RUNTIME_DIR", "")
    facts = {
        "runtime": runtime,
        "wayland": env.get("WAYLAND_DISPLAY", ""),
        "display": env.get("DISPLAY", ""),
        "xauth": env.get("XAUTHORITY", ""),
        "audio": "none",
    }
    if runtime and os.path.exists(os.path.join(runtime, "pipewire-0")):
        facts["audio"] = "pipewire"
    elif runtime and os.path.exists(os.path.join(runtime, "pulse", "native")):
        facts["audio"] = "pulseaudio"
    return facts


# ---------------------------------------------------------------- reading

def _devices(root):
    devices = root.find("devices")
    if devices is None:
        devices = ET.SubElement(root, "devices")
    return devices


def _graphics(root):
    return _devices(root).findall("graphics")


def _display_arg(root):
    """Return the value of a raw "-display" QEMU argument, if one is set."""
    cmdline = root.find(_CMDLINE)
    if cmdline is None:
        return None
    args = [a.get("value") for a in cmdline.findall(_ARG)]
    for index, arg in enumerate(args[:-1]):
        if arg == "-display":
            return args[index + 1]
    return None


def _set_display_arg(root, value):
    """Set or (with None) remove the raw "-display" QEMU argument."""
    cmdline = root.find(_CMDLINE)
    if cmdline is not None:
        args = cmdline.findall(_ARG)
        for index, arg in enumerate(args[:-1]):
            if arg.get("value") == "-display":
                cmdline.remove(arg)
                cmdline.remove(args[index + 1])
                break
    if value is not None:
        if cmdline is None:
            cmdline = ET.SubElement(root, _CMDLINE)
        # Arguments come before environment variables in the schema.
        position = len(cmdline.findall(_ARG))
        for offset, text in enumerate(("-display", value)):
            arg = ET.Element(_ARG, {"value": text})
            cmdline.insert(position + offset, arg)
    elif cmdline is not None and len(cmdline) == 0:
        root.remove(cmdline)


def display_kind(root):
    """Return sdl, gtk, spice, another graphics type, or none."""
    types = [g.get("type") for g in _graphics(root)]
    for kind in ("sdl", "spice"):
        if kind in types:
            return kind
    arg = _display_arg(root)
    if arg and arg.split(",")[0] == "gtk":
        return "gtk"
    others = [t for t in types if t != "egl-headless"]
    return others[0] if others else "none"


def _has_gl_display(root):
    arg = _display_arg(root)
    if arg and "gl=on" in arg.split(","):
        return True
    for g in _graphics(root):
        if g.get("type") == "egl-headless":
            return True
        gl = g.find("gl")
        if gl is not None and gl.get("enable", "yes") == "yes":
            return True
    return False


def _video_model(root):
    return _devices(root).find("video/model")


def _accel_requested(root):
    model = _video_model(root)
    if model is None or model.get("type") != "virtio":
        return False
    accel = model.find("acceleration")
    if accel is None or accel.get("accel3d") != "yes":
        return False
    device = model.get("device")
    return device is None or device in PLAIN_DEVICE


def _sound_has_input(sound):
    model = sound.get("model")
    if model == "pcspk":
        return False
    if model in ICH:
        codecs = sound.findall("codec")
        return not codecs or any(
            c.get("type") in ("duplex", "micro") for c in codecs)
    return True


def read_state(root, host=None):
    """Return {switch: {value, enabled, meaning}} for one definition.

    host holds what only the machine running the VM can say, gathered by
    the manager: "networks", the forward mode of each libvirt network, and
    "render", the GPU render nodes as dicts of node, driver and usable.
    Missing keys leave those parts of the meanings unsaid.
    """
    host = host or {}
    state = {}
    sounds = _devices(root).findall("sound")

    mic_on = any(_sound_has_input(s) for s in sounds)
    fixed = [s.get("model") for s in sounds
             if s.get("model") not in ICH + ("pcspk",)]
    if not sounds:
        mic = ("off", False, "no sound card")
    elif fixed:
        mic = ("on", False, "sound model %s cannot drop its input" % fixed[0])
    elif mic_on:
        mic = ("on", True, "guest can record from host microphone")
    else:
        mic = ("off", True, "guest has no recording device")
    state["mic"] = mic

    if sounds:
        state["sound"] = ("on", True, "plays through host")
    else:
        state["sound"] = ("off", True, "no sound card")

    kind = display_kind(root)
    render = host.get("render")
    usable = [r for r in render or () if r["usable"]]
    if _accel_requested(root) and _has_gl_display(root):
        if render is not None and not render:
            state["accel3d"] = ("on", True,
                                "set, but this host has no GPU render node")
        elif render is not None and not usable:
            state["accel3d"] = ("on", True,
                                "set, but %s cannot be opened" %
                                render[0]["node"])
        else:
            meaning = "set for the host GPU"
            if usable:
                meaning += " (%s)" % usable[0]["driver"]
            if kind not in OWN_WINDOW:
                meaning += "; picture is copied, may tear"
            state["accel3d"] = ("on", True, meaning)
    elif _accel_requested(root):
        state["accel3d"] = ("off", True,
                            "requested, but no OpenGL display; software")
    elif render is not None and not render:
        state["accel3d"] = ("off", False, "this host has no GPU render node")
    else:
        state["accel3d"] = ("off", True, "software rendering")

    if kind == "sdl":
        state["display"] = ("sdl", True,
                            "pointer held inside; Ctrl+Alt+G releases")
    elif kind == "gtk":
        state["display"] = ("gtk", True,
                            "click to hold pointer; Ctrl+Alt+G releases")
    elif kind == "spice":
        state["display"] = ("spice", True,
                            "inside virt-manager")
    else:
        state["display"] = (kind, True, "display type: %s" % kind)

    if kind != "spice":
        state["clipboard"] = ("off", False, "not shared with this display")
    elif _clipboard_channels(root):
        state["clipboard"] = ("on", True, "host and guest share it")
    else:
        state["clipboard"] = ("off", True, "not shared")

    interfaces = _devices(root).findall("interface")
    modes = host.get("networks", {})
    if not interfaces:
        state["network"] = ("off", False, "no network card")
    elif all(_link_down(i) for i in interfaces):
        state["network"] = ("off", True, "cable unplugged")
    else:
        plugged = [attachment(i, modes) for i in interfaces
                   if not _link_down(i)]
        state["network"] = ("on", True, "plugged into " + "; ".join(plugged))

    return {name: {"value": v, "enabled": e, "meaning": m}
            for name, (v, e, m) in state.items()}


# What libvirt's forward mode makes of a virtual network, as a label.
NETWORK_KINDS = {
    "nat": "NAT network",
    "isolated": "isolated network",
    "route": "routed network",
    "open": "open network",
    "bridge": "bridged network",
    "passthrough": "passthrough network",
    "private": "private network",
    "vepa": "VEPA network",
    "hostdev": "host-device network",
}


def attachment(interface, modes=None):
    """Name what one network card is plugged into, from the definition.

    modes maps a libvirt network's name to its forward mode ("isolated"
    when it has none); without it a network is named by type alone.
    """
    kind = interface.get("type")
    source = interface.find("source")
    source = {} if source is None else source.attrib
    if kind == "network":
        name = source.get("network", "?")
        mode = (modes or {}).get(name)
        return "%s %s" % (NETWORK_KINDS.get(mode, "network"), name)
    if kind == "bridge":
        return "host bridge %s" % source.get("bridge", "?")
    if kind == "direct":
        return "macvtap on %s" % source.get("dev", "?")
    if kind == "user":
        return "user-mode networking"
    return "%s interface" % kind


def values(root):
    return {name: s["value"] for name, s in read_state(root).items()}


def _clipboard_channels(root):
    return [c for c in _devices(root).findall("channel")
            if c.get("type") == "spicevmc"
            and c.find("target") is not None
            and c.find("target").get("name") == CLIPBOARD_CHANNEL]


def exposures(root):
    """Name what links the guest to the host and has no switch here."""
    devices = _devices(root)
    found = []
    for tag, one, many in (
            ("filesystem", "shared folder", "shared folders"),
            ("hostdev", "host device passed through",
             "host devices passed through"),
            ("redirdev", "USB redirection port", "USB redirection ports")):
        count = len(devices.findall(tag))
        if count:
            found.append("%d %s" % (count, one if count == 1 else many))
    return found


def raw_fragments(root):
    """Return the XML behind the switches, as text."""
    parts = []
    devices = _devices(root)
    for tag in ("sound", "audio", "video", "graphics", "interface"):
        for element in devices.findall(tag):
            parts.append(_pretty(element))
    cmdline = root.find("{%s}commandline" % QEMU_NS)
    if cmdline is not None:
        parts.append(_pretty(cmdline))
    return "\n".join(parts)


def _pretty(element):
    copy = ET.fromstring(ET.tostring(element))
    ET.indent(copy)
    text = ET.tostring(copy, encoding="unicode")
    return text.replace(' xmlns:qemu="%s"' % QEMU_NS, "")


# --------------------------------------------------------------- changing

def apply(root, switch, value, facts=None):
    """Change one switch in place. Raise ValueError if it cannot be done."""
    if switch == "mic":
        _set_mic(root, _on(value))
    elif switch == "sound":
        _set_sound(root, _on(value), facts)
    elif switch == "accel3d":
        _set_accel(root, _on(value))
    elif switch == "display":
        _set_display(root, value, facts)
    elif switch == "clipboard":
        _set_clipboard(root, _on(value))
    elif switch == "network":
        _set_network(root, _on(value))
    else:
        raise ValueError("unknown switch: %s" % switch)


def _on(value):
    if value not in ("on", "off"):
        raise ValueError("value must be on or off")
    return value == "on"


def _set_mic(root, on):
    sounds = _devices(root).findall("sound")
    if not sounds:
        raise ValueError("no sound card; turn sound output on first")
    for sound in sounds:
        model = sound.get("model")
        if model == "pcspk":
            continue
        if model not in ICH:
            raise ValueError(
                "sound model %s cannot drop its input" % model)
        for codec in sound.findall("codec"):
            sound.remove(codec)
        if not on:
            ET.SubElement(sound, "codec", {"type": "output"})


def _set_sound(root, on, facts):
    devices = _devices(root)
    sounds = devices.findall("sound")
    if not on:
        for sound in sounds:
            devices.remove(sound)
        return
    if sounds:
        return
    # A new card starts playback-only: the microphone is its own switch.
    sound = ET.SubElement(devices, "sound", {"model": "ich9"})
    ET.SubElement(sound, "codec", {"type": "output"})
    if devices.find("audio") is None:
        _ensure_audio(root, display_kind(root), facts)


def _set_accel(root, on):
    devices = _devices(root)
    model = _video_model(root)
    if on:
        if model is None:
            video = ET.SubElement(devices, "video")
            model = ET.SubElement(video, "model")
        if model.get("type") != "virtio":
            model.attrib.clear()
            model.attrib.update(
                {"type": "virtio", "heads": "1", "primary": "yes"})
            for child in list(model):
                model.remove(child)
        accel = model.find("acceleration")
        if accel is None:
            accel = ET.SubElement(model, "acceleration")
        accel.set("accel3d", "yes")
        device = model.get("device")
        if device in GL_DEVICE:
            model.set("device", GL_DEVICE[device])
    elif model is not None:
        for accel in model.findall("acceleration"):
            model.remove(accel)
        device = model.get("device")
        if device in PLAIN_DEVICE:
            model.set("device", PLAIN_DEVICE[device])
    _sync_gl_display(root, on)


def _sync_gl_display(root, on):
    """Make the display side match the 3D switch."""
    devices = _devices(root)
    kind = display_kind(root)
    for g in _graphics(root):
        if g.get("type") == "egl-headless":
            devices.remove(g)
        elif g.get("type") in ("sdl", "spice"):
            for gl in g.findall("gl"):
                g.remove(gl)
    if kind == "sdl":
        sdl = next(g for g in _graphics(root) if g.get("type") == "sdl")
        ET.SubElement(sdl, "gl", {"enable": "yes" if on else "no"})
    elif kind == "gtk":
        _set_display_arg(root, "gtk,gl=on" if on else "gtk,gl=off")
    elif on:
        # Any other display gets a headless OpenGL renderer beside it.
        ET.SubElement(devices, "graphics", {"type": "egl-headless"})


def _set_display(root, kind, facts):
    if kind not in DISPLAY_LABELS:
        raise ValueError("display must be sdl, gtk or spice")
    devices = _devices(root)
    accel = _accel_requested(root)

    wayland = False
    if kind in OWN_WINDOW:
        facts = session_facts() if facts is None else facts
        wayland = bool(facts["wayland"] and facts["runtime"])
        if not wayland and not facts["display"]:
            raise ValueError(
                "no desktop session found; run this inside the desktop")

    for g in _graphics(root):
        devices.remove(g)
    for tag in ("channel", "redirdev", "smartcard"):
        for dev in devices.findall(tag):
            if dev.get("type") == "spicevmc":
                devices.remove(dev)
    for name in SESSION_ENV:
        _set_env(root, name, None)
    _set_display_arg(root, None)

    if kind in OWN_WINDOW:
        if kind == "sdl":
            window = ET.SubElement(devices, "graphics", {"type": "sdl"})
        else:
            _set_display_arg(root, "gtk,gl=off")
        if wayland:
            _set_env(root, "WAYLAND_DISPLAY", facts["wayland"])
        elif kind == "sdl":
            window.set("display", facts["display"])
            if facts["xauth"]:
                window.set("xauth", facts["xauth"])
        else:
            _set_env(root, "DISPLAY", facts["display"])
            if facts["xauth"]:
                _set_env(root, "XAUTHORITY", facts["xauth"])
        if facts["runtime"]:
            _set_env(root, "XDG_RUNTIME_DIR", facts["runtime"])
    else:
        # A private socket, not a TCP port: only virt-manager attaches.
        spice = ET.SubElement(devices, "graphics", {"type": "spice"})
        ET.SubElement(spice, "listen", {"type": "none"})
        ET.SubElement(spice, "image", {"compression": "off"})
        channel = ET.SubElement(devices, "channel", {"type": "spicevmc"})
        ET.SubElement(channel, "target",
                      {"type": "virtio", "name": "com.redhat.spice.0"})

    _sync_gl_display(root, accel)
    _ensure_audio(root, kind, facts)


def _ensure_audio(root, kind, facts):
    devices = _devices(root)
    for audio in devices.findall("audio"):
        devices.remove(audio)
    if kind == "spice":
        ET.SubElement(devices, "audio", {"id": "1", "type": "spice"})
    elif kind in OWN_WINDOW:
        facts = session_facts() if facts is None else facts
        audio = ET.SubElement(
            devices, "audio", {"id": "1", "type": facts["audio"]})
        if facts["audio"] == "pipewire":
            audio.set("runtimeDir", facts["runtime"])


def _set_env(root, name, value):
    tag = _CMDLINE
    env_tag = _ENV
    cmdline = root.find(tag)
    if cmdline is not None:
        for old in cmdline.findall(env_tag):
            if old.get("name") == name:
                cmdline.remove(old)
    if value is not None:
        if cmdline is None:
            cmdline = ET.SubElement(root, tag)
        ET.SubElement(cmdline, env_tag, {"name": name, "value": value})
    elif cmdline is not None and len(cmdline) == 0:
        root.remove(cmdline)


def _set_clipboard(root, on):
    devices = _devices(root)
    if display_kind(root) != "spice":
        if on:
            raise ValueError(
                "the clipboard is only shared with the virt-manager display")
        return
    for channel in _clipboard_channels(root):
        devices.remove(channel)
    if on:
        channel = ET.SubElement(devices, "channel", {"type": "spicevmc"})
        ET.SubElement(channel, "target",
                      {"type": "virtio", "name": CLIPBOARD_CHANNEL})


def _link_down(interface):
    link = interface.find("link")
    return link is not None and link.get("state") == "down"


def _set_network(root, on):
    interfaces = _devices(root).findall("interface")
    if not interfaces:
        raise ValueError("no network card")
    for interface in interfaces:
        for link in interface.findall("link"):
            interface.remove(link)
        if not on:
            ET.SubElement(interface, "link", {"state": "down"})
