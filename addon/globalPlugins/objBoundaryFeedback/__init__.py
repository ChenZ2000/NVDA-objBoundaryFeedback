# A part of Object Boundary Feedback
# Copyright (C) 2026 Cary-rowen <manchen_0528@outlook.com>
# This file may be used under the terms of the GNU General Public License, version 2 or later.
# For more details see: https://www.gnu.org/licenses/gpl-2.0.html
# pyright: basic

from __future__ import annotations

import functools
import inspect
import os
from collections.abc import Callable, Iterable
from typing import Any, Literal, TypeVar, cast

import addonHandler
import api
import braille
import browseMode
import config
import controlTypes
import cursorManager
from documentNavigation import paragraphHelper
import editableText
import eventHandler
import globalVars
import globalCommands
import globalPluginHandler
import gui
import inputCore
from logHandler import log
from NVDAObjects import NVDAObject
import nvwave
import review
import scriptHandler
import speech
import textInfos
import treeInterceptorHandler
import ui

from . import addonConfig
from . import settings


addonHandler.initTranslation()


_PREVIOUS = "previous"
_NEXT = "next"
_GENERIC = "generic"
_BoundaryDirection = Literal["previous", "next", "generic"]
_BrowseDirection = Literal["previous", "next"]
_TextPosition = str
_TextUnit = str
_CallResultT = TypeVar("_CallResultT")

_WAVE_FILE_BY_DIRECTION = {
	_PREVIOUS: "boundaryPrevious.wav",
	_NEXT: "boundaryNext.wav",
	_GENERIC: "boundaryGeneric.wav",
}

_ADDON_DIR = os.path.dirname(__file__)
_VALUE_SNAPSHOT_ATTRS = ("value", "windowText")
_VALUE_SNAPSHOT_OBJECT_DEPTH = 3

_MethodPatch = tuple[object, str, Callable[..., Any]]
_GestureMapReplacement = tuple[Callable[[], Iterable[Any]], Callable[..., Any], Callable[..., Any]]
_CurrentItemReporter = Callable[[], None]
_QuickNavScript = Callable[
	[
		browseMode.BrowseModeTreeInterceptor,
		inputCore.InputGesture | None,
		str,
		_BrowseDirection,
		str,
		_TextUnit | None,
	],
	None,
]
_MovePastEndOfContainerScript = Callable[
	[browseMode.BrowseModeDocumentTreeInterceptor, inputCore.InputGesture],
	None,
]
_CursorManagerCaretMovementScript = Callable[
	[
		cursorManager.CursorManager,
		inputCore.InputGesture,
		_TextUnit,
		int | None,
		_TextPosition,
		_TextUnit | None,
		bool,
		bool,
		bool,
	],
	None,
]
_EditableTextCaretMovementScript = Callable[
	[editableText.EditableText, inputCore.InputGesture, _TextUnit],
	None,
]
_ParagraphMovementFunction = Callable[[bool, bool, textInfos.TextInfo | None], tuple[bool, bool]]
_ParagraphCurrentReporter = Callable[[textInfos.TextInfo], bool]


def _hasExpectedFunctionSignature(func: Callable[..., Any], expected: tuple[str, ...]) -> bool:
	try:
		actual = tuple(inspect.signature(func).parameters)
	except (TypeError, ValueError):
		log.debugWarning(f"Unable to inspect signature for {func!r}", exc_info=True)
		return False
	if actual != expected:
		log.warning(
			f"Skipping objBoundaryFeedback hook for {func!r}: expected {expected}, got {actual}",
		)
		return False
	return True


def _sameTextRange(first: textInfos.TextInfo, second: textInfos.TextInfo) -> bool:
	try:
		return (
			first.compareEndPoints(second, "startToStart") == 0
			and first.compareEndPoints(second, "endToEnd") == 0
		)
	except Exception:
		log.debugWarning("Unable to compare text ranges for boundary feedback", exc_info=True)
		return False


def _getSelectionRange(obj: cursorManager.CursorManager) -> textInfos.TextInfo | None:
	try:
		return obj.makeTextInfo(textInfos.POSITION_SELECTION).copy()
	except Exception:
		log.debugWarning("Unable to get cursor manager selection for boundary feedback", exc_info=True)
		return None


def _getEditableValueSnapshot(obj: object | None) -> tuple[tuple[int, str, str], ...]:
	snapshot: list[tuple[int, str, str]] = []
	for level in range(_VALUE_SNAPSHOT_OBJECT_DEPTH):
		if obj is None:
			break
		for attr in _VALUE_SNAPSHOT_ATTRS:
			try:
				value = getattr(obj, attr, None)
			except Exception:
				log.debugWarning("Unable to inspect editable value for boundary feedback", exc_info=True)
				continue
			if isinstance(value, str):
				snapshot.append((level, attr, value))
		try:
			obj = getattr(obj, "parent", None)
		except Exception:
			log.debugWarning("Unable to inspect editable parent for boundary feedback", exc_info=True)
			break
	return tuple(snapshot)


def _lineBoundaryIsDocumentBoundary(
	info: textInfos.TextInfo,
	direction: _BoundaryDirection,
) -> bool:
	def canMovePastLineBoundary(previous: bool) -> bool:
		lineInfo = info.copy()
		lineInfo.expand(textInfos.UNIT_LINE)
		lineInfo.collapse(end=not previous)
		return lineInfo.move(textInfos.UNIT_LINE, -1 if previous else 1) != 0

	try:
		if direction == _PREVIOUS:
			return not canMovePastLineBoundary(previous=True)
		if direction == _NEXT:
			return not canMovePastLineBoundary(previous=False)
		return not (canMovePastLineBoundary(previous=True) and canMovePastLineBoundary(previous=False))
	except Exception:
		log.debugWarning("Unable to inspect line boundary for boundary feedback", exc_info=True)
		return False


