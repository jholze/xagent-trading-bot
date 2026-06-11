from notifications.telegram_commands.position_display import send_positions_snapshot


def handle(text: str) -> bool:
    if text not in ["/positions", "/status", "/balance"]:
        return False

    send_positions_snapshot()
    return True