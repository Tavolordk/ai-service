from app.models import ChatRequest


def test_thinking_defaults_false():
    req = ChatRequest(profile={"profileId": "1"}, question="Resume este perfil")
    assert req.thinking is False
    assert req.mode == "auto"


def test_profile_id_numeric_is_accepted_as_string():
    req = ChatRequest(profile={"profileId": 123}, question="Prueba")
    assert req.profile.profileId == "123"
