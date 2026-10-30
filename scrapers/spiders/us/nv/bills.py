import logging
import re
import time
from typing import Union
import dateutil.parser
import pytz

from collections import defaultdict
from dataclasses import dataclass

from scrapy import Request

from openstates.scrape import Bill
from scrapers.spiders import BaseSpider
from scrapers.items import BillStub, BillStubItem, bill_stub_schema, Chamber, BillItem
from scrapers.utils import (
    lxmlize,
    BillTitleLengthError,
    SelectorError)
from .state import Nevada


ACTION_CLASSIFIERS = (
    ("Approved by the Governor", ["executive-signature"]),
    ("Bill read. Veto not sustained", ["veto-override-passage"]),
    ("Bill read. Veto sustained", ["veto-override-failure"]),
    ("Enrolled and delivered to Governor", ["executive-receipt"]),
    ("From committee: .+? adopted", ["committee-passage"]),
    # the committee and chamber passage can be combined, see NV 80 SB 506
    (
        r"From committee: .+? pass(.*)Read Third time\.\s*Passed\.",
        ["committee-passage", "reading-3", "passage"],
    ),
    ("From committee: .+? pass", ["committee-passage"]),
    ("Prefiled. Referred", ["introduction", "referral-committee"]),
    ("Read first time. Referred", ["reading-1", "referral-committee"]),
    ("Read first time.", ["reading-1"]),
    ("Read second time.", ["reading-2"]),
    ("Read third time. Lost", ["failure", "reading-3"]),
    ("Read third time. Passed", ["passage", "reading-3"]),
    ("Read third time.", ["reading-3"]),
    ("Rereferred", ["referral-committee"]),
    ("Resolution read and adopted", ["passage"]),
    ("Enrolled and delivered", ["enrolled"]),
    ("To enrollment", ["passage"]),
    ("Approved by the Governor", ["executive-signature"]),
    ("Vetoed by the Governor", ["executive-veto"]),
)

session_slugs = {
    "2010Special26": "26th2010Special",
    "2013Special27": "27th2013Special",
    "2014Special28": "28th2014Special",
    "2015Special29": "29th2015Special",
    "2016Special30": "30th2016Special",
    "2020Special31": "31st2020Special",
    "2020Special32": "32nd2020Special",
    "2021Special33": "33rd2021Special",
    "2023Special34": "34th2023Special",
    "2023Special35": "35th2023Special",
    "75": "75th2009",
    "76": "76th2011",
    "77": "77th2013",
    "78": "78th2015",
    "79": "79th2017",
    "80": "80th2019",
    "81": "81st2021",
    "82": "82nd2023",
}

# NV sometimes carries-over bills from previous sessions,
# without regard for bill number conflicts.
# so AB1* could get carried in, even if there's already an existing AB1
# The number of asterisks represent which past session it was pulled in from,
# which can include specials, and skip around, so this can't be automated.
# The list is at https://www.leg.state.nv.us/Session/81st2021/Reports/BillsListLegacy.cfm?DoctypeID=1
# where 81st2021 will need to be swapped in for the session code.
CARRYOVERS = {
    "80": {
        "*": "2017",
    },
    "81": {
        "*": "2019",
        "**": "2020Special32",
    },
    "82": {"*": "2021"},
}


TZ = pytz.timezone("PST8PDT")


def get_column_div(name, response):
    # lots of places where we have a <div class='col-md-2 font-weight-bold'>
    # followeed by a <div class='col'>
    # with interesting content in the latter element
    return response.xpath(
        f"//div[contains(text(),'{name}')]/following-sibling::div[@class='col']"
    )


def parse_date(date_str):
    return TZ.localize(dateutil.parser.parse(date_str))


def add_sponsors(bill, sponsor_links, primary):
    seen = set()
    for link in sponsor_links:
        name = link.xpath('text()').get().strip()
        if "Sponsors" in name or name == "":
            continue
        # Removes leg position from name
        # Example: Assemblywoman Alexis Hansen
        if name.split()[0] in ["Assemblywoman", "Assemblyman", "Senator"]:
            name = " ".join(name.split()[1:]).strip()
        if name not in seen:
            seen.add(name)
            bill.add_sponsorship(
                name=name,
                classification="sponsor" if primary else "cosponsor",
                entity_type="person",
                primary=primary,
            )


def add_actions(bill, chamber, response):
    # first action is from originating chamber
    actor = chamber

    # Sometimes NV bill page might just not have an actions section at all
    try:
        for row in response.xpath("//caption/parent::table/tbody/tr"):
            date = row.css('[data-th="Date"]::text').get()
            action = row.css('[data-th="Action"]::text').get()

            date = parse_date(date)

            # catch chamber changes
            if action.startswith("In Assembly"):
                actor = "lower"
            elif action.startswith("In Senate"):
                actor = "upper"
            elif "Governor" in action:
                actor = "executive"

            action_type = []
            for pattern, atype in ACTION_CLASSIFIERS:
                if not re.search(pattern, action, re.IGNORECASE):
                    continue
                # sometimes NV returns multiple actions in the same posting
                # so don't break here
                action_type = action_type + atype

            if not action_type:
                action_type = None
            else:
                action_type = list(set(action_type))

            related_entities = []
            if "Committee on" in action:
                committees = re.findall(
                    r"Committee on ([a-zA-Z, ]*)\.", action)
                for committee in committees:
                    related_entities.append(
                        {"type": "committee", "name": committee}
                    )

            bill.add_action(
                description=action,
                date=date,
                chamber=actor,
                classification=action_type,
                related_entities=related_entities,
            )
    except SelectorError:
        pass


