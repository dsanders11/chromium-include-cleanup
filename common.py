import enum


class IncludeChange(enum.Enum):
    ADD = "add"
    REMOVE = "remove"

    @classmethod
    def from_value(cls, value):
        for enum_value in cls:
            if enum_value.value == value:
                return enum_value
