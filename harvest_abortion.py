#!/usr/bin/env python3
"""
harvest_abortion.py — the abortion wire: laws, courts, access, medicine,
enforcement and outcomes, worldwide.

Self-contained: fetching, feed parsing, word-edge matching and deduplication are
all in this file. Reads sources_abortion.json, writes wire_abortion.json.
Standard library only — no dependencies, no API keys, no model calls.

This is a contested subject, so the harvester is built to show the argument
rather than to settle it. Wires from both advocacy movements are carried
deliberately and labelled on every row — abortion-rights and anti-abortion sit
alongside courts, health institutions, research and general press. Standing is
provenance, not endorsement, and the reader always knows which they are reading.

Separately, each story carries a weight built from what it contains: a ruling or
enacted law, official data or guidance, a measured figure, a pending decision, a
named jurisdiction. A campaign statement and a constitutional judgment both
appear, and the page never lets you mistake one for the other.

    python3 harvest_abortion.py
    python3 harvest_abortion.py --dry-run
    python3 harvest_abortion.py --fixtures DIR
"""

import argparse
import gzip
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(HERE, "sources_abortion.json")
OUT_PATH = os.path.join(HERE, "wire_abortion.json")

RETAIN_DAYS = 45
MAX_ITEMS = 1200
WORKERS = 6
NOTABLE_SCORE = 3       # at or above this a story is marked as consequential

# --------------------------------------------------------------------------
# Plumbing: fetching, feed parsing, word-edge matching, fingerprints.
# --------------------------------------------------------------------------
USER_AGENT = ("Mozilla/5.0 (compatible; space-life-news/1.0; "
              "+https://github.com/WelcomeToYourGalaxy/space-life-news)")

TIMEOUT = 25

SNIPPET_CHARS = 240

TAG_RE = re.compile(r"<[^>]+>")

WS_RE = re.compile(r"\s+")

PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

def build_gnews_url(loc):
    q = loc["query"] + " when:30d"
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q) +
            "&hl=" + loc["hl"] + "&gl=" + loc["gl"] + "&ceid=" + loc["ceid"])

def fetch(url, tries=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
                "Accept-Encoding": "gzip",
                "Accept-Language": "*",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw
        except Exception as exc:                       # noqa: BLE001 — report, don't crash the run
            last = exc
            time.sleep(1.5 * (attempt + 1))
    print("  ! unreachable: %s (%s)" % (url[:90], last), file=sys.stderr)
    return None

def strip_ns(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag

def text_of(el):
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", el.text or ""))).strip() if el is not None else ""

def child(node, *names):
    for kid in node:
        if strip_ns(kid.tag) in names:
            return kid
    return None

def parse_date(raw):
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None

def parse_feed(raw, src):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # Some publishers serve a stray byte before the declaration.
        try:
            root = ET.fromstring(raw[raw.index(b"<"):])
        except Exception:  # noqa: BLE001
            return []

    nodes = [n for n in root.iter() if strip_ns(n.tag) == "item"]
    atom = False
    if not nodes:
        nodes = [n for n in root.iter() if strip_ns(n.tag) == "entry"]
        atom = True

    out = []
    for n in nodes:
        title = text_of(child(n, "title"))
        if atom:
            link = ""
            for kid in n:
                if strip_ns(kid.tag) == "link" and kid.get("rel", "alternate") == "alternate":
                    link = kid.get("href", "")
                    break
        else:
            link_el = child(n, "link")
            link = (link_el.text or "").strip() if link_el is not None else ""
            if not link:
                link = text_of(child(n, "guid"))
        if not title or not link:
            continue

        outlet_el = child(n, "source")
        outlet = text_of(outlet_el) if outlet_el is not None else ""
        if outlet and title.endswith(" - " + outlet):
            title = title[: -(len(outlet) + 3)].strip()
        elif not outlet and src["name"].startswith("Google News") and " - " in title:
            # Google News appends the outlet to the headline when it omits <source>.
            head, _, tail = title.rpartition(" - ")
            if head and 2 <= len(tail) <= 45:
                title, outlet = head.strip(), tail.strip()

        stamp = parse_date(text_of(child(n, "pubDate", "published", "updated", "date")))
        snippet = text_of(child(n, "description", "summary", "content"))[:SNIPPET_CHARS]

        out.append({
            "t": title,
            "u": link,
            "o": outlet or src["name"].replace("Google News · ", ""),
            "g": src["lang"],
            "r": src["region"],
            "k": src.get("kind", "news"),
            "d": stamp,
            "s": snippet,
            "w": src["name"],
        })
    return out

def _compile(term):
    if any(ord(ch) > 0x24F for ch in term):        # non-Latin script
        # substring matching is already prefix-like in scripts without word
        # breaks, so a trailing * is a no-op — strip it rather than search for
        # a literal asterisk, which is what used to happen.
        return term[:-1] if term.endswith("*") else term
    if term.endswith("*"):
        return re.compile(r"(?<![a-z0-9])" + re.escape(term[:-1]) + r"[a-z0-9\-]*", re.I)
    return re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", re.I)

def _compile_all(terms):
    return [_compile(t) for t in terms]

def hit(text, compiled):
    """True when any compiled term matches."""
    for c in compiled:
        if isinstance(c, str):
            if c in text:
                return True
        elif c.search(text):
            return True
    return False

def fingerprint(title):
    norm = PUNCT_RE.sub(" ", title.lower())
    return " ".join(WS_RE.sub(" ", norm).strip().split()[:9])

def canon_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parts.query)
        query = [(k, v) for k, v in query if not k.lower().startswith("utm_")]
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"),
                                        urllib.parse.urlencode(query), ""))
    except Exception:  # noqa: BLE001
        return url