def extract_bdr(title):
    """
    bills in NV start with a 'bill draft request'
    the number is in the title but it's useful as a structured extra
    """
    bdr = False
    bdr_regex = re.search(r"\(BDR (\w+-\w+)\)", title)
    if bdr_regex:
        bdr = bdr_regex.group(1)
    return bdr


def shorten_bill_title(title):
    """
    Used in cases where the bill title exceeds the
    300-character limit that we have on this attribute.
    """
    title_bdr_re = re.compile(r"(.+)(\(BDR \w+-\w+\))")
    title_bdr_match = title_bdr_re.search(title)
    bdr_full = ""
    if title_bdr_match:
        title, bdr_full = title_bdr_match.groups()
    title = f"{title[:280]}... + {bdr_full}"
    return title


nv_bill_stub_schema = bill_stub_schema.copy()
nv_bill_stub_schema["properties"]["subjects"] = {
    "items": {"type": "string"},
    "type": "array",
}

@dataclass
class NVBillStub(BillStub):
    subjects: list
    _schema = nv_bill_stub_schema

    def __init__(self, subjects, *args):
        self.subjects = subjects
        super(NVBillStub, self).__init__(*args)


@dataclass
class NVBillStubItem(BillStubItem):
    subjects: list

    def __init__(self, item: Union[BillStub, None] = None):
        super().__init__(item)


