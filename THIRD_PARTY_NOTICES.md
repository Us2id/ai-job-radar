# Third-party data and software

This project bundles or depends on the following. Their licenses apply to those parts.

## Bundled data

- **OpenJobs company dataset** (`ai_job_radar/data/seeds/openjobs_boards.json` is derived from
  `data/companies_v2.json` of https://github.com/outscal/OpenJobs). MIT License, Copyright (c) the OpenJobs
  contributors. Only the company name, website, ATS board link and hiring countries were kept.
- **GeoNames** (`ai_job_radar/data/geo.json.gz` is derived from `cities15000.txt` and `countryInfo.txt`,
  https://www.geonames.org). Creative Commons Attribution 4.0 License (https://creativecommons.org/licenses/by/4.0/).

## Python dependencies (installed, not vendored)

- python-jobspy (MIT) — job-site scraping for LinkedIn, Indeed, Glassdoor, Naukri and regional boards
- requests (Apache 2.0), openpyxl (MIT), PyYAML (MIT), rapidfuzz (MIT), python-dateutil (Apache 2.0 / BSD),
  pycountry (LGPL 2.1; ISO country and subdivision names)
