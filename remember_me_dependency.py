"""Fixed Remember-Me release identity; no package import or storage initialization."""
from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class RememberMeDependency:
    version: str
    tag: str
    commit: str
    tree: str
    url: str
    sha256: str


def parse_dependency(text: str) -> RememberMeDependency:
    lines = text.splitlines()
    begin = "# BEGIN REMEMBER-ME PIN"
    end = "# END REMEMBER-ME PIN"
    if lines.count(begin) != 1 or lines.count(end) != 1:
        raise ValueError("remember_me_pin_invalid")
    start, stop = lines.index(begin), lines.index(end)
    if stop != start + 6:
        raise ValueError("remember_me_pin_invalid")
    stanza = "\n".join(lines[start:stop + 1])
    match = re.fullmatch(
        r"# BEGIN REMEMBER-ME PIN\n"
        r"# version: (0\.[0-9]+\.[0-9]+)\n"
        r"# tag: (v0\.[0-9]+\.[0-9]+)\n"
        r"# commit: ([0-9a-f]{40})\n"
        r"# tree: ([0-9a-f]{40})\n"
        r"remember-me @ (https://github\.com/peanutsuee/Remember-Me/releases/download/"
        r"v0\.[0-9]+\.[0-9]+/remember_me-0\.[0-9]+\.[0-9]+\.tar\.gz)"
        r"#sha256=([0-9a-f]{64})\n# END REMEMBER-ME PIN", stanza,
    )
    candidates = [line for line in lines if line and not line.startswith("#")
                  and re.search(r"remember[-_.]me", line, re.IGNORECASE)]
    if match is None or candidates != [lines[start + 5]]:
        raise ValueError("remember_me_pin_invalid")
    version, tag, commit, tree, url, digest = match.groups()
    expected_url = ("https://github.com/peanutsuee/Remember-Me/releases/download/"
                    f"v{version}/remember_me-{version}.tar.gz")
    if tag != f"v{version}" or url != expected_url:
        raise ValueError("remember_me_pin_invalid")
    return RememberMeDependency(version, tag, commit, tree, url, digest)


DEPENDENCY = parse_dependency(
    Path(__file__).resolve().with_name("requirements.txt").read_text(encoding="utf-8")
)
