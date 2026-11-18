"""Tests for the switch logic and the change/undo flow, without libvirt."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from convirtlib import core  # noqa: E402
from convirtlib.manager import (  # noqa: E402
    Files, Manager, audio_devices, forward_mode, host_interfaces,
    render_nodes)

SPICE_VM = """
<domain type='kvm'>
  <name>sample</name>
  <devices>
    <interface type='user'><model type='virtio'/></interface>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
    <channel type='spicevmc'>
      <target type='virtio' name='com.redhat.spice.0'/>
    </channel>
    <graphics type='spice' port='5900' autoport='yes' listen='127.0.0.1'>
      <listen type='address' address='127.0.0.1'/>
    </graphics>
    <sound model='ich9'/>
    <audio id='1' type='spice'/>
    <video>
      <model type='virtio' heads='1' primary='yes' device='virtio-vga'/>
    </video>
    <redirdev bus='usb' type='spicevmc'/>
  </devices>
</domain>
"""

QXL_VM = """
<domain type='kvm'>
  <name>old</name>
  <devices>
    <graphics type='spice' autoport='yes'/>
    <sound model='ac97'/>
    <video>
      <model type='qxl' ram='65536' vram='65536' vgamem='16384' heads='1'/>
    </video>
  </devices>
</domain>
"""

WAYLAND = {"runtime": "/run/user/1000", "wayland": "wayland-0",
           "display": ":0", "xauth": "", "audio": "pipewire"}
X11 = {"runtime": "", "wayland": "", "display": ":0",
       "xauth": "/tmp/xauth", "audio": "none"}


def values(root):
    return core.values(root)


class CoreTest(unittest.TestCase):
    def test_reads_the_starting_state(self):
        root = core.parse(SPICE_VM)
        self.assertEqual(values(root), {
            "sound": "ich9", "mic": "on", "audio": "spice",
            "speaker": "default", "recorder": "default",
            "latency": "default", "rate": "default", "accel3d": "off",
            "display": "spice", "screens": "1", "clipboard": "on",
            "network": "user"})

    def test_clipboard_off_and_on(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "clipboard", "off")
        self.assertEqual(
            [c.get("type") for c in root.findall("devices/channel")],
            ["unix"])
        self.assertEqual(values(root)["clipboard"], "off")
        core.apply(root, "clipboard", "on")
        self.assertEqual(values(root)["clipboard"], "on")

    def test_clipboard_is_fixed_off_with_an_own_window(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "display", "sdl", WAYLAND)
        state = core.read_state(root)["clipboard"]
        self.assertEqual((state["value"], state["enabled"]), ("off", False))
        with self.assertRaises(ValueError):
            core.apply(root, "clipboard", "on")

    def test_exposures_name_what_has_no_switch(self):
        root = core.parse(SPICE_VM)
        self.assertEqual(core.exposures(root), ["1 USB redirection port"])
        devices = root.find("devices")
        for _ in range(2):
            devices.append(core.parse("<filesystem type='mount'/>"))
        self.assertEqual(core.exposures(root),
                         ["2 shared folders", "1 USB redirection port"])


    def test_microphone_off_and_on(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "mic", "off")
        self.assertEqual(root.find("devices/sound/codec").get("type"),
                         "output")
        self.assertEqual(values(root)["mic"], "off")
        core.apply(root, "mic", "on")
        self.assertIsNone(root.find("devices/sound/codec"))
        self.assertEqual(values(root)["mic"], "on")

    def test_microphone_fixed_on_other_sound_models(self):
        root = core.parse(QXL_VM)
        state = core.read_state(root)["mic"]
        self.assertEqual((state["value"], state["enabled"]), ("on", False))
        with self.assertRaises(ValueError):
            core.apply(root, "mic", "off")

    def test_sound_off_then_on_comes_back_without_microphone(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "sound", "off")
        self.assertEqual(values(root)["sound"], "off")
        self.assertEqual(values(root)["mic"], "off")
        core.apply(root, "sound", "on")
        self.assertEqual(values(root)["sound"], "ich9")
        self.assertEqual(values(root)["mic"], "off")

    def test_sound_card_model_changes_keep_what_fits(self):
        root = core.parse(SPICE_VM.replace(
            "<sound model='ich9'/>",
            "<sound model='ich9'><codec type='micro'/>"
            "<address type='pci' bus='0x00' slot='0x1b'/></sound>"))
        core.apply(root, "sound", "usb")
        card = root.find("devices/sound")
        self.assertEqual(card.get("model"), "usb")
        self.assertEqual(list(card), [])
        state = core.read_state(root)["mic"]
        self.assertEqual((state["value"], state["enabled"]), ("off", False))
        self.assertIn("no input", state["meaning"])
        core.apply(root, "sound", "ich6")
        self.assertEqual(root.find("devices/sound/codec").get("type"),
                         "output")
        core.apply(root, "sound", "ac97")
        self.assertEqual(values(root)["mic"], "on")
        core.apply(root, "sound", "ich9")
        self.assertIsNone(root.find("devices/sound/codec"))
        with self.assertRaises(ValueError):
            core.apply(root, "sound", "bogus")

    def test_microphone_codec_choices(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "mic", "micro")
        self.assertEqual(root.find("devices/sound/codec").get("type"),
                         "micro")
        state = core.read_state(root)["mic"]
        self.assertEqual(state["value"], "micro")
        self.assertIn("a microphone", state["meaning"])
        self.assertIn("viewer", state["meaning"])
        with self.assertRaises(ValueError):
            core.apply(root, "mic", "loud")

    def test_host_sound_system_changes_and_strips_its_own_options(self):
        root = core.parse(SPICE_VM)
        with self.assertRaises(ValueError):
            core.apply(root, "speaker", "sink-a")
        core.apply(root, "audio", "pulseaudio")
        core.apply(root, "speaker", "sink-a")
        core.apply(root, "recorder", "source-a")
        core.apply(root, "latency", "20")
        core.apply(root, "rate", "48000")
        audio = root.find("devices/audio")
        self.assertEqual([c.tag for c in audio], ["input", "output"])
        output = audio.find("output")
        self.assertEqual(output.get("name"), "sink-a")
        self.assertEqual(output.get("latency"), "20000")
        self.assertEqual(audio.find("input").get("latency"), "20000")
        self.assertEqual(output.get("fixedSettings"), "yes")
        self.assertEqual(output.get("mixingEngine"), "yes")
        self.assertEqual(output.find("settings").get("frequency"), "48000")
        self.assertEqual(
            {k: values(root)[k] for k in ("audio", "speaker", "recorder",
                                         "latency", "rate")},
            {"audio": "pulseaudio", "speaker": "sink-a",
             "recorder": "source-a", "latency": "20", "rate": "48000"})
        core.apply(root, "audio", "pipewire", WAYLAND)
        self.assertEqual(audio.get("runtimeDir"), "/run/user/1000")
        self.assertIsNone(output.get("name"))
        self.assertIsNone(output.get("latency"))
        self.assertEqual(output.get("fixedSettings"), "yes")
        core.apply(root, "rate", "default")
        self.assertEqual(list(audio), [])
        core.apply(root, "audio", "default")
        self.assertIsNone(root.find("devices/audio"))
        self.assertEqual(values(root)["audio"], "default")

    def test_spice_sound_needs_the_spice_display(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "display", "sdl", WAYLAND)
        with self.assertRaises(ValueError):
            core.apply(root, "audio", "spice")
        choices = dict(core.sound_choices(root)["audio"])
        self.assertNotIn("spice", choices)

    def test_recorder_needs_an_input_on_the_card(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "audio", "pulseaudio")
        core.apply(root, "mic", "off")
        self.assertFalse(core.read_state(root)["recorder"]["enabled"])
        with self.assertRaises(ValueError):
            core.apply(root, "recorder", "source-a")

    def test_sound_meanings_use_what_the_host_has(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "audio", "pipewire", WAYLAND)
        core.apply(root, "speaker", "sink-a")
        host = {"sinks": [("sink-a", "Desk speakers")], "sources": [],
                "system": True, "audio_types": {"pulseaudio"}}
        state = core.read_state(root, host)
        self.assertEqual(state["speaker"]["meaning"], "Desk speakers")
        self.assertIn("not installed", state["audio"]["meaning"])
        host["audio_types"] = {"pipewire"}
        state = core.read_state(root, host)
        self.assertIn("system's user", state["audio"]["meaning"])
        self.assertEqual(state["recorder"]["meaning"],
                         "the host's default input")
        choices = core.sound_choices(root, host)
        self.assertEqual(choices["speaker"][1], ("sink-a", "Desk speakers"))
        self.assertIn(("pulseaudio", "PulseAudio (not installed)"),
                      choices["audio"])
        self.assertEqual(core.sound_choices(core.parse(QXL_VM))["mic"],
                         [("on", "Fixed: input")])

    def test_3d_on_spice_adds_a_headless_renderer(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "accel3d", "on")
        model = root.find("devices/video/model")
        self.assertEqual(model.get("device"), "virtio-vga-gl")
        self.assertEqual(model.find("acceleration").get("accel3d"), "yes")
        kinds = [g.get("type") for g in root.findall("devices/graphics")]
        self.assertEqual(kinds, ["spice", "egl-headless"])
        self.assertEqual(values(root)["accel3d"], "on")
        core.apply(root, "accel3d", "off")
        self.assertEqual(model.get("device"), "virtio-vga")
        kinds = [g.get("type") for g in root.findall("devices/graphics")]
        self.assertEqual(kinds, ["spice"])

    def test_3d_on_converts_a_qxl_card(self):
        root = core.parse(QXL_VM)
        core.apply(root, "accel3d", "on")
        model = root.find("devices/video/model")
        self.assertEqual(model.get("type"), "virtio")
        self.assertIsNone(model.get("vram"))
        self.assertEqual(values(root)["accel3d"], "on")

    def test_display_sdl_on_wayland(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "accel3d", "on")
        core.apply(root, "display", "sdl", WAYLAND)
        graphics = root.findall("devices/graphics")
        self.assertEqual([g.get("type") for g in graphics], ["sdl"])
        self.assertEqual(graphics[0].find("gl").get("enable"), "yes")
        self.assertEqual(
            [c.get("type") for c in root.findall("devices/channel")],
            ["unix"])
        self.assertEqual(root.findall("devices/redirdev"), [])
        audio = root.find("devices/audio")
        self.assertEqual(audio.get("type"), "pipewire")
        self.assertEqual(audio.get("runtimeDir"), "/run/user/1000")
        env = {e.get("name"): e.get("value") for e in root.iter(
            "{%s}env" % core.QEMU_NS)}
        self.assertEqual(env, {"WAYLAND_DISPLAY": "wayland-0",
                               "XDG_RUNTIME_DIR": "/run/user/1000"})
        self.assertEqual(values(root)["accel3d"], "on")

    def test_display_sdl_on_x11(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "display", "sdl", X11)
        sdl = root.find("devices/graphics")
        self.assertEqual(sdl.get("display"), ":0")
        self.assertEqual(sdl.get("xauth"), "/tmp/xauth")
        self.assertEqual(sdl.find("gl").get("enable"), "no")
        self.assertIsNone(root.find("{%s}commandline" % core.QEMU_NS))

    def test_display_sdl_needs_a_desktop_session(self):
        root = core.parse(SPICE_VM)
        nothing = dict(X11, display="", xauth="")
        with self.assertRaises(ValueError):
            core.apply(root, "display", "sdl", nothing)
        self.assertEqual(values(root)["display"], "spice")

    def test_display_back_to_spice_cleans_up(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "accel3d", "on")
        core.apply(root, "display", "sdl", WAYLAND)
        core.apply(root, "display", "spice")
        kinds = [g.get("type") for g in root.findall("devices/graphics")]
        self.assertEqual(kinds, ["spice", "egl-headless"])
        spice = root.find("devices/graphics")
        self.assertEqual(spice.find("listen").get("type"), "none")
        self.assertIsNone(spice.get("port"))
        # The sound system chosen for the own window stays; it plays on
        # the host either way.
        self.assertEqual(root.find("devices/audio").get("type"), "pipewire")
        self.assertIsNone(root.find("{%s}commandline" % core.QEMU_NS))
        self.assertEqual(
            sorted(c.get("type") for c in root.findall("devices/channel")),
            ["spicevmc", "unix"])
        self.assertEqual(values(root)["accel3d"], "on")

    def test_display_gtk_on_wayland(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "accel3d", "on")
        core.apply(root, "display", "gtk", WAYLAND)
        self.assertEqual(root.findall("devices/graphics"), [])
        cmdline = root.find("{%s}commandline" % core.QEMU_NS)
        self.assertEqual(
            [(c.tag.split("}")[1], c.get("value")) for c in cmdline],
            [("arg", "-display"), ("arg", "gtk,gl=on"),
             ("env", "wayland-0"), ("env", "/run/user/1000")])
        self.assertEqual(root.find("devices/audio").get("type"), "pipewire")
        self.assertEqual(values(root)["display"], "gtk")
        self.assertEqual(values(root)["accel3d"], "on")
        core.apply(root, "accel3d", "off")
        self.assertEqual(cmdline.findall("{%s}arg" % core.QEMU_NS)[1]
                         .get("value"), "gtk,gl=off")
        self.assertEqual(values(root)["display"], "gtk")

    def test_display_gtk_on_x11_passes_the_display_variables(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "display", "gtk", X11)
        env = {e.get("name"): e.get("value") for e in root.iter(
            "{%s}env" % core.QEMU_NS)}
        self.assertEqual(env, {"DISPLAY": ":0", "XAUTHORITY": "/tmp/xauth"})

    def test_switching_between_window_kinds_leaves_no_leftovers(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "accel3d", "on")
        core.apply(root, "display", "gtk", WAYLAND)
        core.apply(root, "display", "sdl", WAYLAND)
        self.assertEqual(root.findall(".//{%s}arg" % core.QEMU_NS), [])
        self.assertEqual(values(root)["display"], "sdl")
        self.assertEqual(values(root)["accel3d"], "on")
        core.apply(root, "display", "gtk", WAYLAND)
        core.apply(root, "display", "spice")
        self.assertIsNone(root.find("{%s}commandline" % core.QEMU_NS))
        self.assertEqual(values(root)["display"], "spice")

    def test_3d_off_on_sdl_keeps_the_window(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "display", "sdl", WAYLAND)
        core.apply(root, "accel3d", "on")
        self.assertEqual(root.find("devices/graphics/gl").get("enable"),
                         "yes")
        core.apply(root, "accel3d", "off")
        self.assertEqual(root.find("devices/graphics/gl").get("enable"),
                         "no")
        self.assertEqual(values(root)["display"], "sdl")

    def test_network_unplug(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "network", "off")
        self.assertEqual(
            root.find("devices/interface/link").get("state"), "down")
        self.assertEqual(values(root)["network"], "off")
        self.assertIsNotNone(root.find("devices/interface/model"))
        core.apply(root, "network", "on")
        self.assertIsNone(root.find("devices/interface/link"))

    def test_network_moves_the_card_to_another_cable(self):
        root = core.parse(SPICE_VM.replace(
            "<interface type='user'>",
            "<interface type='user'><mac address='sentinel-address'/>"))
        core.apply(root, "network", "off")
        core.apply(root, "network", "bridge:br0")
        card = root.find("devices/interface")
        self.assertEqual(card.get("type"), "bridge")
        self.assertEqual(card.find("source").get("bridge"), "br0")
        self.assertIsNone(card.find("link"), "a new cable is plugged in")
        self.assertEqual(card.find("mac").get("address"), "sentinel-address")
        self.assertEqual(card.find("model").get("type"), "virtio")
        self.assertEqual(values(root)["network"], "bridge:br0")

        core.apply(root, "network", "direct:eth0")
        self.assertEqual(card.find("source").attrib,
                         {"dev": "eth0", "mode": "bridge"})
        core.apply(root, "network", "user")
        self.assertIsNone(card.find("source"))
        self.assertEqual(values(root)["network"], "user")
        with self.assertRaises(ValueError):
            core.apply(root, "network", "bridge:")

    def test_on_counts_as_set_for_any_plugged_connection(self):
        self.assertTrue(core.same_value("network", "bridge:br0", "on"))
        self.assertFalse(core.same_value("network", "off", "on"))
        self.assertFalse(core.same_value("network", "user", "bridge:br0"))

    def test_network_names_what_the_card_is_plugged_into(self):
        cases = [
            ("<interface type='network'><source network='default'/>"
             "</interface>", {"default": "nat"}, "NAT network default"),
            ("<interface type='network'><source network='lab'/>"
             "</interface>", {"lab": "isolated"}, "isolated network lab"),
            ("<interface type='network'><source network='lab'/>"
             "</interface>", {}, "network lab"),
            ("<interface type='bridge'><source bridge='br0'/></interface>",
             {}, "host bridge br0"),
            ("<interface type='direct'><source dev='eth0' mode='bridge'/>"
             "</interface>", {}, "macvtap on eth0"),
            ("<interface type='user'/>", {}, "user-mode networking"),
        ]
        for xml, modes, label in cases:
            self.assertEqual(core.attachment(core.parse(xml), modes), label)

    def test_network_meaning_claims_no_reach(self):
        root = core.parse(SPICE_VM)
        meaning = core.read_state(root)["network"]["meaning"]
        self.assertEqual(meaning, "plugged into user-mode networking")
        self.assertNotIn("LAN", meaning)
        self.assertNotIn("internet", meaning)

    def test_3d_row_reports_the_host_render_node(self):
        root = core.parse(SPICE_VM)
        state = core.read_state(root, {"render": []})["accel3d"]
        self.assertEqual((state["value"], state["enabled"]), ("off", False))
        self.assertIn("no GPU render node", state["meaning"])

        core.apply(root, "accel3d", "on")
        node = {"node": "/dev/dri/renderD128", "driver": "amdgpu"}
        state = core.read_state(root, {"render": []})["accel3d"]
        self.assertEqual(state["value"], "on")
        self.assertIn("no GPU render node", state["meaning"])
        state = core.read_state(
            root, {"render": [dict(node, usable=False)]})["accel3d"]
        self.assertIn("/dev/dri/renderD128 cannot be opened",
                      state["meaning"])
        state = core.read_state(
            root, {"render": [dict(node, usable=True)]})["accel3d"]
        self.assertIn("(amdgpu)", state["meaning"])
        self.assertIn("may tear", state["meaning"])

    def test_raw_fragments_show_the_switch_xml_only(self):
        text = core.raw_fragments(core.parse(SPICE_VM))
        self.assertIn("<sound", text)
        self.assertIn("<graphics", text)
        self.assertNotIn("guest_agent", text)


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vms = os.path.join(self.tmp.name, "vms")
        os.mkdir(self.vms)
        with open(os.path.join(self.vms, "sample.xml"), "w") as handle:
            handle.write(SPICE_VM)
        self.manager = Manager(Files(self.vms),
                               os.path.join(self.tmp.name, "backups"))
        self.vm = self.manager.find("sample")

    def tearDown(self):
        self.tmp.cleanup()

    def test_change_then_undo_restores_the_exact_config(self):
        self.assertFalse(self.manager.can_undo(self.vm))
        self.assertTrue(self.manager.change(self.vm, "mic", "off"))
        self.assertTrue(self.manager.change(self.vm, "network", "off"))
        self.assertEqual(self.manager.state(self.vm)["mic"]["value"], "off")
        self.manager.undo(self.vm)
        state = self.manager.state(self.vm)
        self.assertEqual(state["network"]["value"], "user")
        self.assertEqual(state["mic"]["value"], "off")
        self.manager.undo(self.vm)
        with open(os.path.join(self.vms, "sample.xml")) as handle:
            self.assertEqual(handle.read(), SPICE_VM)
        self.assertFalse(self.manager.can_undo(self.vm))

    def test_same_value_changes_nothing_and_saves_no_backup(self):
        self.assertFalse(self.manager.change(self.vm, "mic", "on"))
        self.assertFalse(self.manager.can_undo(self.vm))

    def test_refused_change_leaves_no_backup(self):
        with self.assertRaises(RuntimeError):
            self.manager.change(self.vm, "display", "bogus")
        self.assertFalse(self.manager.can_undo(self.vm))

    def test_backups_are_private(self):
        self.manager.change(self.vm, "mic", "off")
        directory = os.path.join(self.tmp.name, "backups", "session-sample")
        self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
        name = [f for f in os.listdir(directory) if f.endswith(".xml")][0]
        mode = os.stat(os.path.join(directory, name)).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        slots = os.path.join(directory, "slots")
        self.assertEqual(os.stat(slots).st_mode & 0o777, 0o700)
        mode = os.stat(os.path.join(slots, "original.xml")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_first_change_keeps_the_original_in_a_slot(self):
        self.assertEqual(self.manager.slots(self.vm), [])
        self.manager.change(self.vm, "mic", "off")
        self.manager.change(self.vm, "network", "off")
        self.assertEqual(self.manager.slots(self.vm), ["original"])
        self.manager.restore_slot(self.vm, "original")
        with open(os.path.join(self.vms, "sample.xml")) as handle:
            self.assertEqual(handle.read(), SPICE_VM)
        with self.assertRaises(RuntimeError):
            self.manager.save_slot(self.vm, "original")

    def test_save_and_restore_a_named_slot(self):
        self.manager.change(self.vm, "mic", "off")
        self.manager.save_slot(self.vm, "quiet one")
        self.manager.change(self.vm, "mic", "on")
        self.assertEqual(self.manager.slots(self.vm),
                         ["original", "quiet one"])
        self.manager.restore_slot(self.vm, "quiet one")
        self.assertEqual(self.manager.state(self.vm)["mic"]["value"], "off")
        self.manager.undo(self.vm)
        self.assertEqual(self.manager.state(self.vm)["mic"]["value"], "on")

    def test_slot_names_cannot_leave_the_slot_folder(self):
        for bad in ("", "../x", "a/b", "x" * 33, "dot.dot"):
            with self.assertRaises(RuntimeError):
                self.manager.save_slot(self.vm, bad)
        with self.assertRaises(RuntimeError):
            self.manager.restore_slot(self.vm, "../sample")


    def test_state_reads_the_network_forward_mode(self):
        text = SPICE_VM.replace(
            "<interface type='user'>",
            "<interface type='network'><source network='default'/>")
        for name, xml in (("sample", text),
                          ("networks/default",
                           "<network><name>default</name>"
                           "<forward mode='nat'/></network>")):
            path = os.path.join(self.vms, name + ".xml")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(xml)
        state = self.manager.state(self.manager.find("sample"))
        self.assertEqual(state["network"]["meaning"],
                         "plugged into NAT network default")

    def test_network_choices_list_what_the_host_offers(self):
        os.makedirs(os.path.join(self.vms, "networks"))
        for name, forward in (("default", "<forward mode='nat'/>"),
                              ("lab", "")):
            with open(os.path.join(self.vms, "networks", name + ".xml"),
                      "w") as handle:
                handle.write("<network><name>%s</name>%s</network>"
                             % (name, forward))
        manager = Manager(
            Files(self.vms, interfaces={"bridges": ["br0"],
                                        "wired": ["eth0"]}),
            os.path.join(self.tmp.name, "backups"))
        choices = manager.network_choices(manager.find("sample"))
        self.assertEqual(choices, [
            ("off", "Unplugged"),
            ("network:default", "NAT network default"),
            ("network:lab", "Isolated network lab"),
            ("bridge:br0", "Host bridge br0"),
            ("direct:eth0", "Macvtap on eth0"),
            ("user", "User-mode networking"),
        ])

    def test_host_interfaces_split_bridges_from_wired_cards(self):
        net = os.path.join(self.tmp.name, "net")
        for name, parts in (("br0", ["bridge"]), ("eth0", ["device"]),
                            ("wlan0", ["device", "wireless"]), ("lo", [])):
            os.makedirs(os.path.join(net, name))
            for part in parts:
                os.makedirs(os.path.join(net, name, part))
        self.assertEqual(host_interfaces(net),
                         {"bridges": ["br0"], "wired": ["eth0"]})

    def test_forward_mode_without_forward_is_isolated(self):
        self.assertEqual(forward_mode("<network><name>x</name></network>"),
                         "isolated")
        self.assertEqual(forward_mode("<network><forward/></network>"), "nat")
        self.assertIsNone(forward_mode(None))

    def test_render_nodes_read_the_driver(self):
        dev = os.path.join(self.tmp.name, "dri")
        sysfs = os.path.join(self.tmp.name, "drm")
        os.makedirs(dev)
        os.makedirs(os.path.join(sysfs, "renderD128", "device"))
        open(os.path.join(dev, "renderD128"), "w").close()
        os.makedirs(os.path.join(self.tmp.name, "drivers", "i915"))
        os.symlink(os.path.join(self.tmp.name, "drivers", "i915"),
                   os.path.join(sysfs, "renderD128", "device", "driver"))
        nodes = render_nodes(root=dev, sysfs=sysfs)
        self.assertEqual(nodes, [{"node": os.path.join(dev, "renderD128"),
                                  "driver": "i915", "usable": True}])
        self.assertEqual(render_nodes(root=os.path.join(dev, "none")), [])

    def test_pending_compares_running_and_stored(self):
        with open(os.path.join(self.vms, "sample.live.xml"), "w") as handle:
            handle.write(SPICE_VM)
        vm = self.manager.find("sample")
        self.assertTrue(vm.running)
        self.assertEqual(self.manager.pending(vm), [])
        self.manager.change(vm, "mic", "off")
        self.assertEqual(self.manager.pending(vm), ["mic"])

    def test_host_sound_comes_from_audio_json(self):
        with open(os.path.join(self.vms, "audio.json"), "w") as handle:
            handle.write('{"sinks": [["sink-a", "Desk speakers"]], '
                         '"sources": [["mic-a", "Headset"]], '
                         '"types": ["spice", "pipewire"]}')
        choices = self.manager.choices(self.vm)
        self.assertIn(("mic-a", "Headset"), choices["recorder"])
        self.assertIn(("pulseaudio", "PulseAudio (not installed)"),
                      choices["audio"])
        with self.assertRaises(RuntimeError):
            self.manager.change(self.vm, "audio", "pulseaudio")
        self.assertFalse(self.manager.can_undo(self.vm))
        self.assertTrue(self.manager.change(self.vm, "audio", "pipewire"))

    def test_audio_devices_skips_monitors(self):
        outputs = {
            "sinks": '[{"name": "sink-a", "description": "Desk speakers",'
                     ' "properties": {"device.class": "sound"}}]',
            "sources": '[{"name": "sink-a.monitor", "description": "M",'
                       ' "properties": {"device.class": "monitor"}},'
                       ' {"name": "mic-a", "description": "Headset",'
                       ' "properties": {}}]'}

        class Result:
            def __init__(self, text):
                self.returncode, self.stdout = 0, text

        def run(command, **_kwargs):
            return Result(outputs[command[-1]])

        self.assertEqual(audio_devices(run), {
            "sinks": [("sink-a", "Desk speakers")],
            "sources": [("mic-a", "Headset")]})

        def missing(command, **_kwargs):
            raise FileNotFoundError(command[0])

        self.assertIsNone(audio_devices(missing))


class ScreensTest(unittest.TestCase):
    def test_screens_set_heads_on_virtio(self):
        root = core.parse(SPICE_VM)
        self.assertEqual(values(root)["screens"], "1")
        core.apply(root, "screens", "2")
        model = root.find("devices/video/model")
        self.assertEqual(model.get("heads"), "2")
        state = core.read_state(root)["screens"]
        self.assertEqual(state["value"], "2")
        self.assertIn("virt-viewer", state["meaning"])
        core.apply(root, "display", "sdl", WAYLAND)
        self.assertIn("untested", core.read_state(root)["screens"]["meaning"])
        for bad in ("0", "5", "two"):
            with self.assertRaises(ValueError):
                core.apply(root, "screens", bad)

    def test_screens_fixed_without_a_multihead_model(self):
        root = core.parse(SPICE_VM.replace("type='virtio' heads='1'",
                                           "type='vga' heads='1'"))
        state = core.read_state(root)["screens"]
        self.assertEqual((state["value"], state["enabled"]), ("1", False))
        with self.assertRaises(ValueError):
            core.apply(root, "screens", "2")
        self.assertEqual(core.value_label("screens", "3"), "3 screens")


class CautionTest(unittest.TestCase):
    """Each row whose value opens the host says so; closed rows say
    nothing."""

    def levels(self, root, **host):
        return {k: level for k, (level, _text)
                in core.cautions(root, host).items()}

    def test_starting_vm_cautions(self):
        root = core.parse(SPICE_VM)
        self.assertEqual(self.levels(root, system=False), {
            "vm": "caution", "mic": "note", "clipboard": "caution",
            "network": "caution", "attached": "caution"})
        self.assertNotIn("vm", self.levels(root, system=True))

    def test_closed_settings_say_nothing(self):
        root = core.parse(SPICE_VM)
        for switch, value in (("sound", "off"), ("clipboard", "off"),
                              ("network", "off")):
            core.apply(root, switch, value)
        for tag in ("redirdev",):
            for element in root.findall("devices/" + tag):
                root.find("devices").remove(element)
        self.assertEqual(self.levels(root, system=True), {})

    def test_3d_own_window_and_x11(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "accel3d", "on")
        core.apply(root, "display", "sdl", WAYLAND)
        found = self.levels(root, wayland=True)
        self.assertEqual(found["accel3d"], "caution")
        self.assertEqual(found["display"], "note")
        self.assertEqual(self.levels(root, wayland=False)["display"],
                         "caution")

    def test_sound_cautions_follow_the_sound_system(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "audio", "pipewire", WAYLAND)
        found = core.cautions(root, {"system": True})
        self.assertEqual(found["mic"][0], "caution")
        self.assertIn("every node", found["audio"][1])
        self.assertEqual(core.cautions(root, {"system": False})["audio"][0],
                         "note")
        core.apply(root, "mic", "off")
        self.assertNotIn("mic", core.cautions(root))
        core.apply(root, "sound", "sb16")
        self.assertEqual(core.cautions(root)["sound"][0], "note")

    def test_network_cautions_by_kind(self):
        root = core.parse(SPICE_VM)
        cases = (("network:lab", {"lab": "isolated"}, "note", "host itself"),
                 ("network:default", {"default": "nat"}, "note", "LAN"),
                 ("network:br", {"br": "bridge"}, "caution", "full peer"),
                 ("bridge:br0", {}, "caution", "full peer"),
                 ("direct:eth0", {}, "caution", "full peer"),
                 ("user", {}, "caution", "localhost"))
        for key, modes, level, words in cases:
            core.apply(root, "network", key)
            found = core.cautions(root, {"networks": modes})["network"]
            self.assertEqual(found[0], level, key)
            self.assertIn(words, found[1], key)


if __name__ == "__main__":
    unittest.main()
