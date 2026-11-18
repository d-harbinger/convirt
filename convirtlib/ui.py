"""The window: one VM at a time, one row per switch."""
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from . import core  # noqa: E402

CSS = b"""
.convirt-title { font-weight: bold; }
.convirt-meaning { opacity: 0.7; font-size: 90%; }
.convirt-row { padding: 10px 12px; }
.convirt-status { opacity: 0.8; }
"""


class Window(Gtk.Window):
    def __init__(self, manager):
        super().__init__(title="convirt")
        self.manager = manager
        self.vms = []
        self.vm = None
        self.loading = False
        self.controls = {}
        self.meanings = {}
        self.set_default_size(460, -1)
        self.set_border_width(18)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add(box)

        header = Gtk.Box(spacing=10)
        header.pack_start(Gtk.Label(label="VM"), False, False, 0)
        self.picker = Gtk.ComboBoxText()
        self.picker.connect("changed", self.on_pick)
        header.pack_start(self.picker, False, False, 0)
        self.running = Gtk.Label()
        self.running.get_style_context().add_class("convirt-status")
        header.pack_end(self.running, False, False, 0)
        box.pack_start(header, False, False, 0)

        rows = Gtk.ListBox()
        rows.set_selection_mode(Gtk.SelectionMode.NONE)
        frame = Gtk.Frame()
        frame.add(rows)
        box.pack_start(frame, False, False, 0)
        for name in core.SWITCHES:
            rows.add(self.build_row(name))

        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        self.status.get_style_context().add_class("convirt-status")
        box.pack_start(self.status, False, False, 0)

        buttons = Gtk.Box(spacing=8)
        self.undo = Gtk.Button(label="Undo last change")
        self.undo.connect("clicked", self.on_undo)
        buttons.pack_start(self.undo, False, False, 0)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _b: self.reload_list())
        buttons.pack_end(refresh, False, False, 0)
        raw = Gtk.Button(label="Show raw config")
        raw.connect("clicked", self.on_raw)
        buttons.pack_end(raw, False, False, 0)
        box.pack_start(buttons, False, False, 0)

        self.connect("destroy", Gtk.main_quit)
        self.reload_list()

    def build_row(self, name):
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        line = Gtk.Box(spacing=12)
        line.get_style_context().add_class("convirt-row")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=core.TITLES[name], xalign=0)
        title.get_style_context().add_class("convirt-title")
        meaning = Gtk.Label(xalign=0)
        meaning.set_ellipsize(Pango.EllipsizeMode.END)
        meaning.get_style_context().add_class("convirt-meaning")
        text.pack_start(title, False, False, 0)
        text.pack_start(meaning, False, False, 0)
        line.pack_start(text, True, True, 0)

        if name == "display":
            control = Gtk.ComboBoxText()
            control.connect("changed", self.on_display)
        else:
            control = Gtk.Switch()
            control.connect("notify::active", self.on_switch, name)
        control.set_valign(Gtk.Align.CENTER)
        line.pack_end(control, False, False, 0)
        self.controls[name] = control
        self.meanings[name] = meaning
        row.add(line)
        return row

    # ------------------------------------------------------------ loading

    def reload_list(self):
        keep = self.vm.label if self.vm else None
        try:
            self.vms = self.manager.list()
        except RuntimeError as error:
            self.vms = []
            self.status.set_text(str(error))
        self.loading = True
        self.picker.remove_all()
        for vm in self.vms:
            self.picker.append_text(vm.label)
        self.loading = False
        if not self.vms:
            self.vm = None
            self.set_sensitive_rows(False)
            if not self.status.get_text():
                self.status.set_text("No VMs found")
            return
        labels = [vm.label for vm in self.vms]
        self.picker.set_active(labels.index(keep) if keep in labels else 0)

    def set_sensitive_rows(self, sensitive):
        for control in self.controls.values():
            control.set_sensitive(sensitive)
        self.undo.set_sensitive(sensitive)

    def reload(self, message=None):
        """Read the selected VM again and redraw every row."""
        if self.vm is None:
            return
        try:
            state = self.manager.state(self.vm)
            pending = self.manager.pending(self.vm)
        except RuntimeError as error:
            self.set_sensitive_rows(False)
            self.status.set_text(str(error))
            return
        self.loading = True
        for name in core.SWITCHES:
            entry = state[name]
            control = self.controls[name]
            if name == "display":
                control.remove_all()
                for kind, label in core.DISPLAY_LABELS.items():
                    control.append(kind, label)
                if entry["value"] not in core.DISPLAY_LABELS:
                    control.append(entry["value"], entry["value"])
                control.set_active_id(entry["value"])
            else:
                control.set_active(entry["value"] == "on")
            control.set_sensitive(entry["enabled"])
            self.meanings[name].set_text(entry["meaning"])
        self.loading = False

        self.running.set_text(
            "● running" if self.vm.running else "○ shut off")
        self.undo.set_sensitive(self.manager.can_undo(self.vm))
        if message:
            self.status.set_text(message)
        elif pending:
            names = ", ".join(core.TITLES[n] for n in pending)
            self.status.set_text(
                "Waiting for next full start: %s" % names)
        elif self.vm.running:
            self.status.set_text("Running VM matches the stored config")
        else:
            self.status.set_text("Changes apply at next start")

    # ----------------------------------------------------------- handlers

    def on_pick(self, picker):
        if self.loading:
            return
        index = picker.get_active()
        self.vm = self.vms[index] if index >= 0 else None
        self.reload()

    def on_switch(self, control, _param, name):
        if self.loading:
            return
        self.change(name, "on" if control.get_active() else "off")

    def on_display(self, control):
        if self.loading or control.get_active_id() is None:
            return
        self.change("display", control.get_active_id())

    def change(self, name, value):
        try:
            self.manager.change(self.vm, name, value)
        except RuntimeError as error:
            self.reload("Not changed: %s" % error)
            return
        self.reload()

    def on_undo(self, _button):
        try:
            self.manager.undo(self.vm)
        except RuntimeError as error:
            self.reload("Not undone: %s" % error)
            return
        self.reload()

    def on_raw(self, _button):
        if self.vm is None:
            return
        try:
            text = core.raw_fragments(self.manager.stored(self.vm))
        except RuntimeError as error:
            text = str(error)
        dialog = Gtk.Dialog(title="Raw config: %s" % self.vm.label,
                            transient_for=self, modal=True)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(620, 460)
        view = Gtk.TextView(editable=False, monospace=True)
        view.set_left_margin(10)
        view.set_top_margin(10)
        view.get_buffer().set_text(text)
        scroller = Gtk.ScrolledWindow()
        scroller.add(view)
        dialog.get_content_area().pack_start(scroller, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()


def snapshot(window, path):
    """Draw the window into a PNG file, then quit. Used to check the look."""
    import cairo

    def draw():
        width = window.get_allocated_width()
        height = window.get_allocated_height()
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        window.draw(cairo.Context(surface))
        surface.write_to_png(path)
        Gtk.main_quit()
        return False

    GLib.timeout_add(600, draw)


def run(manager, snapshot_path=None):
    window = Window(manager)
    window.show_all()
    if snapshot_path:
        snapshot(window, snapshot_path)
    Gtk.main()
