from library import Transport


def upload(transport: Transport, data: bytes) -> tuple[bytes, float]:
    return transport.send(data, 30.0)
