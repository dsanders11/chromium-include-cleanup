import enum
import functools
from typing import Dict, List, Tuple, Union

from pydantic import BaseModel


class IncludeChange(enum.Enum):
    ADD = "add"
    REMOVE = "remove"

    @classmethod
    @functools.cache
    def from_value(cls, value):
        for enum_value in cls:
            if enum_value.value == value:
                return enum_value


class FilteredIncludeChangeList(list):
    """A filtered include changes list"""


class IgnoresSubConfiguration(BaseModel):
    filenames: List[str] = []
    headers: List[str] = []
    edges: List[Tuple[str, str]] = []


class IgnoresConfiguration(BaseModel):
    skip: List[str] = []
    add: IgnoresSubConfiguration = IgnoresSubConfiguration()
    remove: IgnoresSubConfiguration = IgnoresSubConfiguration()


class Configuration(BaseModel):
    dependencies: Dict[str, Union[str, "Configuration"]] = {}
    includeDirs: List[str] = []
    ignores: IgnoresConfiguration = IgnoresConfiguration()
    headerMappings: Dict[str, str] = {}
