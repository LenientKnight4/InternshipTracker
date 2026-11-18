"""Decides whether a posting is relevant and how loudly to alert about it.

Priorities (keyword lists come from filters.yaml):
  high   - strong_focus / focus match, or any non-off-target internship at a
           firm of kind "focus" (or "credit") -> instant phone push
  medium - "related" match, or other internships at a firm of kind "am" / "related"
           -> one grouped push per run
  low    - internship at a watched firm but off-focus -> dashboard only
  None   - not an internship, wrong year, wrong level, or wrong location -> ignored

Location: with only_locations set (e.g. NYC / Boston), a posting must be in one of
those places. Vague locations ("2 Locations", "United States", "Remote") are
"maybe" and get settled from the description. Without it, any US location passes.

fit() then checks your class year can apply (see graduation_year in filters.yaml).
"""
import re

US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV "
             "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()
STATE_RE = re.compile(r"(?:,|-|\|)\s*(?:%s)\b" % "|".join(US_STATES))
CANADA_RE = re.compile(r",\s*(?:AB|BC|MB|NB|NL|NS|ON|PE|QC|SK|CAN)\b")
YEAR_RE = re.compile(r"(?<!\d)(20\d\d)(?!\d)")
# Wording that means sophomores can apply
UNDERCLASS_RE = re.compile(
    # "sophomore internship / experience" means they want someone who already did one, so it doesn't count
    r"(?<![a-z])(sophomores?(?![a-z])(?!\s+(?:year\s+)?(?:summer\s+)?(?:internships?|experience))"
    r"|second[- ]year (?:undergrad\w*|students?|college\w*)|rising juniors?|underclass\w*"
    r"|first[- ] and second[- ]year|freshm[ae]n and sophomores?|early insights?|insight program(?:me)?s?)(?![a-z])", re.I)
# Wording that means it's for students a year ahead
UPPERCLASS_RE = re.compile(
    r"(?<![a-z])(rising seniors?|entering (?:their|your) (?:senior|final) year|penultimate year|final[- ]year students?)(?![a-z])",
    re.I)
# Text right after these words usually holds the required graduation (or full-time start) dates
GRAD_RE = re.compile(r"(?:graduat\w*|class of|grad(?:uation)? date|degree (?:completion|by)|expected to (?:complete|finish)"
                     r"|full[- ]time (?:employment|position|role|start))[^.;]{0,90}", re.I)
SPLIT_YEAR_RE = re.compile(r"(?<!\d)(20\d)\s(\d)(?!\d)")  # web pages sometimes break "2028" into "202 8"
# Location text that doesn't name a specific city
VAGUE_LOCATION_RE = re.compile(r"^\s*$|\d+\s+locations?|multiple|various|remote|virtual|nationwide|anywhere|flexible"
                               r"|^\W*(?:united states(?: of america)?|usa?|u\.s\.|north america|americas)\W*$", re.I)
KIND_DEFAULT = {"focus": "high", "credit": "high", "am": "medium", "related": "medium"}


def _phrases(terms):
    """Whole-word, case-insensitive regex for a list of plain phrases."""
    if not terms:
        return re.compile(r"(?!x)x")
    parts = [re.escape(t.lower()).replace(r"\ ", r"[\s\-]+") for t in terms]
    return re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % "|".join(parts), re.I)


class Matcher:
    def __init__(self, cfg):
        grad = int(cfg.get("graduation_year") or max(cfg["target_years"]) + 1)
        self.grad = str(grad)
        self.soph_summer = str(grad - 2)    # summer after sophomore year
        self.junior_summer = str(grad - 1)  # summer after junior year
        self.years = {self.soph_summer, self.junior_summer}
        self.early = _phrases(cfg["early_career"])
        self.exclude = _phrases(cfg["exclude"])
        self.events = _phrases(cfg["events"])
        self.strong = _phrases(cfg["strong_focus"])
        self.focus = _phrases(cfg["focus"])
        self.related = _phrases(cfg["related"])
        self.hard_off = _phrases(cfg["off_target_hard"])
        self.soft_off = _phrases(cfg["off_target_soft"])
        self.non_us = _phrases(cfg.get("non_us_locations"))
        self.us = _phrases(cfg.get("us_markers"))
        only = cfg.get("only_locations") or []
        self.only_label = " / ".join(cfg.get("only_locations_label") or only[:2])
        # In a location field, "Albany, New York" means the state, so a place only counts as the city
        # when it isn't right after a comma. Titles ("Marketing Intern, New York") are matched loosely.
        self.only_loc = re.compile(r"(?<!, )" + _phrases(only).pattern, re.I) if only else None
        self.only_any = _phrases(only) if only else None

    def is_us_or_unknown(self, text):
        if not (self.non_us.search(text) or CANADA_RE.search(text)):
            return True
        return bool(self.us.search(text) or STATE_RE.search(text))

    def location_status(self, job):
        """"ok", "no", or "maybe" (only_locations set and the location is too vague to tell)."""
        if not self.only_loc:
            return "ok" if self.is_us_or_unknown(f"{job.location} | {job.title}") else "no"
        if self.only_loc.search(job.location) or self.only_any.search(job.title):
            return "ok"
        return "maybe" if VAGUE_LOCATION_RE.search(job.location) else "no"

    def place_in(self, description):
        return bool(self.only_any and self.only_any.search(description or ""))

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
        if self.location_status(job) == "no":
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
        return KIND_DEFAULT.get(firm_kind, "low")

    def fit(self, job, description=None):
        """Can someone graduating in self.grad apply? Returns (ok, reason).

        Returns None when the title alone can't tell and the description is needed.
        Summer-after-junior-year programs always fit. Summer-after-sophomore-year
        programs fit only if the posting says sophomores / your class can apply.
        Undated postings fit unless they name other class years.
        """
        years = set(YEAR_RE.findall(job.title))
        if self.junior_summer in years:
            return True, f"summer {self.junior_summer} (after junior year)"
        if UNDERCLASS_RE.search(job.title):
            return True, "for sophomores"
        if description is None:
            return None
        # An explicit graduation year beats looser wording like "rising junior"
        description = SPLIT_YEAR_RE.sub(r"\1\2", description)
        grads = {y for m in GRAD_RE.finditer(description) for y in YEAR_RE.findall(m.group(0))}
        if self.grad in grads:
            return True, f"open to class of {self.grad}"
        if grads:
            return False, f"for class of {'/'.join(sorted(grads))}"
        if UNDERCLASS_RE.search(description):
            return True, "open to sophomores"
        if self.soph_summer in years:
            return False, f"summer {self.soph_summer}, no sign sophomores can apply"
        if UPPERCLASS_RE.search(description):
            return False, "for rising seniors"
        return True, "no class year listed; check eligibility"
