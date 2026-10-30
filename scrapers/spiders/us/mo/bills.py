import re
import pytz
import datetime as dt

import requests

from collections import defaultdict
from io import BytesIO
from zipfile import ZipFile

from scrapy import Request, Selector

from .state import Missouri
from openstates.scrape import Bill, VoteEvent
from scrapers.spiders import BaseSpider
from scrapers.items import Chamber, BillItem, VoteEventItem
from scrapers.utils import (
    custom_headers,
    clean_text,
    lxmlize,
    UnrecognizedSessionType)


bill_types = {
    "HB ": "bill",
    "HJR": "joint resolution",
    "HCR": "concurrent resolution",
    "SB ": "bill",
    "SJR": "joint resolution",
    "SCR": "concurrent resolution",
}
flags = [
    ("Introduced", "introduction"),
    ("Offered", "introduction"),
    ("First Read", "reading-1"),
    ("Read Second Time", "reading-2"),
    ("Second Read", "reading-2"),
    # make sure passage is checked before reading-3
    ("Third Read and Passed", "passage"),
    ("Reported Do Pass", "committee-passage"),
    ("Voted Do Pass", "committee-passage"),
    ("Third Read", "reading-3"),
    ("Referred", "referral-committee"),
    ("Withdrawn", "withdrawal"),
    ("S adopted", "passage"),
    ("Truly Agreed To and Finally Passed", "passage"),
    ("Signed by Governor", "executive-signature"),
    ("Approved by Governor", "executive-signature"),
    ("Vetoed by Governor", "executive-veto"),
    ("Vetoed in Part by Governor", "executive-veto-line-item"),
    ("Legislature voted to override Governor's veto", "veto-override-passage"),
]

TIMEZONE = pytz.timezone("America/Chicago")


def house_get_actor_from_action(text):
    m = re.search(r"\((\bH\b|\bS\b)\)", text)
    if not m:
        if text.endswith("Governor"):
            return Chamber.UPPER
        else:
            return Chamber.LOWER

    abbrev = m.group(1)
    if abbrev == "S":
        return Chamber.UPPER
    return Chamber.LOWER


def senate_get_actor_from_action(text):
    if re.search("Prefiled", text):
        return Chamber.UPPER

    m = re.search(r"(\bH\b|\bS\b|House)", text)
    if not m:
        if text.endswith("Governor"):
            return Chamber.EXECUTIVE
        else:
            return Chamber.UPPER

    if m.group(1) == "S":
        return Chamber.UPPER
    else:
        return Chamber.LOWER


def get_action(actor, action):
    # Alright. This covers both chambers and everything else.
    categories = []
    for flag, acat in flags:
        if flag in action:
            categories.append(acat)

    return categories or None


