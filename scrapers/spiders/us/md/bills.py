import re
import datetime
from urllib.parse import urljoin

from scrapy import Request

from core.scrape import Bill
from scrapers.spiders import BaseSpider
from scrapers.items import BillStub, BillStubItem, BillItem
from .state import Maryland

# Ported from openstates-scrapers scrapers/md/bills.py; selectors kept identical so
# output can be diffed 1:1 against os-update.

# stored URLs keep http:// to match os-update; requests go straight to https://
# since the site 301s every http:// request and each redirect costs a DOWNLOAD_DELAY
BASE_URL = "http://mgaleg.maryland.gov/mgawebsite/"
HTTPS_BASE_URL = BASE_URL.replace("http://", "https://")
CHAMBERS = {"upper": "senate", "lower": "house"}

classifiers = {
    r"Committee Amendment .+? Adopted": "amendment-passage",
    r"Favorable": "committee-passage-favorable",
    r"First Reading": "referral-committee",
    r"Floor (Committee )?Amendment\s?\(.+?\)$": "amendment-introduction",
    r"Floor Amendment .+? Rejected": "amendment-failure",
    r"Floor (Committee )?Amendment.+?Adopted": "amendment-passage",
    r"Floor Amendment.+? Withdrawn": "amendment-withdrawal",
    r"Pre\-filed": "introduction",
    r"Withdrawn": "withdrawal",
    r"Re\-(referred|assigned)": "referral-committee",
    r"Recommit to Committee": "referral-committee",
    r"Referred": "referral-committee",
    r"Third Reading Passed": "passage",
    r"Third Reading Failed": "failure",
    r"Unfavorable": "committee-passage-unfavorable",
    r"Vetoed": "executive-veto",
    r"Gubernatorial Veto Override": "veto-override-passage",
    r"Veto overridden": "veto-override-passage",
    r"Overridden": "veto-override-passage",
    r"Approved by the Governor": "executive-signature",
    r"Enacted under Article II": "became-law",
    r"Became law": "became-law",
}


def _classify_action(action):
    if not action:
        return None

    ctty = None

    for regex, type in classifiers.items():
        if re.match(regex, action):
            if "referral-committee" in type:
                ctty = re.sub(regex, "", action).strip()
            return (type, ctty)
    return (None, ctty)


def text_content(sel):
    # equivalent of lxml's element.text_content()
    return "".join(sel.xpath(".//text()").getall())


def add_actions(bill, response):
    chamber_map = {"Senate": "upper", "House": "lower", "Post Passage": "executive"}
    for row in response.xpath('//table[@id="detailsHistory"]/tbody/tr'):
        # actions have something in the first column
        potential_chamber = row.xpath("td[1]/text()").getall()
        if len(potential_chamber) > 0:
            chamber = chamber_map[potential_chamber[0].strip()]
            action_date = row.xpath("td[2]/text()").get().strip()
            action_date = datetime.datetime.strptime(action_date, "%m/%d/%Y")
            action_text = row.xpath("td[4]/text()").get().strip()
            atype, committee = _classify_action(action_text)
            action = bill.add_action(
                action_text,
                action_date.strftime("%Y-%m-%d"),
                chamber=chamber,
                classification=atype,
            )
            if committee:
                action.add_related_entity(committee, "organization")
        elif row.xpath("td[4]/a"):
            link = row.xpath("td[4]/a")[0]
            link_text = text_content(link).strip()
            if link_text.startswith("Text"):
                link_text = link_text.replace("Text - ", "")
                bill.add_version_link(
                    link_text,
                    urljoin(BASE_URL, link.attrib["href"]),
                    media_type="application/pdf",
                    on_duplicate="ignore",
                )


def add_subjects(bill, response):
    # TODO: this xpath gets both 'subjects' and 'file codes'
    # they both link to subjects on the site and seem to fit the definition of subject
    for row in response.xpath(
        '//a[contains(@href, "/Legislation/SubjectIndex/annotac")]/text()'
    ).getall():
        # row[0] is only the first character; kept to match os-update output
        bill.add_subject(row[0])


def add_sponsors(bill, response):
    sponsors_title = "".join(
        response.xpath(
            '//dt[contains(text(), "Sponsored by")]/following-sibling::dd[1]/text()'
        ).getall()
    )
    if "Delegate" in sponsors_title:
        chamber = "lower"
    elif "Senator" in sponsors_title:
        chamber = "upper"
    else:
        chamber = None

    sponsors = response.xpath(
        '//dt[contains(text(), "Sponsored by")]/following-sibling::dd[1]/a'
    )
    sponsor_type = "primary"

    for sponsor in sponsors:
        sponsor = sponsor.xpath("text()").get("").strip()
        entity_type = "organization" if "Committee" in sponsor else "person"
        if not sponsor:
            continue
        if sponsor == "Departmental":
            # there seems to be meta information about the requestor, but that
            # doesn't really fit our definition of sponsor.  once we get to this
            # sponsor the list is done
            # (e.g. http://mgaleg.maryland.gov/mgawebsite/Legislation/Details/sb0125?ys=2021RS)
            break
        if chamber:
            bill.add_sponsorship(
                sponsor,
                sponsor_type,
                primary=sponsor_type == "primary",
                entity_type=entity_type,
                chamber=chamber,
            )
        else:
            bill.add_sponsorship(
                sponsor,
                sponsor_type,
                primary=sponsor_type == "primary",
                entity_type=entity_type,
            )


