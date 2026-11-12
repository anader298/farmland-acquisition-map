#!/usr/bin/env python3
"""Collapse owner-name variants into one owner per real holder.

Grouping owners by normalized string splits one holder across every way an
assessor typed the name: "EXAMPLE FARMS LLC", "EXAMPLE FARMS L.L.C." and
"EXAMPLE FARMS LLC C/O SAMPLE AG MGMT" are one company reported as three.

Two levels come out, and they mean different things:

  owner_id     one legal holder: name variants of the same person, trust or
               company. A wrong merge here is worse than a miss.
  owner_group  a family or control group: legally distinct owners that share
               a mailing address and a surname, or an LLC and the people the
               state registry names as its managers. The map can say "this
               family holds X acres through 3 entities" without pretending
               the three are one owner.

owner_id passes, in order:

  1. Canonical core, exact. Care-of tails, trust dates, undivided-interest
     fractions and legal suffixes are stripped; equal cores merge. Trust
     numbers stay in a digit signature that must match (TRUST #351 never
     joins #352) and two different trust dates never merge.
  2. Word order. "BRANNOCK JOHN A" = "JOHN A BRANNOCK": each name word keeps
     the initials after it, and the sorted groups must be equal.
  3. Fuzzy. Jaro-Winkler >= 0.92 only proposes a pair; token_guard decides:
     every word pairs with an equal word, a typo or an abbreviation, an
     initial pairs only within the same holder (never the spouse after "&"),
     and at most one lone initial is left over.

Short cores, word-order and fuzzy merges also need evidence of place: a
shared county, mailing address or registry file number. Disjoint registry
file numbers always refuse a merge, and a company never merges with a trust
(they can share an owner_group).

Every merge and group link is written to a log with its reason, and the raw
owner string is never overwritten, so any merge can be audited or undone.
"""

from __future__ import annotations

import re
from collections import defaultdict

CARE = re.compile(r"\s*(?:\bC/O\b|\bC/0\b|(?<!\d)%|\bATTN\b:?|\bATTENTION\b:?)\s*")
DATE_NUM = re.compile(r"(?:\b(?:DTD|DATED|U/?A|UAD|UTD)\s*:?\s*)?\b(\d{1,2})[/-](\d{1,2})[/-](\d{4}|\d{2})\b")
DATE_WORD = re.compile(r"\b(?:DTD|DATED|DATE)\b")
FRACTION = re.compile(
    r"\(?\b(?:UNDIV(?:IDED)?|UND)\.?\s*\d+\s*/\s*\d+\s*(?:INT(?:EREST)?\b)?\)?"
    r"|\(?\b\d+\s*/\s*\d+\s*(?:UNDIV(?:IDED)?\s*)?INT(?:EREST)?\b\)?"
    r"|\(?\b\d+(?:\.\d+)?\s*%\s*(?:UNDIV(?:IDED)?\s*)?(?:INT(?:EREST)?\b)?\)?")
SUFFIX = re.compile(
    r"\b(LIMITED\s+PARTNERSHIP|GENERAL\s+PARTNERSHIP|L\s*L\s*C|L\s*L\s*P|L\s*P|LLC|LLP|LP|"
    r"INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|LTD|LIMITED|PARTNERSHIP|PARTNERS|"
    r"TRUSTEES|TRUSTEE|TRUSTS|TRUST|TRSTE|TRST|TRS|TR|TTEES|TTEE|TUW|"
    r"ET\s*AL|ETAL|ET\s*UX|ETUX|ESTATE|DECEASED|DECD|LIFE\s+ESTATE|AND)\b")
EQUIV = [(re.compile(r"\bFARMS\b"), "FARM"), (re.compile(r"\bBROTHERS\b|\bBROS\b"), "BROTHER"),
         (re.compile(r"\bPROPERTIES\b"), "PROPERTY"), (re.compile(r"\bHOLDINGS\b"), "HOLDING")]
TRUST_ONLY = re.compile(r"\b(REVOCABLE|REVOC|REV|IRREVOCABLE|IRREV|LIVING|LVG|FAMILY|FAM|"
                        r"DECLARATION|DECL|AGREEMENT|AGMT|UTA|UAD|AS)\b")
IS_TRUST = re.compile(r"\b(TRUST|TRUSTS|TRUSTEE|TRUSTEES|TRST|TR|TRS|TTEE|TTEES|TUW)\b")
COMPANY = re.compile(r"\b(LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|LP|LLP|LTD|"
                     r"PARTNERSHIP|PARTNERS|COMPANY)\b")
DIGITS = re.compile(r"\d+")
GEN = {"JR", "SR", "II", "III", "IV"}

JW_PROPOSE = 0.92


