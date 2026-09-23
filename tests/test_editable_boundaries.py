# Copyright (C) 2026
# This file is covered by the GNU General Public License, version 2 or later.
# pyright: basic
"""Regression tests runnable without NVDA, Word, or a desktop session.

The fake native script follows NVDA's send/wait/report contract. Text ranges
deliberately permit movement past the keyboard-accessible end, as Word UIA
does. Tests exercise the installed plugin hook, including native delegation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "addon/globalPlugins/objBoundaryFeedback/__init__.py"


class TextRange:
	def __init__(self, obj, start, end=None):
		self.obj = obj
		self.start = start
		self.end = start if end is None else end

	@property
	def isCollapsed(self):
		return self.start == self.end

	@property
	def bookmark(self):
		return self.start, self.end

	def copy(self):
		if self.obj.copyFails:
			raise RuntimeError("range unavailable")
		return type(self)(self.obj, self.start, self.end)

	def compareEndPoints(self, other, which):
		if self.obj.compareFails:
			raise RuntimeError("stale provider range")
		first, second = which.lower().split("to")
		return getattr(self, first) - getattr(other, second)

	def collapse(self, end=False):
		self.start = self.end = self.end if end else self.start

	def expand(self, unit):
		# Native speech mutates the range returned by the wait. The observer
		# must capture the collapsed caret before that happens.
		self.end += 1

	def move(self, unit, direction):
		self.obj.probeCalls.append((unit, direction))
		return self.obj.probeResults[0 if direction < 0 else 1]


class NativeEditable:
	def __init__(self, position=0, selectionEnd=None, probes=(0, 1)):
		self.start = position
		self.end = position if selectionEnd is None else selectionEnd
		self.probeResults = probes
		self.probeCalls = []
		self.copyFails = False
		self.compareFails = False
		self.unavailablePositions = set()
		self.waitInterrupted = False
		self.waitRaises = False
		self.waitCalls = 0
		self.reportCalls = 0
		self.originalCalls = 0
		self.eventReportedMovement = False
		self.value = "unchanged text"
		self.windowText = "Document"
		self.parent: SimpleNamespace | None = None

	def makeTextInfo(self, position):
		if position in self.unavailablePositions:
			raise RuntimeError("position unavailable")
		return TextRange(self, self.start, self.end if position == "selection" else self.start)

	def _hasCaretMoved(self, bookmark, retryInterval=0.01, timeout=None, origWord=None):
		self.waitCalls += 1
		if self.waitRaises:
			raise RuntimeError("native wait failed")
		if self.waitInterrupted:
			return False, None
		info = self.makeTextInfo("caret")
		return self.eventReportedMovement or info.bookmark != bookmark, info

	def _caretMovementScriptHelper(self, gesture, unit):
		self.originalCalls += 1
		try:
			info = self.makeTextInfo("caret")
		except RuntimeError:
			gesture.send()
			return
		bookmark = info.bookmark
		gesture.send()
		moved, info = self._hasCaretMoved(bookmark)
		self.reportCalls += 1
		if info is not None:
			info.expand(unit)


def makeGesture(key="downarrow", action=None, source="kb"):
	return SimpleNamespace(
		normalizedIdentifiers=[f"{source}:{key}"],
		send=Mock(side_effect=action),
	)


class EditableBoundaryTests(unittest.TestCase):
	def setUp(self):
		moduleNames = (
			"addonHandler api braille browseMode config controlTypes cursorManager "
			"documentNavigation editableText eventHandler globalVars globalCommands "
			"globalPluginHandler gui inputCore logHandler NVDAObjects nvwave review "
			"scriptHandler speech textInfos treeInterceptorHandler ui"
		).split()
		modules: dict[str, Any] = {name: Mock(name=name) for name in moduleNames}
		modules["globalPluginHandler"].GlobalPlugin = object
		modules["NVDAObjects"].NVDAObject = object
		modules["browseMode"].BrowseModeTreeInterceptor = object
		modules["browseMode"].BrowseModeDocumentTreeInterceptor = object
		modules["cursorManager"].CursorManager = object
		modules["editableText"].EditableText = NativeEditable
		modules["inputCore"].InputGesture = object
		modules["textInfos"].TextInfo = TextRange
		for name in ("CARET", "SELECTION", "FIRST", "LAST"):
			setattr(modules["textInfos"], f"POSITION_{name}", name.lower())
		for name in ("CHARACTER", "WORD", "LINE", "PARAGRAPH"):
			setattr(modules["textInfos"], f"UNIT_{name}", name.lower())
		modules["scriptHandler"].isScriptWaiting.return_value = False
		modules["eventHandler"].isPendingEvents.return_value = False
		self.api = modules["api"]
		self.events = modules["eventHandler"]
		self.scripts = modules["scriptHandler"]
		self.addonConfig = Mock()
		self.addonConfig.BoundaryFeedbackMode.NVDA_DEFAULT = 0
		self.addonConfig.getScenarioMode.return_value = 3
		self.addonConfig.modePlaysSound.side_effect = lambda mode: mode == 3
		self.addonConfig.SCENARIO_EDITABLE_TEXT_CARET = "editableTextCaretBoundaries"
		packageName = "_boundary_plugin_under_test"
		modules[f"{packageName}.addonConfig"] = self.addonConfig
		modules[f"{packageName}.settings"] = Mock()
		spec = importlib.util.spec_from_file_location(packageName, PLUGIN_PATH)
		assert spec is not None and spec.loader is not None
		self.module = importlib.util.module_from_spec(spec)
		modules[packageName] = self.module
		patcher = patch.dict(sys.modules, modules)
		patcher.start()
		self.addCleanup(patcher.stop)
		spec.loader.exec_module(self.module)
		self.plugin = object.__new__(self.module.GlobalPlugin)
		self.plugin._methodPatches = []
		self.plugin._playBoundarySound = Mock()
		self.plugin._installEditableTextHook()
		self.addCleanup(self.restoreHooks)

	def restoreHooks(self):
		for owner, name, original in reversed(self.plugin._methodPatches):
			setattr(owner, name, original)

	def runMovement(self, obj, key="downarrow", unit="line", action=None, source="kb"):
		self.api.getFocusObject.return_value = obj
		self.plugin._playBoundarySound.reset_mock()
		gesture = makeGesture(key, action, source)
		obj._caretMovementScriptHelper(gesture, unit)
		gesture.send.assert_called_once_with()
		self.assertNotIn("_hasCaretMoved", obj.__dict__)
		return self.plugin._playBoundarySound.call_args_list

	def assertSound(self, obj, key, direction, unit="line", **kwargs):
		self.runMovement(obj, key, unit, **kwargs)
		self.plugin._playBoundarySound.assert_called_once_with(direction)
		self.assertEqual(obj.originalCalls, 1)
		self.assertEqual(obj.waitCalls, 1)
		self.assertEqual(obj.reportCalls, 1)
		self.assertEqual(obj.probeCalls, [])

	def test_word_single_line_uses_attempted_direction(self):
		for key, unit, direction in (
			("uparrow", "line", "previous"),
			("downarrow", "line", "next"),
			("control+uparrow", "paragraph", "previous"),
			("control+downarrow", "paragraph", "next"),
		):
			with self.subTest(key=key):
				# Old code inferred "previous" for every key from these probes.
				self.assertSound(NativeEditable(probes=(0, 1)), key, direction, unit)

	def test_word_multiline_bottom_with_movable_provider_end(self):
		for key, unit in (("downarrow", "line"), ("control+downarrow", "paragraph")):
			with self.subTest(key=key):
				# Old code returned None when both virtual moves succeeded.
				self.assertSound(NativeEditable(100, probes=(-1, 1)), key, "next", unit)

	def test_empty_document_has_both_boundaries(self):
		for key, direction in (("uparrow", "previous"), ("downarrow", "next")):
			with self.subTest(key=key):
				self.assertSound(NativeEditable(probes=(0, 0)), key, direction)

	def test_direction_does_not_depend_on_provider_movement_results(self):
		for probes in ((0, 1), (-1, 1), (-1, 0), (0, 0)):
			for position in (0, 3, 100):
				with self.subTest(probes=probes, position=position):
					self.assertSound(NativeEditable(position, probes=probes), "downarrow", "next")

	def test_navigation_keys_whose_spoken_unit_is_not_the_destination(self):
		for key, unit, direction in (
			("pageup", "line", "previous"),
			("pagedown", "line", "next"),
			("control+home", "line", "previous"),
			("control+end", "line", "next"),
			("leftarrow", "character", "previous"),
			("rightarrow", "character", "next"),
			("control+leftarrow", "word", "previous"),
			("control+rightarrow", "word", "next"),
		):
			with self.subTest(key=key):
				self.assertSound(NativeEditable(), key, direction, unit)

	def test_keyboard_layout_identifier(self):
		self.assertSound(NativeEditable(), "control+downarrow", "next", source="kb(laptop)")

	def test_successful_movement_including_arrival_at_boundary_is_silent(self):
		for destination in (0, 10, 100):
			with self.subTest(destination=destination):
				obj = NativeEditable(50)

				def move():
					obj.start = obj.end = destination

				self.assertEqual(self.runMovement(obj, action=move), [])

	def test_collapsing_selection_at_unchanged_caret_start_is_silent(self):
		obj = NativeEditable(0, selectionEnd=8)
		self.assertEqual(self.runMovement(obj, "uparrow", action=lambda: setattr(obj, "end", 0)), [])

	def test_native_exception_when_collapsing_selection_is_not_retried(self):
		obj = NativeEditable(0, selectionEnd=8)
		gesture = makeGesture(action=Mock(side_effect=RuntimeError("native error")))
		with self.assertRaisesRegex(RuntimeError, "native error"):
			obj._caretMovementScriptHelper(gesture, "line")
		gesture.send.assert_called_once_with()

	def test_selection_created_by_native_action_is_silent(self):
		obj = NativeEditable()
		self.assertEqual(self.runMovement(obj, action=lambda: setattr(obj, "end", 8)), [])

	def test_value_change_without_caret_move_is_silent(self):
		obj = NativeEditable()
		self.assertEqual(self.runMovement(obj, action=lambda: setattr(obj, "value", "new value")), [])

	def test_parent_value_change_is_silent(self):
		obj = NativeEditable()
		obj.parent = SimpleNamespace(value="before", parent=None)
		self.assertEqual(self.runMovement(obj, action=lambda: setattr(obj.parent, "value", "after")), [])

	def test_default_mode_delegates_without_feedback(self):
		self.addonConfig.getScenarioMode.return_value = 0
		self.assertEqual(self.runMovement(NativeEditable()), [])

	def test_unknown_and_selection_gestures_are_not_guessed(self):
		for source, key in (("br(test)", "downarrow"), ("kb", "f5"), ("kb", "shift+downarrow")):
			with self.subTest(source=source, key=key):
				self.assertEqual(self.runMovement(NativeEditable(), key, source=source), [])

	def test_wait_interruption_remains_silent_even_if_queue_later_clears(self):
		obj = NativeEditable()
		obj.waitInterrupted = True
		self.assertEqual(self.runMovement(obj), [])

	def test_wait_missing_after_native_caret_read_failure_is_silent(self):
		obj = NativeEditable()
		obj.unavailablePositions.add("caret")
		self.assertEqual(self.runMovement(obj), [])
		self.assertEqual(obj.waitCalls, 0)

	def test_pending_script_or_focus_before_or_after_native_action_is_silent(self):
		for pending in (self.scripts.isScriptWaiting, self.events.isPendingEvents):
			for afterSend in (False, True):
				with self.subTest(pending=pending, afterSend=afterSend):
					pending.return_value = not afterSend
					self.assertEqual(
						self.runMovement(
							NativeEditable(),
							action=lambda: setattr(pending, "return_value", True),
						),
						[],
					)
					pending.return_value = False

	def test_processed_focus_change_is_silent(self):
		self.assertEqual(
			self.runMovement(
				NativeEditable(),
				action=lambda: setattr(self.api.getFocusObject, "return_value", object()),
			),
			[],
		)

	def test_caret_event_with_unchanged_position_still_reports_boundary(self):
		obj = NativeEditable()
		obj.eventReportedMovement = True
		self.assertSound(obj, "downarrow", "next")

	def test_observer_restores_method_after_native_exception(self):
		obj = NativeEditable()
		obj.waitRaises = True
		gesture = makeGesture()
		with self.assertRaisesRegex(RuntimeError, "native wait failed"):
			obj._caretMovementScriptHelper(gesture, "line")
		self.assertNotIn("_hasCaretMoved", obj.__dict__)
		gesture.send.assert_called_once_with()
		self.plugin._playBoundarySound.assert_not_called()

	def test_observer_preserves_existing_instance_override(self):
		obj = NativeEditable()
		override = Mock(wraps=obj._hasCaretMoved)
		obj._hasCaretMoved = override
		obj._caretMovementScriptHelper(makeGesture(), "line")
		self.assertIs(obj._hasCaretMoved, override)
		override.assert_called_once()
		self.plugin._playBoundarySound.assert_called_once_with("next")

	def test_unavailable_selection_or_range_comparison_is_silent(self):
		for failure in ("selection", "copy", "compare"):
			with self.subTest(failure=failure):
				obj = NativeEditable()
				if failure == "selection":
					obj.unavailablePositions.add("selection")
				elif failure == "copy":
					obj.copyFails = True
				else:
					obj.compareFails = True
				self.assertEqual(self.runMovement(obj), [])

	def test_unmodified_home_end_keep_document_boundary_filter(self):
		for key, direction in (("home", "previous"), ("end", "next")):
			for isDocumentBoundary in (False, True):
				with self.subTest(key=key, isDocumentBoundary=isDocumentBoundary):
					with patch.object(
						self.module,
						"_lineBoundaryIsDocumentBoundary",
						return_value=isDocumentBoundary,
					) as detector:
						self.runMovement(NativeEditable(), key, "character")
						detector.assert_called_once()
						self.assertEqual(detector.call_args.args[1], direction)
						if isDocumentBoundary:
							self.plugin._playBoundarySound.assert_called_once_with(direction)
						else:
							self.plugin._playBoundarySound.assert_not_called()


if __name__ == "__main__":
	unittest.main()
