"""Read and change the switchable parts of a libvirt domain definition.

Everything here works on an ElementTree of the domain XML and touches
neither libvirt nor the screen, so it can be tested on plain files.

The switches:

    sound     off | MODEL       the sound card, by model (on: ich9)
    mic       off | on | micro  the card's input: none, line in (QEMU's
                                default hda-duplex) or a microphone
    audio     default | TYPE    the host sound system QEMU plays into
    speaker   default | NAME    the host output it plays to
    recorder  default | NAME    the host input it records from
    latency   default | MS      the latency asked of PipeWire or Pulse
    rate      default | HZ      a fixed sample rate, both ways
    accel3d   on | off          the guest renders on the host GPU (virgl)
    display   sdl | gtk | spice own window (two kinds) | virt-manager
    clipboard on | off          host and guest share the clipboard
    network   off | on | KEY    what the network card is plugged into

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

SWITCHES = ("sound", "mic", "audio", "speaker", "recorder", "latency",
            "rate", "accel3d", "display", "screens", "clipboard", "network")
# Video models that take a number of screens (libvirt's heads).
MULTIHEAD_MODELS = ("virtio", "qxl")
MAX_SCREENS = 4
# The card first, then the rows that only mean something with a card.
SOUND_ROWS = SWITCHES[:7]
TITLES = {
    "sound": "Sound card",
    "mic": "Microphone",
    "audio": "Host sound",
    "speaker": "Plays to",
    "recorder": "Records from",
    "latency": "Latency",
    "rate": "Sample rate",
    "accel3d": "3D acceleration",
    "display": "Display",
    "screens": "Screens",
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
# The sound cards QEMU emulates, as the Sound card switch offers them, and
# what each one gives the guest. Which cards record was read from QEMU
# itself: USB audio and SB16 only open an output voice.
SOUND_MODELS = (
    ("ich9", "Intel HDA (ICH9)"),
    ("ich6", "Intel HDA (ICH6)"),
    ("ac97", "AC97"),
    ("usb", "USB audio"),
    ("virtio", "virtio sound"),
    ("es1370", "Ensoniq ES1370"),
    ("sb16", "Sound Blaster 16"),
)
SOUND_NOTES = {
    "ich9": "plays and records; the usual choice",
    "ich6": "plays and records; the older Intel HDA",
    "ac97": "plays and records; for older guests",
    "usb": "plays only; a USB sound device",
    "virtio": "plays and records; Linux 5.13+, QEMU 8.2+",
    "es1370": "plays and records; for older guests",
    "sb16": "plays only; for DOS-era guests",
    "pcspk": "PC speaker beeps only",
}
RECORDING_MODELS = ICH + ("ac97", "es1370", "virtio")
MIC_LABELS = (
    ("off", "None (hda-output)"),
    ("micro", "Microphone (hda-micro)"),
    ("on", "Line in (hda-duplex)"),
)
# The host sound systems libvirt can hand QEMU's sound to, and the QEMU
# module each one needs where QEMU is split into modules.
AUDIO_TYPES = (
    ("pipewire", "PipeWire"),
    ("pulseaudio", "PulseAudio"),
    ("spice", "SPICE, to the viewer"),
    ("alsa", "ALSA"),
    ("sdl", "SDL"),
    ("jack", "JACK"),
    ("oss", "OSS"),
    ("dbus", "D-Bus"),
    ("none", "None, silent"),
)
AUDIO_MODULES = {
    "pipewire": "audio-pipewire", "pulseaudio": "audio-pa",
    "spice": "audio-spice", "alsa": "audio-alsa", "sdl": "audio-sdl",
    "jack": "audio-jack", "oss": "audio-oss", "dbus": "audio-dbus",
}
# Sound systems that take a host output and input by name and a latency.
NAMED_AUDIO = ("pipewire", "pulseaudio")
LATENCIES = ("10", "20", "50", "100", "200")
RATES = (("44100", "44.1 kHz"), ("48000", "48 kHz"))
# Attributes that belong to one sound system and go when it changes.
_AUDIO_OWN = ("runtimeDir", "serverName", "tryMMap", "exclusive",
              "dspPolicy")
_STREAM_OWN = ("name", "streamName", "latency", "dev", "bufferCount",
               "tryPoll", "serverName", "clientName", "connectPorts",
               "exactName")
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


def gl_uses(root):
    """The parts of QEMU the 3D path of this definition needs, by name."""
    uses = {"virgl"}
    model = _video_model(root)
    if model is not None and model.get("device") == "virtio-vga-gl":
        uses.add("virtio-vga-gl")
    kind = display_kind(root)
    if kind in ("sdl", "gtk", "spice"):
        uses.add(kind)
    if any(g.get("type") == "egl-headless" for g in _graphics(root)):
        uses.add("egl-headless")
    return uses


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


def _sound_card(root):
    """The card the sound switches change: the first that is not a PC
    speaker, or None."""
    cards = [s for s in _devices(root).findall("sound")
             if s.get("model") != "pcspk"]
    return cards[0] if cards else None


def _mic_value(sound):
    """off, on (a line in) or micro (a microphone) for one card."""
    model = sound.get("model")
    if model in ICH:
        types = [c.get("type") for c in sound.findall("codec")]
        if not types:
            return "on"  # QEMU attaches hda-duplex
        if "micro" in types:
            return "micro"
        return "on" if "duplex" in types else "off"
    if model == "virtio":
        return "on" if sound.get("streams", "2") != "1" else "off"
    return "on" if model in RECORDING_MODELS else "off"


def _audio_backend(root):
    """The audio element the sound card plays into, or None when libvirt
    picks one itself."""
    audios = _devices(root).findall("audio")
    card = _sound_card(root)
    link = card.find("audio") if card is not None else None
    if link is not None:
        for audio in audios:
            if audio.get("id") == link.get("id"):
                return audio
    return audios[0] if audios else None


def _default_audio(root):
    """What libvirt picks with no audio element: SPICE for a SPICE display,
    otherwise nothing."""
    return "spice" if display_kind(root) == "spice" else "none"


def _stream(audio, direction):
    """The input or output element of an audio backend, or None."""
    return None if audio is None else audio.find(direction)


def _device_label(name, devices):
    for key, label in devices or ():
        if key == name:
            return label
    return name


def _sound_state(root, host, state):
    card = _sound_card(root)
    cards = [s for s in _devices(root).findall("sound")
             if s.get("model") != "pcspk"]
    if card is None:
        for name in SOUND_ROWS:
            state[name] = ("off" if name in ("sound", "mic") else "default",
                           name == "sound", "no sound card")
        return
    model = card.get("model")
    meaning = SOUND_NOTES.get(model, "model %s" % model)
    if len(cards) > 1:
        meaning += "; %d cards, the first one switches" % len(cards)
    state["sound"] = (model, True, meaning)

    audio = _audio_backend(root)
    kind = audio.get("type") if audio is not None else None
    effective = kind or _default_audio(root)
    system = host.get("system", False)
    if kind is None:
        audio_meaning = "libvirt's choice for this display: %s" % effective
    elif kind == "pipewire":
        audio_meaning = "this desktop's PipeWire"
        if system:
            audio_meaning += ("; QEMU runs as the system's user and needs "
                              "leave to open this PipeWire")
    elif kind == "pulseaudio":
        audio_meaning = "this desktop's PulseAudio (or pipewire-pulse)"
    elif kind == "spice":
        audio_meaning = "sent to the viewer; silent while none is open"
        if display_kind(root) != "spice":
            audio_meaning = "SPICE, but this display is not SPICE; silent"
    elif kind == "none":
        audio_meaning = "goes nowhere; the guest is silent"
    elif kind == "alsa":
        audio_meaning = "straight to an ALSA device, past the desktop mixer"
    else:
        audio_meaning = "through %s" % dict(AUDIO_TYPES).get(kind, kind)
    available = host.get("audio_types")
    if kind and available is not None and kind != "none" \
            and kind not in available:
        audio_meaning = "QEMU's %s module is not installed; silent" % (
            AUDIO_MODULES.get(kind, kind))
    state["audio"] = (kind or "default", True, audio_meaning)

    named = effective in NAMED_AUDIO
    output = _stream(audio, "output")
    sink = output.get("name") if output is not None else None
    if named:
        speaker = ("the host's default output" if not sink else
                   _device_label(sink, host.get("sinks")))
    elif effective == "spice":
        speaker = "the viewer's speakers"
    elif effective == "none":
        speaker = "nothing"
    else:
        speaker = "chosen by %s" % dict(AUDIO_TYPES).get(effective, effective)
    state["speaker"] = (sink or "default", named, speaker)

    mic = _mic_value(card)
    source_element = _stream(audio, "input")
    source = source_element.get("name") \
        if source_element is not None else None
    if named:
        hears = ("the host's default input" if not source else
                 _device_label(source, host.get("sources")))
    elif effective == "spice":
        hears = "the viewer's microphone, if it sends one"
    elif effective == "none":
        hears = "silence"
    else:
        hears = "what %s records" % dict(AUDIO_TYPES).get(
            effective, effective)
    if mic == "off":
        mic_meaning = ("plays only; the guest has no input" if model in ICH
                       else "%s has no input" % dict(SOUND_MODELS).get(
                           model, model))
    else:
        device = {"micro": "a microphone", "on": "a line in"}[mic] \
            if model in ICH else "an input"
        mic_meaning = "%s; hears %s" % (device, hears)
    state["mic"] = (mic, model in ICH, mic_meaning)
    state["recorder"] = (source or "default", named and mic != "off",
                         hears if mic != "off" else "no input on the card")

    latency = output.get("latency") if output is not None else None
    if latency and latency.isdigit():
        value = str(int(latency) // 1000)
        latency_meaning = ("%s ms; lower answers sooner, too low crackles"
                           % value)
    else:
        value = "default"
        latency_meaning = "the sound system's default"
    if not named:
        latency_meaning = "only PipeWire and PulseAudio take a latency"
    state["latency"] = (value, named, latency_meaning)

    settings = output.find("settings") if output is not None else None
    if output is not None and output.get("fixedSettings") == "yes" \
            and settings is not None and settings.get("frequency"):
        rate = settings.get("frequency")
        rate_meaning = "fixed at %s Hz both ways" % rate
    else:
        rate = "default"
        rate_meaning = "QEMU converts to the rate the guest picks"
    state["rate"] = (rate, True, rate_meaning)


def sound_choices(root, host=None):
    """[(value, label)] for each sound chooser, from what the host has."""
    host = host or {}
    available = host.get("audio_types")
    choices = {"sound": [("off", "Off")] + list(SOUND_MODELS)}
    card = _sound_card(root)
    if card is None:
        for name in SOUND_ROWS[1:]:
            value = "off" if name == "mic" else "default"
            choices[name] = [(value, "No card")]
        return choices
    model = card.get("model")
    if model in ICH:
        choices["mic"] = list(MIC_LABELS)
    else:
        value = _mic_value(card)
        choices["mic"] = [(value, "Fixed: input" if value == "on"
                           else "Fixed: none")]
    audio = []
    if _audio_backend(root) is None:
        audio.append(("default", "libvirt's choice"))
    for kind, label in AUDIO_TYPES:
        if kind == "spice" and display_kind(root) != "spice":
            continue
        if available is not None and kind != "none" \
                and kind not in available:
            label += " (not installed)"
        audio.append((kind, label))
    choices["audio"] = audio
    choices["speaker"] = [("default", "Host default output")] + list(
        host.get("sinks") or ())
    choices["recorder"] = [("default", "Host default input")] + list(
        host.get("sources") or ())
    choices["latency"] = [("default", "Default")] + [
        (ms, "%s ms" % ms) for ms in LATENCIES]
    choices["rate"] = [("default", "Guest's choice")] + list(RATES)
    return choices


def screen_choices():
    return [(str(n), screens_label(str(n)))
            for n in range(1, MAX_SCREENS + 1)]


def screens_label(value):
    return "1 screen" if value == "1" else "%s screens" % value


def _screens_state(root, kind):
    """(value, enabled, meaning) for the number of screens the guest sees."""
    model = _video_model(root)
    if model is None or model.get("type") not in MULTIHEAD_MODELS:
        kind_name = "none" if model is None else model.get("type")
        return ("1", False, "video model %s has one screen" % kind_name)
    heads = model.get("heads", "1")
    if not heads.isdigit() or int(heads) < 1:
        heads = "1"
    if heads == "1":
        return ("1", True, "one screen")
    if kind == "spice":
        meaning = "Open shows each; virt-manager only the first"
    elif kind in OWN_WINDOW:
        meaning = "how QEMU's own window shows the extra ones is untested"
    else:
        meaning = "the viewer decides how they show"
    return (heads, True, "%s; %s" % (screens_label(heads), meaning))


def _set_screens(root, value):
    if not value.isdigit() or not 1 <= int(value) <= MAX_SCREENS:
        raise ValueError("screens takes 1 to %d" % MAX_SCREENS)
    model = _video_model(root)
    if model is None or model.get("type") not in MULTIHEAD_MODELS:
        raise ValueError("only a virtio or QXL video model has more than "
                         "one screen")
    model.set("heads", value)


def value_label(switch, value):
    """A sound or screens value as the window names it, without the host;
    used for what a running VM has, which may not be on today's lists."""
    if switch == "screens":
        return screens_label(value)
    tables = {
        "sound": [("off", "Off")] + list(SOUND_MODELS),
        "mic": list(MIC_LABELS),
        "audio": [("default", "libvirt's choice")] + list(AUDIO_TYPES),
        "speaker": [("default", "Host default output")],
        "recorder": [("default", "Host default input")],
        "latency": [("default", "Default")],
        "rate": [("default", "Guest's choice")] + list(RATES),
    }
    label = dict(tables.get(switch, ())).get(value)
    if label is None and switch == "latency":
        return "%s ms" % value
    return label or value


