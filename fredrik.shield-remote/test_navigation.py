import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import navigation


class States:
    FOCUSABLE = "focusable"
    FOCUSED = "focused"
    SHOWING = "showing"
    VISIBLE = "visible"
    SENSITIVE = "sensitive"
    DEFUNCT = "defunct"
    MODAL = "modal"


class Element:
    def __init__(self, role="link", name="", rect=(0, 0, 100, 100), children=(),
                 states=("focusable", "showing", "visible", "sensitive"), accepts_focus=True):
        self.role = role
        self.name = name
        self.rect = navigation.Rect(*rect)
        self.children = children
        self.states = set(states)
        self.accepts_focus = accepts_focus
        self.focus_calls = 0

    def get_name(self):
        return self.name

    def get_role_name(self):
        return self.role

    def get_state_set(self):
        return SimpleNamespace(contains=lambda state: state in self.states)

    def get_component_iface(self):
        return self

    def get_extents(self, coordinate_type):
        return self.rect

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def set_cache_mask(self, mask):
        self.cache_mask = mask

    def grab_focus(self):
        self.focus_calls += 1
        return self.accepts_focus


def control(x, y, width=100, height=150, role="link", name="", showing=True, focused=False):
    element = Element(role, name, (x, y, width, height))
    return navigation.Control(element, element.rect, role, name, focused, showing)


class GeometryTests(unittest.TestCase):
    def test_grid_moves_one_item_in_each_direction(self):
        current = control(150, 200)
        left, right, up, down = control(0, 200), control(300, 200), control(150, 0), control(150, 400)
        controls = [current, left, right, up, down]
        for direction, target in (("left", left), ("right", right), ("up", up), ("down", down)):
            self.assertIs(navigation.neighbor(current, controls, direction), target)

    def test_same_row_beats_nearer_diagonal_control(self):
        current, diagonal, same_row = control(0, 0), control(110, 160), control(300, 0)
        self.assertIs(navigation.neighbor(current, [current, diagonal, same_row], "right"), same_row)

    def test_same_column_beats_nearer_diagonal_control(self):
        current, diagonal, same_column = control(0, 0), control(110, 160), control(0, 400)
        self.assertIs(navigation.neighbor(current, [current, diagonal, same_column], "down"), same_column)

    def test_next_row_preserves_column(self):
        current = control(300, 0)
        next_row = [control(0, 250), control(150, 250), control(300, 250)]
        self.assertIs(navigation.neighbor(current, [current, *next_row], "down"), next_row[-1])

    def test_posters_skip_auxiliary_menu_button_in_the_same_row(self):
        current, menu, next_poster = control(0, 0), control(110, 90, 20, 40, role="combo box"), control(150, 0)
        self.assertIs(navigation.neighbor(current, [current, menu, next_poster], "right"), next_poster)

    def test_small_card_alignment_gap_does_not_prioritize_auxiliary_button(self):
        current = control(320, 577, 170, 310)
        resume = control(120, 128, 197, 349)
        menu = control(309, 416, 23, 61, role="combo box")
        self.assertIs(navigation.neighbor(current, [current, menu, resume], "up"), resume)

    def test_offscreen_next_item_is_available_for_native_auto_scroll(self):
        current, next_item = control(1500, 900, 400, 90), control(1500, 1000, 400, 90, showing=False)
        self.assertIs(navigation.neighbor(current, [current, next_item], "down"), next_item)

    def test_stream_navigation_does_not_skip_compact_rows(self):
        current = control(547, 496, 392, 62)
        compact, following = control(547, 565, 392, 56), control(547, 628, 392, 62)
        self.assertIs(navigation.neighbor(current, [current, compact, following], "down"), compact)

    def test_initial_keeps_compact_first_stream_ahead_of_taller_rows(self):
        compact, following = control(547, 151, 392, 56), control(547, 214, 392, 62)
        self.assertIs(navigation.initial([compact, following]), compact)

    def test_diagonal_fallback_prefers_alignment(self):
        current = control(0, 0)
        close_diagonal, aligned_diagonal = control(500, 160), control(110, 500)
        self.assertIs(navigation.neighbor(current, [current, close_diagonal, aligned_diagonal], "down"),
                      aligned_diagonal)

    def test_edge_does_not_wrap_to_unrelated_control(self):
        current, left = control(150, 0), control(0, 0)
        self.assertIsNone(navigation.neighbor(current, [current, left], "right"))

    def test_negative_monitor_coordinates_do_not_change_direction(self):
        current, right = control(-1500, -200), control(-1300, -200)
        self.assertIs(navigation.neighbor(current, [current, right], "right"), right)

    def test_initial_prefers_first_visible_poster_or_stream_over_toolbar(self):
        toolbar, poster, offscreen = control(0, 0, 40, 40), control(120, 200), control(120, 400, showing=False)
        self.assertIs(navigation.initial([toolbar, poster, offscreen]), poster)

    def test_initial_does_not_guess_an_invisible_control(self):
        with self.assertRaisesRegex(RuntimeError, "visible"):
            navigation.initial([control(0, 200, showing=False)])


