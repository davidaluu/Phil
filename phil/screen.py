# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""The screen pipeline: parse the UI hierarchy, prune the noise, hash it, and diff it.

This is the Python port of Phil's "edge": the pruning rules of the Android
accessibility service plus the diff engine of the original Node backend.

Pipeline for every step::

    XML dump -> parse -> prune -> render -> MD5 hash -> compare with last screen
                                                     -> full tree or changes-only text
"""

from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

logger = logging.getLogger(__name__)

DEFAULT_IGNORED_PACKAGES = frozenset({"com.android.systemui"})
MAX_TREE_CHARS = 12_000
MAX_DIFF_LINES = 40
MAX_LABEL_CHARS = 80

_ACTION_KEY_LABELS = frozenset({"enter", "search", "go", "done"})
_KEYBOARD_HINTS = ("inputmethod", "keyboard", "honeyboard", "swiftkey")
_TRAIT_ORDER = ("clickable", "focusable", "scrollable", "checked", "password")
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_INDEX_SUFFIX_RE = re.compile(r"^(.*\S)\s*#(\d+)$")
_WHITESPACE_RE = re.compile(r"\s+")


class ScreenError(RuntimeError):
    """Raised when a UI hierarchy cannot be parsed."""


# --------------------------------------------------------------------- nodes


@dataclass(eq=False)
class Node:
    """One accessibility node from the hierarchy dump."""

    class_name: str = ""
    text: str = ""
    desc: str = ""
    resource_id: str = ""
    package: str = ""
    clickable: bool = False
    focusable: bool = False
    scrollable: bool = False
    checked: bool = False
    password: bool = False
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    parent: Node | None = field(default=None, repr=False)
    children: list[Node] = field(default_factory=list, repr=False)

    @property
    def short_class(self) -> str:
        """Class name without its package, e.g. ``Button``."""
        return self.class_name.rsplit(".", 1)[-1] or "View"

    @property
    def area(self) -> int:
        left, top, right, bottom = self.bounds
        return max(0, right - left) * max(0, bottom - top)

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    @property
    def is_interactive(self) -> bool:
        return self.clickable or self.focusable or self.scrollable

    @property
    def has_label(self) -> bool:
        return bool(self.text or self.desc)

    @property
    def display_text(self) -> str:
        """Text shown to the model. Password contents are never exposed."""
        if self.password and self.text:
            return "\u2022" * 8
        return self.text


def _flag(attributes: dict[str, str], name: str) -> bool:
    return attributes.get(name, "false").lower() == "true"


def _parse_bounds(raw: str | None) -> tuple[int, int, int, int]:
    match = _BOUNDS_RE.search(raw or "")
    if not match:
        return (0, 0, 0, 0)
    left, top, right, bottom = (int(group) for group in match.groups())
    return (left, top, right, bottom)


def _build_node(element: ET.Element, parent: Node) -> None:
    attributes = element.attrib
    # uiautomator uses <node class="...">; uiautomator2 may use the class as the tag name.
    class_name = attributes.get("class") or ("" if element.tag == "node" else element.tag)
    node = Node(
        class_name=class_name,
        text=attributes.get("text", ""),
        desc=attributes.get("content-desc", ""),
        resource_id=attributes.get("resource-id", ""),
        package=attributes.get("package", ""),
        clickable=_flag(attributes, "clickable"),
        focusable=_flag(attributes, "focusable"),
        scrollable=_flag(attributes, "scrollable"),
        checked=_flag(attributes, "checked"),
        password=_flag(attributes, "password"),
        bounds=_parse_bounds(attributes.get("bounds")),
        parent=parent,
    )
    parent.children.append(node)
    for child in element:
        _build_node(child, node)


def parse_hierarchy(xml_text: str) -> Node:
    """Parse a hierarchy dump into a tree under a synthetic root node.

    Raises:
        ScreenError: if the XML is malformed.
    """
    try:
        element = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ScreenError(f"Could not parse the UI hierarchy: {exc}") from exc
    root = Node()
    if element.tag == "hierarchy":
        for child in element:
            _build_node(child, root)
    else:
        _build_node(element, root)
    return root


# ------------------------------------------------------------------- pruning


def _is_keyboard_package(package: str) -> bool:
    lowered = package.lower()
    return any(hint in lowered for hint in _KEYBOARD_HINTS) or "ime" in lowered.split(".")


def _is_action_key(node: Node) -> bool:
    return (node.text or node.desc).strip().casefold() in _ACTION_KEY_LABELS


def _is_image(node: Node) -> bool:
    return node.short_class.endswith(("ImageView", "Image"))


def _keep_leaf(node: Node) -> bool:
    """Decide whether a node with no surviving children is worth keeping.

    Rules (from the Android service): non-clickable images go; empty layouts go; a leaf
    that is not clickable, focusable or scrollable goes. One deliberate deviation: a leaf
    that carries text or a content description is kept, because the model must be able to
    read labels, messages and titles, and to target them by text.
    """
    if _is_image(node) and not node.clickable:
        return False
    return node.is_interactive or node.has_label


def _keep_container(node: Node) -> bool:
    """Keep a container with surviving children only if it is meaningful on its own."""
    return node.clickable or node.scrollable or node.has_label


class _Kept(NamedTuple):
    node: Node
    depth: int


def _prune_node(node: Node, depth: int, ignored: frozenset[str]) -> list[_Kept]:
    if node.area <= 0 or node.package in ignored:
        return []

    collected: list[_Kept] = []
    for child in node.children:
        collected.extend(_prune_node(child, depth + 1, ignored))

    if _is_keyboard_package(node.package):
        # Drop every key except the action keys (Enter, Search, Go, Done).
        return ([_Kept(node, depth)] if _is_action_key(node) else []) + collected

    if not collected:
        return [_Kept(node, depth)] if _keep_leaf(node) else []

    if _keep_container(node):
        return [_Kept(node, depth), *collected]

    # A plain wrapper: hoist its children one level up instead of printing it.
    return [_Kept(item.node, item.depth - 1) for item in collected]


def _clean(value: str) -> str:
    """Single-line, quote-safe, length-limited text for display."""
    cleaned = _WHITESPACE_RE.sub(" ", value).replace('"', "'").strip()
    if len(cleaned) > MAX_LABEL_CHARS:
        cleaned = cleaned[: MAX_LABEL_CHARS - 1] + "\u2026"
    return cleaned


def _traits(node: Node) -> frozenset[str]:
    return frozenset(name for name in _TRAIT_ORDER if getattr(node, name))


@dataclass(frozen=True)
class Element:
    """A node that survived pruning, ready to be shown to the model."""

    key: str
    depth: int
    line: str
    text: str
    desc: str
    traits: frozenset[str]
    node: Node = field(compare=False, repr=False)


def _base_key(node: Node) -> str:
    """Identity of an element across screens (mirrors the original Node backend)."""
    if node.resource_id:
        return f"id:{node.resource_id}"
    if node.desc:
        return f"desc:{_clean(node.desc)}"
    if node.text:
        return f"text:{node.short_class}|{_clean(node.display_text)}"
    return f"pos:{node.short_class}@{node.bounds[0]},{node.bounds[1]}"


def _render_line(node: Node, occurrence: int) -> str:
    parts = [f'[{node.short_class}] "{_clean(node.display_text)}"']
    if node.desc:
        parts.append(f'content-desc: "{_clean(node.desc)}"')
    if node.resource_id:
        parts.append(f"id:{node.resource_id}")
    traits = [name for name in _TRAIT_ORDER if name in _traits(node)]
    if traits:
        parts.append(f"({', '.join(traits)})")
    if occurrence > 1:
        parts.append(f"#{occurrence}")
    return " ".join(parts)


def _build_elements(kept: list[_Kept]) -> list[Element]:
    seen: Counter[str] = Counter()
    elements: list[Element] = []
    for item in kept:
        base = _base_key(item.node)
        seen[base] += 1
        occurrence = seen[base]
        elements.append(
            Element(
                key=base if occurrence == 1 else f"{base}#{occurrence}",
                depth=max(0, item.depth),
                line=_render_line(item.node, occurrence),
                text=_clean(item.node.display_text),
                desc=_clean(item.node.desc),
                traits=_traits(item.node),
                node=item.node,
            )
        )
    return elements


def prune(
    root: Node, ignored_packages: Iterable[str] = DEFAULT_IGNORED_PACKAGES
) -> list[Element]:
    """Reduce a parsed hierarchy to the elements worth showing the model."""
    ignored = frozenset(ignored_packages)
    kept: list[_Kept] = []
    for child in root.children:
        kept.extend(_prune_node(child, 0, ignored))
    return _build_elements(kept)


def foreground_package(root: Node, ignored_packages: Iterable[str] = ()) -> str:
    """Best guess at the app on screen: the most common non-system, non-keyboard package."""
    ignored = frozenset(ignored_packages)
    counts: Counter[str] = Counter()
    stack = list(root.children)
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if (
            node.package
            and node.package not in ignored
            and not _is_keyboard_package(node.package)
        ):
            counts[node.package] += 1
    return counts.most_common(1)[0][0] if counts else ""


# ---------------------------------------------------------------- rendering


def render_tree(elements: list[Element]) -> str:
    """Render elements as indented text, one per line."""
    return "\n".join(f"{'  ' * element.depth}{element.line}" for element in elements)


def _md5(text: str) -> str:
    # MD5 is used only as a cheap change detector, never for security.
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def _truncate_tree(elements: list[Element]) -> str:
    lines: list[str] = []
    used = 0
    for index, element in enumerate(elements):
        line = f"{'  ' * element.depth}{element.line}"
        if used + len(line) > MAX_TREE_CHARS and lines:
            lines.append(f"... ({len(elements) - index} more elements not shown)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _overlap_ratio(previous: dict[str, Element], current: dict[str, Element]) -> float:
    """Fraction of the previous screen's element keys that still exist."""
    if not previous:
        return 1.0
    return len(previous.keys() & current.keys()) / len(previous)


