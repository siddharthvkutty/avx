#!/usr/bin/env python3
"""avX: audio/video player. GTK4 + libadwaita UI, libmpv renders into a GtkGLArea."""
import ctypes, locale, os, sys, time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

import mpv

HIDE_AFTER_MS = 3000
SEEK_STEP = 5
RESTART_AFTER = 3  # seconds into a track after which Previous restarts it instead of going back
FADE_MS = 450       # keep in step with the CSS transition below
HERE = os.path.dirname(os.path.abspath(__file__))
APP_ID = "io.github.avx"
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2]
REPEAT = [  # (icon, tooltip): off -> this file -> whole playlist
    ("media-playlist-repeat-symbolic", "Repeat: off"),
    ("media-playlist-repeat-song-symbolic", "Repeat: this file"),
    ("media-playlist-repeat-symbolic", "Repeat: playlist"),
]

CSS = """
.dock { background: alpha(#1c1e24, .88); border-radius: 22px; padding: 10px 16px 8px 16px;
        box-shadow: 0 10px 40px alpha(black, .55); color: #eceef2; }
.dock button, .dock menubutton > button { color: #eceef2; }
.dock button.on { color: #b878ff; }
.dock .time { color: alpha(#eceef2, .7); }
scale.seek trough { min-height: 4px; border-radius: 2px; background: alpha(white, .2); }
scale.seek highlight { background: #b878ff; border-radius: 2px; }
scale.seek slider { min-width: 12px; min-height: 12px; margin: -5px; background: white;
                    border-radius: 6px; box-shadow: none; opacity: 0; transition: opacity 150ms; }
scale.seek:hover slider { opacity: 1; }
progressbar.hairline trough { min-height: 2px; background: alpha(white, .15); border-radius: 0; }
progressbar.hairline progress { min-height: 2px; background: #b878ff; border-radius: 0; }
.scrim { background: linear-gradient(alpha(black, .7), transparent); }
.scrim headerbar { background: none; box-shadow: none; color: #eceef2; }
.fade { transition: opacity 120ms ease-out; }
.fade.gone { opacity: 0; transition: opacity 450ms ease-in-out; }
.stage { background: black; }
.hint-title { font-size: 24pt; font-weight: 700; color: #eceef2; }
.hint-sub { font-size: 12pt; color: alpha(#eceef2, .6); }
.dock list { background: none; }
"""


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


def icon_button(icon, tip, cb):
    b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat"], focus_on_click=False)
    b.connect("clicked", lambda *_: cb())
    return b


