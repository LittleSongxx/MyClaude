def parse_record(line: str) -> list[str]:
    return line.rstrip("\n").split(",")