# --------------------------------------------------------------------------
# Where the story is.  This is the region the finding concerns, not the region
# the wire was read from — a Japanese outlet reporting on the Amazon files
# under Latin America.  A story with global scope files under Global, and one
# can carry several: a study spanning Africa and South Asia files under both.
# --------------------------------------------------------------------------
GEO = [
    ("africa", "Africa", [
        ("africa*", None), ("sahel", None), ("congo basin", None), ("nigeria*", None),
        ("kenya*", None), ("ethiopia*", None), ("democratic republic of congo", None),
        ("drc", None), ("ghana", None), ("tanzania*", None), ("uganda*", None),
        ("south africa*", None), ("zimbabwe*", None), ("zambia*", None), ("mozambique", None),
        ("angola*", None), ("senegal", None), ("mali", ["africa", "sahel", "bamako", "drought"]),
        ("chad", ["lake", "africa", "sahel", "basin"]), ("sudan*", None), ("somalia*", None),
        ("madagascar", None), ("cameroon", None), ("côte d'ivoire", None), ("ivory coast", None),
        ("botswana", None), ("namibia", None), ("malawi", None), ("rwanda", None),
        ("okavango", None), ("lake victoria", None), ("serengeti", None), ("kalahari", None),
        ("horn of africa", None), ("afrique", None), ("áfrica", None), ("afrika", None),
        ("非洲", None), ("アフリカ", None), ("африк*", None), ("أفريقيا", None), ("अफ्रीका", None),
    ]),
    ("mena", "Middle East & North Africa", [
        ("middle east*", None), ("egypt*", None), ("morocco", None), ("algeria*", None),
        ("tunisia*", None), ("libya*", None), ("saudi arabia", None), ("emirates", None),
        ("qatar", None), ("kuwait", None), ("oman", None), ("yemen*", None), ("iraq*", None),
        ("iran*", None), ("israel*", None), ("palestin*", None), ("gaza", None), ("jordan", None),
        ("lebanon", None), ("syria*", None), ("turkey", ["drought", "climate", "pollution", "earthquake", "istanbul", "anatolia"]),
        ("türkiye", None), ("persian gulf", None), ("red sea", None), ("euphrates", None),
        ("tigris", None), ("dead sea", None), ("sahara", None), ("الشرق الأوسط", None),
        ("中东", None), ("北アフリカ", None),
    ]),
    ("asia", "Asia", [
        ("asia*", None), ("china", None), ("chinese", ["government", "province", "coal", "emissions", "cities"]),
        ("japan*", None), ("korea*", None), ("india", None), ("indian", ["ocean", "government", "farmers", "cities", "monsoon", "state"]),
        ("pakistan*", None), ("bangladesh*", None), ("nepal*", None), ("sri lanka", None),
        ("indonesia*", None), ("vietnam*", None), ("thailand", None), ("philippines", None),
        ("malaysia*", None), ("myanmar", None), ("cambodia*", None), ("laos", None),
        ("mongolia*", None), ("kazakhstan", None), ("uzbekistan", None), ("central asia", None),
        ("himalaya*", None), ("mekong", None), ("ganges", None), ("yangtze", None),
        ("brahmaputra", None), ("tibet*", None), ("borneo", None), ("sumatra", None),
        ("aral sea", None), ("gobi", None), ("siberia*", None), ("アジア", None), ("亚洲", None),
        ("아시아", None), ("एशिया", None), ("азия", None),
    ]),
    ("europe", "Europe", [
        ("europe*", ["union", "countries", "climate", "commission", "continent", "wide", "study", "across"]),
        ("european union", None), ("european commission", None), ("brussels", None),
        ("eu", ["deforestation", "regulation", "law", "directive", "commission", "member states",
                "emissions", "green deal", "farm", "policy", "ban", "target"]),
        ("united kingdom", None), ("britain", None), ("england", None),
        ("scotland", None), ("wales", ["climate", "flood", "farm", "coast"]), ("ireland", None),
        ("france", None), ("germany", None), ("spain", None), ("portugal", None), ("italy", None),
        ("greece", None), ("netherlands", None), ("belgium", None), ("poland", None),
        ("ukraine", None), ("russia*", None), ("sweden", None), ("norway", None), ("finland", None),
        ("denmark", None), ("switzerland", None), ("austria", None), ("romania", None),
        ("hungary", None), ("czech*", None), ("balkans", None), ("danube", None), ("alps", None),
        ("mediterranean", None), ("baltic", None), ("北欧", None), ("欧洲", None), ("ヨーロッパ", None),
        ("유럽", None), ("европ*", None), ("أوروبا", None),
    ]),
    ("latam", "Latin America & Caribbean", [
        ("latin america*", None), ("south america*", None), ("central america*", None),
        ("brazil*", None), ("brasil", None), ("amazon", None), ("amazônia", None), ("amazonía", None),
        ("argentina", None), ("chile", None), ("peru", None), ("colombia*", None),
        ("venezuela*", None), ("ecuador", None), ("bolivia*", None), ("paraguay", None),
        ("uruguay", None), ("mexico", None), ("méxico", None), ("guatemala", None),
        ("honduras", None), ("nicaragua", None), ("costa rica", None), ("panama", None),
        ("cuba", None), ("haiti", None), ("dominican republic", None), ("caribbean", None),
        ("patagonia", None), ("andes", None), ("cerrado", None), ("pantanal", None),
        ("gran chaco", None), ("orinoco", None), ("américa latina", None), ("拉丁美洲", None),
        ("ラテンアメリカ", None), ("латинская америка", None),
    ]),
    ("northam", "North America", [
        ("united states", None), ("u.s.", None), ("usa", None), ("american", ["government", "cities", "states", "west", "farmers", "midwest", "coast"]),
        ("canada", None), ("canadian", None), ("alaska*", None), ("california", None),
        ("texas", None), ("florida", None), ("great lakes", None), ("colorado river", None),
        ("mississippi", None), ("appalachia*", None), ("quebec", None), ("ontario", None),
        ("british columbia", None), ("gulf of mexico", None), ("états-unis", None),
        ("estados unidos", None), ("美国", None), ("加拿大", None), ("アメリカ合衆国", None),
        ("미국", None), ("сша", None),
    ]),
    ("oceania", "Oceania", [
        ("australia*", None), ("new zealand", None), ("aotearoa", None), ("papua", None),
        ("pacific island*", None), ("fiji", None), ("samoa", None), ("tonga", None),
        ("vanuatu", None), ("solomon islands", None), ("kiribati", None), ("tuvalu", None),
        ("great barrier reef", None), ("tasmania*", None), ("murray-darling", None),
        ("オセアニア", None), ("大洋洲", None), ("océanie", None),
    ]),
    ("polar", "Arctic & Antarctic", [
        ("arctic", None), ("antarctic*", None), ("greenland", None), ("svalbard", None),
        ("north pole", None), ("south pole", None), ("tundra", None), ("北極", None),
        ("南極", None), ("арктик*", None), ("antártic*", None), ("arctique", None),
    ]),
    ("ocean", "Oceans & high seas", [
        ("pacific ocean", None), ("atlantic ocean", None), ("indian ocean", None),
        ("southern ocean", None), ("high seas", None), ("open ocean", None),
        ("coral triangle", None), ("mariana", None), ("deep sea", None), ("north sea", None),
        ("bering sea", None), ("south china sea", None), ("océan pacifique", None),
        ("公海", None), ("深海", None),
    ]),
]


