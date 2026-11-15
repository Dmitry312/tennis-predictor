from tennis_predictor.ingestion.telegram_auth import authorized_command, is_authorized_update


def update(chat_id=123, user_id=123, chat_type="private", text="/status"):
    return {"message": {"chat": {"id": chat_id, "type": chat_type},
                        "from": {"id": user_id, "is_bot": False}, "text": text}}


def test_owner_private_chat_only():
    assert is_authorized_update(update(), "123", "123")
    assert authorized_command(update(), "123", "123") == "/status"


def test_rejects_other_user_even_in_allowed_chat():
    assert not is_authorized_update(update(user_id=999), "123", "123")


def test_rejects_other_chat_and_groups():
    assert not is_authorized_update(update(chat_id=999), "123", "123")
    assert not is_authorized_update(update(chat_type="group"), "123", "123")


def test_rejects_missing_config_and_other_updates():
    assert not is_authorized_update(update(), "", "123")
    assert not is_authorized_update({"callback_query": {}}, "123", "123")
    assert authorized_command(update(text="hello"), "123", "123") is None
