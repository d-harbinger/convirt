"""Tests for the 3D check and the start and shut down flow, without libvirt."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from convirtlib import check, core  # noqa: E402
from convirtlib.manager import Files, Manager  # noqa: E402
from test_switches import SPICE_VM  # noqa: E402

NODE = {"node": "/dev/dri/renderD128", "driver": "amdgpu", "usable": True}
GOOD_ARGS = ["/usr/bin/qemu-system-x86_64", "-name",
             "guest=sample,debug-threads=on", "-device",
             '{"driver":"virtio-vga-gl","id":"video0"}',
             "-display", "sdl,gl=on"]


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.vms = os.path.join(self.root, "vms")
        os.mkdir(self.vms)
        root = core.parse(SPICE_VM)
        core.apply(root, "accel3d", "on")
        with open(os.path.join(self.vms, "sample.xml"), "w") as handle:
            handle.write(core.serialize(root))
        self.proc = os.path.join(self.root, "proc")
        self.home = os.path.join(self.root, "home")
        self.modules = os.path.join(self.root, "qemu")
        for directory in (self.proc, self.home, self.modules):
            os.mkdir(directory)
        for module in ("hw-display-virtio-gpu-gl", "ui-opengl",
                       "hw-display-virtio-vga-gl",
                       "ui-spice-core", "ui-egl-headless"):
            open(os.path.join(self.modules, module + ".so"), "w").close()
        self.manager = Manager(
            Files(self.vms, render=[NODE]),
            os.path.join(self.root, "state"),
            {"proc": self.proc, "home": self.home, "dirs": (self.modules,)})
        self.vm = self.manager.find("sample")

    def tearDown(self):
        self.tmp.cleanup()

    def run_qemu(self, args):
        os.makedirs(os.path.join(self.proc, "4242"))
        with open(os.path.join(self.proc, "4242", "cmdline"), "wb") as f:
            f.write("\0".join(args).encode() + b"\0")

    def write_log(self, text):
        path = check.log_path("sample", True, self.home)
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            handle.write(text)

    def verdict(self):
        return self.manager.check_3d(self.vm)

    def test_off_has_nothing_to_check(self):
        root = core.parse(SPICE_VM)
        with open(os.path.join(self.vms, "sample.xml"), "w") as handle:
            handle.write(core.serialize(root))
        self.assertEqual(self.verdict()[0], "off")

    def test_shut_off_vm_is_unknown_until_started(self):
        verdict, text = self.verdict()
        self.assertEqual(verdict, "unknown")
        self.assertIn("start the VM to check", text)

    def test_missing_module_is_named(self):
        os.remove(os.path.join(self.modules, "ui-opengl.so"))
        verdict, text = self.verdict()
        self.assertEqual(verdict, "not working")
        self.assertIn("ui-opengl", text)

    def test_running_with_gl_and_a_quiet_log_is_working(self):
        self.manager.start(self.vm)
        self.run_qemu(GOOD_ARGS)
        self.write_log("2026-11-17 starting up libvirt version: 12.5.0\n"
                       "LC_ALL=C /usr/bin/qemu-system-x86_64 -name x\n")
        verdict, text = self.verdict()
        self.assertEqual(verdict, "working", text)
        self.assertIn("virtio-vga-gl", text)

    def test_log_error_since_the_last_start_fails_the_check(self):
        self.manager.start(self.vm)
        self.run_qemu(GOOD_ARGS)
        self.write_log(
            "starting up\nqemu: egl: no drm render node available\n"
            "starting up\n"
            "qemu-system-x86_64: OpenGL is not supported by the display\n")
        verdict, text = self.verdict()
        self.assertEqual(verdict, "not working")
        self.assertIn("OpenGL is not supported", text)
        self.assertNotIn("no drm render node", text, "an older start")

    def test_qemu_without_a_gl_device_fails(self):
        self.manager.start(self.vm)
        self.run_qemu([a.replace("virtio-vga-gl", "virtio-vga")
                       for a in GOOD_ARGS])
        self.write_log("starting up\n")
        verdict, text = self.verdict()
        self.assertEqual(verdict, "not working")
        self.assertIn("started without a GL device", text)

    def test_report_is_saved_privately(self):
        self.verdict()
        path = os.path.join(self.root, "state", "3d-session-sample.txt")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_start_and_force_off(self):
        self.assertFalse(self.vm.running)
        self.manager.start(self.vm)
        self.assertTrue(self.manager.refresh_running(self.vm))
        self.manager.force_off(self.vm)
        self.assertFalse(self.manager.refresh_running(self.vm))


if __name__ == "__main__":
    unittest.main()