# --------------------------------------------------------------------------
# Subjects
# --------------------------------------------------------------------------
TOPICS = [
    ("courts", "Courts & rulings", [
        ("supreme court", ["abortion", "reproductive", "pregnancy"]),
        ("constitutional court", ["abortion", "pregnancy"]), ("appeals court", ["abortion"]),
        ("ruling", ["abortion", "reproductive", "pregnancy termination"]),
        ("struck down", ["abortion", "law", "ban"]), ("upheld", ["abortion", "ban", "law"]),
        ("injunction", ["abortion", "clinic", "law"]), ("lawsuit", ["abortion", "clinic", "provider"]),
        ("european court of human rights", None), ("inter-american court", None),
        ("dobbs", None), ("roe v. wade", None), ("roe v wade", None),
        ("sentencia", ["aborto"]), ("tribunal", ["aborto", "avortement"]),
        ("cour", ["avortement", "ivg"]), ("verfassungsgericht", ["abtreibung", "schwangerschaftsabbruch"]),
        ("суд", ["аборт"]), ("最高法院", ["堕胎", "墮胎"]), ("헌법재판소", ["낙태"]),
    ]),
    ("legislation", "Legislation & referendums", [
        ("bill", ["abortion", "reproductive", "pregnancy"]), ("legislation", ["abortion", "reproductive"]),
        ("parliament", ["abortion", "pregnancy termination", "reproductive"]),
        ("referendum", ["abortion", "aborto", "avortement"]), ("ballot measure", ["abortion"]),
        ("decriminalis*", ["abortion", "aborto"]), ("decriminaliz*", ["abortion"]),
        ("legaliz*", ["abortion", "aborto"]), ("gestational limit", None), ("week ban", None),
        ("heartbeat law", None), ("trigger law", ["abortion"]), ("constitutional amendment", ["abortion"]),
        ("ley del aborto", None), ("despenalización", ["aborto"]), ("loi", ["avortement", "ivg"]),
        ("gesetz", ["abtreibung", "schwangerschaftsabbruch"]), ("legge 194", None),
        ("ustawa", ["aborcyjn", "aborcja"]), ("законопроект", ["аборт"]),
    ]),
    ("access", "Access & services", [
        ("clinic", ["abortion", "closed", "opened", "reproductive"]),
        ("provider*", ["abortion", "reproductive"]), ("travel", ["abortion", "state lines", "cross-border"]),
        ("waiting period", None), ("abortion fund*", None), ("appointment*", ["abortion", "clinic"]),
        ("shortage", ["provider", "clinic", "obstetric"]), ("maternity ward", ["closed", "shortage"]),
        ("conscientious objection", None), ("obiezione di coscienza", None),
        ("buffer zone*", None), ("safe access zone*", None), ("crisis pregnancy cent*", None),
        ("acceso al aborto", None), ("accès à l'avortement", None), ("dostęp do aborcji", None),
    ]),
    ("medication", "Medication & telehealth", [
        ("mifepristone", None), ("misoprostol", None), ("abortion pill*", None),
        ("medication abortion", None), ("medical abortion", None), ("telehealth", ["abortion", "pills"]),
        ("telemedicine", ["abortion", "pills"]), ("mail", ["abortion pills", "mifepristone"]),
        ("fda", ["mifepristone", "abortion"]), ("ema", ["abortion", "mifepristone"]),
        ("pastilla abortiva", None), ("píldora abortiva", None), ("pilule abortive", None),
        ("abtreibungspille", None), ("経口中絶薬", None), ("낙태약", None), ("堕胎药", None),
    ]),
    ("enforcement", "Criminalisation & enforcement", [
        ("prosecut*", ["abortion", "miscarriage", "pregnancy"]),
        ("charged", ["abortion", "miscarriage", "pregnancy"]),
        ("jailed", ["abortion", "miscarriage"]), ("sentenced", ["abortion", "miscarriage"]),
        ("arrest*", ["abortion", "clinic", "pills"]), ("criminal penalt*", ["abortion"]),
        ("investigation", ["abortion", "miscarriage", "clinic"]),
        ("surveillance", ["abortion", "clinic", "pregnancy"]),
        ("bounty law", None), ("civil suit", ["abortion", "aiding"]),
        ("procesada", ["aborto"]), ("condenada", ["aborto"]), ("осуждена", ["аборт"]),
    ]),
    ("health", "Health outcomes & data", [
        ("maternal mortality", None), ("maternal death*", None), ("unsafe abortion", None),
        ("sepsis", ["pregnancy", "miscarriage", "abortion"]),
        ("miscarriage care", None), ("ectopic pregnancy", None), ("obstetric care", None),
        ("abortion rate*", None), ("study", ["abortion", "pregnancy termination"]),
        ("data show*", ["abortion", "births", "pregnanc"]), ("statistics", ["abortion", "pregnanc"]),
        ("mortalidad materna", None), ("mortalité maternelle", None), ("müttersterblichkeit", None),
        ("aborto inseguro", None), ("母体死亡", None),
    ]),
    ("international", "International bodies & aid", [
        ("world health organization", None), ("world health organisation", None), ("who guidance", None),
        ("united nations", ["abortion", "reproductive"]), ("cedaw", None), ("unfpa", None),
        ("global gag rule", None), ("mexico city policy", None), ("foreign aid", ["reproductive", "abortion", "family planning"]),
        ("human rights council", ["abortion", "reproductive"]), ("treaty body", ["abortion"]),
        ("geneva consensus declaration", None), ("funding cut*", ["reproductive", "family planning", "abortion"]),
    ]),
    ("movements", "Movements & protest", [
        ("march for life", None), ("abortion rights march", None), ("protest*", ["abortion", "clinic", "reproductive"]),
        ("campaign", ["abortion", "pro-life", "abortion rights"]), ("petition", ["abortion"]),
        ("activist*", ["abortion", "reproductive", "pro-life"]),
        ("pro-life", None), ("anti-abortion", None), ("abortion-rights", None),
        ("manifestación", ["aborto"]), ("manifestation", ["avortement", "ivg"]),
        ("pañuelo verde", None), ("marea verde", None),
    ]),
    ("privacy", "Data & privacy", [
        ("period tracking app*", None), ("menstrual app*", None), ("location data", ["clinic", "abortion"]),
        ("data privacy", ["abortion", "reproductive", "health"]),
        ("subpoena", ["records", "abortion", "clinic"]), ("shield law*", None),
        ("search warrant", ["abortion", "clinic", "records"]),
        ("digital surveillance", ["abortion", "reproductive"]),
    ]),
    ("crisis", "Conflict & crisis settings", [
        ("refugee*", ["abortion", "reproductive", "maternal"]),
        ("humanitarian", ["abortion", "reproductive", "maternal"]),
        ("conflict zone", ["abortion", "reproductive", "maternal"]),
        ("displaced", ["abortion", "reproductive", "maternal"]),
        ("wartime", ["rape", "pregnancy", "abortion"]),
        ("famine", ["maternal", "pregnan"]), ("camp", ["reproductive health", "maternal care"]),
    ]),
]

