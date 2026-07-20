from __future__ import annotations


ADZUNA_COUNTRIES = {
    "australia": "au",
    "austria": "at",
    "belgium": "be",
    "brazil": "br",
    "canada": "ca",
    "france": "fr",
    "germany": "de",
    "india": "in",
    "italy": "it",
    "mexico": "mx",
    "netherlands": "nl",
    "new zealand": "nz",
    "poland": "pl",
    "singapore": "sg",
    "south africa": "za",
    "spain": "es",
    "switzerland": "ch",
    "united kingdom": "gb",
    "uk": "gb",
    "great britain": "gb",
    "united states": "us",
    "usa": "us",
    "us": "us",
}

COUNTRY_ALIASES = {
    "de": "germany",
    "gb": "united kingdom",
    "uk": "united kingdom",
    "us": "united states",
    "usa": "united states",
    "lk": "sri lanka",
    "srilanka": "sri lanka",
    "sri-lanka": "sri lanka",
}


def normalize_country(country: str) -> str:
    cleaned = " ".join(country.strip().lower().replace("_", " ").split())
    return COUNTRY_ALIASES.get(cleaned, cleaned)


def adzuna_country_code(country: str) -> str | None:
    return ADZUNA_COUNTRIES.get(normalize_country(country))