class BillsSpider(BaseSpider):
    name = "mo-bills"
    jurisdiction = Missouri
    subject_mapping = defaultdict(set)

    def __init__(self, session=None, chamber=None, **kwargs):
        self.session = session
        self.chamber = chamber
        self.jurisdiction = Missouri()
        super().__init__(**kwargs)

    def do_scrape(self):
        # parse subjects
        self.parse_subjects(self.session)

        if self.chamber in [Chamber.UPPER, None]:
            # scrape senate bills
            year2 = "%02d" % (int(self.session[:4]) % 100)

            # Save the root URL, since we'll use it later.
            bill_root = f"http://www.senate.mo.gov/{year2}info/BTS_Web/"
            sb_url = f"{bill_root}BillList.aspx?SessionType={self.get_session_type(self.session)}"

            yield Request(url=sb_url, callback=self.parse_upper_chamber, headers=custom_headers)

        if self.chamber in [Chamber.LOWER, None]:
            # scrape house bills
            year = str(dt.date.today().year)[2:4]
            session_list_url = f'https://documents.house.mo.gov/xml/{year}1-SessionList.XML'
            yield Request(url=session_list_url, callback=self.parse_lower_chamber, headers=custom_headers)

    def parse_upper_chamber(self, response):
        for bill_table in response.xpath("//a[contains(@id, 'hlBillNum')]"):
            yield Request(url=response.urljoin(bill_table.xpath('@href').get()), callback=self.parse_senate_billpage)

    def parse_lower_chamber(self, response):
        session_id = None

        for session in response.xpath('//Session'):
            session_id = session.xpath('./ID/text()').get()
            session_year = session.xpath('./SessionYear/text()').get()
            session_code = session.xpath('./SessionCode/text()').get()
            session_code = '' if session_code == 'R' else session_code
            if f'{session_year}{session_code}' == self.session:
                break

        if not session_id:
            raise UnrecognizedSessionType(self.session)

        zipped_xml_url = f'https://documents.house.mo.gov/xml/{session_id}.zip'
        zip_response = requests.get(zipped_xml_url, headers=custom_headers)
        zip_file = ZipFile(BytesIO(zip_response.content))
        # read 221-BillList.xml in zip file
        bill_list_content = zip_file.read(
            f'{session_id}/{session_id}-BillList.xml')
        bl_response = Selector(text=bill_list_content, type='xml')

        for request in self.parse_house_bill_list(bl_response, zip_file=zip_file, session_id=session_id):
            yield request

    # Get House Bills
    def parse_house_bill_list(self, response, **kwargs):
        for bill in response.xpath('//BillXML'):
            bill_url = bill.xpath('./BillXMLLink/text()').get()
            bill_type = bill.xpath('./BillType/text()').get()
            bill_num = bill.xpath('./BillNumber/text()').get()
            bill_year = bill.xpath('./SessionYear/text()').get()
            bill_code = bill.xpath('./SessionCode/text()').get()
            bill_id = f'{bill_type} {bill_num}'

            if kwargs:
                zip_file = kwargs['zip_file']
                session_id = kwargs['session_id']
                bill_file_name = bill_url.replace(
                    'https://documents.house.mo.gov/xml', session_id)
                bill_content = zip_file.read(bill_file_name)
                ib_response = Selector(text=bill_content, type='xml')

                for item in self.parse_house_bill(ib_response, bill_id, bill_year, bill_code):
                    yield item
            else:
                yield Request(url=bill_url, callback=self.parse_house_bill,
                              cb_kwargs=dict(bill_id=bill_id, bill_year=bill_year, bill_code=bill_code))

    def parse_house_bill(self, response, bill_id, bill_year, bill_code):
        official_title = response.xpath(
            '//BillInformation/CurrentBillString/text()').get()
        bill_desc = response.xpath(
            '//BillInformation/Title/LongTitle/text()').get()
        bill_type = "bill"
        triplet = bill_id[:3]
        if triplet in bill_types:
            bill_type = bill_types[triplet]
            bill_number = int(bill_id[3:].strip())
        else:
            bill_number = int(bill_id[3:])
        subs = []
        bid = bill_id.replace(" ", "")

        if bid in self.subject_mapping:
            subs = self.subject_mapping[bid]
            self.logger.info("With subjects for this bill")

        if bill_desc == "":
            if bill_number <= 20:
                # blank bill titles early in session are approp. bills
                bill_desc = "Appropriations Bill"
            else:
                self.logger.error(
                    "Blank title. Skipping. {} / {} / {}".format(
                        bill_id, bill_desc, official_title
                    )
                )
                return

        bill = Bill(
            identifier=bill_id,
            title=bill_desc,
            chamber="lower",
            legislative_session=self.session,
            classification=bill_type
        )

        bill.subject = subs
        bill.add_title(official_title, note="official")
        bill_url = f'https://www.house.mo.gov/BillContent.aspx?bill={bid}&year={bill_year}&code={bill_code}&style=new'
        bill.add_source(bill_url)

        # add sponsors
        self.parse_house_sponsors(response, bill_id, bill)
        # add actions
        votes = self.parse_house_actions(response, bill)
        # yield if there are votes in the actions
        for vote in votes:
            yield VoteEventItem(vote)
        # add bill versions
        self.parse_house_bill_versions(response, bill)

        yield BillItem(bill)

    # Get house sponsors
    def parse_house_sponsors(self, response, bill_id, bill):
        bill_sponsors = response.xpath("//BillInformation/Sponsor")
        for sponsor in bill_sponsors:
            sponsor_type = sponsor.xpath("./SponsorType/text()")[0]
            if sponsor_type == "Co-Sponsor":
                classification = "cosponsor"
                primary = False
            elif sponsor_type == "Sponsor":
                classification = "primary"
                primary = True
            elif sponsor_type == "HouseConferee" or sponsor_type == "SenateConferee":
                # these appear not to be actual sponsors of the bill but rather people who will
                # negotiate differing versions of the bill in a cross-chamber conference
                continue
            elif sponsor_type == "Handler":
                # slightly distinct from sponsor: The member who manages a bill on the floor of the House or Senate.
                # https://house.mo.gov/billtracking/info/glossary.htm
                # we decided to consider this a "cosponsor" relationship
                classification = "cosponsor"
                primary = False
            else:
                # didn't recognize sponsorship type, so we can't make this a sponsor
                # as classification is required (cannot be empty string)
                continue

            bill_sponsor = sponsor.xpath("./FullName/text()")[0]
            if bill_sponsor == "" and "HEC" in bill_id:
                bill.add_sponsorship(
                    "Petition", entity_type="", classification="primary", primary=True
                )
            else:
                bill.add_sponsorship(
                    bill_sponsor,
                    entity_type="person",
                    classification=classification,
                    primary=primary,
                )

    # Get the house bill actions and return the possible votes
    def parse_house_actions(self, response, bill):
        # add actions
        bill_actions = response.xpath('//BillInformation/Action')
        old_action_url = ''
        votes = []

        for action in bill_actions:
            action_url = action.xpath(
                './Link/text()').get().replace('.aspx', 'actions.aspx').strip()
            # the correct action description in the website is the combination of
            # Description, Comments, and RollCall
            # = Description - Comments - RollCall
            action_title = action.xpath('./Description/text()').get()
            # if there is comments
            if action.xpath('./Comments'):
                action_comment = action.xpath('./Comments/text()').get()
                action_title = f'{action_title} - {action_comment}'
            action_date = dt.datetime.strptime(
                action.xpath('./PubDate/text()').get(), '%Y-%m-%d')
            actor = house_get_actor_from_action(action_title)
            type_class = get_action(actor, action_title)

            # if there is rollcall
            if action.xpath('./RollCall'):
                rc_yes = action.xpath('./RollCall/TotalYes/text()').get('')
                rc_no = action.xpath('./RollCall/TotalNo/text()').get('')
                rc_present = action.xpath(
                    './RollCall/TotalPresent/text()').get('')
                action_title = f'{action_title} - AYES: {rc_yes} NOES: {rc_no} PRESENT: {rc_present}'

                vote = VoteEvent(
                    chamber=actor,
                    motion_text=action_title,
                    result="pass" if rc_yes > rc_no else "fail",
                    classification="passage",
                    start_date=TIMEZONE.localize(action_date),
                    bill=bill,
                )

                vote.add_source(action_url)
                votes.append(vote)

            bill.add_action(
                action_title,
                TIMEZONE.localize(action_date),
                chamber=actor,
                classification=type_class,
            )
            if old_action_url != action_url:
                bill.add_source(action_url)

            old_action_url = action_url

            # get journals (uncomments if this script needs)
            # journal_link = action.xpath('./JournalLink/text()').get()
            # if journal_link:
            #     house_journal_start = action.xpath(
            #         './HouseJournalStartPage/text()').get()
            #     senate_journal_start = action.xpath(
            #         './SenateJournalStartPage/text()').get()
            #     house_journal_end = action.xpath(
            #         './HouseJournalEndPage/text()').get()
            #     senate_journal_end = action.xpath(
            #         './SenateJournalEndPage/text()').get()
            #     version = ' - '.join(list(filter(None, [house_journal_start, house_journal_end]))) or ' - '.join(
            #         list(filter(None, [senate_journal_start, senate_journal_end])))
            #     if version:
            #         version = 'S' if senate_journal_start else 'H' + version
            #     else:
            #         version = "Missing description"
            #     if 'pdf' in journal_link:
            #         mimetype = "application/pdf"
            #     else:
            #         mimetype = "text/html"
            #     bill.add_version_link(
            #         version,
            #         journal_link,
            #         media_type=mimetype,
            #         on_duplicate="ignore",
            #     )
        return votes

    # Get the house bill versions
    def parse_house_bill_versions(self, response, bill):
        # house bill text
        for row in response.xpath('//BillInformation/BillText'):
            # some rows are just broken links, not real versions
            if row.xpath('./BillTextLink/text()'):
                version = row.xpath('./DocumentName/text()').get()
                if not version:
                    version = "Missing description"
                path = row.xpath('./BillTextLink/text()').get()
                if ".pdf" in path:
                    mimetype = "application/pdf"
                else:
                    mimetype = "text/html"
                bill.add_version_link(
                    version, path, media_type=mimetype, on_duplicate="ignore"
                )

        # house bill summaries
        for row in response.xpath('//BillInformation/BillSummary'):
            version = row.xpath('./DocumentName/text()').get()
            if version:
                path = row.xpath('./SummaryTextLink/text()').get()
                summary_name = "Bill Summary ({})".format(version)
                if ".pdf" in path:
                    mimetype = "application/pdf"
                else:
                    mimetype = "text/html"
                bill.add_document_link(
                    summary_name, path, media_type=mimetype, on_duplicate="ignore"
                )

        # house bill amendments
        for row in response.xpath('//BillInformation/Amendment'):
            version = row.xpath('./AmendmentDescription/text()').get()
            path = row.xpath('./AmendmentText/text()').get().strip()
            path_name = path.split('/')[-1].replace('.pdf', '')
            summary_name = f"Amendment {version or path_name}"

            status_desc = row.xpath('./StatusDescription/text()').get()
            if status_desc:
                summary_name = f"{summary_name} ({status_desc})"

            if ".pdf" in path:
                mimetype = "application/pdf"
            else:
                mimetype = "text/html"
            bill.add_version_link(
                summary_name, path, media_type=mimetype, on_duplicate="ignore"
            )

        # house fiscal notes
        for row in response.xpath('//BillInformation/FiscalNote'):
            path = row.xpath('./FiscalNoteLink/text()').get().strip()
            version = path.split('/')[-1].replace('.pdf', '')
            summary_name = f'Fiscal Note {version}'
            if ".pdf" in path:
                mimetype = "application/pdf"
            else:
                mimetype = ""
            bill.add_document_link(
                summary_name, path, media_type=mimetype, on_duplicate="ignore"
            )

        # house Witnesses
        for row in response.xpath('//BillInformation/Witness'):
            path = row.xpath('./WitnessFormsLink/text()').get().strip()
            summary_name = f'Bill Summary (Witnesses)'
            if ".pdf" in path:
                mimetype = "application/pdf"
            else:
                mimetype = ""
            bill.add_document_link(
                summary_name, path, media_type=mimetype, on_duplicate="ignore"
            )

    # Get Senate bill page
    def parse_senate_billpage(self, response):
        # get all the info needed to record the bill
        bill_url = response.url
        bill_id = response.xpath('//*[@id="lblBillNum"]//text()').get()
        bill_title = response.xpath('//*[@id="lblBillTitle"]//text()').get()
        bill_desc = response.xpath('//*[@id="lblBriefDesc"]//text()').get()
        # bill_lr = response.xpath('//*[@id="lblLRNum"]/text()').get()

        bill_type = "bill"
        triplet = bill_id[:3]
        if triplet in bill_types:
            bill_type = bill_types[triplet]

        subs = []
        bid = bill_id.replace(" ", "")

        if bid in self.subject_mapping:
            subs = self.subject_mapping[bid]

        if bid == "XXXXXX":
            self.logger.warning(f"Skipping Junk Bill {bid}")
            return

        bill = Bill(
            bill_id,
            title=bill_desc,
            chamber="upper",
            legislative_session=self.session,
            classification=bill_type,
        )
        bill.subject = subs
        bill.add_abstract(bill_desc, note="abstract")
        bill.add_source(bill_url)

        if bill_title:
            bill.add_title(bill_title)

        # Get the primary sponsor
        bill_sponsor = response.xpath(
            '//a[@id="hlSponsor"]//text()').get() or response.xpath('//span[@id="lSponsor"]//text()').get()

        bill.add_sponsorship(
            bill_sponsor, entity_type="person", classification="primary", primary=True
        )

        # Get the amendment links
        amendment_links = response.xpath(
            '//a[contains(@href,"ShowAmendment.asp")]')
        for link in amendment_links:
            link_text = link.xpath(".//text()").get().strip()
            if not link_text:
                link_text = "Missing description"
            if "adopted" in link_text.lower():
                link_url = response.urljoin(link.xpath("@href").get())
                bill.add_version_link(
                    link_text,
                    link_url,
                    media_type="application/pdf",
                    on_duplicate="ignore",
                )

        # Get consponsors
        # This begins a chain of subsequent requests for: sponsors, actions, versions, fiscal notes
        # all requests are required to yield the complete Bill
        cosponsor_url = response.url.replace('Bill.aspx', 'CoSponsors.aspx')
        yield Request(
            url=cosponsor_url,
            callback=self.parse_senate_cosponsors,
            cb_kwargs=dict(bill=bill)
        )

    # Get senate cosponsors
    def parse_senate_cosponsors(self, response, bill):
        # cosponsors are all in a table
        cosponsor_rows = response.xpath('//table[@id="dgCoSponsors"]/tr/td/a')
        if len(cosponsor_rows) > 0:
            bill.add_source(response.url)
        for cosponsor_row in cosponsor_rows:
            # cosponsors include district, so parse that out
            cosponsor_string = cosponsor_row.xpath('.//text()').get()
            cosponsor = clean_text(cosponsor_string)
            cosponsor = cosponsor.split(",")[0]
            # they give us a link to the congressperson, so we might
            # as well keep it.
            if cosponsor_row.xpath('@href').get():
                # cosponsor_url = cosponsor_row.attrib['href']
                bill.add_sponsorship(
                    cosponsor,
                    entity_type="person",
                    classification="cosponsor",
                    primary=False,
                )
            else:
                bill.add_sponsorship(
                    cosponsor,
                    entity_type="person",
                    classification="cosponsor",
                    primary=False,
                )

        action_url = response.url.replace('CoSponsors.aspx', 'Actions.aspx')
        yield Request(
            url=action_url,
            callback=self.parse_senate_actions,
            cb_kwargs=dict(bill=bill)
        )

    # Get senate actions
    def parse_senate_actions(self, response, bill):
        bigtable = response.xpath(
            "/html/body/font/form/table/tr[3]/td/div/table/tr"
        )
        if len(bigtable) > 0:
            bill.add_source(response.url)

        for row in bigtable:
            date = row.xpath('td[1]//text()').get()
            date = dt.datetime.strptime(date, "%m/%d/%Y")
            action = row.xpath('td[2]//text()').get()
            actor = senate_get_actor_from_action(action)
            type_class = get_action(actor, action)
            bill.add_action(
                action,
                TIMEZONE.localize(date),
                chamber=actor,
                classification=type_class,
            )

        versions_url = response.url.replace('Actions.aspx', 'BillText.aspx')
        yield Request(
            url=versions_url,
            callback=self.parse_senate_bill_versions,
            cb_kwargs=dict(bill=bill)
        )

    # Get senate bill versions
    def parse_senate_bill_versions(self, response, bill):
        version_tags = response.xpath("//li/font/a")
        # some pages are updated and use different structure
        if not version_tags:
            version_tags = response.xpath("//tr/td")
        if len(version_tags) > 0:
            bill.add_source(response.url)

        for version_tag in version_tags:
            description = version_tag.xpath('.//text()').get()
            pdf_url = version_tag.xpath('./a/@href').get()
            if description == "" and "intro" in pdf_url:
                description = "Introduced"
            elif not description:
                description = "Missing description"

            if pdf_url.endswith("pdf"):
                mimetype = "application/pdf"
            else:
                mimetype = None
            bill.add_version_link(
                description, pdf_url, media_type=mimetype, on_duplicate="ignore"
            )

        fiscal_url = response.url.replace(
            'BTS_Web/BillText.aspx', 'BTS_FiscalNotes/index.aspx')
        yield Request(
            url=fiscal_url,
            callback=self.parse_senate_fiscal_notes,
            cb_kwargs=dict(bill=bill)
        )

    # Get senate fiscal notes
    def parse_senate_fiscal_notes(self, response, bill):
        fiscal_notes = response.xpath("//tr/td/a")
        if len(fiscal_notes) > 0:
            bill.add_source(response.url)
        for fiscal_note in fiscal_notes:
            description = fiscal_note.xpath('.//text()').get().strip()
            pdf_url = fiscal_note.xpath('@href').get()
            if description == "" and "intro" in pdf_url:
                description = "Introduced"
            elif not description:
                description = "Missing description"

            if pdf_url.endswith("pdf"):
                mimetype = "application/pdf"
            else:
                mimetype = None

            bill.add_document_link(
                description, pdf_url, media_type=mimetype, on_duplicate="ignore"
            )

        yield BillItem(bill)

    # Get session type for senate
    def get_session_type(self, session):
        # R or S1
        if len(session) == 4:
            return "R"
        elif "S1" in session:
            return "E1"
        elif "S2" in session:
            return "E2"
        else:
            raise UnrecognizedSessionType(session)

    # Get session code for house
    def get_session_code(self, session):
        # R or S1
        year = session[2:]
        if len(session) == 4:
            return f"{year}1"
        elif "S1" in session:
            return f"{year}3"
        elif "S2" in session:
            return f"{year}4"
        else:
            raise UnrecognizedSessionType(session)

    # Scrape subjects
    def parse_subjects(self, session):
        self.parse_senate_subjects(session)
        if "S" in session:
            self.logger.warning("skipping house subjects for special session")
        else:
            self.parse_house_subjects(session)

    # Scrape senate subjects
    def parse_senate_subjects(self, session):
        self.logger.info("Collecting subject tags from senate.")

        subject_list_url = (
            "http://www.senate.mo.gov/{}info/BTS_Web/"
            "Keywords.aspx?SessionType={}".format(
                session[2:4], self.get_session_type(session)
            )
        )
        subject_page = lxmlize(subject_list_url)

        # Create a list of all possible bill subjects.
        for subject in subject_page.xpath('//h4'):
            subject_text = subject.xpath(
                "./a[string-length(text()) > 0]/text()[normalize-space()]").get() or ''
            if not subject_text:
                continue
            subject_text = re.sub(r"([\s]*\([0-9]+\)$)", "", subject_text)

            # Bills are in hidden spans after the subject labels.
            bill_ids = subject.xpath(
                "./following-sibling::span[1]/a[contains(@href, 'BillID')]/text()[normalize-space()]").getall()

            for bill_id in bill_ids:
                self.subject_mapping[bill_id].add(subject_text)

    # Scrape house subjects
    def parse_house_subjects(self, session):
        self.logger.info("Collecting subject tags from lower house.")

        subject_list_url = "https://house.mo.gov/LegislationSP.aspx?code=R&category=subjectindex&year={}".format(
            session
        )
        self.logger.info(subject_list_url)
        subject_page = lxmlize(subject_list_url)

        # Create a list of all the possible bill subjects.
        subjects = subject_page.xpath(
            '//div[@id="ExpandedPanel3"]/div[@class="panelCell"]/a',
        )

        # Find the list of bills within each subject.
        for subject in subjects:
            subject_text = subject.xpath('@id').get('').strip()
            self.logger.info(f"Searching for bills in {subject_text}.")
            subject_page = lxmlize(
                'https://house.mo.gov/' + subject.xpath('@href').get())
            bill_nodes = subject_page.xpath(
                '//table[@id="reportgrid"]/tbody/tr[@class="reportbillinfo"]')

            # Move onto the next subject if no bills were found.
            if bill_nodes is None or not (len(bill_nodes) > 0):
                continue

            for bill_node in bill_nodes:
                bill_id = bill_node.xpath(
                    "./td[1]/a/text()[normalize-space()]").get()
                # Skip to the next bill if no ID could be found.
                if bill_id is None or not (len(bill_id) > 0):
                    continue
                self.logger.info("Found {}.".format(bill_id))
                self.subject_mapping[bill_id].add(subject_text)