# How strongly a row's current value opens the host: a caution where a
# channel opens or the guest drives a large body of host code, a note where
# the exposure is small or bounded by the host's own firewall.
CAUTION, NOTE = "caution", "note"
LEGACY_SOUND = ("sb16", "es1370", "ac97")


def cautions(root, host=None):
    """{row: (level, text)} for each row whose current value opens
    something to the host. Besides the switches, "vm" speaks for the VM
    as a whole and "attached" for the devices that have no switch.

    host is read_state's, plus "wayland": whether the desktop the window
    would open on is Wayland. The texts describe the setting, never the
    machine, so they hold on any host.
    """
    host = host or {}
    state = read_state(root, host)
    value = {name: entry["value"] for name, entry in state.items()}
    found = {}

    if host.get("system") is False:
        found["vm"] = (CAUTION, "Session VM: QEMU runs as your user. A guest "
                       "escape reaches what your account can reach.")

    if value["accel3d"] == "on":
        found["accel3d"] = (CAUTION, "Guest GL commands are parsed by host "
                            "code (virglrenderer) and passed to the host "
                            "GPU driver: the most host code the guest "
                            "drives here.")

    if value["display"] in OWN_WINDOW:
        if host.get("wayland") is False:
            found["display"] = (CAUTION, "On X11 any client, QEMU's window "
                                "too, can read input to other windows and "
                                "capture them.")
        else:
            found["display"] = (NOTE, "QEMU joins your desktop session as a "
                                "client; this needs a session VM.")

    if value["sound"] != "off":
        effective = value["audio"]
        if effective == "default":
            effective = _default_audio(root)
        if value["mic"] != "off":
            if effective in NAMED_AUDIO:
                found["mic"] = (CAUTION, "The guest can record from the "
                                "host input.")
            elif effective == "spice":
                found["mic"] = (NOTE, "The guest records from the viewer's "
                                "microphone when the viewer sends it.")
        if effective == "pipewire" and host.get("system"):
            found["audio"] = (CAUTION, "The system's QEMU user is let into "
                              "your session's PipeWire, which gives native "
                              "clients every node: microphones, cameras, "
                              "screen-share streams.")
        elif effective in NAMED_AUDIO:
            found["audio"] = (NOTE, "QEMU is a client of your sound server, "
                              "with the same access as your own apps.")
        if value["sound"] in LEGACY_SOUND:
            found["sound"] = (NOTE, "Older emulated hardware: more legacy "
                              "device code than Intel HDA or virtio.")

    if value["clipboard"] == "on":
        found["clipboard"] = (CAUTION, "Synced both ways while a viewer is "
                              "open: the guest can read what you copy. The "
                              "same agent channel carries drag-and-drop "
                              "file transfer.")

    network = value["network"]
    kind, _, name = network.partition(":")
    mode = host.get("networks", {}).get(name) if kind == "network" else None
    if kind in ("bridge", "direct") or mode == "bridge":
        found["network"] = (CAUTION, "The guest is a full peer on the LAN.")
    elif kind == "user":
        found["network"] = (CAUTION, "Services the host binds only to "
                            "localhost are reachable from the guest at its "
                            "gateway address.")
    elif mode == "isolated":
        found["network"] = (NOTE, "Nothing leaves the host, but the guest "
                            "still reaches the host itself; the host "
                            "firewall decides which services answer.")
    elif kind == "network":
        found["network"] = (NOTE, "The guest reaches the LAN through the "
                            "host and the host on the bridge address; the "
                            "host firewall decides which host services "
                            "answer.")

    devices = _devices(root)
    parts = []
    if devices.findall("filesystem"):
        parts.append("Host folders the guest can read and write.")
    if devices.findall("hostdev") or devices.findall("redirdev"):
        parts.append("Host hardware handed to the guest.")
    if parts:
        found["attached"] = (CAUTION, " ".join(parts))
    return found