# --------------------------------------------------------------------------
# The gate.
#
# ANCHOR — the story is about abortion, its law, its provision or its
#          consequences, in any of the feed's languages.
# BLOCK  — "abort" in its aviation, computing and mission senses, plus the
#          usual commercial and horoscope noise.
# --------------------------------------------------------------------------
ANCHOR = [
    "abortion", "abortions", "abortive", "anti-abortion", "abortion-rights", "pro-choice",
    "pro-life", "reproductive rights", "reproductive health", "reproductive justice",
    "termination of pregnancy", "pregnancy termination", "medication abortion",
    "medical abortion", "mifepristone", "misoprostol", "abortion pill", "abortion pills",
    "roe v. wade", "roe v wade", "dobbs", "unsafe abortion", "maternal mortality",
    "march for life", "marcha por la vida", "marche pour la vie", "marsch für das leben",
    "crisis pregnancy cent*", "planned parenthood", "heartbeat law", "fetal personhood",
    "aborto", "abortos", "abortista", "interrupción del embarazo", "interrupción voluntaria",
    "despenalización del aborto", "avortement", "ivg", "interruption volontaire de grossesse",
    "abtreibung", "schwangerschaftsabbruch", "paragraf 218", "paragraph 218",
    "interruzione di gravidanza", "legge 194", "abortus", "abortuswet", "abortlag", "aborträtt",
    "aborcja", "aborcyjn*", "аборт*", "прерывание беременности", "переривання вагітності",
    "kürtaj", "gebeliğin sonlandırılması", "άμβλωση", "αμβλώσεις",
    "الإجهاض", "إجهاض", "سقط جنین", "गर्भपात", "গর্ভপাত", "aborsi", "phá thai",
    "ทำแท้ง", "ยุติการตั้งครรภ์", "人工妊娠中絶", "中絶", "堕胎", "墮胎", "人工流产", "人工流產",
    "낙태", "임신중지", "임신중절", "utoaji mimba", "kutoa mimba", "הפלה", "הפלות",
]

