"""SQL for a card date as a period: imported memories may give only a year or a month."""


def period_start(column):
    return (f"(CASE length({column}) WHEN 4 THEN {column}||'-01-01' "
            f"WHEN 7 THEN {column}||'-01' ELSE left({column},10) END)")


def period_end(column):
    # '-31' compares correctly as text for every month.
    return (f"(CASE length({column}) WHEN 4 THEN {column}||'-12-31' "
            f"WHEN 7 THEN {column}||'-31' ELSE left({column},10) END)")