def read_state(root, host=None):
    """Return {switch: {value, enabled, meaning}} for one definition.

    host holds what only the machine running the VM can say, gathered by
    the manager: "networks", the forward mode of each libvirt network;
    "render", the GPU render nodes as dicts of node, driver and usable;
    "sinks" and "sources", the host's outputs and inputs as (name, label);
    "audio_types", the sound systems QEMU has modules for; and "system",
    true for a VM under the system connection. Missing keys leave those
    parts of the meanings unsaid.
    """
    host = host or {}
    state = {}
    _sound_state(root, host, state)

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

    state["screens"] = _screens_state(root, kind)

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
        plugged = [i for i in interfaces if not _link_down(i)]
        meaning = "plugged into " + "; ".join(
            attachment(i, modes) for i in plugged)
        if len(interfaces) > 1:
            meaning += "; %d cards, the first one switches" % len(interfaces)
        state["network"] = (connection_key(plugged[0]), True, meaning)

    return {name: {"value": v, "enabled": e, "meaning": m}
            for name, (v, e, m) in state.items()}


def connection_key(interface):
    """The Network switch's value for a plugged-in card.

    network:NAME, bridge:NAME, direct:DEVICE or user; the Network switch
    takes any of them, "off" to unplug, and "on" to plug back in.
    """
    kind = interface.get("type")
    source = interface.find("source")
    source = {} if source is None else source.attrib
    field = {"network": "network", "bridge": "bridge", "direct": "dev"}
    if kind in field:
        return "%s:%s" % (kind, source.get(field[kind], "?"))
    return kind or "?"


