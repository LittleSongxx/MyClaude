from dataclasses import dataclass

from app.service import format_user


@dataclass
class User:
    name: str

    @property
    def label(self) -> str:
        return format_user(self)
