"""The window: one VM at a time, its switches grouped, every change shown."""
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from . import core  # noqa: E402

# Colours from the GNOME palette, which reads on light and dark themes.
CSS = b"""
.convirt-heading { font-size: 85%; font-weight: bold; opacity: 0.55;
                   margin: 6px 4px 0 4px; }
.convirt-title { font-weight: bold; }
.convirt-meaning { opacity: 0.7; font-size: 90%; }
.convirt-row { padding: 10px 12px;
               transition: background-color 900ms ease-out; }
.convirt-row.pending { box-shadow: inset 3px 0 0 #e5a50a; }
.convirt-row.changed { background-color: alpha(#3584e4, 0.22);
                       transition: none; }
.convirt-badge { font-size: 80%; font-weight: bold; color: #1e1e1e;
                 background-color: #e5a50a; border-radius: 9px;
                 padding: 1px 8px; }
.convirt-pill { font-size: 85%; font-weight: bold; border-radius: 10px;
                padding: 2px 10px; }
.convirt-pill.running { background-color: #26a269; color: #ffffff; }
.convirt-pill.off { background-color: alpha(currentColor, 0.15); }
.convirt-status { border-radius: 8px; padding: 6px 12px; min-height: 34px;
                  background-color: alpha(currentColor, 0.06);
                  box-shadow: inset 3px 0 0 alpha(currentColor, 0.25);
                  transition: background-color 600ms ease-out; }
.convirt-status.done { background-color: alpha(#26a269, 0.20);
                       box-shadow: inset 3px 0 0 #26a269;
                       transition: none; }
.convirt-status.failed { background-color: alpha(#c01c28, 0.20);
                         box-shadow: inset 3px 0 0 #c01c28;
                         transition: none; }
.convirt-status.pending { background-color: alpha(#e5a50a, 0.18);
                          box-shadow: inset 3px 0 0 #e5a50a; }
.convirt-dim { opacity: 0.7; }
.convirt-more { padding: 4px 12px 8px 12px; }
"""

# Each section: its heading, the rows always shown, and the rows under a
# "More" expander at its foot.
SECTIONS = (
    ("Sound", ("sound", "mic", "audio", "speaker", "recorder"),
     ("latency", "rate")),
    ("Picture", ("accel3d", "display"), ()),
    ("Links to the host", ("clipboard", "network"), ()),
)
MORE = {"Sound": "Latency and sample rate"}
ICONS = {
    "sound": "audio-card-symbolic",
    "mic": "audio-input-microphone-symbolic",
    "audio": "computer-symbolic",
    "speaker": "audio-speakers-symbolic",
    "recorder": "media-record-symbolic",
    "latency": "preferences-system-time-symbolic",
    "rate": "emblem-system-symbolic",
    "accel3d": "applications-graphics-symbolic",
    "display": "video-display-symbolic",
    "clipboard": "edit-paste-symbolic",
    "network": "network-wired-symbolic",
}
# The rows that are on or off; every other row chooses from a list.
TOGGLES = ("accel3d", "clipboard")
# The space the header, the status lines and the buttons take; the rows
# get the rest of the screen before they scroll.
CHROME_HEIGHT = 330
SMALLEST_ROWS_HEIGHT = 360


def rows_height():
    """The tallest the rows grow before they scroll, from the screen."""
    display = Gdk.Display.get_default()
    monitor = display.get_primary_monitor() or display.get_monitor(0) \
        if display else None
    if monitor is None:
        return 640
    height = monitor.get_workarea().height
    return max(SMALLEST_ROWS_HEIGHT, height - CHROME_HEIGHT)
BANNER_SECONDS = 7
# How long a guest gets to power off before Force off is offered.
SHUTDOWN_SECONDS = 60


