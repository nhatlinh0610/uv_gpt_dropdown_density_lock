"""Pure contract tests for the two registered Align Similar Pro modes.

These tests intentionally use source inspection plus tiny Python doubles.  They
do not start Blender, create a mesh/fixture, launch a helper process, or build
an artifact.  The operator contract is written here first so the UI split and
the shared lifecycle remain independently verifiable while the implementation
is refactored.
"""

import ast
from pathlib import Path
import types
import unittest


try:
    from test_align_similar_selected import STACK_TOOLS
except ModuleNotFoundError:
    # Support both the repository's existing ``tests/unit`` invocation and
    # package-qualified unittest/pytest collection from the project root.
    from tests.unit.test_align_similar_selected import STACK_TOOLS


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UI_SOURCE_PATH = PROJECT_ROOT / "uv_gpt" / "ui.py"

FAST_ID = "uv_gpt.align_similar_pro_fast"
EXACT_ID = "uv_gpt.align_similar_pro_exact"
OLD_ID = "uv_gpt.align_similar_pro"
FAST_MODE = "VERIFIED_NEAREST_ONLY"
EXACT_MODE = "EXACT_ONLY"


def _registered_pro_classes():
    return tuple(
        cls
        for cls in getattr(STACK_TOOLS, "classes", ())
        if str(getattr(cls, "bl_idname", "")).startswith("uv_gpt.align_similar_pro")
    )


def _class_for_id(operator_id):
    for cls in _registered_pro_classes():
        if getattr(cls, "bl_idname", None) == operator_id:
            return cls
    raise AssertionError("operator is not registered: %s" % operator_id)


def _panel_operator_calls(source):
    """Return literal ``layout.operator`` calls from the panel source."""

    tree = ast.parse(source, filename=str(UI_SOURCE_PATH))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not isinstance(function, ast.Attribute) or function.attr != "operator":
            continue
        if not node.args:
            continue
        try:
            operator_id = ast.literal_eval(node.args[0])
        except (ValueError, TypeError):
            continue
        if not isinstance(operator_id, str):
            continue
        text = None
        for keyword in node.keywords:
            if keyword.arg == "text":
                try:
                    text = ast.literal_eval(keyword.value)
                except (ValueError, TypeError):
                    text = None
        calls.append((operator_id, text))
    return tuple(calls)


class _FakeWorkspace:
    def __init__(self):
        self.status_calls = []

    def status_text_set(self, value):
        self.status_calls.append(value)


class _FakeWindowManager:
    def __init__(self):
        self.progress_calls = []
        self.removed_timers = []
        self.modal_operators = []

    def progress_begin(self, start, end):
        self.progress_calls.append(("begin", float(start), float(end)))

    def progress_update(self, value):
        self.progress_calls.append(("update", float(value)))

    def progress_end(self):
        self.progress_calls.append(("end",))

    def event_timer_add(self, interval, *, window=None):
        del interval, window
        return object()

    def event_timer_remove(self, timer):
        self.removed_timers.append(timer)

    def modal_handler_add(self, operator):
        self.modal_operators.append(operator)


class _FakeContext:
    def __init__(self):
        self.window_manager = _FakeWindowManager()
        self.workspace = _FakeWorkspace()
        self.window = object()


class _FakeSession:
    """Small session double covering the operator lifecycle seam."""

    def __init__(self, mode, *, done=False):
        self.mode = mode
        self.done = bool(done)
        self.cancelled = False
        self.error = None
        self.cancel_reason = None
        self.cancel_kwargs = {}
        self.run_calls = 0
        self.step_calls = []
        self.started = 0.0
        self.report = {
            "mode": mode,
            "selected_count": 2,
            "candidate_pairs_processed": 1,
            "candidate_pairs_planned": 2,
            "process_active_workers": 0,
            "process_worker_count": 0,
            "aligned_exact": 1,
            "group_count": 1,
            "skipped_shape": 0,
            "skipped_topology_unproven": 0,
            "skipped_invalid_density": 0,
        }

    def run_to_completion(self):
        self.run_calls += 1
        self.done = True
        return (0, 0)

    def step(self, **kwargs):
        self.step_calls.append(dict(kwargs))
        self.done = True
        return {"done": True}

    def cancel(self, reason, **kwargs):
        self.cancel_reason = str(reason)
        self.cancel_kwargs = dict(kwargs)
        self.cancelled = True
        self.done = True


def _capture_report(operator):
    messages = []
    operator.report = lambda levels, message: messages.append((levels, str(message)))
    return messages


def _contains_mode(text, operator_class, mode):
    text = str(text)
    return any(
        token and token in text
        for token in (
            mode,
            str(getattr(operator_class, "bl_label", "")),
            str(getattr(operator_class, "bl_idname", "")),
        )
    )