class BillsSpider(BaseSpider):
    name = "nv-bills"
    jurisdiction = Nevada
    subject_mapping = defaultdict(set)

    def __init__(self, session=None, chamber=None, **kwargs):
        self.session = session
        self.chamber = chamber
        super().__init__(Nevada(), **kwargs)

    def do_scrape(self):
        # TODO default active sessions
        slug = session_slugs[self.session]
        # fetch and parse subjects report to create bill:subjects mapping
        year = slug[4:]
        url = (
            f"https://www.leg.state.nv.us/Session/{slug}/Reports/"
            f"TablesAndIndex/{year}_{self.session}-index.html"
        )
        self.parse_bill_subjects_page(lxmlize(url))

        # proceed bill listing
        bill_listing_url = (f"https://www.leg.state.nv.us/App/NELIS/REL/{slug}/"
                            "HomeBill/BillsTab?selectedTab=List&Filters.PageSize=2147483647"
                            f"&_={time.time()}")
        meta = {
            "is_scout": True
        }

        yield Request(url=bill_listing_url, callback=self.parse_bill_list, meta=meta)

    def parse_bill_subjects_page(self, response):
        # first, a bit about this page:
        # Level0 are the bolded titles
        # Level1,2,3,4 are detailed titles, contain links to bills
        # all links under a Level0 we can consider categorized by it
        # there are random newlines *everywhere* that should get replaced
        subject = None
        for p in response.xpath("//p"):
            if p.xpath("@class").get() == "Level0":
                # many items include "<i>(See also ...)</i>" references that we want to exclude from the main subject
                see_also_text = "".join(
                    p.xpath('./i//text()').getall()).replace("\r\n", " ")
                subject = ''.join(p.xpath('.//text()').getall()
                                  ).replace("\r\n", " ").replace(see_also_text, "")
            else:
                if subject:
                    # exclude links that are references to other subjects ie <i>(See <a>ELECTIONS</a>)</i>
                    for a in p.xpath(".//a[not(ancestor::i)]"):
                        bill_id = a.xpath('text()').get().replace(
                            "\r\n", "") if a.xpath('text()').get() else None
                        self.subject_mapping[bill_id].add(subject)

    def parse_bill_list(self, response):
        bill_link_elems = response.css(".row a")
        logging.info(f"Found {len(bill_link_elems)} bill link elements")
        for link in bill_link_elems:
            bill_url = response.urljoin(link.xpath("@href").get())
            identifier = link.xpath("text()").get()
            chamber = Chamber.UPPER if identifier.startswith(
                "S") else Chamber.LOWER
            bill_stub = NVBillStub(
                list(self.subject_mapping[identifier]),
                bill_url,
                link.xpath("text()").get(),
                self.session,
                chamber,
            )

            # Yield the stub
            yield NVBillStubItem(bill_stub)

            # Yield a request for the bill details page
            bill_key = bill_url.split("/")[-2]
            slug = session_slugs[self.session]
            bill_ajax_request_url = (
                f"https://www.leg.state.nv.us/App/NELIS/REL/{slug}/Bill/"
                f"FillSelectedBillTab?selectedTab=Overview&billKey={bill_key}"
                f"&_={time.time()}"
            )
            bill_overview_parser_args = {
                "bill_stub": bill_stub,
            }

            yield Request(
                url=bill_ajax_request_url,
                callback=self.parse_bill_overview,
                cb_kwargs=bill_overview_parser_args
            )

    def parse_bill_overview(self, response, bill_stub):
        short_title = get_column_div("Summary", response).xpath("text()").get()
        long_title = response.css("#title::text").get()

        if "*" in bill_stub.identifier:
            stars = re.search(r"\*+", bill_stub.identifier).group()
            if (
                    self.session in CARRYOVERS
                    and stars in CARRYOVERS[self.session]
            ):
                bill_stub.identifier = re.sub(
                    r"\*+",
                    "-" + CARRYOVERS[self.session][stars],
                    bill_stub.identifier,
                )
            else:
                logging.error(
                    f"Unidentified carryover bill {bill_stub.identifier}. Update CARRYOVERS dict in bills.py"
                )
                return

        # Only known cases where bill title is over 300 characters
        if self.session == "82":
            if bill_stub.identifier in ("SJR7-2021", "AB488"):
                short_title = shorten_bill_title(short_title)
            # If additional case arises in future
        elif len(short_title) > 300:
            raise BillTitleLengthError(bill_stub.identifier, short_title)

        bill = Bill(
            identifier=bill_stub.identifier,
            legislative_session=self.session,
            title=short_title,
            chamber=bill_stub.chamber,
        )
        bill.subject = bill_stub.subjects
        # use the pretty source URL
        bill.add_source(bill_stub.source_url)
        bill.add_title(long_title)

        try:
            sponsors = get_column_div("Primary Sponsor", response)
            add_sponsors(bill, sponsors.css("a"), primary=True)
        except SelectorError:
            pass
        try:
            cosponsors = get_column_div("Co-Sponsor", response)
            add_sponsors(bill, cosponsors.css("a"), primary=False)
        except SelectorError:
            pass

        # TODO: figure out cosponsor div name, can't find any as of Feb 2021
        add_actions(bill, bill_stub.chamber, response)

        bdr = extract_bdr(short_title)
        if bdr:
            bill.extras["BDR"] = bdr

        text_url = response.url.replace("Overview", "Text")
        bill_tab_text_parser_args = {"bill": bill}

        yield Request(
            url=text_url,
            callback=self.parse_bill_tab_text,
            cb_kwargs=bill_tab_text_parser_args
        )

    def parse_bill_tab_text(self, response, bill):
        # some BDRs have no text link
        for row in response.css(".d-md-none .h5 a"):
            title = ''.join(row.xpath('.//text()').getall()).strip()
            link = response.urljoin(row.xpath("@href").get())
            bill.add_version_link(
                title, link, media_type="application/pdf", on_duplicate="ignore"
            )

        ex_url = response.url.replace("Text", "Exhibits")
        exhibit_tab_text_parser_args = {"bill": bill}

        yield Request(
            url=ex_url,
            callback=self.parse_exhibit_tab_text,
            cb_kwargs=exhibit_tab_text_parser_args
        )

    def parse_exhibit_tab_text(self, response, bill):
        for row in response.css("#divExhibits li.my-4 a"):
            title = ''.join(row.xpath('.//text()').getall()).strip()
            link = response.urljoin(row.xpath("@href").get())
            bill.add_document_link(
                title, link, media_type="application/pdf", on_duplicate="ignore"
            )

        am_url = response.url.replace("Exhibits", "Amendments")
        amendment_tab_text_parser_args = {"bill": bill}

        yield Request(
            url=am_url,
            callback=self.parse_amendment_tab_text,
            cb_kwargs=amendment_tab_text_parser_args
        )

    def parse_amendment_tab_text(self, response, bill):
        for row in response.css(".col-11.col-md a"):
            title = ''.join(row.xpath('.//text()').getall()).strip()
            link = response.urljoin(row.xpath("@href").get())
            bill.add_version_link(
                title, link, media_type="application/pdf", on_duplicate="ignore"
            )

        fn_url = response.url.replace("Amendments", "FiscalNotes")
        fiscal_tab_text_parser_args = {"bill": bill}

        yield Request(
            url=fn_url,
            callback=self.parse_fiscal_tab_text,
            cb_kwargs=fiscal_tab_text_parser_args
        )

    def parse_fiscal_tab_text(self, response, bill):
        for row in response.css("ul.list-unstyled li a"):
            title = ''.join(row.xpath('.//text()').getall()).strip()
            title = f"Fiscal Note: {title}"
            link = response.urljoin(response.urljoin(row.xpath("@href").get()))
            bill.add_document_link(
                title, link, media_type="application/pdf", on_duplicate="ignore"
            )

        yield BillItem(bill)