def _caretMovementFromGesture(gesture: inputCore.InputGesture) -> tuple[_BoundaryDirection, bool] | None:
	"""Return the requested direction and whether this is an unmodified Home/End.

	The helper's unit is the unit NVDA speaks, not necessarily the unit moved:
	Page Down and Control+End, for example, both speak a line. Never infer the
	key's direction or destination by probing TextInfo.move in both directions.
	"""
	directions: dict[str, _BoundaryDirection] = {
		"uparrow": _PREVIOUS,
		"leftarrow": _PREVIOUS,
		"pageup": _PREVIOUS,
		"home": _PREVIOUS,
		"downarrow": _NEXT,
		"rightarrow": _NEXT,
		"pagedown": _NEXT,
		"end": _NEXT,
	}
	for identifier in gesture.normalizedIdentifiers:
		source, _, keys = identifier.partition(":")
		if source != "kb" and not (source.startswith("kb(") and source.endswith(")")):
			continue
		keyNames = set(keys.split("+"))
		for key, direction in directions.items():
			if keyNames in ({key}, {"control", key}):
				return direction, key in ("home", "end") and "control" not in keyNames
	# Unknown or non-keyboard gestures do not provide enough information to
	# choose a boundary. In particular, do not guess on a one-line document.
	return None


def _callWithCaretMoveObservation(
	obj: editableText.EditableText,
	original: _EditableTextCaretMovementScript,
	gesture: inputCore.InputGesture,
	unit: _TextUnit,
) -> textInfos.TextInfo | None:
	"""Observe NVDA's completed caret wait without sending or waiting a second time."""
	methodName = "_hasCaretMoved"
	originalWait = obj._hasCaretMoved
	hadInstanceOverride = methodName in obj.__dict__
	instanceOverride = obj.__dict__.get(methodName)
	observedInfo = None

	@functools.wraps(originalWait)
	def observe(*args: Any, **kwargs: Any) -> Any:
		nonlocal observedInfo
		result = originalWait(*args, **kwargs)
		# None means an interrupted wait or an unavailable caret, not a boundary.
		# Copy before _caretScriptPostMovedHelper expands the returned range for
		# speech. The boolean alone is insufficient: a caret event may be fired
		# even when Word leaves the insertion point at exactly the same position.
		try:
			observedInfo = result[1].copy() if result[1] is not None else None
		except Exception:
			log.debugWarning("Unable to copy observed caret for boundary feedback", exc_info=True)
			observedInfo = None
		return result

	setattr(obj, methodName, observe)
	try:
		original(obj, gesture, unit)
	finally:
		if hadInstanceOverride:
			setattr(obj, methodName, instanceOverride)
		else:
			delattr(obj, methodName)
	return observedInfo


def _directionFromBrowseDirection(direction: _BrowseDirection) -> _BoundaryDirection:
	if direction == "previous":
		return _PREVIOUS
	return _NEXT


