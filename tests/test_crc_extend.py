"""CR-C: atbildes termiņa pagarināšana (tracker/CR-C.md). Viena rinda = viens tests."""

import logging
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import clock, storage
from app.main import app

TODAY = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
REASON = "Jāsaņem būvvaldes atzinums"

# Sintētiski dati. Personas kods neatbilst reālai personai.
PERSON = {
    "personalCode": "32000000901",
    "fullName": "Ilze Kalniņa",
    "email": "ilze.kalnina@example.com",
    "preferredChannel": "EMAIL",
    "topic": "PLANNING",
    "subject": "Būvatļauja šķūnim",
    "body": "Lūdzu, izskatiet manu būvatļaujas pieprasījumu šķūnim Ezera ielā 7.",
    "replyChannel": "EMAIL",
    "reasonCode": None,
}


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    # Laiku aizstāj, lai "šodiena" testā būtu zināma.
    monkeypatch.setattr(clock, "now", lambda: TODAY)


def _submission(
    status="RECEIVED", received="2026-09-25T13:40:00+00:00", due="2026-10-26"
):
    record = {**PERSON, "status": status, "receivedAt": received, "dueDate": due}
    return storage.add(record)["id"]


def _extend(client, submission_id, new_due_date, reason=REASON):
    return client.post(
        f"/submissions/{submission_id}/extend",
        json={"newDueDate": new_due_date, "reason": reason},
    )


def _due_date(client, submission_id):
    return client.get(f"/submissions/{submission_id}").json()["dueDate"]


def _assert_error(response, status, code):
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("status", ["RECEIVED", "IN_PROGRESS"])
def test_crc_ac1_extend_ok(client, status):
    submission_id = _submission(status=status)
    response = _extend(client, submission_id, "2026-12-15")
    assert response.status_code == 200
    assert response.json()["dueDate"] == "2026-12-15"
    assert _due_date(client, submission_id) == "2026-12-15"


def test_crc_ac2_exactly_4_months_ok(client):
    submission_id = _submission(received="2026-09-25T13:40:00+00:00")
    response = _extend(client, submission_id, "2027-01-25")
    assert response.status_code == 200
    assert response.json()["dueDate"] == "2027-01-25"


def test_crc_ac3_over_4_months_400(client):
    submission_id = _submission(received="2026-09-25T13:40:00+00:00", due="2026-10-26")
    response = _extend(client, submission_id, "2027-01-26")
    _assert_error(response, 400, "INVALID_DUE_DATE")
    assert _due_date(client, submission_id) == "2026-10-26"


@pytest.mark.parametrize("new_due_date", ["2026-10-26", "2026-10-25"])
def test_crc_ac4_not_later_than_due_400(client, new_due_date):
    submission_id = _submission(due="2026-10-26")
    response = _extend(client, submission_id, new_due_date)
    _assert_error(response, 400, "INVALID_DUE_DATE")
    assert _due_date(client, submission_id) == "2026-10-26"


@pytest.mark.parametrize("status", ["FORWARDED", "ANSWERED", "WITHDRAWN"])
def test_crc_ac5_wrong_status_409(client, status):
    submission_id = _submission(status=status, due="2026-10-26")
    response = _extend(client, submission_id, "2026-12-15")
    _assert_error(response, 409, "INVALID_STATE")
    assert _due_date(client, submission_id) == "2026-10-26"


def test_crc_ac6_unknown_id_404(client):
    response = _extend(client, "IES-2026-999999", "2026-12-15")
    _assert_error(response, 404, "NOT_FOUND")


@pytest.mark.parametrize(
    "body, field",
    [
        ({"reason": REASON}, "newDueDate"),
        ({"newDueDate": "2026-12-15"}, "reason"),
        ({"newDueDate": "15.12.2026", "reason": REASON}, "newDueDate"),
        ({"newDueDate": "2026-02-30", "reason": REASON}, "newDueDate"),
        ({"newDueDate": "1797292800", "reason": REASON}, "newDueDate"),
        ({"newDueDate": "2026-12-15T00:00:00Z", "reason": REASON}, "newDueDate"),
        ({"newDueDate": "2026-12-15", "reason": "a" * 9}, "reason"),
        ({"newDueDate": "2026-12-15", "reason": "a" * 501}, "reason"),
        ({"newDueDate": "2026-12-15", "reason": " " * 12}, "reason"),
        ({"newDueDate": "2026-12-15", "reason": "  " + "a" * 9 + "  "}, "reason"),
    ],
)
def test_crc_ac7_validation_400(client, body, field):
    submission_id = _submission(due="2026-10-26")
    response = client.post(f"/submissions/{submission_id}/extend", json=body)
    _assert_error(response, 400, "VALIDATION_ERROR")
    assert field in [detail["field"] for detail in response.json()["error"]["details"]]
    assert _due_date(client, submission_id) == "2026-10-26"


