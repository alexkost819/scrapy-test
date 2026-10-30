import inspect
from typing import Any, Union
from dataclasses import dataclass
from enum import Enum
from openstates.scrape import Bill, State, Organization, VoteEvent
from openstates.scrape.base import BaseModel


class Chamber(str, Enum):
    UPPER = "upper"
    LOWER = "lower"
    LEGISLATURE = "legislature"
    EXECUTIVE = "executive"


bill_stub_schema = {
    "type": "object",
    "properties": {
        "source_url": {"type": "string", "minLength": 1},
        "identifier": {"type": "string", "minLength": 1},
        "legislative_session": {"type": "string", "minLength": 1},
        "chamber": {"type": "string", "enum": Chamber},
        "extras": {"type": "object"},
    },
}


class BillStub(BaseModel):
    _type = "bill_stub"
    _schema = bill_stub_schema

    def __init__(
            self,
            source_url,
            identifier,
            legislative_session,
            chamber,
    ):
        super(BillStub, self).__init__()

        self.source_url = source_url
        self.identifier = identifier
        self.legislative_session = legislative_session
        self.chamber = chamber

    def __str__(self):
        return self.identifier + " in " + self.legislative_session


class BaseItem:
    def __init__(self, item: Any):
        # item should be Openstates class objects like Bill, State, Organization, VoteEvent
        if not item:
            raise TypeError(
                'item argument of BaseItem should\'t be empty.')
        for name, value in inspect.getmembers(item):
            if name.startswith('__'):
                continue
            self.__setattr__(name,  value)


@dataclass
class BillItem(BaseItem):
    file_urls: list
    files: list

    def __init__(self, item: Union[Bill, None] = None):
        # handle files that can be downloaded
        if type(item) is Bill:
            self.file_urls = []
            for version in item.versions:
                for link in version["links"]:
                    self.file_urls.append(link["url"])

        super().__init__(item)

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({self._id})'


@dataclass
class BillStubItem(BaseItem):
    source_url: str
    identifier: str
    legislative_session: str
    chamber: Chamber

    def __init__(self, item: Union[BillStub, None] = None):
        super().__init__(item)

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({self._id})'


@dataclass
class StateItem(BaseItem):
    def __init__(self, item: Union[State, None] = None):
        super().__init__(item)

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({self._id})'


@dataclass
class OrganizationItem(BaseItem):
    def __init__(self, item: Union[Organization, None] = None):
        super().__init__(item)

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({self._id})'


@dataclass
class VoteEventItem(BaseItem):
    def __init__(self, item: Union[VoteEvent, None] = None):
        super().__init__(item)

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({self._id})'
