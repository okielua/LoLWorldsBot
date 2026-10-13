TIMEZONE_REGIONS = {
    "Europe": [
        ("Amsterdam", "Europe/Amsterdam"),
        ("Berlin / Frankfurt", "Europe/Berlin"),
        ("Brussels", "Europe/Brussels"),
        ("Dublin", "Europe/Dublin"),
        ("Istanbul", "Europe/Istanbul"),
        ("Lisbon", "Europe/Lisbon"),
        ("London", "Europe/London"),
        ("Madrid", "Europe/Madrid"),
        ("Paris", "Europe/Paris"),
        ("Rome", "Europe/Rome"),
        ("Stockholm", "Europe/Stockholm"),
        ("Warsaw", "Europe/Warsaw"),
    ],
    "North America": [
        ("Los Angeles", "America/Los_Angeles"),
        ("Vancouver", "America/Vancouver"),
        ("Denver", "America/Denver"),
        ("Phoenix", "America/Phoenix"),
        ("Chicago", "America/Chicago"),
        ("Winnipeg", "America/Winnipeg"),
        ("Mexico City", "America/Mexico_City"),
        ("Toronto", "America/Toronto"),
        ("New York", "America/New_York"),
        ("Halifax", "America/Halifax"),
        ("Anchorage", "America/Anchorage"),
        ("Honolulu", "Pacific/Honolulu"),
    ],
    "Asia": [
        ("Seoul", "Asia/Seoul"),
        ("Tokyo / Osaka", "Asia/Tokyo"),
        ("Shanghai / Beijing", "Asia/Shanghai"),
        ("Hong Kong", "Asia/Hong_Kong"),
        ("Taipei", "Asia/Taipei"),
        ("Singapore", "Asia/Singapore"),
        ("Bangkok", "Asia/Bangkok"),
        ("Jakarta", "Asia/Jakarta"),
        ("Manila", "Asia/Manila"),
        ("Kuala Lumpur", "Asia/Kuala_Lumpur"),
        ("Delhi", "Asia/Kolkata"),
        ("Dubai", "Asia/Dubai"),
    ],
    "South America": [
        ("São Paulo", "America/Sao_Paulo"),
        ("Buenos Aires", "America/Argentina/Buenos_Aires"),
        ("Santiago", "America/Santiago"),
        ("Lima", "America/Lima"),
        ("Bogotá", "America/Bogota"),
        ("Caracas", "America/Caracas"),
        ("Montevideo", "America/Montevideo"),
    ],
    "Oceania": [
        ("Sydney", "Australia/Sydney"),
        ("Melbourne", "Australia/Melbourne"),
        ("Brisbane", "Australia/Brisbane"),
        ("Perth", "Australia/Perth"),
        ("Adelaide", "Australia/Adelaide"),
        ("Auckland", "Pacific/Auckland"),
        ("Fiji", "Pacific/Fiji"),
    ],
    "Africa / Middle East": [
        ("Cairo", "Africa/Cairo"),
        ("Johannesburg", "Africa/Johannesburg"),
        ("Lagos", "Africa/Lagos"),
        ("Casablanca", "Africa/Casablanca"),
        ("Nairobi", "Africa/Nairobi"),
        ("Jerusalem / Tel Aviv", "Asia/Jerusalem"),
        ("Doha", "Asia/Qatar"),
        ("Kuwait City", "Asia/Kuwait"),
        ("Riyadh", "Asia/Riyadh"),
        ("Tehran", "Asia/Tehran"),
    ],
    "UTC": [
        ("UTC", "UTC"),
    ],
}


def get_timezone_label(
    timezone_name: str,
) -> str:
    for locations in TIMEZONE_REGIONS.values():
        for label, value in locations:
            if value == timezone_name:
                return label

    return timezone_name