def same_value(switch, current, wanted):
    """Whether a switch already holds a wanted value."""
    if switch == "network" and wanted == "on":
        return current != "off"
    if switch == "sound" and wanted == "on":
        return current != "off"
    return current == wanted


def connection_label(key, modes=None):
    """A Network switch value as the label the window shows."""
    if key == "off":
        return "Unplugged"
    kind, _, name = key.partition(":")
    source = {"network": "network", "bridge": "bridge", "direct": "dev"}
    element = ET.Element("interface", {"type": kind})
    if kind in source:
        ET.SubElement(element, "source", {source[kind]: name})
    text = attachment(element, modes)
    return text[0].upper() + text[1:]


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
        _set_mic(root, value)
    elif switch == "sound":
        _set_sound(root, value, facts)
    elif switch == "audio":
        _set_audio(root, value, facts)
    elif switch == "speaker":
        _set_device(root, "output", value)
    elif switch == "recorder":
        _set_device(root, "input", value)
    elif switch == "latency":
        _set_latency(root, value)
    elif switch == "rate":
        _set_rate(root, value)
    elif switch == "accel3d":
        _set_accel(root, _on(value))
    elif switch == "screens":
        _set_screens(root, value)
    elif switch == "display":
        _set_display(root, value, facts)
    elif switch == "clipboard":
        _set_clipboard(root, _on(value))
    elif switch == "network":
        if value in ("on", "off"):
            _set_network(root, _on(value))
        else:
            _set_connection(root, value)
    else:
        raise ValueError("unknown switch: %s" % switch)


