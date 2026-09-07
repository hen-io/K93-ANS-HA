from __future__ import annotations

from homeassistant.core import HomeAssistant, State

from .models import ChatMessage


def async_find_person_for_user(hass: HomeAssistant, user_id: str) -> State | None:
    for state in hass.states.async_all("person"):
        if state.attributes.get("user_id") == user_id:
            return state
    return None


def resolve_sender(hass: HomeAssistant, user_names: dict[str, str], message: ChatMessage) -> dict:
    if message.get("sender_user_id") is None:
        return {
            "name": message.get("sender_name") or "System",
            "icon": message.get("sender_icon") or "mdi:robot",
            "picture": None,
        }

    user_id = message["sender_user_id"]
    person = async_find_person_for_user(hass, user_id)
    name = (person.name if person else None) or user_names.get(user_id) or "Unknown user"
    picture = person.attributes.get("entity_picture") if person else None
    return {"name": name, "icon": None, "picture": picture}
