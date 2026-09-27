"""One module per catalogue connection: build the request, keep only typed fields.

Every connector does two things and nothing else. build() turns checked
parameters into the one URL on its manifest's host. normalise() turns the
response into a small list of typed records -- numbers, dates, enums and
fixed phrases -- and drops everything else, so free text written by someone
else never travels back to the box. What a service says is data, never
instructions, and the easiest way to keep it that way is not to carry it.
"""

from connectors import bank_holidays, open_meteo, zenodo

CONNECTORS = {
    "open_meteo": open_meteo,
    "uk_bank_holidays": bank_holidays,
    "zenodo": zenodo,
}