# ------------------------------------------------------------------ parsing
def parse_name(name: str) -> dict:
    """-> owner (holder text), core (comparison key), dig (number signature),
    date (trust date or ''), care (care-of contact or '')."""
    n = re.sub(r"\s+", " ", (name or "").upper()).strip()
    parts = CARE.split(n, maxsplit=1)
    owner, care = parts[0].strip(" -,"), (parts[1].strip(" -,") if len(parts) > 1 else "")
    if not owner:
        owner, care = care, ""
    date = ""
    m = DATE_NUM.search(owner)
    if m and 1 <= int(m.group(1)) <= 12:
        date = f"{int(m.group(1))}/{int(m.group(2))}/{m.group(3)[-2:]}"
        owner = owner[:m.start()] + " " + owner[m.end():]
    owner = FRACTION.sub(" ", DATE_WORD.sub(" ", owner))
    owner = re.sub(r"\s+", " ", owner).strip(" -,")
    return {"owner": owner, "core": _core(owner), "dig": ",".join(DIGITS.findall(owner)),
            "date": date, "care": care}


def _core(n: str) -> str:
    # "&", "AND" and "/" all join two holders; kept as one token "&" so an
    # initial can never pair with a spouse's name.
    n = re.sub(r"\s*(?:&|/|\bAND\b)\s*", " & ", n)
    n = re.sub(r"[^A-Z0-9& ]", " ", n)
    for pat, rep in EQUIV:
        n = pat.sub(rep, n)
    if IS_TRUST.search(n):
        n = TRUST_ONLY.sub(" ", n)
    n = re.sub(r"^THE\s+", "", n)
    n = SUFFIX.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"(?:^|\s)&(?=\s&|$)", "", n)
    return re.sub(r"^&\s*", "", n).strip()


def form(name: str) -> str:
    n = CARE.split(re.sub(r"\s+", " ", (name or "").upper()), maxsplit=1)[0]
    n = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", n))       # L.L.C. -> L L C
    if COMPANY.search(n):
        return "company"
    if IS_TRUST.search(re.sub(r"[^A-Z0-9 ]", " ", n)):
        return "trust"
    return "other"


def order_key(c: str) -> str:
    """Word-order-free key: each name word keeps the initials that follow it.
    "BRANNOCK JOHN A" and "JOHN A BRANNOCK" -> "BRANNOCK | JOHN A" both."""
    groups: list[list[str]] = []
    for t in (x for x in c.split() if x != "&"):
        if len(t) == 1 and groups and t not in GEN:
            groups[-1].append(t)
        else:
            groups.append([t])
    return " | ".join(sorted(" ".join(g) for g in groups))


def jaro_winkler(a: str, b: str) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    rng = max(max(la, lb) // 2 - 1, 0)
    fa, fb = [False] * la, [False] * lb
    m = 0
    for i, ch in enumerate(a):
        for j in range(max(0, i - rng), min(lb, i + rng + 1)):
            if not fb[j] and b[j] == ch:
                fa[i] = fb[j] = True
                m += 1
                break
    if not m:
        return 0.0
    t = k = 0
    for i in range(la):
        if fa[i]:
            while not fb[k]:
                k += 1
            t += a[i] != b[k]
            k += 1
    j = (m / la + m / lb + (m - t / 2) / m) / 3
    p = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        p += 1
    return j + p * 0.1 * (1 - j)


def _tok_match(x: str, y: str) -> bool:
    if x == y:
        return True
    if len(x) == 1 or len(y) == 1:                  # J matches JOHN; E never matches K
        return x[0] == y[0] and max(len(x), len(y)) > 1
    if min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x)):
        return True
    return min(len(x), len(y)) >= 4 and jaro_winkler(x, y) >= 0.9


def token_guard(a: str, b: str) -> bool:
    """True only if two cores are the same words up to typos and one initial."""
    def seg(c):
        out, k = [], 0
        for t in c.split():
            if t == "&":
                k += 1
            else:
                out.append((t, k))
        return out
    ta, left = seg(a), seg(b)
    unmatched = []
    for x, sx in ta:
        hit = None
        for test in (lambda y, sy: x == y and sx == sy,
                     lambda y, sy: _tok_match(x, y) and sx == sy,
                     lambda y, sy: x == y,
                     lambda y, sy: _tok_match(x, y) and min(len(x), len(y)) > 1):
            hit = next((i for i, (y, sy) in enumerate(left) if test(y, sy)), None)
            if hit is not None:
                break
        if hit is None:
            unmatched.append(x)
        else:
            left.pop(hit)
    extra = unmatched + [y for y, _ in left]
    return not extra or (len(extra) == 1 and len(extra[0]) == 1 and extra[0].isalpha())


