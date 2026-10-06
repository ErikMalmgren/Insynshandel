"""Befattning → role groups (data/seed/position_group.csv). Hermetic."""

from __future__ import annotations

import pytest

from insynshandel import config, positions


@pytest.fixture
def roles(db_conn):
    return positions.Classifier.load(db_conn)


# The most common spellings in the 2016-2026 register, and the traps.
@pytest.mark.parametrize(
    "text,groups",
    [
        ("Styrelseledamot", {"Styrelse"}),
        ("Styrelseledamot/suppleant", {"Styrelse"}),
        ("Verkställande direktör (VD)", {"VD"}),
        ("VD", {"VD"}),
        ("vd", {"VD"}),
        ("CEO", {"VD"}),
        ("President", {"VD"}),
        ("Styrelseordförande", {"Ordförande"}),
        ("styrelseorförande", {"Ordförande"}),
        ("Chairman of the Board", {"Ordförande"}),
        ("Ekonomichef/finanschef/finansdirektör", {"CFO"}),
        ("Ekonomi/finanschef", {"CFO"}),
        ("Chief Financial Officer", {"CFO"}),
        ("Vice VD", {"Vice VD"}),
        ("Annan ledande befattningshavare", {"Övrig ledning"}),
        ("Medlem i koncernledningen", {"Övrig ledning"}),
        ("COO", {"Övrig ledning"}),
        ("Arbetstagarrepresentant i styrelsen eller arbetstagarsuppleant", {"Styrelse"}),
        ("Board member/Board Deputy", {"Styrelse"}),
        ("Director", {"Styrelse"}),
        # several roles in one field
        ("Verkställande direktör (VD), Styrelseledamot", {"VD", "Styrelse"}),
        ("VD,Styrelseledamot/suppleant", {"VD", "Styrelse"}),
        ("Verkställande direktör (VD), Vice VD", {"VD", "Vice VD"}),
        ("Ordförande resp VD", {"Ordförande", "VD"}),
        # traps
        ("VD dotterbolag", {"Övrig ledning"}),            # not the issuer's CEO
        ("CEO of Omnicar Holding AB", {"Övrig ledning"}),
        ("Vice President", {"Övrig ledning"}),            # not a deputy CEO
        ("Vice styrelseordförande", {"Styrelse"}),        # not the chair
        ("Deputy CFO", {"Övrig ledning"}),                # not the CFO
        # FI's catch-all: its comma must not split it, and its "lednings-" must
        # not read as management
        ("Annan medlem i bolagets administrations-, lednings- eller kontrollorgan",
         {"Övrigt"}),
    ],
)
def test_groups(roles, text, groups):
    assert roles.groups(text) == groups


def test_unmatched_is_ovrigt_and_counted(roles):
    ovrigt = 1 << config.POSITION_GROUPS.index("Övrigt")
    assert roles.groups("Konsult") == set()
    assert roles.mask("Konsult") == ovrigt
    assert roles.mask(None) == ovrigt
    assert roles.unmatched == 2
    roles.mask("VD")
    assert roles.unmatched == 2


def test_mask_sets_one_bit_per_group(roles):
    g = config.POSITION_GROUPS
    assert roles.mask("VD, Styrelseledamot") == (1 << g.index("VD")) | (1 << g.index("Styrelse"))


def test_every_seed_group_is_a_known_group(db_conn):
    seeded = {r[0] for r in db_conn.execute("SELECT DISTINCT grp FROM position_group")}
    assert seeded <= set(config.POSITION_GROUPS)