def _forwarded_mode(calls):
    """Extract the explicit mode value from captured factory arguments."""

    if not calls:
        raise AssertionError("the Pro session factory was not called")
    args, kwargs = calls[0]
    if "mode" in kwargs:
        return kwargs["mode"]
    values = list(args) + list(kwargs.values())
    for mode in (FAST_MODE, EXACT_MODE):
        if mode in values:
            return mode
    raise AssertionError("no explicit Pro mode was forwarded: %r %r" % (args, kwargs))


class ProTwoModesUIContractTests(unittest.TestCase):
    def setUp(self):
        self._original_active_session = getattr(STACK_TOOLS, "_ACTIVE_PRO_SESSION", None)
        self._original_active_operator = getattr(STACK_TOOLS, "_ACTIVE_PRO_OPERATOR", None)

    def tearDown(self):
        STACK_TOOLS._ACTIVE_PRO_SESSION = self._original_active_session
        STACK_TOOLS._ACTIVE_PRO_OPERATOR = self._original_active_operator

    def test_panel_exposes_exactly_two_mode_buttons_and_no_legacy_button(self):
        source = UI_SOURCE_PATH.read_text(encoding="utf-8")
        calls = _panel_operator_calls(source)
        mode_calls = tuple(
            (operator_id, text)
            for operator_id, text in calls
            if operator_id in {FAST_ID, EXACT_ID, OLD_ID}
        )

        self.assertEqual(
            set(mode_calls),
            {(FAST_ID, "Pro Fast"), (EXACT_ID, "Pro Exact")},
        )
        self.assertEqual(len(mode_calls), 2)
        self.assertNotIn(OLD_ID, [operator_id for operator_id, _text in calls])
        self.assertNotIn('"%s"' % OLD_ID, source)
        self.assertNotIn("'%s'" % OLD_ID, source)

    def test_only_fast_and_exact_are_registered_for_the_pro_ui(self):
        classes = _registered_pro_classes()
        registered_ids = tuple(getattr(cls, "bl_idname", None) for cls in classes)

        self.assertEqual(set(registered_ids), {FAST_ID, EXACT_ID})
        self.assertEqual(len(classes), 2)
        self.assertNotIn(OLD_ID, registered_ids)

    def test_labels_and_tooltips_identify_the_two_modes(self):
        fast = _class_for_id(FAST_ID)
        exact = _class_for_id(EXACT_ID)

        self.assertEqual(fast.bl_label, "Pro Fast")
        self.assertEqual(exact.bl_label, "Pro Exact")
        fast_description = str(getattr(fast, "bl_description", ""))
        exact_description = str(getattr(exact, "bl_description", ""))
        self.assertTrue(fast_description.strip())
        self.assertTrue(exact_description.strip())
        self.assertIn("nearest", fast_description.lower())
        self.assertIn("exact", exact_description.lower())
        self.assertNotEqual(fast_description, exact_description)

    def _replace_session_factory(self, session):
        calls = []
        original = STACK_TOOLS._pro_create_session

        def fake_create(*args, **kwargs):
            calls.append((args, kwargs))
            STACK_TOOLS._ACTIVE_PRO_SESSION = session
            return session

        STACK_TOOLS._pro_create_session = fake_create
        return original, calls

    def test_execute_forwards_explicit_mode_and_reports_mode(self):
        for operator_id, mode in ((FAST_ID, FAST_MODE), (EXACT_ID, EXACT_MODE)):
            with self.subTest(operator_id=operator_id):
                operator_class = _class_for_id(operator_id)
                operator = operator_class()
                messages = _capture_report(operator)
                session = _FakeSession(mode)
                original, calls = self._replace_session_factory(session)
                try:
                    result = operator.execute(_FakeContext())
                finally:
                    STACK_TOOLS._pro_create_session = original

                self.assertEqual(result, {"FINISHED"})
                self.assertEqual(_forwarded_mode(calls), mode)
                self.assertGreaterEqual(session.run_calls, 1)
                self.assertTrue(
                    any(_contains_mode(message, operator_class, mode) for _levels, message in messages),
                    messages,
                )

    def test_invoke_forwards_explicit_mode_and_progress_identifies_mode(self):
        for operator_id, mode in ((FAST_ID, FAST_MODE), (EXACT_ID, EXACT_MODE)):
            with self.subTest(operator_id=operator_id):
                operator_class = _class_for_id(operator_id)
                operator = operator_class()
                messages = _capture_report(operator)
                session = _FakeSession(mode)
                original, calls = self._replace_session_factory(session)
                context = _FakeContext()
                try:
                    result = operator.invoke(context, types.SimpleNamespace(type="LEFTMOUSE"))
                finally:
                    STACK_TOOLS._pro_create_session = original

                self.assertEqual(result, {"RUNNING_MODAL"})
                self.assertEqual(_forwarded_mode(calls), mode)
                statuses = [value for value in context.workspace.status_calls if value]
                self.assertTrue(statuses)
                self.assertTrue(
                    any(_contains_mode(status, operator_class, mode) for status in statuses),
                    statuses,
                )
                operator._cleanup_modal(context, cancel=True, reason="test_cleanup")

    def test_modal_timer_completes_and_report_identifies_mode(self):
        for operator_id, mode in ((FAST_ID, FAST_MODE), (EXACT_ID, EXACT_MODE)):
            with self.subTest(operator_id=operator_id):
                operator_class = _class_for_id(operator_id)
                operator = operator_class()
                messages = _capture_report(operator)
                session = _FakeSession(mode)
                STACK_TOOLS._ACTIVE_PRO_SESSION = session
                STACK_TOOLS._ACTIVE_PRO_OPERATOR = operator
                operator._session = session
                context = _FakeContext()

                result = operator.modal(context, types.SimpleNamespace(type="TIMER"))

                self.assertEqual(result, {"FINISHED"})
                self.assertEqual(len(session.step_calls), 1)
                self.assertTrue(
                    any(_contains_mode(message, operator_class, mode) for _levels, message in messages),
                    messages,
                )
                self.assertIsNone(STACK_TOOLS._ACTIVE_PRO_SESSION)
                self.assertIsNone(STACK_TOOLS._ACTIVE_PRO_OPERATOR)

    def test_modal_esc_cancels_mode_and_releases_shared_lock(self):
        for operator_id, mode in ((FAST_ID, FAST_MODE), (EXACT_ID, EXACT_MODE)):
            with self.subTest(operator_id=operator_id):
                operator_class = _class_for_id(operator_id)
                operator = operator_class()
                messages = _capture_report(operator)
                session = _FakeSession(mode)
                STACK_TOOLS._ACTIVE_PRO_SESSION = session
                STACK_TOOLS._ACTIVE_PRO_OPERATOR = operator
                operator._session = session

                result = operator.modal(_FakeContext(), types.SimpleNamespace(type="ESC"))

                self.assertIn("CANCELLED", result)
                self.assertTrue(session.cancelled)
                self.assertEqual(session.cancel_reason, "user_cancelled")
                self.assertTrue(
                    any(_contains_mode(message, operator_class, mode) for _levels, message in messages),
                    messages,
                )
                self.assertIsNone(STACK_TOOLS._ACTIVE_PRO_SESSION)
                self.assertIsNone(STACK_TOOLS._ACTIVE_PRO_OPERATOR)

    def test_fast_and_exact_share_one_active_lock(self):
        for first_id, first_mode, second_id in (
            (FAST_ID, FAST_MODE, EXACT_ID),
            (EXACT_ID, EXACT_MODE, FAST_ID),
        ):
            with self.subTest(first_id=first_id, second_id=second_id):
                first_session = _FakeSession(first_mode)
                STACK_TOOLS._ACTIVE_PRO_SESSION = first_session
                second_operator_class = _class_for_id(second_id)
                second_mode = FAST_MODE if second_id == FAST_ID else EXACT_MODE
                second_operator = second_operator_class()
                messages = _capture_report(second_operator)

                result = second_operator.execute(_FakeContext())

                self.assertEqual(result, {"CANCELLED"})
                self.assertIs(STACK_TOOLS._ACTIVE_PRO_SESSION, first_session)
                self.assertTrue(
                    any(
                        _contains_mode(message, second_operator_class, second_mode)
                        for _levels, message in messages
                    ),
                    messages,
                )

    def test_unregister_cancels_active_mode_and_releases_shared_lock(self):
        original_context = getattr(STACK_TOOLS.bpy, "context", None)
        original_utils = getattr(STACK_TOOLS.bpy, "utils", None)
        unregister_calls = []
        STACK_TOOLS.bpy.context = _FakeContext()
        STACK_TOOLS.bpy.utils = types.SimpleNamespace(
            unregister_class=lambda cls: unregister_calls.append(cls)
        )
        try:
            for operator_id, mode in ((FAST_ID, FAST_MODE), (EXACT_ID, EXACT_MODE)):
                with self.subTest(operator_id=operator_id):
                    operator_class = _class_for_id(operator_id)
                    operator = operator_class()
                    session = _FakeSession(mode)
                    operator._session = session
                    STACK_TOOLS._ACTIVE_PRO_SESSION = session
                    STACK_TOOLS._ACTIVE_PRO_OPERATOR = operator

                    STACK_TOOLS.unregister()

                    self.assertTrue(session.cancelled)
                    self.assertEqual(session.cancel_reason, "unregister")
                    self.assertEqual(session.report["mode"], mode)
                    self.assertIsNone(STACK_TOOLS._ACTIVE_PRO_SESSION)
                    self.assertIsNone(STACK_TOOLS._ACTIVE_PRO_OPERATOR)
        finally:
            if original_context is None:
                try:
                    del STACK_TOOLS.bpy.context
                except AttributeError:
                    pass
            else:
                STACK_TOOLS.bpy.context = original_context
            if original_utils is None:
                try:
                    del STACK_TOOLS.bpy.utils
                except AttributeError:
                    pass
            else:
                STACK_TOOLS.bpy.utils = original_utils


if __name__ == "__main__":
    unittest.main()
