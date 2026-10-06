"""Befattning → role groups, for the leaderboard's position filter.

FI's position field is free text — ~3,900 spellings of perhaps ten roles
("VD", "Verkställande direktör (VD)", "CEO", "vd", "VD,Styrelseledamot/suppleant").
The ordered patterns in data/seed/position_group.csv (table `position_group`)
fold them into config.POSITION_GROUPS. The rules for reading that file are in
its own header.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from . import config

# A comma right after a hyphen is inside a phrase ("administrations-, lednings-"),
# not between two roles.
_SPLIT = re.compile(r"(?<!-),|/|;|\+|\s(?:och|and|&|resp\.?)\s")
_FALLBACK = "Övrigt"


def parts(text: str | None) -> list[str]:
    s = re.sub(r"[()]", " ", (text or "").casefold().replace("\xa0", " "))
    s = re.sub(r"\s+", " ", s)
    return [p for p in (x.strip(" .-:") for x in _SPLIT.split(s)) if p]


@dataclass
class Classifier:
    rules: list[tuple[re.Pattern[str], str]]
    unmatched: int = 0                      # rows that fell through to Övrigt
    _memo: dict[str | None, tuple[int, bool]] = field(default_factory=dict)

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> Classifier:
        return cls([
            (re.compile(r["pattern"]), r["grp"])
            for r in conn.execute("SELECT pattern, grp FROM position_group ORDER BY ord")
        ])

    def groups(self, text: str | None) -> set[str]:
        """Every group any part of `text` matched — empty when none did."""
        out: set[str] = set()
        for p in parts(text):
            for rx, grp in self.rules:
                if rx.search(p):
                    out.add(grp)
                    break
        return out

    def mask(self, text: str | None) -> int:
        """Bit i set = config.POSITION_GROUPS[i]. Never 0: a row nothing
        matched is Övrigt, and is counted in `unmatched`."""
        if text not in self._memo:
            g = self.groups(text)
            self._memo[text] = (
                sum(1 << i for i, name in enumerate(config.POSITION_GROUPS) if name in g)
                or 1 << config.POSITION_GROUPS.index(_FALLBACK),
                bool(g),
            )
        m, matched = self._memo[text]
        if not matched:
            self.unmatched += 1
        return m
