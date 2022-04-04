import enum
from typing import Dict, List, Tuple, Union

from pydantic import BaseModel


class IncludeChange(enum.Enum):
    ADD = "add"
    REMOVE = "remove"

    @classmethod
    def from_value(cls, value):
        for enum_value in cls:
            if enum_value.value == value:
                return enum_value


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