@pytest.mark.parametrize("length", [10, 500])
def test_crc_ac7_reason_boundaries_ok(client, length):
    submission_id = _submission()
    response = _extend(client, submission_id, "2026-12-15", reason="a" * length)
    assert response.status_code == 200


def test_crc_reason_outer_spaces_removed(client):
    submission_id = _submission()
    response = _extend(client, submission_id, "2026-12-15", reason=f"  {REASON}  ")
    assert response.status_code == 200
    audit = client.get(f"/submissions/{submission_id}/audit").json()
    assert audit[-1]["detail"] == REASON


def test_crc_ac8_audit_extend_with_reason(client):
    submission_id = _submission()
    _extend(client, submission_id, "2026-12-15")
    audit = client.get(f"/submissions/{submission_id}/audit").json()
    assert {"at": "2026-10-05T12:00:00Z", "action": "EXTEND", "detail": REASON} in audit


def test_crc_ac9_no_personal_data_in_logs_or_errors(client, caplog):
    submission_id = _submission()
    forwarded_id = _submission(status="FORWARDED")
    with caplog.at_level(logging.DEBUG):
        responses = [
            _extend(client, submission_id, "2026-12-15"),
            _extend(client, submission_id, "2027-01-26"),
            _extend(client, submission_id, "nav-datums"),
            _extend(client, forwarded_id, "2026-12-15"),
            _extend(client, "IES-2026-999999", "2026-12-15"),
        ]
    assert [response.status_code for response in responses] == [200, 400, 400, 409, 404]
    errors = " ".join(response.text for response in responses[1:])
    for value in ("personalCode", "fullName", "email", "body"):
        assert PERSON[value] not in caplog.text
        assert PERSON[value] not in errors


# Atvērtais jautājums: ja mērķa mēnesī nav saņemšanas dienas datuma,
# robeža ir mēneša pēdējā diena (pieņēmums, tāpat kā docs/requirements.md).
@pytest.mark.parametrize(
    "received, due, new_due_date, expected",
    [
        ("2026-05-31T07:20:00+00:00", "2026-06-30", "2026-09-30", 200),
        ("2026-05-31T07:20:00+00:00", "2026-06-30", "2026-10-01", 400),
        ("2026-10-31T09:00:00+00:00", "2026-11-30", "2027-02-28", 200),
        ("2026-10-31T09:00:00+00:00", "2026-11-30", "2027-03-01", 400),
        ("2027-10-31T09:00:00+00:00", "2027-11-30", "2028-02-29", 200),
        ("2027-10-31T09:00:00+00:00", "2027-11-30", "2028-03-01", 400),
    ],
)
def test_crc_open_question_month_end(
    client, monkeypatch, received, due, new_due_date, expected
):
    monkeypatch.setattr(clock, "now", lambda: datetime.fromisoformat(received))
    submission_id = _submission(received=received, due=due)
    response = _extend(client, submission_id, new_due_date)
    assert response.status_code == expected


# Pieņēmums: jaunais termiņš nedrīkst būt pagātnē. Šodiena ir atļauta.
def test_crc_past_date_400(client):
    submission_id = _submission(
        status="IN_PROGRESS", received="2026-08-20T10:00:00+00:00", due="2026-09-21"
    )
    response = _extend(client, submission_id, "2026-10-04")
    _assert_error(response, 400, "INVALID_DUE_DATE")
    assert _due_date(client, submission_id) == "2026-09-21"


def test_crc_today_ok(client):
    submission_id = _submission(
        status="IN_PROGRESS", received="2026-08-20T10:00:00+00:00", due="2026-09-21"
    )
    response = _extend(client, submission_id, "2026-10-05")
    assert response.status_code == 200


# Precizējums: termiņu drīkst pagarināt vairākas reizes.
def test_crc_extend_twice_ok(client):
    submission_id = _submission()
    assert _extend(client, submission_id, "2026-11-30").status_code == 200
    response = _extend(client, submission_id, "2027-01-25")
    assert response.status_code == 200
    assert response.json()["dueDate"] == "2027-01-25"
    actions = [
        entry["action"]
        for entry in client.get(f"/submissions/{submission_id}/audit").json()
    ]
    assert actions == ["EXTEND", "EXTEND"]


# Atradums: 500 atbildē nedrīkst būt iekšējā informācija (kontrolsaraksta 5. punkts).
def test_crc_finding_500_hides_internal_details(client, monkeypatch):
    # Ieraksts pazūd starp nolasīšanu un atjaunināšanu: update_due_date met LookupError.
    record = {
        **PERSON,
        "status": "RECEIVED",
        "receivedAt": "2026-09-25T13:40:00+00:00",
        "dueDate": "2026-10-26",
    }
    monkeypatch.setattr(storage, "get", lambda submission_id: record)
    response = _extend(
        TestClient(app, raise_server_exceptions=False), "IES-2026-999999", "2026-12-15"
    )
    _assert_error(response, 500, "INTERNAL_ERROR")
    assert "sqlite" not in response.text
    assert ":memory:" not in response.text
