"""Reading the dates from the text Google Drive reads off a card (app/hr/ocr_dates.py): Oregon
Food Handler Cards, OLCC Service Permits and others, with the misreads, missing labels, birth
dates and layouts real cards come with."""

from datetime import date

from hr.ocr_dates import pick_dates

TODAY = date(2026, 10, 26)


def read(text, kind="food_handler", **extra):
    return pick_dates(text, kind, TODAY, **extra)


def warned(result, words):
    return any(words in w for w in result["warnings"])


# ---------------------------------------------------------------- Oregon Food Handler Cards

def test_county_card_with_both_labels():
    r = read("""OREGON FOOD HANDLER CARD
Multnomah County Environmental Health
Name: JANE SMITH
Card No: 1234567
Date Issued: 06/14/2026
Expiration Date: 06/14/2029
This card is valid in all Oregon counties.""", name="Jane Smith")
    assert (r["expires"], r["issued"], r["how"]) == (date(2029, 6, 14), date(2026, 6, 14), "read")
    assert r["warnings"] == []


def test_misread_letters_in_the_numbers_are_fixed():
    r = read("""Food Handler Card
Oregon
JANE SMITH
Certificate # 9988-7766
Issued O6/l4/2O26
Expires O6/14/2O29""")
    assert (r["expires"], r["issued"], r["how"]) == (date(2029, 6, 14), date(2026, 6, 14), "read")
    assert read("EXP: 1O/3l/2O28")["expires"] == date(2028, 10, 31)
    assert read("Expires: 0S/2B/2029")["expires"] == date(2029, 5, 28)


def test_spaces_and_other_separators_in_dates():
    assert read("Expiration Date 06 / 14 / 2029")["expires"] == date(2029, 6, 14)
    assert read("Expires 06-14-2029")["expires"] == date(2029, 6, 14)
    assert read("Expires 06.14.2029")["expires"] == date(2029, 6, 14)
    assert read("Expires: 6/4/29")["expires"] == date(2029, 6, 4)
    assert read("Valid through 2029-06-14")["expires"] == date(2029, 6, 14)


def test_dates_written_with_month_names():
    assert read("Expiration: June 14, 2029")["expires"] == date(2029, 6, 14)
    assert read("Expires Jun. 14th 2029")["expires"] == date(2029, 6, 14)
    assert read("EXPIRES 14 JUN 2029")["expires"] == date(2029, 6, 14)
    assert read("Exp 14-Jun-2029")["expires"] == date(2029, 6, 14)
    assert read("Good through Sept 3, 2O29")["expires"] == date(2029, 9, 3)
    r = read("Issued: October 25, 2026")
    assert (r["issued"], r["expires"], r["how"]) == (date(2026, 10, 25), date(2029, 10, 25), "calculated")


def test_labels_side_by_side_above_the_dates():
    r = read("""OREGON FOOD HANDLER CARD
Issue Date        Expiration Date
10/25/2026        10/25/2029
Card # 4455661""")
    assert (r["issued"], r["expires"], r["how"]) == (date(2026, 10, 25), date(2029, 10, 25), "read")


def test_labels_printed_under_the_dates():
    r = read("""FOOD HANDLER
06/14/2026
Issue Date
06/14/2029
Expiration Date""")
    assert (r["issued"], r["expires"], r["how"]) == (date(2026, 6, 14), date(2029, 6, 14), "read")
    r = read("""10/25/2026     10/25/2029
Issued         Expires""")
    assert (r["issued"], r["expires"]) == (date(2026, 10, 25), date(2029, 10, 25))


def test_labels_and_dates_on_one_line():
    r = read("Issued: 06/14/2026   Expires: 06/14/2029")
    assert (r["issued"], r["expires"], r["how"]) == (date(2026, 6, 14), date(2029, 6, 14), "read")
    r = read("Expiration Date: 06/14/2029 Date of Issue: 06/14/2026")
    assert (r["issued"], r["expires"]) == (date(2026, 6, 14), date(2029, 6, 14))