def _on(value):
    if value not in ("on", "off"):
        raise ValueError("value must be on or off")
    return value == "on"


def _set_codec(sound, value):
    """Give an Intel HDA card the codec for off, on (line in) or micro."""
    for codec in sound.findall("codec"):
        sound.remove(codec)
    if value in ("off", "micro"):
        codec = ET.Element("codec", {"type": "output" if value == "off"
                                     else "micro"})
        sound.insert(0, codec)


def _set_mic(root, value):
    if value not in ("off", "on", "micro"):
        raise ValueError("mic takes off, on (line in) or micro")
    card = _sound_card(root)
    if card is None:
        raise ValueError("no sound card; choose one first")
    if card.get("model") not in ICH:
        raise ValueError("the %s card's input is fixed" % dict(
            SOUND_MODELS).get(card.get("model"), card.get("model")))
    _set_codec(card, value)


def _set_sound(root, value, facts):
    devices = _devices(root)
    sounds = devices.findall("sound")
    if value == "off":
        for sound in sounds:
            devices.remove(sound)
        return
    card = _sound_card(root)
    if value == "on":
        if card is not None:
            return
        value = "ich9"
    if value not in dict(SOUND_MODELS):
        raise ValueError("sound takes off, on or a model: %s" % ", ".join(
            m for m, _ in SOUND_MODELS))
    if card is None:
        # A new card starts playback-only: the microphone is its own switch.
        card = ET.SubElement(devices, "sound", {"model": value})
        if value in ICH:
            _set_codec(card, "off")
        if _audio_backend(root) is None:
            _ensure_audio(root, display_kind(root), facts)
        return
    had_input = _mic_value(card) != "off"
    card.set("model", value)
    # A PCI address does not fit a USB card, and the reverse; libvirt
    # gives the card a new one.
    for child in list(card):
        if child.tag == "address" or (child.tag == "codec"
                                      and value not in ICH):
            card.remove(child)
    if value != "virtio":
        card.attrib.pop("streams", None)
    if value != "usb":
        card.attrib.pop("multichannel", None)
    if value in ICH and not card.findall("codec"):
        _set_codec(card, "on" if had_input else "off")