def _directionFromCursorMovement(
	direction: int | None,
	posConstant: _TextPosition,
	posUnit: _TextUnit | None,
	posUnitEnd: bool,
) -> _BoundaryDirection:
	if direction is not None:
		try:
			if direction < 0:
				return _PREVIOUS
			if direction > 0:
				return _NEXT
		except TypeError:
			return _GENERIC
	if posConstant == textInfos.POSITION_FIRST:
		return _PREVIOUS
	if posConstant == textInfos.POSITION_LAST:
		return _NEXT
	if posUnit is not None:
		return _NEXT if posUnitEnd else _PREVIOUS
	return _GENERIC


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self, *args: Any, **kwargs: Any) -> None:
		super().__init__(*args, **kwargs)
		addonConfig.installConfigSpec()
		self._methodPatches: list[_MethodPatch] = []
		self._gestureMapReplacements: list[_GestureMapReplacement] = []
		self._settingsPanelRegistered = False
		self._registerSettingsPanel()
		self._installGlobalCommandHooks()
		self._installBrowseModeHooks()
		self._installCursorManagerHook()
		self._installEditableTextHook()
		self._installParagraphHelperHooks()

	def terminate(self) -> None:
		self._unregisterSettingsPanel()
		self._restoreGestureMapReplacements()
		for owner, name, original in reversed(self._methodPatches):
			try:
				setattr(owner, name, original)
			except Exception:
				log.debugWarning(
					f"Unable to restore objBoundaryFeedback hook {owner!r}.{name}",
					exc_info=True,
				)
		self._methodPatches.clear()
		self._gestureMapReplacements.clear()
		super().terminate()

	def _registerSettingsPanel(self) -> None:
		if globalVars.appArgs.secure:
			return
		categoryClasses = gui.settingsDialogs.NVDASettingsDialog.categoryClasses
		settingsPanel = settings.BoundaryFeedbackSettingsPanel
		if settingsPanel in categoryClasses:
			self._settingsPanelRegistered = True
			return
		categoryClasses.append(settingsPanel)
		self._settingsPanelRegistered = True

	def _unregisterSettingsPanel(self) -> None:
		if not self._settingsPanelRegistered:
			return
		try:
			gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(
				settings.BoundaryFeedbackSettingsPanel,
			)
		except ValueError:
			pass
		self._settingsPanelRegistered = False

	def _playBoundarySound(self, direction: _BoundaryDirection) -> None:
		fileName = _WAVE_FILE_BY_DIRECTION.get(direction, _WAVE_FILE_BY_DIRECTION[_GENERIC])
		filePath = os.path.join(_ADDON_DIR, fileName)
		try:
			nvwave.playWaveFile(filePath)
		except Exception:
			log.debugWarning(f"Unable to play boundary feedback sound: {filePath}", exc_info=True)

	def _isObjectBelowLockScreen(self, obj: object) -> bool:
		checker = getattr(globalCommands, "objectBelowLockScreenAndWindowsIsLocked", None)
		if checker is None:
			return False
		try:
			return bool(checker(obj))
		except Exception:
			log.debugWarning("Unable to check lock screen state for boundary feedback", exc_info=True)
			return False

	def _reportCurrentReviewMode(self) -> None:
		currentMode = review.getCurrentMode()
		for modeId, label, _modeGetter in review.modes:
			if modeId == currentMode:
				ui.reviewMessage(label)
				return
		ui.reviewMessage(str(currentMode))

	def _reportCurrentNavigatorObject(self) -> None:
		curObject = api.getNavigatorObject()
		if not isinstance(curObject, NVDAObject):
			# Translators: Reported when there is no current navigator object.
			ui.reviewMessage(_("No navigator object"))
			return
		if self._isObjectBelowLockScreen(curObject):
			ui.reviewMessage(gui.blockAction.Context.WINDOWS_LOCKED.translatedMessage)
			return
		try:
			speechSequence = speech.getObjectSpeech(curObject, reason=controlTypes.OutputReason.QUERY)
		except Exception:
			log.debugWarning("Unable to get navigator object speech for boundary feedback", exc_info=True)
			return
		if not speechSequence:
			return
		try:
			speech.speak(speechSequence)
		except Exception:
			log.debugWarning("Unable to speak navigator object for boundary feedback", exc_info=True)
		brailleMessage = " ".join(item for item in speechSequence if isinstance(item, str))
		brailleHandler = braille.handler
		if brailleMessage and brailleHandler is not None:
			try:
				brailleHandler.message(brailleMessage)
			except Exception:
				log.debugWarning("Unable to braille navigator object for boundary feedback", exc_info=True)

	def _getParagraphCurrentTextInfo(self, ti: textInfos.TextInfo | None) -> textInfos.TextInfo | None:
		if ti is not None:
			try:
				return ti.copy()
			except Exception:
				return None
		try:
			return api.getFocusObject().makeTextInfo(textInfos.POSITION_CARET)
		except Exception:
			return None

	def _speakParagraphTextInfo(self, info: textInfos.TextInfo, unit: _TextUnit) -> bool:
		if self._isObjectBelowLockScreen(info.obj):
			ui.reviewMessage(gui.blockAction.Context.WINDOWS_LOCKED.translatedMessage)
			return True
		try:
			speech.speakTextInfo(info, unit=unit, reason=controlTypes.OutputReason.CARET)
		except Exception:
			log.debugWarning("Unable to report current paragraph item for boundary feedback", exc_info=True)
			return False
		return True

	def _reportCurrentSingleLineParagraph(self, info: textInfos.TextInfo) -> bool:
		lineInfo = info.copy()
		lineInfo.expand(textInfos.UNIT_LINE)
		return self._speakParagraphTextInfo(lineInfo, textInfos.UNIT_LINE)

	def _isBlankTextInfoLine(self, info: textInfos.TextInfo) -> bool:
		lineInfo = info.copy()
		lineInfo.expand(textInfos.UNIT_LINE)
		return not lineInfo.text.strip()

	def _getCurrentMultiLineParagraphInfo(self, info: textInfos.TextInfo) -> textInfos.TextInfo:
		firstLine = info.copy()
		firstLine.expand(textInfos.UNIT_LINE)
		if not firstLine.text.strip():
			return firstLine

		start = firstLine.copy()
		start.collapse()
		lineCount = 0
		while lineCount < paragraphHelper.MAX_LINES:
			previousLine = start.copy()
			if not previousLine.move(textInfos.UNIT_LINE, -1):
				break
			if self._isBlankTextInfoLine(previousLine):
				break
			previousLine.expand(textInfos.UNIT_LINE)
			previousLine.collapse()
			start = previousLine
			lineCount += 1

		end = firstLine.copy()
		while lineCount < paragraphHelper.MAX_LINES:
			nextLine = end.copy()
			nextLine.collapse(end=True)
			if not nextLine.move(textInfos.UNIT_LINE, 1):
				break
			if self._isBlankTextInfoLine(nextLine):
				break
			nextLine.expand(textInfos.UNIT_LINE)
			end = nextLine
			lineCount += 1

		paragraphInfo = start.copy()
		paragraphInfo.setEndPoint(end, "endToEnd")
		return paragraphInfo

	def _reportCurrentMultiLineParagraph(self, info: textInfos.TextInfo) -> bool:
		try:
			paragraphInfo = self._getCurrentMultiLineParagraphInfo(info)
		except Exception:
			log.debugWarning("Unable to prepare current paragraph for boundary feedback", exc_info=True)
			return False
		return self._speakParagraphTextInfo(paragraphInfo, textInfos.UNIT_PARAGRAPH)

	def _callWithSuppressedFirstUiMessage(
		self,
		func: Callable[..., _CallResultT],
		*args: Any,
		**kwargs: Any,
	) -> _CallResultT:
		originalMessage = ui.message
		originalReviewMessage = ui.reviewMessage
		suppressedMessage = False

		def replacementMessage(*messageArgs: Any, **messageKwargs: Any) -> None:
			nonlocal suppressedMessage
			if not suppressedMessage:
				suppressedMessage = True
				return None
			return originalMessage(*messageArgs, **messageKwargs)

		def replacementReviewMessage(*messageArgs: Any, **messageKwargs: Any) -> None:
			nonlocal suppressedMessage
			if not suppressedMessage:
				suppressedMessage = True
				return None
			return originalReviewMessage(*messageArgs, **messageKwargs)

		ui.message = replacementMessage
		ui.reviewMessage = replacementReviewMessage
		try:
			return func(*args, **kwargs)
		finally:
			ui.message = originalMessage
			ui.reviewMessage = originalReviewMessage

	def _callOriginalForDetectedBoundary(
		self,
		scenario: str,
		direction: _BoundaryDirection,
		original: Callable[..., _CallResultT],
		*args: Any,
		replaceNativeBoundaryMessage: bool = True,
		currentItemReporter: _CurrentItemReporter | None = None,
		**kwargs: Any,
	) -> _CallResultT:
		mode = addonConfig.getScenarioMode(scenario)
		if mode == addonConfig.BoundaryFeedbackMode.NVDA_DEFAULT:
			return original(*args, **kwargs)
		if mode == addonConfig.BoundaryFeedbackMode.SOUND_ONLY and replaceNativeBoundaryMessage:
			result = self._callWithSuppressedFirstUiMessage(
				original,
				*args,
				**kwargs,
			)
			self._playBoundarySound(direction)
			return result
		if addonConfig.modeReportsCurrentItem(mode) and replaceNativeBoundaryMessage:
			result = self._callWithSuppressedFirstUiMessage(
				original,
				*args,
				**kwargs,
			)
			if currentItemReporter is not None:
				currentItemReporter()
			if addonConfig.modePlaysSound(mode):
				self._playBoundarySound(direction)
			return result
		result = original(*args, **kwargs)
		if addonConfig.modePlaysSound(mode):
			self._playBoundarySound(direction)
		return result

	def _playBoundarySoundForScenario(self, scenario: str, direction: _BoundaryDirection) -> None:
		if addonConfig.modePlaysSound(addonConfig.getScenarioMode(scenario)):
			self._playBoundarySound(direction)

	def _replaceGestureMapFunction(
		self,
		obj: Any,
		original: Callable[..., Any],
		replacement: Callable[..., Any],
	) -> None:
		gestureMap = getattr(obj, "_gestureMap", None)
		if not gestureMap:
			return
		for identifier, func in list(gestureMap.items()):
			if func is original:
				gestureMap[identifier] = replacement

	def _restoreGestureMapReplacements(self) -> None:
		for targetProvider, original, replacement in reversed(self._gestureMapReplacements):
			try:
				targets = targetProvider()
			except Exception:
				log.debugWarning(
					"Unable to get gesture map targets for objBoundaryFeedback restore",
					exc_info=True,
				)
				continue
			for target in targets:
				self._replaceGestureMapFunction(target, replacement, original)

	def _installMethodPatch(
		self,
		owner: Any,
		name: str,
		replacement: Callable[..., Any],
	) -> None:
		original = getattr(owner, name)
		setattr(owner, name, replacement)
		self._methodPatches.append((owner, name, original))

	def _installGlobalCommandHook(
		self,
		scenario: str,
		name: str,
		boundaryDetector: Callable[[], _BoundaryDirection | None],
		currentItemReporter: _CurrentItemReporter | None = None,
	) -> None:
		original = getattr(globalCommands.GlobalCommands, name, None)
		if original is None:
			log.warning(f"Skipping objBoundaryFeedback hook: GlobalCommands.{name} does not exist")
			return
		if not _hasExpectedFunctionSignature(original, ("self", "gesture")):
			return

		@functools.wraps(original)
		def replacement(commandObj: globalCommands.GlobalCommands, gesture: inputCore.InputGesture) -> None:
			if addonConfig.getScenarioMode(scenario) == addonConfig.BoundaryFeedbackMode.NVDA_DEFAULT:
				return original(commandObj, gesture)
			direction = self._safeDetectBoundary(boundaryDetector)
			if direction:
				return self._callOriginalForDetectedBoundary(
					scenario,
					direction,
					original,
					commandObj,
					gesture,
					currentItemReporter=currentItemReporter,
				)
			return original(commandObj, gesture)

		self._installMethodPatch(globalCommands.GlobalCommands, name, replacement)
		self._replaceGestureMapFunction(globalCommands.commands, original, replacement)
		self._gestureMapReplacements.append((lambda: (globalCommands.commands,), original, replacement))

	def _safeDetectBoundary(
		self,
		boundaryDetector: Callable[[], _BoundaryDirection | None],
	) -> _BoundaryDirection | None:
		try:
			return boundaryDetector()
		except Exception:
			log.debugWarning("Unable to detect navigation boundary", exc_info=True)
			return None

	def _installGlobalCommandHooks(self) -> None:
		for scenario, name, detector, currentItemReporter in (
			(
				addonConfig.SCENARIO_REVIEW_MODE,
				"script_reviewMode_next",
				self._detectReviewModeNextBoundary,
				self._reportCurrentReviewMode,
			),
			(
				addonConfig.SCENARIO_REVIEW_MODE,
				"script_reviewMode_previous",
				self._detectReviewModePreviousBoundary,
				self._reportCurrentReviewMode,
			),
			(
				addonConfig.SCENARIO_OBJECT_NAVIGATION,
				"script_navigatorObject_parent",
				self._detectNavigatorParentBoundary,
				self._reportCurrentNavigatorObject,
			),
			(
				addonConfig.SCENARIO_OBJECT_NAVIGATION,
				"script_navigatorObject_next",
				self._detectNavigatorNextBoundary,
				self._reportCurrentNavigatorObject,
			),
			(
				addonConfig.SCENARIO_OBJECT_NAVIGATION,
				"script_navigatorObject_previous",
				self._detectNavigatorPreviousBoundary,
				self._reportCurrentNavigatorObject,
			),
			(
				addonConfig.SCENARIO_OBJECT_NAVIGATION,
				"script_navigatorObject_firstChild",
				self._detectNavigatorFirstChildBoundary,
				self._reportCurrentNavigatorObject,
			),
			(
				addonConfig.SCENARIO_OBJECT_NAVIGATION,
				"script_navigatorObject_nextInFlow",
				self._detectNavigatorNextInFlowBoundary,
				self._reportCurrentNavigatorObject,
			),
			(
				addonConfig.SCENARIO_OBJECT_NAVIGATION,
				"script_navigatorObject_previousInFlow",
				self._detectNavigatorPreviousInFlowBoundary,
				self._reportCurrentNavigatorObject,
			),
			(
				addonConfig.SCENARIO_REVIEW_CURSOR,
				"script_review_previousLine",
				self._detectReviewPreviousLineBoundary,
				None,
			),
			(
				addonConfig.SCENARIO_REVIEW_CURSOR,
				"script_review_nextLine",
				self._detectReviewNextLineBoundary,
				None,
			),
			(
				addonConfig.SCENARIO_REVIEW_CURSOR,
				"script_review_previousPage",
				self._detectReviewPreviousPageBoundary,
				None,
			),
			(
				addonConfig.SCENARIO_REVIEW_CURSOR,
				"script_review_nextPage",
				self._detectReviewNextPageBoundary,
				None,
			),
			(
				addonConfig.SCENARIO_REVIEW_CURSOR,
				"script_review_previousWord",
				self._detectReviewPreviousWordBoundary,
				None,
			),
			(
				addonConfig.SCENARIO_REVIEW_CURSOR,
				"script_review_nextWord",
				self._detectReviewNextWordBoundary,
				None,
			),
			(
				addonConfig.SCENARIO_REVIEW_CURSOR,
				"script_review_previousCharacter",
				self._detectReviewPreviousCharacterBoundary,
				None,
			),
			(
				addonConfig.SCENARIO_REVIEW_CURSOR,
				"script_review_nextCharacter",
				self._detectReviewNextCharacterBoundary,
				None,
			),
		):
			self._installGlobalCommandHook(scenario, name, detector, currentItemReporter)

	def _detectReviewModeNextBoundary(self) -> _BoundaryDirection | None:
		return None if self._hasAvailableReviewMode(previous=False) else _NEXT

	def _detectReviewModePreviousBoundary(self) -> _BoundaryDirection | None:
		return None if self._hasAvailableReviewMode(previous=True) else _PREVIOUS

	def _hasAvailableReviewMode(self, previous: bool) -> bool:
		currentMode = review.getCurrentMode()
		modes = review.modes
		currentIndex = next(
			(index for index, mode in enumerate(modes) if mode[0] == currentMode),
			None,
		)
		if currentIndex is None:
			return False
		step = -1 if previous else 1
		index = currentIndex + step
		obj = api.getNavigatorObject()
		while 0 <= index < len(modes):
			modeGetter = modes[index][2]
			if modeGetter(obj):
				return True
			index += step
		return False

	def _isNavigatorRelationMissing(self, simpleAttr: str, fullAttr: str) -> bool:
		curObject = api.getNavigatorObject()
		if not isinstance(curObject, NVDAObject):
			return False
		reviewCursorConfig = cast(Any, config.conf["reviewCursor"])
		attr = simpleAttr if bool(reviewCursorConfig["simpleReviewMode"]) else fullAttr
		return getattr(curObject, attr) is None

	def _detectNavigatorParentBoundary(self) -> _BoundaryDirection | None:
		return _PREVIOUS if self._isNavigatorRelationMissing("simpleParent", "parent") else None

	def _detectNavigatorNextBoundary(self) -> _BoundaryDirection | None:
		return _NEXT if self._isNavigatorRelationMissing("simpleNext", "next") else None

	def _detectNavigatorPreviousBoundary(self) -> _BoundaryDirection | None:
		return _PREVIOUS if self._isNavigatorRelationMissing("simplePrevious", "previous") else None

	def _detectNavigatorFirstChildBoundary(self) -> _BoundaryDirection | None:
		return _NEXT if self._isNavigatorRelationMissing("simpleFirstChild", "firstChild") else None

	def _detectNavigatorNextInFlowBoundary(self) -> _BoundaryDirection | None:
		curObject = api.getNavigatorObject()
		if not isinstance(curObject, NVDAObject):
			return None
		if getattr(curObject, "simpleFirstChild") or getattr(curObject, "simpleNext"):
			return None
		parent = getattr(curObject, "simpleParent")
		while parent and not getattr(parent, "simpleNext"):
			parent = getattr(parent, "simpleParent")
		return None if parent else _NEXT

	def _detectNavigatorPreviousInFlowBoundary(self) -> _BoundaryDirection | None:
		curObject = api.getNavigatorObject()
		if not isinstance(curObject, NVDAObject):
			return None
		return (
			None if getattr(curObject, "simplePrevious") or getattr(curObject, "simpleParent") else _PREVIOUS
		)

	def _detectReviewPreviousLineBoundary(self) -> _BoundaryDirection | None:
		info = api.getReviewPosition().copy()
		info.expand(textInfos.UNIT_LINE)
		info.collapse()
		return _PREVIOUS if info.move(textInfos.UNIT_LINE, -1) == 0 else None

	def _detectReviewNextLineBoundary(self) -> _BoundaryDirection | None:
		origInfo = api.getReviewPosition().copy()
		origInfo.collapse()
		info = origInfo.copy()
		res = info.move(textInfos.UNIT_LINE, 1)
		newLine = info.copy()
		newLine.expand(textInfos.UNIT_LINE)
		return _NEXT if res == 0 or newLine.start <= origInfo.start else None

	def _detectReviewPreviousPageBoundary(self) -> _BoundaryDirection | None:
		info = api.getReviewPosition().copy()
		try:
			info.expand(textInfos.UNIT_PAGE)
			info.collapse()
			res = info.move(textInfos.UNIT_PAGE, -1)
		except (ValueError, NotImplementedError):
			return None
		return _PREVIOUS if res == 0 else None

	def _detectReviewNextPageBoundary(self) -> _BoundaryDirection | None:
		origInfo = api.getReviewPosition().copy()
		origInfo.collapse()
		info = origInfo.copy()
		try:
			res = info.move(textInfos.UNIT_PAGE, 1)
			newPage = info.copy()
			newPage.expand(textInfos.UNIT_PAGE)
		except (ValueError, NotImplementedError):
			return None
		return _NEXT if res == 0 or newPage.start <= origInfo.start else None

	def _detectReviewPreviousWordBoundary(self) -> _BoundaryDirection | None:
		info = api.getReviewPosition().copy()
		info.expand(textInfos.UNIT_WORD)
		info.collapse()
		return _PREVIOUS if info.move(textInfos.UNIT_WORD, -1) == 0 else None

	def _detectReviewNextWordBoundary(self) -> _BoundaryDirection | None:
		origInfo = api.getReviewPosition().copy()
		origInfo.collapse()
		info = origInfo.copy()
		res = info.move(textInfos.UNIT_WORD, 1)
		newWord = info.copy()
		newWord.expand(textInfos.UNIT_WORD)
		return _NEXT if res == 0 or newWord.start <= origInfo.start else None

	def _detectReviewPreviousCharacterBoundary(self) -> _BoundaryDirection | None:
		lineInfo = api.getReviewPosition().copy()
		lineInfo.expand(textInfos.UNIT_LINE)
		charInfo = api.getReviewPosition().copy()
		charInfo.expand(textInfos.UNIT_CHARACTER)
		charInfo.collapse()
		res = charInfo.move(textInfos.UNIT_CHARACTER, -1)
		if res == 0 or charInfo.compareEndPoints(lineInfo, "startToStart") < 0:
			return _PREVIOUS
		return None

	def _detectReviewNextCharacterBoundary(self) -> _BoundaryDirection | None:
		lineInfo = api.getReviewPosition().copy()
		lineInfo.expand(textInfos.UNIT_LINE)
		charInfo = api.getReviewPosition().copy()
		charInfo.expand(textInfos.UNIT_CHARACTER)
		charInfo.collapse()
		res = charInfo.move(textInfos.UNIT_CHARACTER, 1)
		if res == 0 or charInfo.compareEndPoints(lineInfo, "endToEnd") >= 0:
			return _NEXT
		return None

	def _installBrowseModeHooks(self) -> None:
		quickNavOriginal = browseMode.BrowseModeTreeInterceptor._quickNavScript
		if _hasExpectedFunctionSignature(
			quickNavOriginal,
			("self", "gesture", "itemType", "direction", "errorMessage", "readUnit"),
		):
			quickNavReplacement = self._makeQuickNavScriptReplacement(quickNavOriginal)
			self._installMethodPatch(
				browseMode.BrowseModeTreeInterceptor,
				"_quickNavScript",
				quickNavReplacement,
			)

		movePastEndOriginal = getattr(
			browseMode.BrowseModeDocumentTreeInterceptor,
			"script_movePastEndOfContainer",
			None,
		)
		if movePastEndOriginal is None:
			return
		if _hasExpectedFunctionSignature(movePastEndOriginal, ("self", "gesture")):
			movePastEndReplacement = self._makeMovePastEndOfContainerReplacement(movePastEndOriginal)
			self._installMethodPatch(
				browseMode.BrowseModeDocumentTreeInterceptor,
				"script_movePastEndOfContainer",
				movePastEndReplacement,
			)
			self._replaceExistingBrowseModeGestureMaps(movePastEndOriginal, movePastEndReplacement)
			self._gestureMapReplacements.append(
				(
					self._getRunningBrowseModeTreeInterceptors,
					movePastEndOriginal,
					movePastEndReplacement,
				),
			)

	def _makeQuickNavScriptReplacement(self, original: _QuickNavScript) -> _QuickNavScript:
		@functools.wraps(original)
		def replacement(
			treeInterceptor: browseMode.BrowseModeTreeInterceptor,
			gesture: inputCore.InputGesture | None,
			itemType: str,
			direction: _BrowseDirection,
			errorMessage: str,
			readUnit: _TextUnit | None,
		) -> None:
			mode = addonConfig.getScenarioMode(addonConfig.SCENARIO_BROWSE_MODE_QUICK_NAV)
			if mode == addonConfig.BoundaryFeedbackMode.NVDA_DEFAULT:
				return original(treeInterceptor, gesture, itemType, direction, errorMessage, readUnit)
			result, hitBoundary = self._callQuickNavWithBoundaryMessageDetection(
				errorMessage,
				mode == addonConfig.BoundaryFeedbackMode.SOUND_ONLY,
				original,
				treeInterceptor,
				gesture,
				itemType,
				direction,
				errorMessage,
				readUnit,
			)
			if hitBoundary:
				self._playBoundarySoundForScenario(
					addonConfig.SCENARIO_BROWSE_MODE_QUICK_NAV,
					_directionFromBrowseDirection(direction),
				)
			return result

		return replacement

	def _callQuickNavWithBoundaryMessageDetection(
		self,
		errorMessage: str,
		suppressBoundaryMessage: bool,
		func: Callable[..., _CallResultT],
		*args: Any,
		**kwargs: Any,
	) -> tuple[_CallResultT, bool]:
		originalMessage = ui.message
		hitBoundary = False

		def replacementMessage(text: str, *messageArgs: Any, **messageKwargs: Any) -> None:
			nonlocal hitBoundary
			if text == errorMessage:
				hitBoundary = True
				if suppressBoundaryMessage:
					return None
			return originalMessage(text, *messageArgs, **messageKwargs)

		ui.message = replacementMessage
		try:
			return func(*args, **kwargs), hitBoundary
		finally:
			ui.message = originalMessage

	def _makeMovePastEndOfContainerReplacement(
		self,
		original: _MovePastEndOfContainerScript,
	) -> _MovePastEndOfContainerScript:
		@functools.wraps(original)
		def replacement(
			treeInterceptor: browseMode.BrowseModeDocumentTreeInterceptor,
			gesture: inputCore.InputGesture,
		) -> None:
			if (
				addonConfig.getScenarioMode(addonConfig.SCENARIO_BROWSE_MODE_CONTAINER_END)
				== addonConfig.BoundaryFeedbackMode.NVDA_DEFAULT
			):
				return original(treeInterceptor, gesture)
			hitBoundary = self._isMovePastEndOfContainerBoundary(treeInterceptor)
			if hitBoundary:
				return self._callOriginalForDetectedBoundary(
					addonConfig.SCENARIO_BROWSE_MODE_CONTAINER_END,
					_NEXT,
					original,
					treeInterceptor,
					gesture,
				)
			return original(treeInterceptor, gesture)

		return replacement

	def _isMovePastEndOfContainerBoundary(
		self,
		treeInterceptor: browseMode.BrowseModeDocumentTreeInterceptor,
	) -> bool:
		try:
			info = treeInterceptor.makeTextInfo(textInfos.POSITION_CARET)
			info.expand(textInfos.UNIT_CHARACTER)
			container = treeInterceptor.getEnclosingContainerRange(info)
			if not container:
				return False
			container.collapse(end=True)
			docEnd = container.obj.makeTextInfo(textInfos.POSITION_LAST)
			return container.compareEndPoints(docEnd, "endToEnd") >= 0
		except Exception:
			log.debugWarning("Unable to detect container end boundary", exc_info=True)
			return False

	def _getRunningBrowseModeTreeInterceptors(self) -> tuple[browseMode.BrowseModeTreeInterceptor, ...]:
		return tuple(
			treeInterceptor
			for treeInterceptor in treeInterceptorHandler.runningTable
			if isinstance(treeInterceptor, browseMode.BrowseModeTreeInterceptor)
		)

	def _replaceExistingBrowseModeGestureMaps(
		self,
		original: Callable[..., Any],
		replacement: Callable[..., Any],
	) -> None:
		for treeInterceptor in self._getRunningBrowseModeTreeInterceptors():
			self._replaceGestureMapFunction(treeInterceptor, original, replacement)

	def _installCursorManagerHook(self) -> None:
		original = cast(
			_CursorManagerCaretMovementScript,
			cursorManager.CursorManager._caretMovementScriptHelper,
		)
		expected = (
			"self",
			"gesture",
			"unit",
			"direction",
			"posConstant",
			"posUnit",
			"posUnitEnd",
			"extraDetail",
			"handleSymbols",
		)
		if not _hasExpectedFunctionSignature(original, expected):
			return

		@functools.wraps(original)
		def replacement(
			cursorManagerObj: cursorManager.CursorManager,
			gesture: inputCore.InputGesture,
			unit: _TextUnit,
			direction: int | None = None,
			posConstant: _TextPosition = textInfos.POSITION_SELECTION,
			posUnit: _TextUnit | None = None,
			posUnitEnd: bool = False,
			extraDetail: bool = False,
			handleSymbols: bool = False,
		) -> None:
			if (
				addonConfig.getScenarioMode(addonConfig.SCENARIO_BROWSE_MODE_VIRTUAL_CURSOR)
				== addonConfig.BoundaryFeedbackMode.NVDA_DEFAULT
			):
				return original(
					cursorManagerObj,
					gesture,
					unit,
					direction,
					posConstant,
					posUnit,
					posUnitEnd,
					extraDetail,
					handleSymbols,
				)
			if scriptHandler.isScriptWaiting():
				return original(
					cursorManagerObj,
					gesture,
					unit,
					direction,
					posConstant,
					posUnit,
					posUnitEnd,
					extraDetail,
					handleSymbols,
				)
			before = _getSelectionRange(cursorManagerObj)
			boundaryDirection = _directionFromCursorMovement(direction, posConstant, posUnit, posUnitEnd)
			original(
				cursorManagerObj,
				gesture,
				unit,
				direction,
				posConstant,
				posUnit,
				posUnitEnd,
				extraDetail,
				handleSymbols,
			)
			after = _getSelectionRange(cursorManagerObj)
			if before is not None and after is not None and _sameTextRange(before, after):
				if posUnit == textInfos.UNIT_LINE and not _lineBoundaryIsDocumentBoundary(
					after,
					boundaryDirection,
				):
					return
				self._playBoundarySoundForScenario(
					addonConfig.SCENARIO_BROWSE_MODE_VIRTUAL_CURSOR,
					boundaryDirection,
				)

		self._installMethodPatch(cursorManager.CursorManager, "_caretMovementScriptHelper", replacement)

	def _installEditableTextHook(self) -> None:
		original = cast(
			_EditableTextCaretMovementScript,
			editableText.EditableText._caretMovementScriptHelper,
		)
		if not _hasExpectedFunctionSignature(original, ("self", "gesture", "unit")):
			return

		@functools.wraps(original)
		def replacement(
			editableTextObj: editableText.EditableText,
			gesture: inputCore.InputGesture,
			unit: _TextUnit,
		) -> None:
			if (
				addonConfig.getScenarioMode(addonConfig.SCENARIO_EDITABLE_TEXT_CARET)
				== addonConfig.BoundaryFeedbackMode.NVDA_DEFAULT
			):
				return original(editableTextObj, gesture, unit)
			movement = _caretMovementFromGesture(gesture)
			if (
				movement is None
				or scriptHandler.isScriptWaiting()
				or eventHandler.isPendingEvents("gainFocus")
			):
				return original(editableTextObj, gesture, unit)
			boundaryDirection, lineBoundaryOnly = movement
			focusBefore = api.getFocusObject()
			try:
				before = editableTextObj.makeTextInfo(textInfos.POSITION_SELECTION).copy()
				# Collapsing a selection is successful navigation even if its start
				# (what POSITION_CARET reports for Word) does not change.
				hadSelection = not before.isCollapsed
			except Exception:
				original(editableTextObj, gesture, unit)
				return
			if hadSelection:
				return original(editableTextObj, gesture, unit)
			beforeValueSnapshot = _getEditableValueSnapshot(editableTextObj)
			observedInfo = _callWithCaretMoveObservation(editableTextObj, original, gesture, unit)
			if (
				observedInfo is None
				or scriptHandler.isScriptWaiting()
				or eventHandler.isPendingEvents("gainFocus")
				or api.getFocusObject() != focusBefore
				or not _sameTextRange(before, observedInfo)
			):
				return
			afterValueSnapshot = _getEditableValueSnapshot(editableTextObj)
			try:
				after = editableTextObj.makeTextInfo(textInfos.POSITION_SELECTION).copy()
			except Exception:
				return
			if _sameTextRange(before, after):
				if beforeValueSnapshot != afterValueSnapshot:
					return
				if lineBoundaryOnly and not _lineBoundaryIsDocumentBoundary(after, boundaryDirection):
					return
				self._playBoundarySoundForScenario(
					addonConfig.SCENARIO_EDITABLE_TEXT_CARET,
					boundaryDirection,
				)

		self._installMethodPatch(editableText.EditableText, "_caretMovementScriptHelper", replacement)

	def _installParagraphHelperHooks(self) -> None:
		for name, currentItemReporter in (
			("moveToSingleLineBreakParagraph", self._reportCurrentSingleLineParagraph),
			("moveToMultiLineBreakParagraph", self._reportCurrentMultiLineParagraph),
		):
			original = getattr(paragraphHelper, name)
			if not _hasExpectedFunctionSignature(original, ("nextParagraph", "speakNew", "ti")):
				continue
			replacement = self._makeParagraphMovementReplacement(original, currentItemReporter)
			self._installMethodPatch(paragraphHelper, name, replacement)

	def _makeParagraphMovementReplacement(
		self,
		original: _ParagraphMovementFunction,
		currentItemReporter: _ParagraphCurrentReporter,
	) -> _ParagraphMovementFunction:
		@functools.wraps(original)
		def replacement(
			nextParagraph: bool,
			speakNew: bool,
			ti: textInfos.TextInfo | None = None,
		) -> tuple[bool, bool]:
			direction = _NEXT if nextParagraph else _PREVIOUS
			mode = addonConfig.getScenarioMode(addonConfig.SCENARIO_PARAGRAPH_NAVIGATION)
			reportsCurrentItem = addonConfig.modeReportsCurrentItem(mode)
			if reportsCurrentItem:
				reportInfo = self._getParagraphCurrentTextInfo(ti)
				passKey, moved = self._callWithSuppressedFirstUiMessage(
					original,
					nextParagraph,
					speakNew,
					ti,
				)
			else:
				reportInfo = None
				passKey, moved = original(nextParagraph, speakNew, ti)
			if not passKey and not moved:
				if reportsCurrentItem and (reportInfo is None or not currentItemReporter(reportInfo)):
					# Translators: Reported when paragraph navigation cannot find another paragraph.
					ui.message(_("No next paragraph") if nextParagraph else _("No previous paragraph"))
				if addonConfig.modePlaysSound(mode):
					self._playBoundarySound(direction)
			return passKey, moved

		return replacement
