from app.config import request_timeout


class Service:
    def __init__(self, settings: dict[str, int]) -> None:
        self.settings = settings

    @property
    def timeout(self) -> int:
        return request_timeout(self.settings)