BLOCK = [
    # "abort" in its other senses
    "aborted takeoff", "aborted launch", "abort mission", "mission abort", "launch abort",
    "aborted landing", "abort sequence", "process aborted", "aborted transaction",
    "aborted attempt", "aborted merger", "aborted coup", "aborted robbery",
    # commercial and lifestyle noise
    "gift guide", "best deals", "black friday", "coupon", "shopping guide", "recipe",
    "horoscope", "astrolog*", "zodiac", "tarot", "celebrity gossip", "red carpet",
    "box office", "streaming series", "season finale", "video game",
]

# --------------------------------------------------------------------------
# Weight. Standing says who is speaking; this says what the story contains.
# --------------------------------------------------------------------------
DECIDED = [
    "ruling", "ruled", "rules", "struck down", "strikes down", "strike down", "upheld",
    "upholds", "overturns", "overturned", "verdict", "judgment", "judgement",
    "enacted", "signed into law", "passed", "takes effect", "came into force", "repealed",
    "overturned", "injunction", "sentenced", "convicted", "acquitted", "referendum result",
    "approved", "rejected", "banned", "legalised", "legalized", "decriminalised", "decriminalized",
    "sentencia", "fallo", "promulgada", "arrêt", "adoptée", "urteil", "beschlossen",
    "wyrok", "решение суда", "判決", "判决", "판결",
]
INSTITUTIONAL = [
    "world health organization", "world health organisation", "who guidance", "united nations",
    "cedaw", "unfpa", "guttmacher", "ministry of health", "health department", "fda", "ema",
    "official data", "government figures", "national statistics", "court filing", "gazette",
    "peer-reviewed", "published in the lancet", "published in the bmj", "study finds",
    "report finds", "review of", "surveillance report",
]
MEASURED = [
    "per cent", "percent", "%", "rate per", "per 100,000", "per 1,000", "number of abortions",
    "maternal deaths", "figures show", "fell by", "rose by", "increase of", "decrease of",
    "thousands of", "millions of", "estimated", "median", "average",
]
PENDING = [
    "pending", "expected to rule", "will vote", "due to decide", "hearing scheduled",
    "takes effect on", "comes into force", "deadline", "next month", "next year",
    "proposed", "draft law", "under review", "consultation", "ballot in",
]


