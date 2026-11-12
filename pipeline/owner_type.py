"""Type an assessor owner string: person, trust, entity, estate or government.

County tax rolls hold one free-text owner field. The words in it are the only
evidence of what kind of holder it is, so the order of the tests matters:

  government  public bodies are not acquisition targets
  estate      a decedent's estate or heirs (not a life estate, not REAL ESTATE,
              not a subdivision's owners' association)
  trust       family trusts, bank land trusts, trusts under will
  entity      LLCs, corporations, partnerships, co-ops, churches
  person      everything else

The care-of tail ("SMITH JOHN C/O EXAMPLE AG MGMT") is cut before typing, so
the farm manager does not turn a person into a company.
"""

from __future__ import annotations

import re

# Words that mark a company. Words that are also surnames or given names
# (RANCH, BROTHERS, COOP, LODGE) count only after another word, so a person
# whose surname is LODGE or COOP stays a person.
ENTITY_RE = re.compile(
    r"\b(LLC|L\.L\.C|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|LP|LLP|LLLP|DST|"
    r"LTD|LIMITED|PARTNERSHIP|PARTNERS|FARMS|FARM|DAIRY|"
    r"ENTERPRISES|HOLDINGS|PROPERTIES|PROPERTY|PROP|INVESTMENTS?|VENTURES|GROUP|"
    r"ASSN|ASSOC|ASSOCIATION|SOCIETY|INSTITUTE|FOUNDATION|CONSERVANCY|CLUB|"
    r"CHURCH|DIOCESE|MINISTRIES|CEMETERY|HOSPITAL|HOME|LABORATORIES|"
    r"BANK|SERVICES|MANAGEMENT|DEVELOPMENT|ENERGY|GENERATION|UTILITIES|"
    r"RAILROAD|RAILWAY|COOPERATIVE|CO-OP|ELEVATOR|PRESERVE|"
    r"REAL ESTATE|REAL EST)\b"
    r"|\S\s+(RANCH|BROTHERS|BROS)\b"
    r"|\b(GRAIN|RAIL|FARMERS|SERVICES?|SUPPLY|ELECTRIC|TELEPHONE|OIL|PRODUCERS)\s+COOP\b"
    r"|\bLODGE\s*(NO\b|#|\d|OF\b|HOME\b)"
    r"|\b(MASONIC|GRAND|MOOSE|ELKS|IOOF|ODDFELLOWS)\s+LODGE\b"
    r"|\b(EST|ESTATES?)\s+(POA|HOA)\b|\bHOA\b", re.I)

GOV_RE = re.compile(
    r"UNITED STATES|^USA\b|\bU S A\b|FOREST SERVICE|STATE OF IL|ILLINOIS STATE OF|"
    r"\b(IL|ILL|ILLINOIS)\.? (DEPT|DEPARTMENT)\b|DEPT OF CONSERVATION|"
    r"\bU\.? ?S\.? (GOVERNMENT|GOVT|FISH|ARMY|DEPT|DEPARTMENT)\b|"
    r"\bUS (GOVERNMENT|GOVT|FISH|ARMY|DEPT|DEPARTMENT)\b|NATIONAL FOREST|"
    r"\bSTATE (PARK|PK)\b|DEPT OF NATURAL|DEPARTMENT OF NATURAL|\bIDNR\b|"
    r"\bCOUNTY OF\b|COUNTY CLERK|^[A-Z.]+( [A-Z.]+)? COUNTY$|\bCOUNTY FOREST\b|"
    r"\bCITY OF\b|\bVILLAGE OF\b|TOWNSHIP|TOWN OF |"
    r"SCHOOL DISTRICT|PARK DISTRICT|\bPARK DIST\b|SANITARY DIST|DRAINAGE DISTRICT|"
    r"CONSERVANCY DIST|CONSERVATION DIST|LEVEE DIST|WATER DEPT|"
    r"HOUSING AUTHORITY|AIRPORT AUTH|ARMY CORPS|CORPS OF ENGINEERS|"
    r"DEPT OF TRANSPORTATION|DEPARTMENT OF TRANSPORTATION|\bIDOT\b|"
    r"FISH AND WILDLIFE|FISH & WILDLIFE|FOREST PRESERVE|BOARD OF EDUCATION|"
    r"\bCOLLEGE\b|UNIVERSITY|TRUSTEES OF SCHOOLS|STATE HIGHWAY", re.I)

# Trust markers, including the abbreviations assessors use: TTEE, TRS, TR,
# TUW / T/U/W (trust under will), QTIP.
TRUST_RE = re.compile(
    r"\bTRUST|UNITRUST|COTRUST|TTEE|\bTR\b|\bTRS\b|\bTRSTE|\bTRST\b|\bTUW\b|T/U/W|\bQTIP\b", re.I)

# ESTATE means a decedent's estate, except after REAL or LIFE, and except a
# subdivision name followed by POA / HOA / owners' association.
_NOT_DECEDENT_BEFORE = r"(?<!REAL )(?<!REAL-)(?<!LIFE )(?<!LIFE-)(?<!LIF )(?<!LF )(?<!LF-)"
_NOT_HOA_AFTER = (r"(?!\s*(POA|HOA|PROP(ERTY)? OWNERS|COMM(UNITY)? ASSOC|"
                  r"HOME(OWNERS)? ASSOC|ASSOC|TOWNHOMES)\b)")
ESTATE_RE = re.compile(
    _NOT_DECEDENT_BEFORE + r"\bESTATES?\b" + _NOT_HOA_AFTER + r"|HEIRS|"
    + _NOT_DECEDENT_BEFORE + r"\bEST\b" + _NOT_HOA_AFTER, re.I)

# A registered-entity suffix beats the word EST/ESTATE: "EXAMPLE EST LLC" is a
# company, not a decedent's estate, and keeps its registry people.
HARD_ENTITY_RE = re.compile(
    r"\b(LLC|L\.L\.C|INC|INCORPORATED|CORP|CORPORATION|LP|LLP|LLLP|LTD|LIMITED|"
    r"DST|PARTNERSHIP|ASSN|ASSOC|ASSOCIATION)\b", re.I)

# Care-of tail: C/O, C/0, ATTN, or a % that is not a share ("85% INT").
CARE_TAIL = re.compile(r"\s*(?:\bC/O\b|\bC/0\b|(?<!\d)%|\bATTN\b|\bATTENTION\b).*$", re.I)


def owner_type(name: str) -> str:
    if not name:
        return "unknown"
    n = name.upper()
    head = CARE_TAIL.sub("", n).strip(" ,;&-")
    if head:            # "C/O SMITH JOHN" alone: keep the whole string
        n = head
    if GOV_RE.search(n):
        return "government"
    if ESTATE_RE.search(n) and not HARD_ENTITY_RE.search(n):
        return "estate"
    if TRUST_RE.search(n):
        return "trust"
    if ENTITY_RE.search(n):
        return "entity"
    return "person"


def norm_owner(name: str) -> str:
    """Light normalization for grouping: case, punctuation, whitespace, ET AL."""
    n = (name or "").upper()
    n = re.sub(r"[.,]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"\s+(ETAL|ET AL)$", "", n)
    return n