class AccessibilityTests(unittest.TestCase):
    def api(self, *children):
        doc = Element("document web", rect=(0, 46, 1920, 1034), children=children)
        app = Element("application", "stremio", children=(doc,))
        desktop = Element("desktop", children=(app,))
        return SimpleNamespace(
            get_desktop=lambda index: desktop, StateType=States,
            CoordType=SimpleNamespace(SCREEN=0),
            Cache=SimpleNamespace(CHILDREN=1, ROLE=2, NAME=4, STATES=8, INTERFACES=16, PARENT=32),
        )

    def test_snapshot_uses_real_focusable_controls_not_inert_sections(self):
        poster = Element(name="Poster", rect=(100, 300, 160, 240))
        inert = Element("section", states=("showing", "visible", "sensitive"))
        controls = navigation.snapshot(self.api(poster, inert))
        self.assertEqual([item.name for item in controls], ["Poster"])

    def test_custom_focusable_div_buttons_are_included(self):
        button = Element("section", "Share", rect=(100, 300, 50, 50))
        self.assertEqual(navigation.snapshot(self.api(button))[0].name, "Share")

    def test_hidden_disabled_and_focus_guards_are_excluded(self):
        hidden = Element(states=("focusable", "sensitive"))
        disabled = Element(states=("focusable", "showing", "visible"))
        guard = Element(rect=(0, 46, 1, 1))
        self.assertEqual(navigation.snapshot(self.api(hidden, disabled, guard)), [])

    def test_transparent_full_window_file_drop_control_is_excluded(self):
        overlay = Element("button", "Choose File", rect=(7, 53, 1906, 1020))
        self.assertEqual(navigation.snapshot(self.api(overlay)), [])

    def test_offscreen_controls_are_kept_without_being_initial_candidates(self):
        hidden_below = Element(rect=(100, 1200, 100, 150), states=("focusable", "visible", "sensitive"))
        controls = navigation.snapshot(self.api(hidden_below))
        self.assertEqual(len(controls), 1)
        self.assertFalse(controls[0].showing)

    def test_dialog_constrains_navigation_to_dialog_contents(self):
        outside = Element(name="Outside")
        inside = Element(name="Inside")
        dialog = Element("dialog", children=(inside,))
        self.assertEqual([item.name for item in navigation.snapshot(self.api(outside, dialog))], ["Inside"])

    def test_ambiguous_application_is_rejected(self):
        api = self.api(Element())
        desktop = Element("desktop", children=(
            Element("application", "stremio"), Element("application", "Stremio"),
        ))
        api.get_desktop = lambda index: desktop
        with self.assertRaisesRegex(RuntimeError, "unique"):
            navigation.snapshot(api)

    def test_select_without_selection_focuses_but_does_not_activate(self):
        target = control(100, 300)
        with patch.object(navigation, "snapshot", return_value=[target]):
            result = navigation.navigate("select", None)
        self.assertFalse(result["activate"])
        self.assertEqual(target.element.focus_calls, 1)

    def test_select_activates_existing_selection(self):
        target = control(100, 300, focused=True)
        with patch.object(navigation, "snapshot", return_value=[target]):
            result = navigation.navigate("select", None)
        self.assertTrue(result["activate"])
        self.assertEqual(target.element.focus_calls, 0)

    def test_direction_focuses_exact_next_control(self):
        current, next_item = control(100, 300, focused=True), control(300, 300)
        with patch.object(navigation, "snapshot", return_value=[current, next_item]):
            result = navigation.navigate("right", None)
        self.assertFalse(result["activate"])
        self.assertEqual(next_item.element.focus_calls, 1)

    def test_initialize_preserves_current_selection(self):
        current, first = control(300, 300, focused=True), control(100, 300)
        with patch.object(navigation, "snapshot", return_value=[first, current]):
            result = navigation.navigate("initialize", None)
        self.assertEqual(result["bounds"], [300, 300, 100, 150])
        self.assertEqual(first.element.focus_calls, 0)

    def test_back_closes_popup_without_activating_selection(self):
        current = control(300, 300, focused=True)
        current.scope = Element("menu")
        with patch.object(navigation, "snapshot", return_value=[current]):
            result = navigation.navigate("back", None)
        self.assertEqual(result, {"activate": False, "key": "Escape"})
        self.assertEqual(current.element.focus_calls, 0)

    def test_back_on_page_uses_history_shortcut(self):
        with patch.object(navigation, "snapshot", return_value=[control(300, 300)]):
            self.assertEqual(navigation.navigate("back", None), {"activate": False, "key": "BackSpace"})

    def test_back_can_leave_loading_page_without_selectable_controls(self):
        with patch.object(navigation, "snapshot", return_value=[]):
            self.assertEqual(navigation.navigate("back", None), {"activate": False, "key": "BackSpace"})

    def test_refused_focus_is_reported_without_activation(self):
        target = control(100, 300)
        target.element.accepts_focus = False
        with patch.object(navigation, "snapshot", return_value=[target]):
            with self.assertRaisesRegex(RuntimeError, "refused"):
                navigation.navigate("select", None)

    def test_no_controls_is_explicitly_reported(self):
        with patch.object(navigation, "snapshot", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "no selectable"):
                navigation.navigate("right", None)

    def test_initialize_processes_accessibility_updates_between_load_retries(self):
        context = Mock()
        context.pending.side_effect = [True, False, False]
        glib = SimpleNamespace(MainContext=SimpleNamespace(default=lambda: context))
        with (
            patch.object(navigation, "snapshot", side_effect=[[], [control(100, 300)]]),
            patch.object(navigation.time, "sleep"),
        ):
            navigation.navigate("initialize", None, glib)
        self.assertEqual(context.pending.call_count, 3)
        context.iteration.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
