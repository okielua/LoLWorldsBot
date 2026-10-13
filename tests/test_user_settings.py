from services.user_settings import (
    get_user_timezone,
    set_user_timezone,
)


def test_user_timezone_can_be_saved():
    user_id = 999999001

    set_user_timezone(
        user_id,
        "Europe/Berlin",
    )

    assert get_user_timezone(user_id) == "Europe/Berlin"


def test_user_timezone_can_be_updated():
    user_id = 999999002

    set_user_timezone(
        user_id,
        "Europe/Berlin",
    )

    set_user_timezone(
        user_id,
        "Asia/Seoul",
    )

    assert get_user_timezone(user_id) == "Asia/Seoul"