# ------------------------------------------------------------------ resolution
class UnionFind:
    def __init__(self):
        self.p: dict = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def resolve(owners: list[dict], llc_people: dict[str, list[str]] | None = None) -> tuple[dict, dict, list]:
    """owners: one dict per distinct raw owner string with keys
         name, counties (set), mails (set of 'ADDR|ZIP'), files (set of registry file numbers)
    llc_people: raw LLC name -> registry people names.
    -> (owner_id by name, owner_group by name, merge log rows)."""
    llc_people = llc_people or {}
    recs = []
    for i, o in enumerate(sorted(owners, key=lambda o: o["name"])):
        pn = parse_name(o["name"])
        recs.append({**o, **pn, "i": i, "form": form(o["name"]), "ok": order_key(pn["core"])})
    uf = UnionFind()
    log: list[dict] = []

    def place(a, b) -> bool:
        return bool(a["counties"] & b["counties"] or a["mails"] & b["mails"] or a["files"] & b["files"])

    forms = {r["i"]: {r["form"]} for r in recs}          # forms held by each cluster root

    def refuse(a, b) -> str | None:
        # checked on the whole clusters, so A~B and B~C cannot chain a company to a trust
        if {"company", "trust"} <= forms[uf.find(a["i"])] | forms[uf.find(b["i"])]:
            return "company vs trust"
        if a["dig"] != b["dig"]:
            return "different numbers"
        if a["date"] and b["date"] and a["date"] != b["date"]:
            return "different trust dates"
        if a["files"] and b["files"] and not a["files"] & b["files"]:
            return "different registry file numbers"
        if a["mails"] and b["mails"] and not a["mails"] & b["mails"] and not a["files"] & b["files"]:
            return "different mailing addresses"
        return None

    def try_merge(a, b, why, need_place):
        if uf.find(a["i"]) == uf.find(b["i"]):
            return
        r = refuse(a, b)
        if r is None and need_place and not place(a, b):
            r = "no shared county, address or registry file"
        if r is None:
            merged = forms[uf.find(a["i"])] | forms[uf.find(b["i"])]
            uf.union(a["i"], b["i"])
            forms[uf.find(a["i"])] = merged
        log.append({"a": a["name"], "b": b["name"], "pass": why,
                    "decision": "merge" if r is None else "refuse", "reason": r or ""})

    for key, why in (("core", "exact core"), ("ok", "word order")):
        by = defaultdict(list)
        for r in recs:
            if r["core"]:
                by[r[key]].append(r)
        for group in by.values():
            short = len([t for t in group[0]["core"].split() if t != "&"]) < 3
            for j, b in enumerate(group[1:], 1):
                # Try every earlier member: one refusal (a trust sharing the
                # core) must not block the company variants behind it.
                for a in group[:j]:
                    try_merge(a, b, why, need_place=(key == "ok" or short))
                    if uf.find(a["i"]) == uf.find(b["i"]):
                        break

    blocks = defaultdict(list)
    for r in recs:
        t = r["core"].split()
        if t:
            blocks[t[0][:1] + t[0][-3:]].append(r)
    for group in blocks.values():
        for x in range(len(group)):
            for y in range(x + 1, len(group)):
                a, b = group[x], group[y]
                if a["core"] != b["core"] and jaro_winkler(a["core"], b["core"]) >= JW_PROPOSE \
                        and token_guard(a["core"], b["core"]):
                    try_merge(a, b, "fuzzy", need_place=True)

    owner_id = {r["name"]: uf.find(r["i"]) for r in recs}

    # owner_group: shared mailing address + surname, or LLC -> its registry people.
    g = UnionFind()
    by_mail = defaultdict(list)
    for r in recs:
        g.find(owner_id[r["name"]])
        sur = r["core"].split()[0] if r["core"] else ""
        for m in r["mails"]:
            by_mail[(m, sur)].append(r)
    for (m, sur), group in by_mail.items():
        for b in group[1:]:
            if owner_id[group[0]["name"]] != owner_id[b["name"]]:
                g.union(owner_id[group[0]["name"]], owner_id[b["name"]])
                log.append({"a": group[0]["name"], "b": b["name"], "pass": "group",
                            "decision": "group", "reason": "shared mailing address and surname"})
    by_person = defaultdict(list)
    for r in recs:
        if r["form"] == "other":
            by_person[r["ok"]].append(r)
    for llc, people in llc_people.items():
        if llc not in owner_id:
            continue
        for person in people:
            for r in by_person.get(order_key(_core(person.upper())), []):
                g.union(owner_id[llc], owner_id[r["name"]])
                log.append({"a": llc, "b": r["name"], "pass": "group", "decision": "group",
                            "reason": "registry names this owner as LLC manager"})
    owner_group = {n: g.find(oi) for n, oi in owner_id.items()}
    return owner_id, owner_group, log
