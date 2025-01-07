import re


def remove_quotes_for_variables(query: str) -> str:
    # Regex for remove single cote on variables ($)
    return re.sub(r"'(\$[a-zA-Z_]\w*)'", r"\1", query)