class Player(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, default_width=1000, default_height=620, title="avX")
        locale.setlocale(locale.LC_NUMERIC, "C")  # libmpv refuses to start otherwise
        # dev runs from the source tree; an installed copy finds the icon through the normal theme path
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(os.path.join(HERE, "data", "icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._css(), Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.mpv = mpv.MPV(vo="libmpv", input_default_bindings=False, input_vo_keyboard=False,
                           osc=False, hwdec="auto-safe", keep_open=True,
                           sub_auto="fuzzy", save_position_on_quit=True)
        self.ctx = None
        self.gl = _gl_loader()
        self.seeking = False  # guards the scale while we move it programmatically
        self.hide_id = 0
        self.settle_id = 0
        self.last_ptr = None
        self.repeat = 0
        self.shuffled = False
        self.playlist_key = None

        # --- video surface
        self.area = Gtk.GLArea(hexpand=True, vexpand=True, auto_render=False)
        self.area.connect("realize", self.on_realize)
        self.area.connect("unrealize", self.on_unrealize)
        self.area.connect("render", self.on_render)

        # --- centre text: idle hint, or title/artist for audio-only
        self.hint_title = Gtk.Label(css_classes=["hint-title"], wrap=True, justify=Gtk.Justification.CENTER)
        self.hint_sub = Gtk.Label(css_classes=["hint-sub"], wrap=True, justify=Gtk.Justification.CENTER)
        # opaque and full-size: with no video mpv stops drawing, so the last frame would show through
        self.logo = Gtk.Image(icon_name=APP_ID, pixel_size=160, margin_bottom=12)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER,
                        valign=Gtk.Align.CENTER, margin_start=24, margin_end=24)
        for w in (self.logo, self.hint_title, self.hint_sub):
            inner.append(w)
        self.hint = Gtk.Box(can_target=False, css_classes=["stage"])
        self.hint.append(inner)
        inner.set_hexpand(True)

        # --- top bar (gradient scrim + flat header)
        self.header = header = Adw.HeaderBar(css_classes=["flat"])
        header.set_decoration_layout(":minimize,maximize,close")  # same look on every desktop, no title-bar icon
        header.pack_start(icon_button("document-open-symbolic", "Open (Ctrl+O)", self.open_dialog))
        self.plist_btn = Gtk.ToggleButton(icon_name="view-list-symbolic", tooltip_text="Playlist (P)",
                                          css_classes=["flat"], focus_on_click=False)
        self.plist_btn.connect("toggled", lambda b: self.panel.set_reveal_child(b.get_active()))
        header.pack_end(self.plist_btn)
        self.top = Gtk.Box(css_classes=["scrim", "fade"], valign=Gtk.Align.START)
        self.top.append(header)
        header.set_hexpand(True)

        # --- dock
        self.play_btn = icon_button("media-playback-start-symbolic", "Play/pause (Space)", self.toggle_pause)
        self.pos_lbl = Gtk.Label(label="0:00", css_classes=["numeric", "time"])
        self.dur_lbl = Gtk.Label(label="0:00", css_classes=["numeric", "time"])
        self.seek = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, draw_value=False,
                              css_classes=["seek"])
        self.seek.set_range(0, 1)
        self.seek.connect("change-value", self.on_seek)
        row1 = Gtk.Box(spacing=12)
        for w in (self.pos_lbl, self.seek, self.dur_lbl):
            row1.append(w)

        self.vol = Gtk.VolumeButton(value=1.0, focus_on_click=False)
        self.vol.connect("value-changed", lambda b, v: setattr(self.mpv, "volume", v * 100))
        left = Gtk.Box(spacing=2)
        for w in (icon_button("media-skip-backward-symbolic", "Previous: restart, then previous track (<)", self.previous),
                  icon_button("media-seek-backward-symbolic", f"Back {SEEK_STEP}s (Left)", lambda: self.seek_by(-SEEK_STEP)),
                  self.play_btn,
                  icon_button("media-seek-forward-symbolic", f"Forward {SEEK_STEP}s (Right)", lambda: self.seek_by(SEEK_STEP)),
                  icon_button("media-skip-forward-symbolic", "Next (>)", lambda: self.step(1)),
                  self.vol):
            left.append(w)

        self.subs_btn = self.menu_button("media-view-subtitles-symbolic", "Subtitles (V toggles)", self.build_subs)
        self.audio_btn = self.menu_button("audio-headphones-symbolic", "Audio track", self.build_audio)
        self.speed_btn = self.menu_button(None, "Speed ([ and ])", self.build_speed, label="1×")
        self.shuffle_btn = icon_button("media-playlist-shuffle-symbolic", "Shuffle", self.toggle_shuffle)
        self.repeat_btn = icon_button(REPEAT[0][0], REPEAT[0][1] + " (R)", self.cycle_repeat)
        self.menu_btns = [self.subs_btn, self.audio_btn, self.speed_btn]
        right = Gtk.Box(spacing=2)
        for w in (self.subs_btn, self.audio_btn, self.speed_btn, self.shuffle_btn, self.repeat_btn,
                  icon_button("camera-photo-symbolic", "Screenshot (S)", self.screenshot),
                  icon_button("view-fullscreen-symbolic", "Fullscreen (F)", self.toggle_fullscreen)):
            right.append(w)
        row2 = Gtk.CenterBox(start_widget=left, end_widget=right)

        dock = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["dock"])
        dock.append(row1)
        dock.append(row2)
        self.dock = Adw.Clamp(child=dock, maximum_size=900, valign=Gtk.Align.END, css_classes=["fade"],
                              margin_start=16, margin_end=16, margin_bottom=16)
        self.hairline = Gtk.ProgressBar(css_classes=["hairline", "fade", "gone"], valign=Gtk.Align.END,
                                        can_target=False)

        # --- playlist panel
        self.plist = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.plist.connect("row-activated", lambda _l, r: self.cmd("playlist-play-index", r.get_index()))
        head = Gtk.Box(spacing=6)
        head.append(Gtk.Label(label="Playlist", xalign=0, hexpand=True, css_classes=["heading"]))
        head.append(icon_button("list-add-symbolic", "Add files to playlist", lambda: self.open_dialog(append=True)))
        scroll = Gtk.ScrolledWindow(child=self.plist, vexpand=True, min_content_width=300, hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["dock"], width_request=320)
        box.append(head)
        box.append(scroll)
        self.panel = Gtk.Revealer(child=box, transition_type=Gtk.RevealerTransitionType.SLIDE_LEFT,
                                  halign=Gtk.Align.END, valign=Gtk.Align.FILL,
                                  margin_top=64, margin_bottom=150, margin_end=16)

        ov = Gtk.Overlay(child=self.area)
        for w in (self.hint, self.top, self.panel, self.hairline, self.dock):
            ov.add_overlay(w)
        self.toasts = Adw.ToastOverlay(child=ov)
        self.set_content(self.toasts)

        # --- input: click = pause, double-click = fullscreen, wheel = volume, motion = show controls
        click = Gtk.GestureClick()
        click.connect("released", self.on_click)
        self.area.add_controller(click)
        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll", lambda _c, _dx, dy: self.nudge_volume(-0.05 * dy) or True)
        self.area.add_controller(wheel)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        ov.add_controller(motion)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)  # before focused buttons
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", lambda _t, files, *_: self.load([f.get_path() for f in files.get_files()]) or True)
        self.add_controller(drop)

        # no window buttons in fullscreen; Escape/F leave it
        self.connect("notify::fullscreened", lambda *_: self.header.set_show_end_title_buttons(not self.is_fullscreen()))
        self.tick_id = GLib.timeout_add(250, self.tick)
        self.connect("close-request", self.on_close)
        self.reveal()

    @staticmethod
    def _css():
        p = Gtk.CssProvider()
        p.load_from_string(CSS)
        return p

    # ---- GL / mpv rendering
    def on_realize(self, area):
        area.make_current()
        if area.get_error():
            return
        loader = mpv.MpvGlGetProcAddressFn(lambda _ctx, name: self.gl(name))
        self.get_integer = ctypes.CFUNCTYPE(None, ctypes.c_uint, ctypes.c_void_p)(self.gl(b"glGetIntegerv"))
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
        self.get_integer(0x8CA6, ctypes.byref(fbo))  # GL_FRAMEBUFFER_BINDING
        s = area.get_scale_factor()
        self.ctx.render(opengl_fbo={"w": area.get_width() * s, "h": area.get_height() * s, "fbo": fbo.value},
                        flip_y=True)
        return True

    # ---- playback
    def load(self, paths, append=False):
        paths = [p for p in paths if p]
        subs = [p for p in paths if os.path.splitext(p)[1].lower() in SUB_EXT]
        media = [p for p in paths if p not in subs]
        for i, p in enumerate(media):
            self.mpv.loadfile(p, "append-play" if append else ("replace" if i == 0 else "append"))
        if media and not append:
            self.mpv.pause = False
        for s in subs:
            self.add_sub(s)

    def add_sub(self, path):
        if self.mpv.idle_active:
            self.toast("Open a video before adding subtitles")
            return
        self.toast(f"Loaded {os.path.basename(path)}" if self.cmd("sub-add", path, "select")
                   else "Couldn't load that subtitle file")

    def cmd(self, *args):
        """Run an mpv command; mpv refuses some on the wrong media (frame-step on audio, chapters on a
        file with none). That's not an error worth a traceback."""
        try:
            self.mpv.command(*args)
            return True
        except SystemError:
            return False

    def toggle_pause(self):
        m = self.mpv
        if m.idle_active:
            return
        if m.eof_reached:  # keep-open parks us on the last frame when the final track ends: play it again
            self.cmd("seek", 0, "absolute")
            m.pause = False
        else:
            m.pause = not m.pause

    def seek_by(self, seconds):
        # "+exact": by default mpv snaps relative seeks to the nearest keyframe, which can be many seconds away
        self.cmd("seek", seconds, "relative+exact")

    def step(self, n):
        self.cmd("playlist-next" if n > 0 else "playlist-prev")

    def previous(self):
        """Like most players: a few seconds in, Previous restarts the track; click again to go back one."""
        m = self.mpv
        if m.idle_active:
            return
        first = (m.playlist_pos or 0) <= 0 and self.repeat != 2  # nothing before it unless the playlist loops
        if first or (m.time_pos or 0) > RESTART_AFTER:
            self.cmd("seek", 0, "absolute")
        else:
            self.cmd("playlist-prev")

    def cycle_repeat(self):
        self.repeat = (self.repeat + 1) % 3
        self.mpv.loop_file = "inf" if self.repeat == 1 else "no"
        self.mpv.loop_playlist = "inf" if self.repeat == 2 else "no"
        icon, tip = REPEAT[self.repeat]
        self.repeat_btn.set_icon_name(icon)
        self.repeat_btn.set_tooltip_text(tip + " (R)")
        self.set_on(self.repeat_btn, self.repeat > 0)
        self.toast(tip)

    def toggle_shuffle(self):
        self.shuffled = not self.shuffled
        self.cmd("playlist-shuffle" if self.shuffled else "playlist-unshuffle")
        self.set_on(self.shuffle_btn, self.shuffled)
        self.toast("Shuffle on" if self.shuffled else "Shuffle off")

    def screenshot(self):
        if self.mpv.idle_active:
            return
        d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or os.path.expanduser("~")
        path = os.path.join(d, time.strftime("avx-%Y%m%d-%H%M%S.png"))
        self.toast(f"Saved {os.path.basename(path)}" if self.cmd("screenshot-to-file", path)
                   else "Nothing to capture")

    def nudge_volume(self, d):
        self.vol.set_value(max(0, min(1.3, self.vol.get_value() + d)))

    @staticmethod
    def set_on(widget, on):
        (widget.add_css_class if on else widget.remove_css_class)("on")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=2))

    # ---- dialogs
    def _file_dialog(self, title, filt, cb, multiple=True):
        dlg = Gtk.FileDialog(title=title)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(filt)
        dlg.set_filters(store)

        def done(d, res):
            try:
                if multiple:
                    files = d.open_multiple_finish(res)
                    cb([files.get_item(i).get_path() for i in range(files.get_n_items())])
                else:
                    cb([d.open_finish(res).get_path()])
            except GLib.Error:
                pass  # dismissed
        (dlg.open_multiple if multiple else dlg.open)(self, None, done)

    def open_dialog(self, append=False):
        flt = Gtk.FileFilter(name="Media")
        for t in ("audio/*", "video/*", "application/ogg"):
            flt.add_mime_type(t)
        self._file_dialog("Open audio/video", flt, lambda p: self.load(p, append))

    def sub_dialog(self):
        flt = Gtk.FileFilter(name="Subtitles")
        for e in SUB_EXT:
            flt.add_suffix(e.lstrip("."))
        self._file_dialog("Load subtitles", flt, lambda p: self.add_sub(p[0]), multiple=False)

    # ---- popover menus (rebuilt each time they open, so track lists are always current)
    def menu_button(self, icon, tip, build, label=None):
        btn = Gtk.MenuButton(tooltip_text=tip, css_classes=["flat"], focus_on_click=False)
        if label:
            btn.set_label(label)
        else:
            btn.set_icon_name(icon)
        pop = Gtk.Popover()
        btn.set_popover(pop)

        def fill(*_):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            build(box, pop)
            pop.set_child(box)
        pop.connect("show", fill)
        return btn

    @staticmethod
    def item(box, pop, text, cb, checked=None):
        row = Gtk.Box(spacing=8)
        if checked is not None:
            row.append(Gtk.Image(icon_name="object-select-symbolic" if checked else "", pixel_size=16))
        row.append(Gtk.Label(label=text, xalign=0, hexpand=True))
        b = Gtk.Button(child=row, css_classes=["flat"])
        b.connect("clicked", lambda *_: (cb(), pop.popdown()))
        box.append(b)

    def tracks(self, kind):
        out = []
        for t in self.mpv.track_list:
            if t["type"] == kind:
                if t.get("external"):  # mpv titles these by codec ("srt"), the file name is more useful
                    name = os.path.basename(t["external-filename"])
                else:
                    name = ", ".join(x for x in (t.get("title"), t.get("lang")) if x) or f"Track {t['id']}"
                out.append((t["id"], name, bool(t.get("selected"))))
        return out

    def build_subs(self, box, pop):
        m = self.mpv
        tr = self.tracks("sub")
        self.item(box, pop, "Off", lambda: setattr(m, "sid", "no"), checked=not any(s for *_, s in tr))
        for tid, name, sel in tr:
            self.item(box, pop, name, lambda i=tid: setattr(m, "sid", i), checked=sel)
        box.append(Gtk.Separator())
        self.item(box, pop, "Load file…", self.sub_dialog)
        delay = Gtk.Label(label=f"Delay {m.sub_delay or 0:+.1f}s", xalign=0, hexpand=True, margin_start=8)

        def bump(d):
            m.sub_delay = round((m.sub_delay or 0) + d, 1)
            delay.set_text(f"Delay {m.sub_delay:+.1f}s")
        row = Gtk.Box(spacing=2)
        row.append(delay)
        for text, tip, d in (("−", "Earlier (0.1s)", -0.1), ("+", "Later (0.1s)", 0.1)):
            b = Gtk.Button(label=text, tooltip_text=tip, css_classes=["flat"], focus_on_click=False)
            b.connect("clicked", lambda _b, d=d: bump(d))
            row.append(b)
        box.append(row)

    def build_audio(self, box, pop):
        m = self.mpv
        tr = self.tracks("audio")
        if not tr:
            box.append(Gtk.Label(label="No audio tracks", margin_start=8, margin_end=8, margin_top=6, margin_bottom=6))
        for tid, name, sel in tr:
            self.item(box, pop, name, lambda i=tid: setattr(m, "aid", i), checked=sel)

    def build_speed(self, box, pop):
        cur = self.mpv.speed or 1
        for s in SPEEDS:
            self.item(box, pop, f"{s:g}×", lambda v=s: setattr(self.mpv, "speed", v), checked=abs(cur - s) < 0.01)

    # ---- periodic UI sync
    def tick(self):
        m = self.mpv
        idle = m.idle_active
        pos, dur = m.time_pos, m.duration
        self.seeking = True
        self.seek.set_range(0, dur or 1)
        self.seek.set_value(pos or 0)
        self.seeking = False
        self.hairline.set_fraction(min(1, (pos or 0) / dur) if dur else 0)
        self.pos_lbl.set_text(fmt(pos))
        self.dur_lbl.set_text(fmt(dur))
        self.play_btn.set_icon_name("media-playback-start-symbolic" if (m.pause or idle)
                                    else "media-playback-pause-symbolic")
        self.speed_btn.set_label(f"{m.speed or 1:g}×")
        title = m.media_title
        self.set_title(f"{title} - avX" if title and not idle else "avX")
        if idle:
            self.logo.set_visible(True)
            self.hint_title.set_text("Nothing playing")
            self.hint_sub.set_text("Drop files here or press Ctrl+O")
            self.hint.set_visible(True)
        elif m.video_format is None:  # audio only: no picture, so show who's playing
            md = {k.lower(): v for k, v in (m.metadata or {}).items()}
            self.logo.set_visible(False)
            self.hint_title.set_text(title or "")
            self.hint_sub.set_text(" - ".join(x for x in (md.get("artist"), md.get("album")) if x))
            self.hint.set_visible(True)
        else:
            self.hint.set_visible(False)
        self.sync_playlist()
        return True

    def sync_playlist(self):
        pl = self.mpv.playlist or []
        key = tuple(e["filename"] for e in pl)
        if key != self.playlist_key:
            self.playlist_key = key
            self.plist.remove_all()
            for e in pl:
                name = e.get("title") or os.path.basename(e["filename"])
                self.plist.append(Gtk.Label(label=name, xalign=0, ellipsize=3, margin_top=6, margin_bottom=6))
        cur = next((i for i, e in enumerate(pl) if e.get("current")), None)
        row = self.plist.get_row_at_index(cur) if cur is not None else None
        if row and not row.is_selected():
            self.plist.select_row(row)

    # ---- UI behaviour
    def on_seek(self, _scale, _type, value):
        if not self.seeking and not self.mpv.idle_active:
            self.mpv.seek(value, "absolute")
        return False

    def on_click(self, gesture, n_press, *_):
        if n_press == 2:
            self.toggle_fullscreen()
        elif n_press == 1:
            self.toggle_pause()

    def toggle_fullscreen(self):
        self.unfullscreen() if self.is_fullscreen() else self.fullscreen()

    def on_motion(self, _c, x, y):
        # GTK re-sends "motion" at the same spot whenever the UI under a still pointer changes
        # (our 4x/second seek-bar and clock updates do that), which would keep the controls up forever.
        # Only real movement counts.
        if self.last_ptr and abs(x - self.last_ptr[0]) + abs(y - self.last_ptr[1]) < 3:
            return
        self.last_ptr = (x, y)
        self.reveal()

    def reveal(self):
        for w in (self.top, self.dock):
            w.remove_css_class("gone")  # fades in (CSS transition)
            w.set_can_target(True)
        self.hairline.add_css_class("gone")
        if self.settle_id:
            GLib.source_remove(self.settle_id)
            self.settle_id = 0
        # lift subtitles clear of the dock (mpv measures this margin against a 720px-high window)
        self.mpv.sub_margin_y = int(((self.dock.get_height() or 90) + 32) * 720 / max(self.area.get_height(), 1))
        self.set_cursor(None)
        if self.hide_id:
            GLib.source_remove(self.hide_id)
        self.hide_id = GLib.timeout_add(HIDE_AFTER_MS, self.conceal)

    def conceal(self):
        self.hide_id = 0
        busy = any(b.get_active() for b in self.menu_btns) or self.vol.get_popup().get_visible()
        if not (self.mpv.pause or self.mpv.idle_active or busy):
            for w in (self.top, self.dock):
                w.add_css_class("gone")  # fades out; faded controls must not eat clicks meant for the video
                w.set_can_target(False)
            self.hairline.remove_css_class("gone")  # the one thing left on screen: where you are in the file
            self.set_cursor(Gdk.Cursor.new_from_name("none"))
            self.settle_id = GLib.timeout_add(FADE_MS, self.settle)
        return False

    def settle(self):
        self.settle_id = 0
        self.mpv.sub_margin_y = 22  # subtitles drop back down once the dock has finished fading
        return False

    def on_key(self, _c, keyval, _code, state):
        name = Gdk.keyval_name(keyval)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        m = self.mpv
        if ctrl and name == "o":
            self.open_dialog()
        elif ctrl or state & Gdk.ModifierType.ALT_MASK:
            return False
        elif name == "space":
            self.toggle_pause()
        elif name == "Left":
            self.seek_by(-SEEK_STEP)
        elif name == "Right":
            self.seek_by(SEEK_STEP)
        elif name == "Up":
            self.nudge_volume(0.05)
        elif name == "Down":
            self.nudge_volume(-0.05)
        elif name == "m":
            m.mute = not m.mute
            self.toast("Muted" if m.mute else "Unmuted")
        elif name in ("f", "F11"):
            self.toggle_fullscreen()
        elif name == "Escape" and self.is_fullscreen():
            self.unfullscreen()
        elif name == "greater":
            self.step(1)
        elif name == "less":
            self.previous()
        elif name == "Page_Down":
            self.cmd("add", "chapter", 1)
        elif name == "Page_Up":
            self.cmd("add", "chapter", -1)
        elif name == "period":
            self.cmd("frame-step")
        elif name == "comma":
            self.cmd("frame-back-step")
        elif name in ("bracketright", "bracketleft"):
            m.speed = max(0.25, min(4, round((m.speed or 1) + (0.25 if name == "bracketright" else -0.25), 2)))
        elif name == "s":
            self.screenshot()
        elif name == "v":
            m.sub_visibility = not m.sub_visibility
            self.toast("Subtitles on" if m.sub_visibility else "Subtitles off")
        elif name == "r":
            self.cycle_repeat()
        elif name == "p":
            self.plist_btn.set_active(not self.plist_btn.get_active())
        elif name == "q":
            self.close()
            return True  # mpv is gone now, so skip the reveal() below
        else:
            return False
        self.reveal()
        return True

    def on_close(self, *_):
        # stop our timers first: they poll mpv, which is about to be shut down
        for src in (self.tick_id, self.hide_id, self.settle_id):
            if src:
                GLib.source_remove(src)
        self.tick_id = self.hide_id = self.settle_id = 0
        if self.ctx:
            self.area.make_current()
            self.ctx.free()
            self.ctx = None
        try:
            self.mpv.command("write-watch-later-config")  # resume where you left off next time
        except Exception:
            pass
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