ANCHOR_C = _compile_all(ANCHOR)
BLOCK_C = _compile_all(BLOCK)
DECIDED_C = _compile_all(DECIDED)
INSTITUTIONAL_C = _compile_all(INSTITUTIONAL)
MEASURED_C = _compile_all(MEASURED)
PENDING_C = _compile_all(PENDING)
TOPICS_C = [(tid, label, [(_compile(t), _compile_all(g) if g else None) for t, g in terms])
            for tid, label, terms in TOPICS]
GEO_C = [(gid, label, [(_compile(t), _compile_all(g) if g else None) for t, g in terms])
         for gid, label, terms in GEO]


def relevant(text):
    if hit(text, BLOCK_C):
        return False
    return hit(text, ANCHOR_C)


def weight(text, standing, placed):
    """What the story contains, as a score and the reasons for it."""
    total, reasons = 0, []
    if hit(text, DECIDED_C):
        total += 2
        reasons.append("decided")
    if hit(text, INSTITUTIONAL_C):
        total += 2
        reasons.append("institutional")
    if hit(text, MEASURED_C):
        total += 1
        reasons.append("measured")
    if hit(text, PENDING_C):
        total += 1
        reasons.append("pending")
    if placed:
        total += 1
        reasons.append("located")
    if standing in ("official", "research"):
        total += 1
        reasons.append("primary source")
    return total, reasons