def _backend_for_change(root, what):
    audio = _audio_backend(root)
    if _sound_card(root) is None:
        raise ValueError("no sound card; choose one first")
    kind = audio.get("type") if audio is not None else None
    if kind not in NAMED_AUDIO:
        raise ValueError("%s needs PipeWire or PulseAudio as host sound"
                         % what)
    return audio


def _stream_element(audio, direction):
    element = audio.find(direction)
    if element is None:
        element = ET.Element(direction)
        # input comes before output in libvirt's own order
        position = 0 if direction == "input" else len(audio)
        audio.insert(position, element)
    return element


def _tidy_stream(audio, direction):
    element = audio.find(direction)
    if element is not None and not element.attrib and len(element) == 0:
        audio.remove(element)


def _set_audio(root, value, facts):
    devices = _devices(root)
    if _sound_card(root) is None:
        raise ValueError("no sound card; choose one first")
    if value == "default":
        for audio in devices.findall("audio"):
            devices.remove(audio)
        for sound in devices.findall("sound"):
            for link in sound.findall("audio"):
                sound.remove(link)
        return
    if value not in dict(AUDIO_TYPES):
        raise ValueError("audio takes default or one of: %s" % ", ".join(
            kind for kind, _ in AUDIO_TYPES))
    if value == "spice" and display_kind(root) != "spice":
        raise ValueError("SPICE sound needs the virt-manager display")
    audio = _audio_backend(root)
    if audio is None:
        audio = ET.SubElement(devices, "audio", {"id": "1", "type": value})
    _retype(audio, value, facts)


