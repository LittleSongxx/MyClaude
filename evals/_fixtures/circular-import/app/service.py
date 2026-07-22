from app.models import User


def format_user(user: User) -> str:
    return user.name.strip().title()