class BillsSpider(BaseSpider):
    name = "md-bills"
    jurisdiction = Maryland
    # matches scrapelib's default 60 requests/minute used by os-update
    custom_settings = {
        "DOWNLOAD_DELAY": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
    }

    def __init__(self, session=None, chamber=None, **kwargs):
        self.session = session or next(
            s["identifier"]
            for s in reversed(Maryland.legislative_sessions)
            if s.get("active")
        )
        self.chamber = chamber
        super().__init__(Maryland(), **kwargs)

    def do_scrape(self):
        session_slug = self.session if "s" in self.session else self.session + "RS"
        chambers = [self.chamber] if self.chamber else ["upper", "lower"]
        for chamber in chambers:
            # despite only showing one page to the browser,
            # all the bills are conveniently in the markup
            yield Request(
                url=f"{HTTPS_BASE_URL}Legislation/Index/{CHAMBERS[chamber]}?ys={session_slug}",
                callback=self.parse_bill_list,
                cb_kwargs={"chamber": chamber},
                meta={"is_scout": True},
            )

    def parse_bill_list(self, response, chamber):
        for row in response.xpath('//table[@id="billIndex"]/tbody/tr'):
            link = row.xpath("td[1]/a[1]")
            url = urljoin(BASE_URL, link.attrib["href"])
            bill_stub = BillStub(
                url, link.xpath("text()").get().strip(), self.session, chamber
            )
            yield BillStubItem(bill_stub)
            yield Request(
                url=url.replace(BASE_URL, HTTPS_BASE_URL),
                callback=self.parse_bill,
                cb_kwargs={"bill_stub": bill_stub},
            )

    def parse_bill(self, response, bill_stub):
        h2 = '//h2[@style="font-size:1.3rem;"]'
        bill_id = (
            response.xpath(f"{h2}/a[1]/text()").get()
            or response.xpath(f"{h2}/text()").get()
        )
        if not bill_id:
            self.logger.warning(f"No bill id for {bill_stub.source_url}")
            return
        bill_id = bill_id.strip()
        title = (
            response.xpath(
                '//dt[contains(text(), "Title")]/following-sibling::dd[1]/text()'
            )
            .get()
            .strip()
        )

        if "OIS Test" in title:
            self.logger.warning(f"Ignoring test bill {bill_id} - {title}")
            return

        if "B" in bill_id:
            _type = ["bill"]
        elif "J" in bill_id:
            _type = ["joint resolution"]
        elif "HS" in bill_id or "SS" in bill_id:
            _type = ["resolution"]
        else:
            raise ValueError("unknown bill type " + bill_id)

        bill = Bill(
            bill_id,
            legislative_session=self.session,
            chamber=bill_stub.chamber,
            title=title,
            classification=_type,
        )
        # the http list URL, not the redirected https response.url, to match os-update
        bill.add_source(bill_stub.source_url)

        add_subjects(bill, response)
        add_sponsors(bill, response)
        add_actions(bill, response)

        fiscal_note = response.xpath(
            '//dt[contains(text(), "Analysis")]/following-sibling::dd[1]/a'
        )
        if fiscal_note:
            bill.add_document_link(
                text_content(fiscal_note[0]),
                urljoin(BASE_URL, fiscal_note[0].attrib["href"]),
                media_type="application/pdf",
            )

        # effective date, where available
        eff_date = response.xpath(
            '//div[contains(text(), "Effective Date(s)")]/text()'
        ).get()
        if eff_date:
            eff_date = eff_date.strip().replace("Effective Date(s):", "").strip()
            # this can contain multiple dates, eg "July 1, 2020, July 1, 2022"
            bill.extras["date_effective"] = eff_date

        companion_link = response.xpath("//div[contains(.,'Cross-filed')]/a")
        if companion_link:
            bill.add_related_bill(
                companion_link[0].xpath("text()").get(),
                legislative_session=self.session,
                relation_type="companion",
            )

        # previous session version of the bill
        prev_div = response.xpath(
            "//div[contains(text(),'Introduced in a prior session')]"
        )
        if prev_div:
            prev_div_text = prev_div[0].xpath("text()").getall()[1]
            prev_session = re.search(r"Session\:\s(.*)", prev_div_text).group(1)
            prev_bill = prev_div[0].xpath("a/text()").get()
            bill.add_related_bill(
                prev_bill,
                legislative_session=prev_session,
                relation_type="prior-session",
            )

        yield BillItem(bill)