def topics_for(text):
    hits = []
    for tid, _label, terms in TOPICS_C:
        for term, guards in terms:
            if not hit(text, [term]):
                continue
            if guards and not hit(text, guards):
                continue
            hits.append(tid)
            break
    return hits


def regions_for(text):
    hits = []
    for gid, _label, terms in GEO_C:
        for term, guards in terms:
            if not hit(text, [term]):
                continue
            if guards and not hit(text, guards):
                continue
            hits.append(gid)
            break
    return hits or ["unlocated"]


def load_sources():
    with open(SOURCES_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)
    srcs = []
    for s in cfg.get("direct", []):
        srcs.append({"name": s["name"], "lang": s["lang"], "standing": s["standing"],
                     "region": s["standing"], "kind": s.get("kind", "news"), "url": s["url"]})
    for block, prefix in (("gnews", "Google News · "), ("events", "Events · ")):
        for loc in cfg.get(block, []):
            srcs.append({"name": prefix + loc["label"], "lang": loc["lang"],
                         "standing": loc["standing"], "region": loc["standing"],
                         "kind": "news", "url": build_gnews_url(loc)})
    return srcs, cfg


def run(dry_run=False, fixtures=None):
    sources, cfg = load_sources()
    print("Reading %d wires…" % len(sources))

    def read(src):
        if fixtures:
            path = os.path.join(fixtures, re.sub(r"[^\w.-]", "_", src["name"]) + ".xml")
            if not os.path.exists(path):
                return src, None
            with open(path, "rb") as fh:
                return src, fh.read()
        return src, fetch(src["url"])

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for src, raw in pool.map(read, sources):
            results.append((src, raw))

    previous = []
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as fh:
                previous = json.load(fh).get("items", [])
        except Exception:  # noqa: BLE001
            previous = []

    seen_fp, seen_url, items = set(), set(), []

    def absorb(row):
        fp = fingerprint(row["t"])
        cu = canon_url(row["u"])
        if fp in seen_fp or cu in seen_url:
            return False
        seen_fp.add(fp)
        seen_url.add(cu)
        items.append(row)
        return True

    stats, ok_count, refused = [], 0, 0
    for src, raw in results:
        stat = {"name": src["name"], "lang": src["lang"], "standing": src["standing"],
                "region": src["standing"], "kept": 0, "refused": 0, "ok": False}
        if raw:
            stat["ok"] = True
            ok_count += 1
            for row in parse_feed(raw, src):
                text = (row["t"] + " " + row["s"]).lower()
                if hit(text, BLOCK_C):
                    stat["refused"] += 1
                    refused += 1
                    continue
                if not hit(text, ANCHOR_C):
                    continue
                places = regions_for(text)
                total, reasons = weight(text, src["standing"], places != ["unlocated"])
                row["x"] = topics_for(text) or ["access"]
                row["w"] = places
                row["p"] = total
                row["y"] = reasons
                row["st"] = src["standing"]
                if absorb(row):
                    stat["kept"] += 1
        stats.append(stat)
        print("  %-36s %s" % (src["name"][:36],
                              "unreachable" if not raw
                              else "%d kept, %d refused" % (stat["kept"], stat["refused"])))

    fresh_urls = {canon_url(i["u"]) for i in items}
    for row in previous:
        if "x" in row:
            absorb(row)

    cutoff = int(time.time() * 1000) - RETAIN_DAYS * 86400000
    items = [i for i in items if (i.get("d") or cutoff + 1) >= cutoff]
    items.sort(key=lambda i: i.get("d") or 0, reverse=True)
    items = items[:MAX_ITEMS]
    fresh = sum(1 for i in items if canon_url(i["u"]) in fresh_urls)

    languages = {}
    for loc in cfg.get("gnews", []):
        languages.setdefault(loc["lang"], re.sub(r"\s*\(.*\)$", "", loc["label"]))
    languages.setdefault("en", "English")

    by_standing = {}
    for i in items:
        by_standing[i["st"]] = by_standing.get(i["st"], 0) + 1

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": {"stories": len(items), "new_this_run": fresh,
                   "languages": len({i["g"] for i in items}),
                   "notable": sum(1 for i in items if i.get("p", 0) >= NOTABLE_SCORE),
                   "refused": refused,
                   "by_standing": by_standing,
                   "wires_ok": ok_count, "wires_total": len(sources)},
        "notable_score": NOTABLE_SCORE,
        "languages": languages,
        "standings": [
            {"id": "official", "label": "Courts & institutions"},
            {"id": "research", "label": "Research & health"},
            {"id": "press", "label": "Press"},
            {"id": "rights", "label": "Abortion-rights advocacy"},
            {"id": "antiabortion", "label": "Anti-abortion advocacy"},
        ],
        "topics": [{"id": tid, "label": label} for tid, label, _ in TOPICS],
        "geo": ([{"id": gid, "label": label} for gid, label, _ in GEO] +
                [{"id": "unlocated", "label": "No single region"}]),
        "sources": stats,
        "items": items,
    }

    print("\n%d stories (%d new, %d consequential) · %d refused · %d languages · %d/%d wires answered"
          % (len(items), fresh, payload["counts"]["notable"], refused,
             payload["counts"]["languages"], ok_count, len(sources)))
    if by_standing:
        print("By standing: " + ", ".join("%s %d" % (k, v) for k, v in sorted(by_standing.items())))

    if dry_run:
        print("\n--dry-run: wire_abortion.json not written")
        return payload

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print("Wrote %s (%.0f KB)" % (OUT_PATH, os.path.getsize(OUT_PATH) / 1024))
    return payload


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixtures")
    args = ap.parse_args()
    run(dry_run=args.dry_run, fixtures=args.fixtures)
