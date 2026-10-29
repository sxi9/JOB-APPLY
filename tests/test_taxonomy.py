"""Pure tests for the career role taxonomy (no DB, no web)."""

from jobapply.roles import all_fields, field, match_title, subrole, titles_for


def test_fields_and_subroles_are_well_formed():
    fields = all_fields()
    assert len(fields) >= 12
    keys = [f.key for f in fields]
    assert len(keys) == len(set(keys))  # unique field keys
    assert "cybersecurity" in keys
    for f in fields:
        assert f.label and f.subroles
        sub_keys = [s.key for s in f.subroles]
        assert len(sub_keys) == len(set(sub_keys))  # unique within the field
        for s in f.subroles:
            assert s.titles  # every sub-role carries at least one title keyword


def test_cybersecurity_is_thorough():
    cyber = field("cybersecurity")
    assert cyber is not None
    sub_keys = {s.key for s in cyber.subroles}
    for expected in {
        "soc", "threat_intel", "incident_response", "pentest", "blue_team", "grc", "appsec",
        "cloud_security", "security_engineering", "malware_analysis", "vuln_management", "iam",
        "osint", "detection_engineering", "security_architecture",
    }:
        assert expected in sub_keys, expected


def test_titles_for_whole_field_expands_synonyms():
    t = titles_for(["cybersecurity:*"])
    for kw in ("soc analyst", "threat intelligence", "penetration testing", "incident response",
               "security engineer"):
        assert kw in t
    # Bare over-broad tokens were removed so hardware/BI/IR-adjacent titles do not false-match.
    assert "soc" not in t and "penetration" not in t and "intelligence analyst" not in t
    # A bare field key expands to the same whole-field set.
    assert titles_for(["cybersecurity"]) == t


def test_titles_for_single_subrole():
    t = titles_for(["cybersecurity:threat_intel"])
    assert "threat intelligence" in t and "cyber threat intelligence" in t
    assert "penetration" not in t  # other sub-roles are not pulled in


def test_titles_for_ignores_unknown_keys():
    assert titles_for(["nope", "cybersecurity:does_not_exist", ""]) == set()
    # An unknown sub-role under a real field contributes nothing, not the whole field.
    assert titles_for(["cybersecurity:bogus"]) == set()


def test_match_title_positive_and_negative():
    assert match_title("Cyber Threat Intelligence Analyst", ["cybersecurity:threat_intel"]) is True
    assert match_title("Frontend Engineer", ["cybersecurity:*"]) is False
    assert match_title("Security Analyst", ["cybersecurity:soc"]) is True
    assert match_title("Senior Penetration Tester", ["cybersecurity:pentest"]) is True
    # Right field, wrong sub-role selected.
    assert match_title("SOC Analyst", ["cybersecurity:pentest"]) is False


def test_match_title_respects_exclude_list():
    # "Security Guard" / "Physical Security" must not read as cybersecurity.
    assert match_title("Physical Security Guard", ["cybersecurity:*"]) is False
    assert match_title("Building Security Officer", ["cybersecurity"]) is False


def test_match_title_word_boundaries():
    # "insecurity" / "insect" must not trip the "soc"/"security" synonyms.
    assert match_title("Insecurity Researcher", ["cybersecurity:*"]) is False
    assert match_title("Associate Consultant", ["cybersecurity:soc"]) is False  # "soc" inside "associate"


def test_match_title_across_several_fields():
    sel = ["cybersecurity:soc", "software_engineering:backend"]
    assert match_title("Backend Engineer", sel) is True
    assert match_title("SOC Analyst", sel) is True
    assert match_title("Product Manager", sel) is False


def test_match_title_no_false_positive_from_broad_tokens():
    # Regression: over-broad bare tokens used to pull unrelated jobs into the cybersecurity queue.
    # System-on-Chip hardware role must not match the SOC analyst sub-role.
    assert match_title("SoC Design Engineer", ["cybersecurity:soc"]) is False
    assert match_title("SoC Design Engineer", ["cybersecurity:*"]) is False
    # Cross-field false positives for a user who selected the whole cybersecurity field.
    assert match_title("Business Intelligence Analyst", ["cybersecurity:*"]) is False
    assert match_title("Market Penetration Manager", ["cybersecurity:*"]) is False
    assert match_title("IR Analyst", ["cybersecurity:*"]) is False  # Investor Relations, not incident response
    # The real security titles these came from still match.
    assert match_title("SOC Analyst", ["cybersecurity:soc"]) is True
    assert match_title("Threat Intelligence Analyst", ["cybersecurity:threat_intel"]) is True
    assert match_title("Penetration Tester", ["cybersecurity:pentest"]) is True
    assert match_title("Incident Response Analyst", ["cybersecurity:incident_response"]) is True


def test_match_title_bare_po_token_removed():
    # Regression: bare "po" matched "PO Box", purchase orders, etc.
    assert match_title("PO Box Coordinator", ["product:product_owner"]) is False
    assert match_title("Product Owner", ["product:product_owner"]) is True


def test_subrole_lookup():
    assert subrole("cybersecurity:soc").label == "SOC analyst"
    assert subrole("cybersecurity:nope") is None
    assert subrole("") is None
