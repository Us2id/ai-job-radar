from ai_job_radar.geo import gazetteer

g = gazetteer()


def test_resolve_country_aliases():
    assert g.resolve_country("india") == ("IN", "India")
    assert g.resolve_country("IN")[0] == "IN"
    assert g.resolve_country("U.S.")[0] == "US"
    assert g.resolve_country("UK")[0] == "GB"
    assert g.resolve_country("Deutschland")[0] == "DE"
    assert g.resolve_country("not a country at all") is None


def test_parse_indian_locations():
    l = g.parse_location("Bengaluru, Karnataka, India")
    assert (l.city, l.region, l.country, l.work_mode) == ("Bengaluru", "Karnataka", "IN", "onsite")
    assert g.parse_location("Bangalore").city == "Bengaluru"
    assert g.parse_location("Gurgaon, Haryana").city == "Gurugram"
    assert g.parse_location("KA, India").region == "Karnataka"
    assert g.parse_location("Hyderābād, India").city == "Hyderabad"
    assert g.parse_location("Kochi, Kerala").country == "IN"


def test_parse_world_locations():
    assert g.parse_location("San Jose, CA").region == "California"
    assert g.parse_location("Cambridge, MA").country == "US"
    assert g.parse_location("Cambridge").country == "GB"
    assert g.parse_location("Victoria, BC").country == "CA"
    assert g.parse_location("Lahore, Punjab").country == "PK"
    assert g.parse_location("Atlanta, Georgia").country == "US"
    assert g.parse_location("Tbilisi, Georgia").country == "GE"


def test_remote_modes():
    assert g.parse_location("Remote - India").work_mode == "remote"
    assert g.parse_location("Remote - India").country == "IN"
    assert g.parse_location("Remote (Worldwide)").work_mode == "remote-global"
    assert g.parse_location("Hybrid - Bengaluru").work_mode == "hybrid"
    assert g.parse_location("Multiple Locations").country is None


def test_remote_mentions():
    assert g.remote_mentions("Open to candidates anywhere in India", "IN")
    assert g.remote_mentions("APAC time zones preferred", "IN")
    assert g.remote_mentions("US only", "IN") is None
    assert g.remote_mentions("", "DE", location_text="Worldwide")
    assert g.remote_mentions("We are a global company with customers worldwide. Onsite in Berlin.", "IN") is None
    assert g.remote_mentions("REMOTE worldwide, any timezone", "IN")
    assert g.remote_mentions("You can work from anywhere.", "BR")
