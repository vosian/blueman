from gettext import gettext as _
from typing import Dict, Optional, TYPE_CHECKING, List, Any, cast
import html
import logging
import cairo
import os

from blueman.bluez.Device import Device
from blueman.gui.DeviceList import DeviceList
from blueman.DeviceClass import get_minor_class, get_major_class, gatt_appearance_to_name
from blueman.gui.GenericList import ListDataDict
from blueman.gui.manager.ManagerDeviceMenu import ManagerDeviceMenu
from blueman.Constants import PIXMAP_PATH
from blueman.Functions import launch
from blueman.Sdp import ServiceUUID, OBEX_OBJPUSH_SVCLASS_ID
from blueman.gui.GtkAnimation import TreeRowFade, CellFade
from blueman.main.Config import Config
from _blueman import ConnInfoReadError, conn_info

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GdkPixbuf
from gi.repository import Pango

if TYPE_CHECKING:
    from blueman.main.Manager import Blueman


class ManagerDeviceList(DeviceList):
    def __init__(self, adapter: Optional[str] = None, inst: Optional["Blueman"] = None) -> None:
        cr = Gtk.CellRendererText()
        cr.props.ellipsize = Pango.EllipsizeMode.END
        tabledata: List[ListDataDict] = [
            # device picture
            {"id": "device_surface", "type": str, "renderer": Gtk.CellRendererPixbuf(),
             "render_attrs": {}, "celldata_func": (self._set_device_cell_data, None)},
            # device caption
            {"id": "caption", "type": str, "renderer": cr,
             "render_attrs": {"markup": 1}, "view_props": {"expand": True}},
            {"id": "rssi_pb", "type": GdkPixbuf.Pixbuf, "renderer": Gtk.CellRendererPixbuf(),
             "render_attrs": {"pixbuf": 2}, "view_props": {"spacing": 0}},
            {"id": "lq_pb", "type": GdkPixbuf.Pixbuf, "renderer": Gtk.CellRendererPixbuf(),
             "render_attrs": {"pixbuf": 3}, "view_props": {"spacing": 0}},
            {"id": "tpl_pb", "type": GdkPixbuf.Pixbuf, "renderer": Gtk.CellRendererPixbuf(),
             "render_attrs": {"pixbuf": 4}, "view_props": {"spacing": 0}},
            {"id": "alias", "type": str},  # used for quick access instead of device.GetProperties
            {"id": "connected", "type": bool},  # used for quick access instead of device.GetProperties
            {"id": "paired", "type": bool},  # used for quick access instead of device.GetProperties
            {"id": "trusted", "type": bool},  # used for quick access instead of device.GetProperties
            {"id": "objpush", "type": bool},  # used to set Send File button
            {"id": "rssi", "type": float},
            {"id": "lq", "type": float},
            {"id": "tpl", "type": float},
            {"id": "icon_info", "type": Gtk.IconInfo},
            {"id": "cell_fader", "type": CellFade},
            {"id": "row_fader", "type": TreeRowFade},
            {"id": "levels_visible", "type": bool},
            {"id": "initial_anim", "type": bool},
        ]
        super().__init__(adapter, tabledata)
        self.set_name("ManagerDeviceList")
        self.set_headers_visible(False)
        self.props.has_tooltip = True
        self.Blueman = inst

        self.Config = Config("org.blueman.general")
        self.Config.connect('changed', self._on_settings_changed)
        # Set the correct sorting
        self._on_settings_changed(self.Config, "sort-by")
        self._on_settings_changed(self.Config, "sort-type")

        self.connect("query-tooltip", self.tooltip_query)
        self.tooltip_row: Optional[Gtk.TreePath] = None
        self.tooltip_col: Optional[Gtk.TreeViewColumn] = None

        self.connect("popup-menu", self._on_popup_menu)
        self.connect("button_press_event", self.on_event_clicked)
        self.connect("button_release_event", self.on_event_clicked)

        self.menu: Optional[ManagerDeviceMenu] = None

        self.connect("drag_data_received", self.drag_recv)
        self.connect("drag-motion", self.drag_motion)

        Gtk.Widget.drag_dest_set(self, Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY | Gdk.DragAction.DEFAULT)
        Gtk.Widget.drag_dest_add_uri_targets(self)

        self.set_search_equal_func(self.search_func)

        self._faderhandlers: Dict[str, int] = {}

    def _on_settings_changed(self, settings: Config, key: str) -> None:
        if key in ('sort-by', 'sort-order'):
            sort_by = settings['sort-by']
            sort_order = settings['sort-order']

            if sort_order == 'ascending':
                sort_type = Gtk.SortType.ASCENDING
            else:
                sort_type = Gtk.SortType.DESCENDING

            column_id = self.ids.get(sort_by)

            if column_id:
                self.liststore.set_sort_column_id(column_id, sort_type)

    def on_icon_theme_changed(self, _icon_them: Gtk.IconTheme) -> None:
        for row in self.liststore:
            device = self.get(row.iter, "device")["device"]
            self.row_setup_event(row.iter, device)

    def search_func(self, model: Gtk.TreeModel, column: int, key: str, tree_iter: Gtk.TreeIter) -> bool:
        row = self.get(tree_iter, "caption")
        if key.lower() in row["caption"].lower():
            return False
        logging.info(f"{model} {column} {key} {tree_iter}")
        return True

    def drag_recv(self, _widget: Gtk.Widget, context: Gdk.DragContext, x: int, y: int, selection: Gtk.SelectionData,
                  _info: int, time: int) -> None:

        uris = list(selection.get_uris())

        context.finish(True, False, time)

        path = self.get_path_at_pos(x, y)
        if path:
            tree_iter = self.get_iter(path[0])
            assert tree_iter is not None
            device = self.get(tree_iter, "device")["device"]
            command = f"blueman-sendto --device={device['Address']}"

            launch(command, paths=uris, name=_("File Sender"))
            context.finish(True, False, time)
        else:
            context.finish(False, False, time)

    def drag_motion(self, _widget: Gtk.Widget, drag_context: Gdk.DragContext, x: int, y: int, timestamp: int) -> bool:
        result = self.get_path_at_pos(x, y)
        if result is not None:
            path = result[0]
            assert path is not None
            if not self.selection.path_is_selected(path):
                tree_iter = self.get_iter(path)
                assert tree_iter is not None
                has_obj_push = self._has_objpush(self.get(tree_iter, "device")["device"])
                if has_obj_push:
                    Gdk.drag_status(drag_context, Gdk.DragAction.COPY, timestamp)
                    self.set_cursor(path)
                    return True
                else:
                    Gdk.drag_status(drag_context, Gdk.DragAction.DEFAULT, timestamp)
                    return False
            return False
        else:
            Gdk.drag_status(drag_context, Gdk.DragAction.DEFAULT, timestamp)
            return False

    def _on_popup_menu(self, _widget: Gtk.Widget) -> bool:
        if self.Blueman is None:
            return False

        if self.menu is None:
            self.menu = ManagerDeviceMenu(self.Blueman)

        window = self.get_window()
        assert window is not None
        selected = self.selected()
        assert selected is not None
        rect = self.get_cell_area(self.liststore.get_path(selected), self.get_column(1))
        self.menu.popup_at_rect(window, rect, Gdk.Gravity.CENTER, Gdk.Gravity.NORTH)

        return True

    def on_event_clicked(self, _widget: Gtk.Widget, event: Gdk.Event) -> bool:
        if event.type not in (Gdk.EventType._2BUTTON_PRESS, Gdk.EventType.BUTTON_PRESS):
            return False

        path = self.get_path_at_pos(int(cast(Gdk.EventButton, event).x), int(cast(Gdk.EventButton, event).y))
        if path is None:
            return False

        assert path[0] is not None
        row = self.get(path[0], "device", "connected")
        if not row:
            return False

        if self.Blueman is None:
            return False

        if self.menu is None:
            self.menu = ManagerDeviceMenu(self.Blueman)

        if event.type == Gdk.EventType._2BUTTON_PRESS and cast(Gdk.EventButton, event).button == 1:
            if self.menu.show_generic_connect_calc(row["device"]['UUIDs']):
                self.menu.generic_connect(None, device=row["device"], connect=not row["connected"])

        if event.type == Gdk.EventType.BUTTON_PRESS and cast(Gdk.EventButton, event).button == 3:
            self.menu.popup_at_pointer(event)

        return False

    def get_icon_info(self, icon_name: str, size: int = 48, fallback: bool = True) -> Optional[Gtk.IconInfo]:
        if icon_name is None and not fallback:
            return None
        elif icon_name is None and fallback:
            icon_name = "image-missing"

        icon_info = self.icon_theme.lookup_icon_for_scale(icon_name, size, self.get_scale_factor(),
                                                          Gtk.IconLookupFlags.FORCE_SIZE)

        return icon_info

    def make_device_icon(self, icon_info: Gtk.IconInfo, is_paired: bool = False, is_trusted: bool = False
                         ) -> cairo.Surface:
        window = self.get_window()
        scale = self.get_scale_factor()
        target = icon_info.load_surface(window)
        ctx = cairo.Context(target)

        if is_paired:
            _icon_info = self.get_icon_info("dialog-password", 16, False)
            assert _icon_info is not None
            paired_surface = _icon_info.load_surface(window)
            ctx.set_source_surface(paired_surface, 1 / scale, 1 / scale)
            ctx.paint_with_alpha(0.8)

        if is_trusted:
            _icon_info = self.get_icon_info("blueman-trust", 16, False)
            assert _icon_info is not None
            trusted_surface = _icon_info.load_surface(window)
            assert isinstance(target, cairo.ImageSurface)
            assert isinstance(trusted_surface, cairo.ImageSurface)
            height = target.get_height()
            mini_height = trusted_surface.get_height()
            y = height / scale - mini_height / scale - 1 / scale

            ctx.set_source_surface(trusted_surface, 1 / scale, y)
            ctx.paint_with_alpha(0.8)

        return target

    def device_remove_event(self, device: Device) -> None:
        tree_iter = self.find_device(device)
        assert tree_iter is not None

        row_fader = self.get(tree_iter, "row_fader")["row_fader"]
        super().device_remove_event(device)
        self._faderhandlers.update({
            device.get_object_path(): row_fader.connect("animation-finished", self.__on_fader_finished, device)
        })

        row_fader.thaw()
        self.emit("device-selected", None, None)
        row_fader.animate(start=row_fader.get_state(), end=0.0, duration=400)

    def __on_fader_finished(self, fader: TreeRowFade, device: Device) -> None:
        fader.disconnect(self._faderhandlers.pop(device.get_object_path()))
        fader.freeze()

    def device_add_event(self, device: Device) -> None:
        self.add_device(device)

    @staticmethod
    def make_caption(name: str, klass: str, address: str) -> str:
        return "<span size='x-large'>%(0)s</span>\n<span size='small'>%(1)s</span>\n<i>%(2)s</i>" \
               % {"0": html.escape(name), "1": klass, "2": address}

    @staticmethod
    def get_device_class(device: Device) -> str:
        klass = get_minor_class(device['Class'])
        if klass != _("Uncategorized"):
            return klass
        else:
            return get_major_class(device['Class'])

    def row_setup_event(self, tree_iter: Gtk.TreeIter, device: Device) -> None:
        if not self.get(tree_iter, "initial_anim")["initial_anim"]:
            model = self.props.model
            assert model is not None
            cell_fader = CellFade(self, model.get_path(tree_iter), [2, 3, 4])
            row_fader = TreeRowFade(self, model.get_path(tree_iter))

            has_objpush = self._has_objpush(device)

            self.set(tree_iter, row_fader=row_fader, cell_fader=cell_fader, levels_visible=False, objpush=has_objpush)

            cell_fader.freeze()

            def on_finished(fader: TreeRowFade) -> None:
                fader.disconnect(faderhandler)
                fader.freeze()

            faderhandler = row_fader.connect("animation-finished", on_finished)
            row_fader.set_state(0.0)
            row_fader.animate(start=0.0, end=1.0, duration=500)

            self.set(tree_iter, initial_anim=True)

        klass = get_minor_class(device['Class'])
        # Bluetooth >= 4 devices use Appearance property
        appearance = device["Appearance"]
        if klass != _("Uncategorized") and klass != _("Unknown"):
            description = klass
        elif klass == _("Unknown") and appearance:
            description = gatt_appearance_to_name(appearance)
        else:
            description = get_major_class(device['Class'])

        icon_info = self.get_icon_info(device["Icon"], 48, False)
        caption = self.make_caption(device['Alias'], description, device['Address'])

        self.set(tree_iter, caption=caption, icon_info=icon_info, alias=device['Alias'])

        try:
            self.row_update_event(tree_iter, "Trusted", device['Trusted'])
        except Exception as e:
            logging.exception(e)
        try:
            self.row_update_event(tree_iter, "Paired", device['Paired'])
        except Exception as e:
            logging.exception(e)
        try:
            self.row_update_event(tree_iter, "Connected", device["Connected"])
        except Exception as e:
            logging.exception(e)

    def row_update_event(self, tree_iter: Gtk.TreeIter, key: str, value: Any) -> None:
        logging.info(f"{key} {value}")

        if key == "Trusted":
            if value:
                self.set(tree_iter, trusted=True)
            else:
                self.set(tree_iter, trusted=False)

        elif key == "Paired":
            if value:
                self.set(tree_iter, paired=True)
            else:
                self.set(tree_iter, paired=False)

        elif key == "Alias":
            device = self.get(tree_iter, "device")["device"]
            c = self.make_caption(value, self.get_device_class(device), device['Address'])
            self.set(tree_iter, caption=c, alias=value)

        elif key == "UUIDs":
            device = self.get(tree_iter, "device")["device"]
            has_objpush = self._has_objpush(device)
            self.set(tree_iter, objpush=has_objpush)

        elif key == "Connected":
            self.set(tree_iter, connected=value)

    def level_setup_event(self, row_ref: Gtk.TreeRowReference, device: Device, cinfo: Optional[conn_info]) -> None:
        if not row_ref.valid():
            return

        tree_iter = self.get_iter(row_ref.get_path())
        assert tree_iter is not None
        row = self.get(tree_iter, "levels_visible", "cell_fader", "rssi", "lq", "tpl")
        if cinfo is not None:
            # cinfo init may fail for bluetooth devices version 4 and up
            # FIXME Workaround is horrible and we should show something better
            if cinfo.failed:
                rssi_perc = tpl_perc = lq_perc = 100.0
            else:
                try:
                    rssi = float(cinfo.get_rssi())
                except ConnInfoReadError:
                    rssi = 0
                try:
                    lq = float(cinfo.get_lq())
                except ConnInfoReadError:
                    lq = 0

                try:
                    tpl = float(cinfo.get_tpl())
                except ConnInfoReadError:
                    tpl = 0

                rssi_perc = 50 + (rssi / 127 / 2 * 100)
                tpl_perc = 50 + (tpl / 127 / 2 * 100)
                lq_perc = lq / 255 * 100

                if lq_perc < 10:
                    lq_perc = 10
                if rssi_perc < 10:
                    rssi_perc = 10
                if tpl_perc < 10:
                    tpl_perc = 10

            if not row["levels_visible"]:
                logging.info("animating up")
                self.set(tree_iter, levels_visible=True)
                fader = row["cell_fader"]
                fader.thaw()
                fader.set_state(0.0)
                fader.animate(start=0.0, end=1.0, duration=400)

                def on_finished(fader: CellFade) -> None:
                    fader.freeze()
                    fader.disconnect(faderhandler)

                faderhandler = fader.connect("animation-finished", on_finished)

            to_store = {}
            if round(row["rssi"], -1) != round(rssi_perc, -1):
                icon_name = "blueman-rssi-%d.png" % round(rssi_perc, -1)
                icon = GdkPixbuf.Pixbuf.new_from_file(os.path.join(PIXMAP_PATH, icon_name))
                to_store.update({"rssi": rssi_perc, "rssi_pb": icon})

            if round(row["lq"], -1) != round(lq_perc, -1):
                icon_name = "blueman-lq-%d.png" % round(lq_perc, -1)
                icon = GdkPixbuf.Pixbuf.new_from_file(os.path.join(PIXMAP_PATH, icon_name))
                to_store.update({"lq": lq_perc, "lq_pb": icon})

            if round(row["tpl"], -1) != round(tpl_perc, -1):
                icon_name = "blueman-tpl-%d.png" % round(tpl_perc, -1)
                icon = GdkPixbuf.Pixbuf.new_from_file(os.path.join(PIXMAP_PATH, icon_name))
                to_store.update({"tpl": tpl_perc, "tpl_pb": icon})

            if to_store:
                self.set(tree_iter, **to_store)

        else:

            if row["levels_visible"]:
                logging.info("animating down")
                self.set(tree_iter, levels_visible=False,
                         rssi=-1,
                         lq=-1,
                         tpl=-1)
                fader = row["cell_fader"]
                fader.thaw()
                fader.set_state(1.0)
                fader.animate(start=fader.get_state(), end=0.0, duration=400)

                def on_finished(fader: CellFade) -> None:
                    fader.disconnect(faderhandler)
                    fader.freeze()
                    if row_ref.valid():
                        assert tree_iter is not None  # https://github.com/python/mypy/issues/2608
                        self.set(tree_iter, rssi_pb=None, lq_pb=None, tpl_pb=None)

                faderhandler = fader.connect("animation-finished", on_finished)

    def tooltip_query(self, _tw: Gtk.Widget, x: int, y: int, _kb: bool, tooltip: Gtk.Tooltip) -> bool:
        path = self.get_path_at_pos(x, y)

        if path is not None:
            if path[0] != self.tooltip_row or path[1] != self.tooltip_col:
                self.tooltip_row = path[0]
                self.tooltip_col = path[1]
                return False

            if path[1] == self.columns["device_surface"]:
                tree_iter = self.get_iter(path[0])
                assert tree_iter is not None

                row = self.get(tree_iter, "trusted", "paired")
                trusted = row["trusted"]
                paired = row["paired"]
                if trusted and paired:
                    tooltip.set_markup(_("<b>Trusted and Paired</b>"))
                elif paired:
                    tooltip.set_markup(_("<b>Paired</b>"))
                elif trusted:
                    tooltip.set_markup(_("<b>Trusted</b>"))
                else:
                    return False

                self.tooltip_row = path[0]
                self.tooltip_col = path[1]
                return True

            if path[1] == self.columns["tpl_pb"] \
                    or path[1] == self.columns["lq_pb"] \
                    or path[1] == self.columns["rssi_pb"]:
                tree_iter = self.get_iter(path[0])
                assert tree_iter is not None

                dt = self.get(tree_iter, "connected")["connected"]
                if dt:
                    rssi = self.get(tree_iter, "rssi")["rssi"]
                    lq = self.get(tree_iter, "lq")["lq"]
                    tpl = self.get(tree_iter, "tpl")["tpl"]

                    if rssi < 30:
                        rssi_state = _("Poor")
                    elif rssi < 40:
                        rssi_state = _("Sub-optimal")
                    elif rssi < 60:
                        rssi_state = _("Optimal")
                    elif rssi < 70:
                        rssi_state = _("Much")
                    else:
                        rssi_state = _("Too much")

                    if tpl < 30:
                        tpl_state = _("Low")
                    elif tpl < 40:
                        tpl_state = _("Sub-optimal")
                    elif tpl < 60:
                        tpl_state = _("Optimal")
                    elif tpl < 70:
                        tpl_state = _("High")
                    else:
                        tpl_state = _("Very High")

                    tooltip_template: str = ""
                    if path[1] == self.columns["tpl_pb"]:
                        tooltip_template = \
                            "<b>Connected</b>\nReceived Signal Strength: %(rssi)u%% <i>(%(rssi_state)s)</i>\n" \
                            "Link Quality: %(lq)u%%\n<b>Transmit Power Level: %(tpl)u%%</b> <i>(%(tpl_state)s)</i>"
                    elif path[1] == self.columns["lq_pb"]:
                        tooltip_template = \
                            "<b>Connected</b>\nReceived Signal Strength: %(rssi)u%% <i>(%(rssi_state)s)</i>\n" \
                            "<b>Link Quality: %(lq)u%%</b>\nTransmit Power Level: %(tpl)u%% <i>(%(tpl_state)s)</i>"
                    elif path[1] == self.columns["rssi_pb"]:
                        tooltip_template = \
                            "<b>Connected</b>\n<b>Received Signal Strength: %(rssi)u%%</b> <i>(%(rssi_state)s)</i>\n" \
                            "Link Quality: %(lq)u%%\nTransmit Power Level: %(tpl)u%% <i>(%(tpl_state)s)</i>"

                    state_dict = {"rssi_state": rssi_state, "rssi": rssi, "lq": lq, "tpl": tpl, "tpl_state": tpl_state}
                    tooltip.set_markup(tooltip_template % state_dict)
                    self.tooltip_row = path[0]
                    self.tooltip_col = path[1]
                    return True
        return False

    def _has_objpush(self, device: Device) -> bool:
        if device is None:
            return False

        for uuid in device["UUIDs"]:
            if ServiceUUID(uuid).short_uuid == OBEX_OBJPUSH_SVCLASS_ID:
                return True
        return False

    def _set_device_cell_data(self, _col: Gtk.TreeViewColumn, cell: Gtk.CellRenderer, _model: Gtk.TreeModel,
                              tree_iter: Gtk.TreeIter, _data: None) -> None:
        row = self.get(tree_iter, "icon_info", "trusted", "paired")
        surface = self.make_device_icon(row["icon_info"], row["paired"], row["trusted"])
        cell.set_property("surface", surface)