def test_no_expiry_label_works_it_out_from_the_issue_date():
    r = read("""Oregon Food Handler Card
Name: Maria Lopez
Date of Issue: 10/25/2026""")
    assert (r["issued"], r["expires"], r["how"]) == (date(2026, 10, 25), date(2029, 10, 25), "calculated")
    assert r["warnings"] == []
    # February 29th plus three years is February 28th.
    assert read("Issued 02/29/2024")["expires"] == date(2027, 2, 28)


def test_no_labels_at_all_two_dates_three_years_apart():
    r = read("""OREGON FOOD HANDLER CARD
MARIA LOPEZ
10/25/2026
10/25/2029""")
    assert (r["issued"], r["expires"], r["how"]) == (date(2026, 10, 25), date(2029, 10, 25), "read")
    # A day's difference still pairs up.
    assert read("10/25/2026 10/24/2029")["expires"] == date(2029, 10, 24)


def test_one_date_without_a_label():
    r = read("OREGON FOOD HANDLER CARD\nMARIA LOPEZ\n10/25/2029")
    assert (r["expires"], r["how"]) == (date(2029, 10, 25), "read")
    assert warned(r, "check it's the expiry date")
    r = read("OREGON FOOD HANDLER CARD\nMARIA LOPEZ\n10/25/2025")
    assert (r["issued"], r["expires"], r["how"]) == (date(2025, 10, 25), date(2028, 10, 25), "calculated")
    assert warned(r, "taken as the issue date")


def test_birth_dates_are_ignored():
    r = read("""OREGON FOOD HANDLER CARD
Name: JANE SMITH   DOB: 02/03/1998
Issued 06/14/2026""")
    assert (r["issued"], r["expires"], r["how"]) == (date(2026, 6, 14), date(2029, 6, 14), "calculated")
    r = read("""Date of Birth
08/09/2004
06/14/2026
06/14/2029""")
    assert (r["issued"], r["expires"]) == (date(2026, 6, 14), date(2029, 6, 14))
    r = read("D.O.B. 11/12/2007\nBirth: 11/12/2007\n11/12/2029")
    assert r["expires"] == date(2029, 11, 12) and r["issued"] is None


def test_expired_and_odd_dates_are_flagged():
    r = read("Issued: 05/01/2022\nExpires: 05/01/2025")
    assert r["expires"] == date(2025, 5, 1) and warned(r, "expired on 5/1/2025")
    r = read("Expires: 06/14/2099")
    assert r["expires"] == date(2099, 6, 14) and warned(r, "further ahead than this card lasts")
    r = read("Issued: 06/14/2026\nExpires: 06/14/2030")
    assert warned(r, "aren't 3 years apart")
    r = read("Issued: 12/01/2026\nExpires: 12/01/2029")
    assert warned(r, "issue date is in the future")


def test_nothing_found():
    for text in ("", "OREGON FOOD HANDLER CARD\nJANE SMITH", "Exp: OO/OO/OOOO", "Card 1234-56-78",
                 "Call 541-555-1234", "Expires 13/13/2029", None):
        r = read(text)
        assert (r["expires"], r["issued"], r["how"]) == (None, None, "not_found"), text


def test_the_wrong_card_and_the_wrong_person():
    r = read("OREGON LIQUOR AND CANNABIS COMMISSION\nALCOHOL SERVICE PERMIT\nExpires 03/31/2030")
    assert warned(r, "not a Food Handler Card")
    r = read("Food Handler Card\nName: JOHN DOE\nExpires 06/14/2029", name="Jane Smith")
    assert warned(r, "name wasn't found")
    assert not warned(read("FOOD HANDLER\nSMITH, JANE\nExpires 06/14/2029", name="Jane Smith"),
                      "name wasn't found")


# ---------------------------------------------------------------- OLCC Service Permits

def test_olcc_permit():
    r = read("""OREGON LIQUOR AND CANNABIS COMMISSION
ALCOHOL SERVICE PERMIT
PERMIT # 123456
NAME: JOHN DOE
DOB: 04/05/1990
ISSUED: 03/31/2025
EXPIRES: 03/31/2030""", "olcc", name="John Doe")
    assert (r["expires"], r["issued"], r["how"]) == (date(2030, 3, 31), date(2025, 3, 31), "read")
    assert r["warnings"] == []


