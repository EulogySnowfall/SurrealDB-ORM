from enum import StrEnum


class OrderBy(StrEnum):
    ASC = "ASC"
    DESC = "DESC"


class Operator(StrEnum):
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