def _describe_change(old: Element, new: Element) -> str | None:
    parts: list[str] = []
    if old.text != new.text:
        parts.append(f'text: "{old.text}" \u2192 "{new.text}"')
    if old.desc != new.desc:
        parts.append(f'content-desc: "{old.desc}" \u2192 "{new.desc}"')
    added = sorted(new.traits - old.traits)
    removed = sorted(old.traits - new.traits)
    if added:
        parts.append(f"+traits: {', '.join(added)}")
    if removed:
        parts.append(f"-traits: {', '.join(removed)}")
    return "; ".join(parts) or None


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_DIFF_LINES:
        return lines
    return [*lines[:MAX_DIFF_LINES], f"  ... ({len(lines) - MAX_DIFF_LINES} more)"]


def _render_diff(previous: dict[str, Element], current: dict[str, Element]) -> str | None:
    """Changes-only text, or None when there is nothing to report."""
    added = [f"  + [{key}] {element.line}" for key, element in current.items() if key not in previous]
    removed = [f"  - [{key}] {element.line}" for key, element in previous.items() if key not in current]
    changed: list[str] = []
    for key, element in current.items():
        old = previous.get(key)
        if old is None:
            continue
        description = _describe_change(old, element)
        if description:
            changed.append(f"  ~ [{key}] {description}")

    if not (added or removed or changed):
        return None

    unchanged = len(current) - len(added) - len(changed)
    lines = [f"UI Changes since last action ({unchanged} elements unchanged):"]
    for title, section in (("ADDED", added), ("REMOVED", removed), ("CHANGED", changed)):
        if section:
            lines.append(f"{title} elements:")
            lines.extend(_capped(section))
    return "\n".join(lines)


