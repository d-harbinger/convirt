"""Tests for the switch logic and the change/undo flow, without libvirt."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from convirtlib import core  # noqa: E402
from convirtlib.manager import Files, Manager  # noqa: E402

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
            "mic": "on", "sound": "on", "accel3d": "off",
            "display": "spice", "clipboard": "on", "network": "on"})

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

    def test_lockdown_preset(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "accel3d", "on")
        self.assertEqual(core.apply_preset(root, "lockdown"), [])
        self.assertEqual(values(root), {
            "mic": "off", "sound": "off", "accel3d": "off",
            "display": "spice", "clipboard": "off", "network": "off"})

    def test_lockdown_preset_keeps_an_own_window(self):
        root = core.parse(SPICE_VM)
        core.apply(root, "display", "gtk", WAYLAND)
        core.apply_preset(root, "lockdown")
        self.assertEqual(values(root)["display"], "gtk")
        self.assertEqual(values(root)["clipboard"], "off")

    def test_media_preset(self):
        root = core.parse(SPICE_VM)
        core.apply_preset(root, "lockdown")
        self.assertEqual(core.apply_preset(root, "media", WAYLAND), [])
        self.assertEqual(values(root), {
            "mic": "on", "sound": "on", "accel3d": "on",
            "display": "sdl", "clipboard": "off", "network": "on"})

    def test_preset_skips_what_the_vm_cannot_do(self):
        root = core.parse(SPICE_VM)
        nothing = dict(X11, display="", xauth="")
        self.assertEqual(core.apply_preset(root, "media", nothing),
                         ["display"])
        self.assertEqual(values(root)["display"], "spice")
        self.assertEqual(values(root)["accel3d"], "on")
        self.assertEqual(
            core.apply_preset(core.parse(SPICE_VM), "media", WAYLAND,
                              fixed=("display",)), ["display"])

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
        self.assertEqual(values(root)["sound"], "on")
        self.assertEqual(values(root)["mic"], "off")

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
        self.assertEqual(root.find("devices/audio").get("type"), "spice")
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
        self.assertEqual(state["network"]["value"], "on")
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

    def test_preset_is_one_undo_step(self):
        skipped = self.manager.apply_preset(self.vm, "lockdown")
        self.assertEqual(skipped, [])
        state = self.manager.state(self.vm)
        self.assertEqual(state["network"]["value"], "off")
        self.assertEqual(state["sound"]["value"], "off")
        self.manager.undo(self.vm)
        with open(os.path.join(self.vms, "sample.xml")) as handle:
            self.assertEqual(handle.read(), SPICE_VM)
        self.assertFalse(self.manager.can_undo(self.vm))
        with self.assertRaises(RuntimeError):
            self.manager.apply_preset(self.vm, "bogus")

    def test_pending_compares_running_and_stored(self):
        with open(os.path.join(self.vms, "sample.live.xml"), "w") as handle:
            handle.write(SPICE_VM)
        vm = self.manager.find("sample")
        self.assertTrue(vm.running)
        self.assertEqual(self.manager.pending(vm), [])
        self.manager.change(vm, "mic", "off")
        self.assertEqual(self.manager.pending(vm), ["mic"])


if __name__ == "__main__":
    unittest.main()
