import pytest
from pydantic import ValidationError

from backend.schemas.comms import AgendaBase, InboundMessage, PresetBase, ScreenRequest, SendRequest, StationCreate


def test_station_role_and_slug_validation():
    assert StationCreate(slug="emcee-1", name="Emcee 1", role="emcee").role == "emcee"
    for bad in ({"slug": "Bad Slug", "name": "x", "role": "emcee"}, {"slug": "ok", "name": "x", "role": "dj"}, {"slug": "ok", "name": "", "role": "emcee"}):
        with pytest.raises(ValidationError):
            StationCreate(**bad)


def test_preset_role_optional_but_validated():
    assert PresetBase(label="a", message="b").role is None
    with pytest.raises(ValidationError):
        PresetBase(label="a", message="b", role="nope")


@pytest.mark.parametrize("start", ["8:30", "24:00", "09:60", "abc", ""])
def test_agenda_rejects_bad_times(start):
    with pytest.raises(ValidationError):
        AgendaBase(scope="day", start_time=start, title="x")


def test_agenda_accepts_valid_and_rejects_bad_scope():
    assert AgendaBase(scope="finals", start_time="16:00", end_time="18:00", title="Finals").end_time == "18:00"
    with pytest.raises(ValidationError):
        AgendaBase(scope="week", start_time="09:00", title="x")
    with pytest.raises(ValidationError):
        AgendaBase(scope="day", start_time="09:00", end_time="25:00", title="x")


def test_send_and_screen_requests():
    assert SendRequest(text="hi", roles=["emcee"]).flash is False
    with pytest.raises(ValidationError):
        SendRequest(text="", roles=["emcee"])
    with pytest.raises(ValidationError):
        SendRequest(text="hi", roles=["dj"])
    with pytest.raises(ValidationError):
        ScreenRequest(screen="nope")
    with pytest.raises(ValidationError):
        ScreenRequest(screen="messages", roles=["dj"])


def test_inbound_message_is_stripped_and_nonblank():
    assert InboundMessage(text="  hello ").text == "hello"
    with pytest.raises(ValidationError):
        InboundMessage(text="   ")