# ------------------------------------------------------------------ tracker


@dataclass(frozen=True)
class ScreenState:
    """What Phil learned from one look at the screen."""

    digest: str
    changed: bool
    mode: str  # "full" or "diff"
    reason: str
    overlap: float | None
    package: str
    element_count: int
    prompt_text: str

    def describe(self) -> str:
        """One-line summary for terminal logs."""
        overlap = f", overlap {self.overlap:.0%}" if self.overlap is not None else ""
        changed = "changed" if self.changed else "unchanged"
        return (
            f"{changed}; sending {self.mode} ({self.reason}){overlap}; "
            f"{self.element_count} elements; hash {self.digest[:8]}"
        )


@dataclass(frozen=True)
class _Snapshot:
    digest: str
    package: str
    elements: dict[str, Element]


class ScreenTracker:
    """Remembers the previous screen so each step can send a full tree or only the changes."""

    def __init__(
        self,
        *,
        ignored_packages: Iterable[str] = DEFAULT_IGNORED_PACKAGES,
        overlap_threshold: float = 0.4,
        always_send_full: bool = False,
    ) -> None:
        self._ignored = frozenset(ignored_packages)
        self._threshold = overlap_threshold
        self._always_full = always_send_full
        self._previous: _Snapshot | None = None
        self._root: Node | None = None
        self._elements: list[Element] = []

    def reset(self) -> None:
        """Forget the previous screen (call at the start of every task)."""
        self._previous = None
        self._root = None
        self._elements = []

    def element_snapshot(self) -> list[dict[str, object]]:
        """Return pruned element positions and labels for run recording and wireframes."""
        return [
            {
                "key": element.key,
                "text": element.text,
                "description": element.desc,
                "bounds": list(element.node.bounds),
                "traits": sorted(element.traits),
            }
            for element in self._elements
        ]

    def update(self, xml_text: str) -> ScreenState:
        """Process a new hierarchy dump and decide what to send the model."""
        root = parse_hierarchy(xml_text)
        elements = prune(root, self._ignored)
        digest = _md5(render_tree(elements))
        package = foreground_package(root, self._ignored)
        current = _Snapshot(digest, package, {element.key: element for element in elements})
        previous = self._previous

        changed = previous is None or digest != previous.digest
        overlap: float | None = None
        diff_text: str | None = None
        header = "Current UI Tree (full)"

        if previous is None:
            reason, header = "first step", "Current UI Tree (full \u2014 first step)"
        elif package and previous.package and package != previous.package:
            reason = f"foreground app changed to {package}"
            header = (
                f'Current UI Tree (full \u2014 screen changed from "{previous.package}" '
                f'to "{package}")'
            )
        else:
            overlap = _overlap_ratio(previous.elements, current.elements)
            if overlap < self._threshold:
                reason = "major screen change"
                header = (
                    "Current UI Tree (full \u2014 screen changed, only "
                    f"{overlap:.0%} of the previous elements remain)"
                )
            else:
                diff_text = _render_diff(previous.elements, current.elements)
                if diff_text is None:
                    reason = "no visible changes"
                    header = (
                        "Current UI Tree (full \u2014 no visible changes detected, "
                        "sending full tree for safety)"
                    )
                else:
                    reason = "same screen"

        if diff_text is not None and self._always_full:
            diff_text = None
            header = "Current UI Tree (full)"
            reason = "full tree forced by ALWAYS_SEND_FULL_TREE"

        if diff_text is not None:
            mode, prompt_text = "diff", diff_text
        else:
            body = _truncate_tree(elements) if elements else "(no elements detected)"
            mode, prompt_text = "full", f"{header}:\n{body}"

        self._previous = current
        self._root = root
        self._elements = elements
        return ScreenState(
            digest=digest,
            changed=changed,
            mode=mode,
            reason=reason,
            overlap=overlap,
            package=package,
            element_count=len(elements),
            prompt_text=prompt_text,
        )

    def expected_keys(self, target: str | None = None, *, climb: bool = False) -> list[str]:
        """Return up to eight stable screen keys, including the resolved action target."""
        target_node = self.find(target, climb=climb) if target else None
        target_element = next(
            (element for element in self._elements if element.node is target_node), None
        )
        target_key = target_element.key if target_element is not None else None
        stable = [element.key for element in self._elements if not element.key.startswith("pos:")]
        stable.sort(key=lambda key: (not key.startswith("id:"), key))

        selected: list[str] = []
        if target_key and not target_key.startswith("pos:"):
            selected.append(target_key)
        selected.extend(key for key in stable if key not in selected)
        return selected[:8]

    def overlap_fraction(self, expected_keys: list[str]) -> float:
        """Return the fraction of recorded keys present in the current screen."""
        if not expected_keys:
            return 1.0
        current = {element.key for element in self._elements}
        return sum(key in current for key in expected_keys) / len(expected_keys)

    # ------------------------------------------------------------ resolution

    def screen_bounds(self) -> tuple[int, int, int, int] | None:
        """Bounds of the largest top-level window (the app), or None if unknown."""
        if self._root is None:
            return None
        windows = [
            node
            for node in self._root.children
            if node.package not in self._ignored and node.area > 0
        ]
        if not windows:
            return None
        return max(windows, key=lambda node: node.area).bounds

    def full_tree_text(self) -> str:
        """Return the current complete pruned tree, including stable target ids."""
        body = _truncate_tree(self._elements) if self._elements else "(no elements detected)"
        return f"Current UI Tree (full):\n{body}"

    def _match(self, wanted: str) -> list[Element]:
        lowered = wanted.casefold()
        by_id = [
            element
            for element in self._elements
            if element.node.resource_id
            and (element.node.resource_id == wanted or element.node.resource_id.endswith("/" + wanted))
        ]
        if by_id:
            return by_id
        exact = [
            element
            for element in self._elements
            if lowered in (element.node.text.casefold(), element.node.desc.casefold())
        ]
        if exact:
            return exact
        return [
            element
            for element in self._elements
            if lowered in element.node.text.casefold() or lowered in element.node.desc.casefold()
        ]

    def find(self, target: str | None, *, climb: bool = False) -> Node | None:
        """Resolve a model-supplied target (resource id, text or content description).

        A trailing ``#N`` selects the Nth duplicate, matching the marker shown in the tree.
        With ``climb`` set, a non-clickable match is replaced by its nearest clickable
        ancestor (the equivalent of the Android service's parent-climbing).
        """
        if not target or not target.strip():
            return None
        wanted = target.strip()
        matches = self._match(wanted)
        index: int | None = None
        if not matches:
            suffix = _INDEX_SUFFIX_RE.match(wanted)
            if suffix:
                wanted, index = suffix.group(1).strip(), int(suffix.group(2))
                matches = self._match(wanted)
        if not matches:
            return None

        if index is not None:
            if not 1 <= index <= len(matches):
                return None
            node = matches[index - 1].node
        else:
            node = min(matches, key=lambda element: not element.node.is_interactive).node

        if climb and not node.clickable:
            ancestor = node.parent
            while ancestor is not None:
                if ancestor.clickable and ancestor.area > 0:
                    return ancestor
                ancestor = ancestor.parent
        return node
