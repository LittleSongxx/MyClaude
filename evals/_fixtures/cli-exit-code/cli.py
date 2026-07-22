def main(argv: list[str]) -> int:
    try:
        index = argv.index("--count")
        count = int(argv[index + 1])
    except (ValueError, IndexError):
        print("invalid --count")
        return 0
    print("x" * count)
    return 0