def _retype(audio, kind, facts):
    """Make an audio backend another type, keeping what all types share."""
    if audio.get("type") != kind:
        for name in _AUDIO_OWN:
            audio.attrib.pop(name, None)
        for direction in ("input", "output"):
            element = audio.find(direction)
            if element is not None:
                for name in _STREAM_OWN:
                    element.attrib.pop(name, None)
                _tidy_stream(audio, direction)
    audio.set("type", kind)
    if kind == "pipewire":
        facts = session_facts() if facts is None else facts
        if facts["runtime"]:
            audio.set("runtimeDir", facts["runtime"])


def _set_device(root, direction, value):
    what = "a host output" if direction == "output" else "a host input"
    audio = _backend_for_change(root, what)
    if direction == "input" and _mic_value(_sound_card(root)) == "off":
        raise ValueError("the card has no input; turn the microphone on")
    element = _stream_element(audio, direction)
    if value == "default":
        element.attrib.pop("name", None)
    else:
        element.set("name", value)
    _tidy_stream(audio, direction)


def _set_latency(root, value):
    if value != "default" and not (value.isdigit() and int(value) > 0):
        raise ValueError("latency takes default or milliseconds")
    audio = _backend_for_change(root, "a latency")
    for direction in ("input", "output"):
        element = _stream_element(audio, direction)
        if value == "default":
            element.attrib.pop("latency", None)
        else:
            element.set("latency", str(int(value) * 1000))
        _tidy_stream(audio, direction)


def _set_rate(root, value):
    if value != "default" and not (value.isdigit() and int(value) > 0):
        raise ValueError("rate takes default or a frequency in Hz")
    devices = _devices(root)
    if _sound_card(root) is None:
        raise ValueError("no sound card; choose one first")
    audio = _audio_backend(root)
    if audio is None:
        if value == "default":
            return
        audio = ET.SubElement(devices, "audio", {
            "id": "1", "type": _default_audio(root)})
    for direction in ("input", "output"):
        element = _stream_element(audio, direction)
        for settings in element.findall("settings"):
            element.remove(settings)
        if value == "default":
            element.attrib.pop("fixedSettings", None)
            element.attrib.pop("mixingEngine", None)
        else:
            # Fixed settings are only valid with the mixing engine on.
            element.set("mixingEngine", "yes")
            element.set("fixedSettings", "yes")
            ET.SubElement(element, "settings", {"frequency": value})
        _tidy_stream(audio, direction)


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
    """Give the sound a host sound system that works with display kind.

    A chosen system stays, with its output, input and latency; only SPICE
    sound, which needs the SPICE display, is replaced by the session's.
    """
    devices = _devices(root)
    audio = _audio_backend(root)
    if audio is None:
        if kind == "spice":
            ET.SubElement(devices, "audio", {"id": "1", "type": "spice"})
        elif kind in OWN_WINDOW:
            facts = session_facts() if facts is None else facts
            audio = ET.SubElement(devices, "audio", {"id": "1", "type": "none"})
            _retype(audio, facts["audio"], facts)
        return
    if audio.get("type") == "spice" and kind != "spice":
        facts = session_facts() if facts is None else facts
        _retype(audio, facts["audio"], facts)


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


def _set_connection(root, key):
    """Plug the first card into network:NAME, bridge:NAME, direct:DEV or user.

    The card keeps its model and MAC address, so the guest sees the same
    network card on a different cable.
    """
    interfaces = _devices(root).findall("interface")
    if not interfaces:
        raise ValueError("no network card")
    kind, _, name = key.partition(":")
    fields = {"network": "network", "bridge": "bridge", "direct": "dev"}
    if kind == "user" and not name:
        source = None
    elif kind in fields and name:
        source = {fields[kind]: name}
        if kind == "direct":
            source["mode"] = "bridge"
    else:
        raise ValueError(
            "network takes on, off, network:NAME, bridge:NAME, "
            "direct:DEVICE or user")
    interface = interfaces[0]
    interface.set("type", kind)
    for tag in ("source", "link"):
        for element in interface.findall(tag):
            interface.remove(element)
    if source is not None:
        # source comes right after mac in libvirt's own order
        position = 1 if interface.find("mac") is not None else 0
        interface.insert(position, ET.Element("source", source))
