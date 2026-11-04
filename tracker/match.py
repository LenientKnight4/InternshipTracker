"""Decides whether a posting is relevant and how loudly to alert about it.

Priorities:
  high   - AM / ER internship  -> instant phone push
  medium - related: wealth roles anywhere, or other business internships at an
           asset manager -> one grouped push per run
  low    - internship at a watched firm but off-focus (IB, tech, ops, events) -> dashboard only
  None   - not an internship, wrong year, wrong level, or outside the US -> ignored
"""
import re

US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV "
             "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()
STATE_RE = re.compile(r"(?:,|-|\|)\s*(?:%s)\b" % "|".join(US_STATES))
CANADA_RE = re.compile(r",\s*(?:AB|BC|MB|NB|NL|NS|ON|PE|QC|SK|CAN)\b")


def _phrases(terms):
    """Whole-word, case-insensitive regex for a list of plain phrases."""
    if not terms:
        return re.compile(r"(?!x)x")
    parts = [re.escape(t.lower()).replace(r"\ ", r"[\s\-]+") for t in terms]
    return re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % "|".join(parts), re.I)


class Matcher:
    def __init__(self, cfg):
        self.years = {str(y) for y in cfg["target_years"]}
        self.early = _phrases(cfg["early_career"])
        self.exclude = _phrases(cfg["exclude"])
        self.events = _phrases(cfg["events"])
        self.strong = _phrases(cfg["strong_focus"])
        self.focus = _phrases(cfg["focus"])
        self.related = _phrases(cfg["related"])
        self.hard_off = _phrases(cfg["off_target_hard"])
        self.soft_off = _phrases(cfg["off_target_soft"])
        self.non_us = _phrases(cfg["non_us_locations"])
        self.us = _phrases(cfg["us_markers"])

    def is_us_or_unknown(self, text):
        if not (self.non_us.search(text) or CANADA_RE.search(text)):
            return True
        return bool(self.us.search(text) or STATE_RE.search(text))

    def classify(self, job, firm_kind):
        title = job.title
        text = f"{title} {job.extra}"
        if not self.early.search(title):
            return None
        if self.exclude.search(title):
            return None
        years = set(re.findall(r"(?<!\d)(20\d\d)(?!\d)", title))
        if years and not years & self.years:
            return None
        if not self.is_us_or_unknown(f"{job.location} | {title}"):
            return None

        if self.events.search(title) or self.hard_off.search(text):
            return "low"
        if self.strong.search(text):
            return "high"
        if self.soft_off.search(text):
            return "low"
        if self.focus.search(text):
            return "high"
        if self.related.search(text):
            return "medium"
        return "medium" if firm_kind == "am" else "low"
