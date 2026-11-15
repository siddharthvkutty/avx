#!/usr/bin/env python3
"""avX: audio/video player. GTK4 + libadwaita UI, libmpv renders into a GtkGLArea."""
import ctypes, locale, sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

import mpv

HIDE_AFTER_MS = 3000
SEEK_STEP = 5


def fmt(s):
    s = int(s or 0)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def _gl_loader():
    # GTK4 uses EGL on Wayland (and usually X11); fall back to GLX.
    for lib, fn in (("libEGL.so.1", "eglGetProcAddress"), ("libGL.so.1", "glXGetProcAddressARB")):
        try:
            f = getattr(ctypes.CDLL(lib), fn)
            f.restype = ctypes.c_void_p
            f.argtypes = [ctypes.c_char_p]
            return f
        except (OSError, AttributeError):
            pass
    raise RuntimeError("no EGL/GLX loader found")


class Player(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, default_width=960, default_height=600, title="avX")
        locale.setlocale(locale.LC_NUMERIC, "C")  # libmpv refuses to start otherwise
        self.mpv = mpv.MPV(vo="libmpv", input_default_bindings=False, input_vo_keyboard=False,
                           osc=False, hwdec="auto-safe", keep_open=True)
        self.ctx = None
        self.gl = _gl_loader()
        self.seeking = False  # guards the scale while we move it programmatically
        self.hide_id = 0

        # --- video surface
        self.area = Gtk.GLArea(hexpand=True, vexpand=True, auto_render=False)
        self.area.connect("realize", self.on_realize)
        self.area.connect("unrealize", self.on_unrealize)
        self.area.connect("render", self.on_render)

        # --- centre label (idle hint / audio-only title)
        self.hint = Gtk.Label(css_classes=["title-1"], wrap=True, justify=Gtk.Justification.CENTER,
                              can_target=False, margin_start=24, margin_end=24)

        # --- top bar
        self.top = Adw.HeaderBar(css_classes=["osd"], valign=Gtk.Align.START)
        open_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Open (Ctrl+O)")
        open_btn.connect("clicked", lambda *_: self.open_dialog())
        self.top.pack_start(open_btn)

        # --- bottom controls
        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic", css_classes=["flat", "circular"])
        self.play_btn.connect("clicked", lambda *_: self.toggle_pause())
        self.pos_lbl = Gtk.Label(label="0:00", css_classes=["numeric"])
        self.dur_lbl = Gtk.Label(label="0:00", css_classes=["numeric"])
        self.seek = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, draw_value=False)
        self.seek.set_range(0, 1)
        self.seek.connect("change-value", self.on_seek)
        vol = Gtk.VolumeButton(value=1.0)
        vol.connect("value-changed", lambda b, v: setattr(self.mpv, "volume", v * 100))
        self.vol = vol
        self.bar = Gtk.Box(spacing=8, css_classes=["osd", "toolbar"], valign=Gtk.Align.END,
                           margin_start=12, margin_end=12, margin_bottom=12)
        for w in (self.play_btn, self.pos_lbl, self.seek, self.dur_lbl, vol):
            self.bar.append(w)

        ov = Gtk.Overlay(child=self.area)
        for w in (self.hint, self.top, self.bar):
            ov.add_overlay(w)
        self.set_content(ov)

        # --- input: click = pause, double-click = fullscreen, motion = show controls
        click = Gtk.GestureClick()
        click.connect("released", self.on_click)
        self.area.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda *_: self.reveal())
        ov.add_controller(motion)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", lambda _t, files, *_: self.load([f.get_path() for f in files.get_files()]) or True)
        self.add_controller(drop)

        GLib.timeout_add(250, self.tick)
        self.connect("close-request", self.on_close)
        self.show_idle_hint()

    # ---- GL / mpv rendering
    def on_realize(self, area):
        area.make_current()
        if area.get_error():
            return
        loader = mpv.MpvGlGetProcAddressFn(lambda _ctx, name: self.gl(name))
        self.ctx = mpv.MpvRenderContext(
            self.mpv, "opengl", opengl_init_params={"get_proc_address": loader})
        self.ctx.update_cb = lambda: GLib.idle_add(area.queue_render)

    def on_unrealize(self, area):
        if self.ctx:
            area.make_current()
            self.ctx.free()
            self.ctx = None

    def on_render(self, area, _gl):
        if not self.ctx:
            return False
        fbo = ctypes.c_int()
        self.gl_get_integer(0x8CA6, ctypes.byref(fbo))  # GL_FRAMEBUFFER_BINDING
        s = area.get_scale_factor()
        self.ctx.render(opengl_fbo={"w": area.get_width() * s, "h": area.get_height() * s, "fbo": fbo.value},
                        flip_y=True)
        return True

    @property
    def gl_get_integer(self):
        f = ctypes.CFUNCTYPE(None, ctypes.c_uint, ctypes.c_void_p)(self.gl(b"glGetIntegerv"))
        return f

    # ---- playback
    def load(self, paths):
        paths = [p for p in paths if p]
        if not paths:
            return
        self.mpv.loadfile(paths[0], "replace")
        for p in paths[1:]:
            self.mpv.loadfile(p, "append")
        self.mpv.pause = False

    def toggle_pause(self):
        if self.mpv.idle_active:
            return
        self.mpv.pause = not self.mpv.pause

    def open_dialog(self):
        dlg = Gtk.FileDialog(title="Open audio/video")
        flt = Gtk.FileFilter(name="Media")
        flt.add_mime_type("audio/*")
        flt.add_mime_type("video/*")
        flt.add_mime_type("application/ogg")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dlg.set_filters(store)

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            self.load([files.get_item(i).get_path() for i in range(files.get_n_items())])
        dlg.open_multiple(self, None, done)

    def on_seek(self, _scale, _type, value):
        if not self.seeking and not self.mpv.idle_active:
            self.mpv.seek(value, "absolute")
        return False

    def tick(self):
        m = self.mpv
        idle = m.idle_active
        pos, dur = m.time_pos, m.duration
        self.seeking = True
        self.seek.set_range(0, dur or 1)
        self.seek.set_value(pos or 0)
        self.seeking = False
        self.pos_lbl.set_text(fmt(pos))
        self.dur_lbl.set_text(fmt(dur))
        self.play_btn.set_icon_name("media-playback-start-symbolic" if (m.pause or idle)
                                    else "media-playback-pause-symbolic")
        title = m.media_title
        self.set_title(f"{title} - avX" if title and not idle else "avX")
        if idle:
            self.show_idle_hint()
        elif m.video_format is None:  # audio only: no picture, so show the title
            self.hint.set_text(title or "")
            self.hint.set_visible(True)
        else:
            self.hint.set_visible(False)
        return True

    def show_idle_hint(self):
        self.hint.set_text("Drop a file here or press Ctrl+O")
        self.hint.set_visible(True)

    # ---- UI behaviour
    def on_click(self, gesture, n_press, *_):
        if n_press == 2:
            self.toggle_fullscreen()
        elif n_press == 1:
            self.toggle_pause()

    def toggle_fullscreen(self):
        self.set_fullscreened(not self.is_fullscreen())

    def reveal(self):
        for w in (self.top, self.bar):
            w.set_visible(True)
        self.set_cursor(None)
        if self.hide_id:
            GLib.source_remove(self.hide_id)
        self.hide_id = GLib.timeout_add(HIDE_AFTER_MS, self.conceal)

    def conceal(self):
        self.hide_id = 0
        if not self.mpv.pause and not self.mpv.idle_active:
            self.top.set_visible(False)
            self.bar.set_visible(False)
            self.set_cursor(Gdk.Cursor.new_from_name("none"))
        return False

    def on_key(self, _c, keyval, _code, state):
        name = Gdk.keyval_name(keyval)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        m = self.mpv
        if ctrl and name == "o":
            self.open_dialog()
        elif name == "space":
            self.toggle_pause()
        elif name == "Left":
            m.seek(-SEEK_STEP, "relative")
        elif name == "Right":
            m.seek(SEEK_STEP, "relative")
        elif name == "Up":
            self.vol.set_value(min(1.3, self.vol.get_value() + 0.05))
        elif name == "Down":
            self.vol.set_value(max(0, self.vol.get_value() - 0.05))
        elif name == "m":
            m.mute = not m.mute
        elif name in ("f", "F11"):
            self.toggle_fullscreen()
        elif name == "Escape" and self.is_fullscreen():
            self.set_fullscreened(False)
        elif name == "greater":
            m.command("playlist-next")
        elif name == "less":
            m.command("playlist-prev")
        elif name == "q":
            self.close()
        else:
            return False
        self.reveal()
        return True

    def on_close(self, *_):
        if self.ctx:
            self.area.make_current()
            self.ctx.free()
            self.ctx = None
        self.mpv.terminate()
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.avx", flags=Gio.ApplicationFlags.HANDLES_OPEN)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    def get_win(self):
        return self.props.active_window or Player(self)

    def do_activate(self):
        self.get_win().present()

    def do_open(self, files, _n, _hint):
        win = self.get_win()
        win.present()
        win.load([f.get_path() for f in files])


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
