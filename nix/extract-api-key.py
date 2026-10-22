"""Read the serialized API_KEY assembly attribute without executing the PE."""

import pathlib
import sys


def extract(data):
    marker = b"\x01\x00\x07API_KEY"
    if data.count(marker) != 1:
        raise ValueError("Expected exactly one API_KEY attribute")
    offset = data.index(marker) + len(marker)
    first = data[offset]
    offset += 1
    if first < 0x80:
        length = first
    elif first < 0xC0:
        length = ((first & 0x3F) << 8) | data[offset]
        offset += 1
    elif first < 0xE0:
        length = ((first & 0x1F) << 24) | int.from_bytes(data[offset:offset + 3], "big")
        offset += 3
    else:
        raise ValueError("Invalid serialized string length")
    value = data[offset:offset + length]
    if not value or data[offset + length:offset + length + 2] != b"\x00\x00":
        raise ValueError("Empty or malformed API_KEY attribute")
    key = value.decode("ascii")
    if not key.isprintable() or any(c.isspace() for c in key):
        raise ValueError("Unexpected API_KEY format")
    return key


if __name__ == "__main__":
    pathlib.Path(sys.argv[2]).write_text(extract(pathlib.Path(sys.argv[1]).read_bytes()))
