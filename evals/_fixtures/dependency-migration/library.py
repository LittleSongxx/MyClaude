class Transport:
    def send(self, payload: bytes, *, timeout: float) -> tuple[bytes, float]:
        return payload, timeout