def test_olcc_effective_date_gives_five_years():
    r = read("OLCC Service Permit\nPermit No 778899\nEffective Date: 10/20/2026", "olcc")
    assert (r["issued"], r["expires"], r["how"]) == (date(2026, 10, 20), date(2031, 10, 20), "calculated")
    r = read("OLCC SERVICE PERMIT\n10/20/2026\n10/20/2031", "olcc")
    assert (r["issued"], r["expires"], r["how"]) == (date(2026, 10, 20), date(2031, 10, 20), "read")
    # Three years apart isn't an OLCC permit's lifetime.
    r = read("OLCC SERVICE PERMIT\n10/20/2026\n10/20/2029", "olcc")
    assert r["expires"] == date(2029, 10, 20) and warned(r, "check it's the expiry date")


def test_olcc_under_21_is_flagged():
    r = read("""OREGON LIQUOR AND CANNABIS COMMISSION
SERVICE PERMIT CARD
NAME: ALEX KIM   DOB: 04/05/2006
PERMIT # 445566
Expires: 03/31/2031""", "olcc")
    assert r["expires"] == date(2031, 3, 31) and warned(r, "under 21")
    # Turning 21 before today: no warning.
    assert not warned(read("Permit # 1\nDOB: 10/25/2005\nExpires: 03/31/2031", "olcc"), "under 21")


def test_olcc_course_certificate_is_not_a_permit():
    r = read("""Certificate of Completion
Oregon Alcohol Server Education
This certifies that JOHN DOE has successfully completed the
OLCC approved Alcohol Server Education course.
Course completed: October 20, 2026
This certificate is valid for 2 years.""", "olcc")
    assert warned(r, "Server Education course certificate")
    # Its date isn't a permit's: nothing is worked out from it.
    assert (r["issued"], r["expires"], r["how"]) == (date(2026, 10, 20), None, "not_found")


def test_olcc_temporary_permit():
    r = read("""OREGON LIQUOR AND CANNABIS COMMISSION
TEMPORARY ALCOHOL SERVICE PERMIT
Permit Number: T-99123
Valid until 12/19/2026""", "olcc")
    assert r["expires"] == date(2026, 12, 19) and warned(r, "temporary permit")


def test_olcc_food_handler_card_sent_by_mistake():
    r = read("OREGON FOOD HANDLER CARD\nExpires 06/14/2029", "olcc")
    assert warned(r, "not an OLCC Service Permit")


def test_month_and_year_only_needs_an_expiry_label():
    assert read("Permit # 1\nEXP 06/2029", "olcc")["expires"] == date(2029, 6, 30)
    assert read("Permit # 1\nExpires 02/2028", "olcc")["expires"] == date(2028, 2, 29)
    assert read("Permit # 1\nIssued 06/2026", "olcc")["how"] == "not_found"


# ---------------------------------------------------------------- other certificates

def test_generic_certificates():
    r = read("CPR / First Aid\nAmerican Heart Association\nRecommended renewal date: 01/31/2028\n"
             "Expires 01/31/2028", "generic")
    assert (r["expires"], r["how"]) == (date(2028, 1, 31), "read")
    # No known lifetime: an issue date alone gives no expiry.
    r = read("Pool Operator Certificate\nIssued 10/01/2026", "generic")
    assert (r["issued"], r["expires"], r["how"]) == (date(2026, 10, 1), None, "not_found")
    r = read("Massage license\n10/01/2025\n10/01/2027", "generic")
    assert (r["issued"], r["expires"], r["how"]) == (date(2025, 10, 1), date(2027, 10, 1), "read")
    assert warned(read("Expires 01/01/2050", "generic"), "further ahead")
    # Food handler and OLCC warnings are for those cards only.
    assert read("Certificate of Completion\nhas completed\nExpires 01/31/2028", "generic")["warnings"] == []
