"""AprilTag mapping rules for inventory cabinets.

The current convention is AprilTag ID == int(柜号).  The database cabinet id is
a different number and must never be used as a tag id.  Anything that cannot be
converted confidently is shown with the reason instead of being guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TAG_FAMILY = "tag36h11"
# tag36h11 defines tag ids 0..586.
TAG_MAX_ID = 586

_NUMERIC = re.compile(r"^\d+$")


@dataclass
class TagEntry:
    cabinet: "object"
    raw_number: str
    tag_id: int | None
    valid: bool
    reason: str = ""
    ambiguous: bool = False
    duplicates: list = field(default_factory=list)

    @property
    def selectable(self):
        return self.valid and not self.ambiguous

    def status_text(self):
        if not self.valid:
            return "无有效标签映射：" + self.reason
        if self.ambiguous:
            return "重复映射：" + "、".join(self.duplicates)
        return "有效"

    def cabinet_label(self):
        cabinet = self.cabinet
        name = cabinet.equipment_name or "未命名柜子"
        return f"{cabinet.cabinet_number} 号柜·{name}"


def parse_cabinet_number(cabinet):
    raw = str(cabinet.cabinet_number or "").strip()
    if not raw:
        return TagEntry(cabinet, raw, None, False, "柜号为空")
    if not _NUMERIC.match(raw):
        return TagEntry(cabinet, raw, None, False, "非数字柜号")
    value = int(raw)
    if value < 0 or value > TAG_MAX_ID:
        return TagEntry(
            cabinet,
            raw,
            None,
            False,
            f"超出 {TAG_FAMILY} 的 ID 范围 0-{TAG_MAX_ID}",
        )
    entry = TagEntry(cabinet, raw, value, True)
    if raw != str(value):
        entry.reason = f"按整数解析为 {value}"
    return entry


def build_entries(cabinets):
    """Return TagEntry list ordered valid -> ambiguous -> invalid."""

    entries = [parse_cabinet_number(cabinet) for cabinet in cabinets]
    grouped = {}
    for entry in entries:
        if entry.valid and entry.tag_id is not None:
            grouped.setdefault(entry.tag_id, []).append(entry)
    for tag_id, group in grouped.items():
        if len(group) <= 1:
            continue
        labels = [
            f"{item.cabinet.cabinet_number}（数据库 ID {item.cabinet.cabinet_id}）"
            for item in group
        ]
        for entry in group:
            entry.ambiguous = True
            own = f"{entry.cabinet.cabinet_number}（数据库 ID {entry.cabinet.cabinet_id}）"
            entry.duplicates = [label for label in labels if label != own] or labels

    def sort_key(entry):
        if entry.valid and not entry.ambiguous:
            return (0, entry.tag_id, entry.raw_number)
        if entry.valid:
            return (1, entry.tag_id, entry.raw_number)
        return (2, 10**9, entry.raw_number)

    return sorted(entries, key=sort_key)


def find_by_tag(entries, tag_id):
    return [entry for entry in entries if entry.valid and entry.tag_id == tag_id]


def summary(entries):
    valid = sum(1 for entry in entries if entry.selectable)
    ambiguous = sum(1 for entry in entries if entry.ambiguous)
    invalid = sum(1 for entry in entries if not entry.valid)
    return {"valid": valid, "ambiguous": ambiguous, "invalid": invalid, "total": len(entries)}