class Window(Gtk.Window):
    def __init__(self, manager, initial=None):
        super().__init__(title="convirt")
        self.manager = manager
        self.initial = initial
        self.vms = []
        self.vm = None
        self.loading = False
        self.controls = {}
        self.meanings = {}
        self.badges = {}
        self.lines = {}
        self.banner_timer = None
        # While a restart runs: the seconds left before Force off is offered.
        self.restarting = None
        self.start_after = False
        self.set_default_size(680, -1)
        self.set_border_width(18)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(box)

        header = Gtk.Box(spacing=10)
        header.pack_start(Gtk.Label(label="VM"), False, False, 0)
        self.picker = Gtk.ComboBoxText()
        self.picker.connect("changed", self.on_pick)
        header.pack_start(self.picker, False, False, 0)
        self.open_button = Gtk.Button(label="Open")
        self.open_button.connect("clicked", self.on_open)
        header.pack_end(self.open_button, False, False, 0)
        self.power = Gtk.Button(label="Start")
        self.power.connect("clicked", self.on_power)
        header.pack_end(self.power, False, False, 0)
        self.running = Gtk.Label()
        self.running.get_style_context().add_class("convirt-pill")
        self.running.set_valign(Gtk.Align.CENTER)
        header.pack_end(self.running, False, False, 0)
        box.pack_start(header, False, False, 0)

        # The rows scroll once they outgrow the screen; the header above
        # and the two status lines below stay where they are, so nothing
        # moves when a change is confirmed or starts to wait.
        sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for heading, names, more in SECTIONS:
            label = Gtk.Label(label=heading.upper(), xalign=0)
            label.get_style_context().add_class("convirt-heading")
            sections.pack_start(label, False, False, 0)
            rows = Gtk.ListBox()
            rows.set_selection_mode(Gtk.SelectionMode.NONE)
            frame = Gtk.Frame()
            frame.add(rows)
            sections.pack_start(frame, False, False, 0)
            for name in names:
                row = Gtk.ListBoxRow()
                row.set_activatable(False)
                row.add(self.build_row(name))
                rows.add(row)
            if more:
                inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                for name in more:
                    inner.pack_start(self.build_row(name), False, False, 0)
                expander = Gtk.Expander(label=MORE[heading])
                expander.get_style_context().add_class("convirt-more")
                expander.add(inner)
                row = Gtk.ListBoxRow()
                row.set_activatable(False)
                row.add(expander)
                rows.add(row)
        self.attached = Gtk.Label(xalign=0)
        self.attached.set_line_wrap(True)
        self.attached.get_style_context().add_class("convirt-dim")
        sections.pack_start(self.attached, False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(rows_height())
        scroller.add(sections)
        box.pack_start(scroller, True, True, 0)

        box.pack_start(self.build_banner(), False, False, 0)

        self.waiting = Gtk.Box(spacing=10)
        self.waiting.get_style_context().add_class("convirt-status")
        self.waiting_text = Gtk.Label(xalign=0)
        self.waiting_text.set_ellipsize(Pango.EllipsizeMode.END)
        self.waiting.pack_start(self.waiting_text, True, True, 0)
        self.apply_button = Gtk.Button(label="Shut down and start")
        self.apply_button.get_style_context().add_class("suggested-action")
        self.apply_button.set_valign(Gtk.Align.CENTER)
        self.apply_button.set_no_show_all(True)
        self.apply_button.connect("clicked", self.on_apply)
        self.waiting.pack_end(self.apply_button, False, False, 0)
        box.pack_start(self.waiting, False, False, 0)

        slots = Gtk.Box(spacing=8)
        slots.pack_start(Gtk.Label(label="Slot"), False, False, 0)
        self.slot_picker = Gtk.ComboBoxText()
        slots.pack_start(self.slot_picker, True, True, 0)
        self.restore = Gtk.Button(label="Restore")
        self.restore.connect("clicked", self.on_restore)
        slots.pack_start(self.restore, False, False, 0)
        save = Gtk.Button(label="Save as…")
        save.connect("clicked", self.on_save)
        slots.pack_start(save, False, False, 0)
        self.extras = [self.slot_picker, save]
        box.pack_start(slots, False, False, 0)

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

    def build_banner(self):
        """The line that confirms each change, or says why it failed.

        It always holds its place: green or red while news is fresh, then
        it fades to grey and keeps saying what happened last.
        """
        line = Gtk.Box(spacing=10)
        line.get_style_context().add_class("convirt-status")
        self.banner_icon = Gtk.Image.new_from_icon_name(
            "emblem-default-symbolic", Gtk.IconSize.BUTTON)
        line.pack_start(self.banner_icon, False, False, 0)
        self.banner_title = Gtk.Label(xalign=0, label="No changes yet")
        self.banner_title.get_style_context().add_class("convirt-title")
        line.pack_start(self.banner_title, False, False, 0)
        self.banner_detail = Gtk.Label(xalign=0, label=" ")
        self.banner_detail.set_ellipsize(Pango.EllipsizeMode.END)
        self.banner_detail.get_style_context().add_class("convirt-meaning")
        line.pack_start(self.banner_detail, True, True, 0)
        self.banner_undo = Gtk.Button(label="Undo")
        self.banner_undo.set_valign(Gtk.Align.CENTER)
        self.banner_undo.set_no_show_all(True)
        self.banner_undo.connect("clicked", self.on_banner_action)
        self.banner_action = self.on_undo
        line.pack_end(self.banner_undo, False, False, 0)
        self.banner_line = line
        return line

    def build_row(self, name):
        line = Gtk.Box(spacing=12)
        line.get_style_context().add_class("convirt-row")
        icon = Gtk.Image.new_from_icon_name(ICONS[name], Gtk.IconSize.BUTTON)
        icon.get_style_context().add_class("convirt-dim")
        line.pack_start(icon, False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        titles = Gtk.Box(spacing=8)
        title = Gtk.Label(label=core.TITLES[name], xalign=0)
        title.get_style_context().add_class("convirt-title")
        titles.pack_start(title, False, False, 0)
        badge = Gtk.Label(label="at next start")
        badge.get_style_context().add_class("convirt-badge")
        badge.set_no_show_all(True)
        titles.pack_start(badge, False, False, 0)
        meaning = Gtk.Label(xalign=0)
        meaning.set_ellipsize(Pango.EllipsizeMode.END)
        meaning.get_style_context().add_class("convirt-meaning")
        text.pack_start(titles, False, False, 0)
        text.pack_start(meaning, False, False, 0)
        line.pack_start(text, True, True, 0)

        if name == "accel3d":
            details = Gtk.Button(label="Details")
            details.get_style_context().add_class("flat")
            details.set_valign(Gtk.Align.CENTER)
            details.connect("clicked", self.on_check)
            line.pack_end(details, False, False, 0)
            self.details = details

        if name in TOGGLES:
            control = Gtk.Switch()
            control.connect("notify::active", self.on_switch, name)
        else:
            control = Gtk.ComboBoxText()
            # Long host device names would widen the window; they are cut
            # in the list and said in full on the row's line.
            cell = control.get_cells()[0]
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
            cell.set_property("max-width-chars", 26)
            control.connect("changed", self.on_choose, name)
        control.set_valign(Gtk.Align.CENTER)
        line.pack_end(control, False, False, 0)
        self.controls[name] = control
        self.meanings[name] = meaning
        self.badges[name] = badge
        self.lines[name] = line
        return line

    # ------------------------------------------------------------ feedback

    def on_banner_action(self, button):
        self.banner_action(button)

    def announce(self, title, detail="", failed=False, undo=False,
                 action=None):
        """Show the banner. undo offers Undo; action is (label, callback)
        for any other single follow-up, such as Force off."""
        if action:
            self.banner_undo.set_label(action[0])
            self.banner_action = action[1]
            undo = True
        else:
            self.banner_undo.set_label("Undo")
            self.banner_action = self.on_undo
        style = self.banner_line.get_style_context()
        style.remove_class("done" if failed else "failed")
        style.add_class("failed" if failed else "done")
        self.banner_icon.set_from_icon_name(
            "dialog-error-symbolic" if failed else "emblem-ok-symbolic",
            Gtk.IconSize.BUTTON)
        self.banner_title.set_text(title)
        self.banner_title.set_tooltip_text(title)
        self.banner_detail.set_text(detail or " ")
        self.banner_detail.set_tooltip_text(detail or None)
        self.banner_undo.set_visible(undo)
        if self.banner_timer:
            GLib.source_remove(self.banner_timer)
        self.banner_timer = GLib.timeout_add_seconds(
            BANNER_SECONDS, self.hide_banner)

    def hide_banner(self, reset=False):
        """Let the news fade: the line goes grey and keeps its words; a
        reset, on picking another VM, clears them too."""
        style = self.banner_line.get_style_context()
        style.remove_class("done")
        style.remove_class("failed")
        if reset:
            self.banner_icon.set_from_icon_name(
                "emblem-default-symbolic", Gtk.IconSize.BUTTON)
            self.banner_title.set_text("No changes yet")
            self.banner_detail.set_text(" ")
            self.banner_undo.set_visible(False)
        if self.banner_timer and reset:
            GLib.source_remove(self.banner_timer)
        self.banner_timer = None
        return False

    def flash(self, name):
        style = self.lines[name].get_style_context()
        style.add_class("changed")

        def fade():
            style.remove_class("changed")
            return False

        GLib.timeout_add(250, fade)

    # ------------------------------------------------------------ loading

    def reload_list(self):
        keep = self.vm.label if self.vm else None
        try:
            self.vms = self.manager.list()
        except RuntimeError as error:
            self.vms = []
            self.announce("No VMs", str(error), failed=True)
        self.loading = True
        self.picker.remove_all()
        for vm in self.vms:
            self.picker.append_text(vm.label)
        self.loading = False
        if not self.vms:
            self.vm = None
            self.set_sensitive_rows(False)
            self.waiting_text.set_text(
                "No VM found under qemu:///session or qemu:///system")
            self.apply_button.set_visible(False)
            self.power.set_sensitive(False)
            self.open_button.set_sensitive(False)
            for meaning in self.meanings.values():
                meaning.set_text("")
            return
        labels = [vm.label for vm in self.vms]
        index = labels.index(keep) if keep in labels else 0
        if keep is None and self.initial:
            name, uri = self.initial
            index = next((i for i, vm in enumerate(self.vms)
                          if vm.name == name and uri in (None, vm.uri)), 0)
        self.picker.set_active(index)

    def set_sensitive_rows(self, sensitive):
        for control in list(self.controls.values()) + self.extras:
            control.set_sensitive(sensitive)
        self.undo.set_sensitive(sensitive)
        self.restore.set_sensitive(sensitive)

    def reload(self):
        """Read the selected VM again and redraw every row."""
        if self.vm is None:
            return
        try:
            self.manager.refresh_running(self.vm)
            state = self.manager.state(self.vm)
            pending = self.manager.pending(self.vm)
            live = self.manager.live_values(self.vm)
            attached = core.exposures(self.manager.stored(self.vm))
            slots = self.manager.slots(self.vm)
            choices = self.manager.choices(self.vm)
        except RuntimeError as error:
            self.set_sensitive_rows(False)
            self.announce("Cannot read this VM", str(error), failed=True)
            return
        for control in self.extras:
            control.set_sensitive(True)
        self.attached.set_text(
            "Also attached: " + ", ".join(attached) if attached
            else "Nothing else links this VM to the host")
        chosen = self.slot_picker.get_active_text()
        self.slot_picker.remove_all()
        for slot in slots:
            self.slot_picker.append_text(slot)
        if slots:
            self.slot_picker.set_active(
                slots.index(chosen) if chosen in slots else 0)
        self.restore.set_sensitive(bool(slots))

        self.loading = True
        for name in core.SWITCHES:
            entry = state[name]
            control = self.controls[name]
            if name not in TOGGLES:
                control.remove_all()
                for value, label in choices[name]:
                    control.append(value, label)
                if entry["value"] not in dict(choices[name]):
                    control.append(entry["value"], entry["value"])
                control.set_active_id(entry["value"])
            else:
                control.set_active(entry["value"] == "on")
            control.set_sensitive(entry["enabled"])
            waiting = name in pending
            meaning = entry["meaning"]
            if name == "accel3d":
                meaning = self.verdict_text(meaning)
            if waiting and live:
                labels = dict(choices.get(name, ()))
                running = labels.get(live[name]) or core.value_label(
                    name, live[name])
                meaning = "now: %s → next start: %s" % (running, meaning)
            self.meanings[name].set_text(meaning)
            self.meanings[name].set_tooltip_text(meaning)
            self.badges[name].set_visible(waiting)
            style = self.lines[name].get_style_context()
            (style.add_class if waiting else style.remove_class)("pending")
        self.loading = False

        pill = self.running.get_style_context()
        if self.vm.running:
            self.running.set_text("Running")
            pill.remove_class("off")
            pill.add_class("running")
        else:
            self.running.set_text("Shut off")
            pill.remove_class("running")
            pill.add_class("off")
        busy = self.restarting is not None
        self.power.set_label("Shut down" if self.vm.running else "Start")
        self.power.set_sensitive(not busy)
        own = state["display"]["value"] in core.OWN_WINDOW
        self.open_button.set_sensitive(not own and not busy)
        self.open_button.set_tooltip_text(
            "An own-window display opens when the VM starts" if own
            else "Show the VM's screen in virt-manager")
        self.undo.set_sensitive(self.manager.can_undo(self.vm))
        style = self.waiting.get_style_context()
        if pending:
            text = "%s: %s" % (
                "1 change waits" if len(pending) == 1
                else "%d changes wait" % len(pending),
                ", ".join(core.TITLES[n] for n in pending))
            style.add_class("pending")
            self.apply_button.set_sensitive(not busy)
        elif self.vm.running:
            text = "The running VM matches these settings"
            style.remove_class("pending")
        else:
            text = "Shut off: every setting applies at the next start"
            style.remove_class("pending")
        self.waiting_text.set_text(text)
        self.waiting_text.set_tooltip_text(text)
        self.apply_button.set_visible(bool(pending))

    def verdict_text(self, meaning):
        """The 3D row's line: the setting, then what the check found."""
        try:
            verdict, text = self.manager.check_3d(self.vm, write=False)
        except RuntimeError:
            return meaning
        if verdict == "working":
            return "✓ working in QEMU · " + meaning
        if verdict == "not working":
            reason = next((line[5:].strip() for line in text.splitlines()
                           if line.startswith("FAIL")), "")
            return "✗ not working: " + " ".join(reason.split())
        return meaning

    # ----------------------------------------------------------- handlers

    def on_pick(self, picker):
        if self.loading:
            return
        index = picker.get_active()
        self.vm = self.vms[index] if index >= 0 else None
        self.hide_banner(reset=True)
        self.reload()

    def on_switch(self, control, _param, name):
        if self.loading:
            return
        self.change(name, "on" if control.get_active() else "off",
                    "on" if control.get_active() else "off")

    def on_choose(self, control, name):
        if self.loading or control.get_active_id() is None:
            return
        self.change(name, control.get_active_id(), control.get_active_text())

    def change(self, name, value, label):
        title = "%s: %s" % (core.TITLES[name], label)
        try:
            self.manager.change(self.vm, name, value)
        except RuntimeError as error:
            self.reload()
            self.announce("Not changed: " + title, str(error), failed=True)
            return
        self.reload()
        self.flash(name)
        if self.vm.running:
            detail = "Saved; applies after a full shut down and start"
        else:
            detail = "Saved; applies at the next start"
        self.announce(title, detail, undo=True)

    # ---------------------------------------------------------- lifecycle

    def on_power(self, _button):
        if self.vm.running:
            self.shut_down(start_after=False)
        else:
            self.start_vm()

    def on_apply(self, _button):
        self.shut_down(start_after=True)

    def start_vm(self):
        try:
            self.manager.start(self.vm)
        except RuntimeError as error:
            self.reload()
            self.announce("Did not start", str(error), failed=True)
            return
        self.reload()
        self.announce("Starting " + self.vm.label)
        # QEMU writes its log and opens its display in the first seconds.
        GLib.timeout_add_seconds(4, self.after_start)

    def after_start(self):
        self.reload()
        verdict, _text = self.manager.check_3d(self.vm)
        detail = "The running VM now matches these settings."
        if verdict == "working":
            detail += " 3D is working in QEMU."
        elif verdict == "not working":
            detail += " 3D is not working; Details on its row says why."
        self.announce("Started " + self.vm.label, detail,
                      failed=verdict == "not working")
        return False

    def shut_down(self, start_after):
        try:
            self.manager.shutdown(self.vm)
        except RuntimeError as error:
            self.reload()
            self.announce("Did not shut down", str(error), failed=True)
            return
        self.restarting = SHUTDOWN_SECONDS
        self.start_after = start_after
        self.picker.set_sensitive(False)
        self.reload()
        self.announce("Shutting down " + self.vm.label,
                      "Waiting for the guest to power off; then it starts "
                      "again" if start_after
                      else "Waiting for the guest to power off")
        GLib.timeout_add_seconds(1, self.poll_shutdown)

    def poll_shutdown(self):
        if not self.manager.refresh_running(self.vm):
            self.finish_shutdown()
            return False
        self.restarting -= 1
        if self.restarting > 0:
            return True
        self.restarting = None
        self.picker.set_sensitive(True)
        self.reload()
        self.announce(
            "Still running after %d s" % SHUTDOWN_SECONDS,
            "The guest did not power off. Force off cuts its power, "
            "like pulling the plug.", failed=True,
            action=("Force off", self.on_force_off))
        return False

    def finish_shutdown(self):
        self.restarting = None
        self.picker.set_sensitive(True)
        if self.start_after:
            self.start_vm()
        else:
            self.reload()
            self.announce("Shut down " + self.vm.label)

    def on_force_off(self, _button):
        try:
            self.manager.force_off(self.vm)
        except RuntimeError as error:
            self.announce("Did not power off", str(error), failed=True)
            return
        self.finish_shutdown()

    def on_open(self, _button):
        try:
            if not self.vm.running:
                self.manager.start(self.vm)
            self.manager.open_screen(self.vm)
        except RuntimeError as error:
            self.announce("Cannot open the screen", str(error), failed=True)
        self.reload()

    def on_check(self, _button):
        if self.vm is None:
            return
        try:
            verdict, text = self.manager.check_3d(self.vm)
        except RuntimeError as error:
            verdict, text = "unknown", str(error)
        dialog = Gtk.Dialog(title="3D check: %s" % self.vm.label,
                            transient_for=self, modal=True)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(640, 360)
        view = Gtk.TextView(editable=False, monospace=True)
        view.set_left_margin(10)
        view.set_top_margin(10)
        view.get_buffer().set_text(
            text + "\nSaved in " + self.manager.check_file(self.vm) + "\n")
        scroller = Gtk.ScrolledWindow()
        scroller.add(view)
        dialog.get_content_area().pack_start(scroller, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def on_restore(self, _button):
        slot = self.slot_picker.get_active_text()
        if slot is None:
            return
        try:
            self.manager.restore_slot(self.vm, slot)
        except RuntimeError as error:
            self.reload()
            self.announce("Not restored: " + slot, str(error), failed=True)
            return
        self.reload()
        self.announce("Slot restored: " + slot, undo=True)

    def on_save(self, _button):
        dialog = Gtk.Dialog(title="Save slot", transient_for=self,
                            modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_placeholder_text("Slot name")
        entry.set_max_length(32)
        entry.set_activates_default(True)
        area = dialog.get_content_area()
        area.set_border_width(12)
        area.pack_start(entry, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        name = entry.get_text()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        try:
            self.manager.save_slot(self.vm, name)
        except RuntimeError as error:
            self.reload()
            self.announce("Not saved", str(error), failed=True)
            return
        self.reload()
        self.announce("Slot saved: " + name.strip())

    def on_undo(self, _button):
        try:
            self.manager.undo(self.vm)
        except RuntimeError as error:
            self.reload()
            self.announce("Not undone", str(error), failed=True)
            return
        self.reload()
        self.announce("Last change undone")

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


def snapshot(window, path, change=None):
    """Draw the window into a PNG file, then quit. Used to check the look.

    The content is drawn offscreen at its full natural size, with every
    "More" expander open, so the picture holds every row whatever size a
    compositor would give the window. change is an optional (switch,
    value) made first, so the picture shows how the window answers it.
    """
    child = window.get_child()
    window.remove(child)
    offscreen = Gtk.OffscreenWindow()
    frame = Gtk.Box()
    frame.set_border_width(window.get_border_width())
    frame.pack_start(child, True, True, 0)
    offscreen.add(frame)
    offscreen.set_size_request(window.get_default_size()[0], -1)

    def open_all(widget):
        if isinstance(widget, Gtk.Expander):
            widget.set_expanded(True)
        if isinstance(widget, Gtk.ScrolledWindow):
            # An offscreen window sizes a scroller by its minimum.
            widget.set_max_content_height(-1)
            widget.set_min_content_height(
                widget.get_child().get_preferred_height()[1])
        if isinstance(widget, Gtk.Container):
            widget.forall(open_all)

    offscreen.show_all()
    window.hide()

    def act():
        if change:
            name, value = change
            control = window.controls[name]
            if name not in TOGGLES:
                control.set_active_id(value)
            else:
                control.set_active(value == "on")
        return False

    def expand():
        open_all(frame)
        return False

    def draw():
        offscreen.get_pixbuf().savev(path, "png", [], [])
        Gtk.main_quit()
        return False

    # Open the expanders, let them take their size, then size the
    # scroller to the rows, then draw.
    GLib.timeout_add(300, act)
    GLib.timeout_add(600, expand)
    GLib.timeout_add(1000, expand)
    GLib.timeout_add(1500, draw)


def run(manager, snapshot_path=None, change=None, initial=None):
    """initial is (name, uri or None): the VM to show first."""
    window = Window(manager, initial)
    window.show_all()
    window.reload()
    if snapshot_path:
        snapshot(window, snapshot_path, change)
    Gtk.main()
