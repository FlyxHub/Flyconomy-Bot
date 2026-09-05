"""The member-facing economy guide, and how it splits into Discord messages.

The guide's text lives beside this module as ``data/economy-guide.md`` rather
than in the source, so editing it needs no Python. It ships inside the package
because that is what the Docker image installs -- the repository's ``docs/``
directory is not copied into the image, so a guide kept there could be edited
forever without the running bot ever seeing the change.

Like the rules modules, this imports nothing from ``discord``: splitting the
file and deciding whether a section changed are pure operations, and the cog
that talks to a channel is tested separately from them.
"""

from __future__ import annotations

import hashlib
import re
from importlib import resources
from typing import Final

#: Package-relative location of the guide's text.
GUIDE_RESOURCE: Final = "data/economy-guide.md"

#: Discord's hard cap on the length of one message.
MESSAGE_LIMIT: Final = 2_000

#: The line that divides one message from the next. The numbering inside it is
#: decorative -- the bot publishes sections in file order, so renumbering by
#: hand after inserting one is never needed and this pattern ignores it.
_SEPARATOR: Final = re.compile(r"^═+\s*\d+\s+of\s+\d+\s*═+$", re.MULTILINE)


def read_guide_source() -> str:
    """Return the guide's raw markdown, as shipped inside the package.

    Returns:
        The file's contents.
    """
    return (resources.files("flyconomy") / GUIDE_RESOURCE).read_text(encoding="utf-8")


def split_sections(source: str) -> tuple[str, ...]:
    """Split the guide's markdown into one string per Discord message.

    Everything before the first separator is the note to whoever maintains the
    file, so it is dropped rather than posted. Blank sections are dropped too,
    which is what makes a trailing separator harmless.

    Args:
        source: The guide's raw markdown.

    Returns:
        The sections, in the order they should appear in the channel.
    """
    blocks = _SEPARATOR.split(source)[1:]
    return tuple(stripped for block in blocks if (stripped := block.strip()))


def load_sections() -> tuple[str, ...]:
    """Read and split the packaged guide in one step.

    Returns:
        The sections, in the order they should appear in the channel.
    """
    return split_sections(read_guide_source())


def checksum(section: str) -> str:
    """Return a stable fingerprint of one section's text.

    Stored beside the message id so a restart can tell an unchanged section
    from an edited one without reading the message back from Discord. Only
    equality is ever asked of it, so the digest is truncated to keep the
    stored rows readable.

    Args:
        section: The section's text.

    Returns:
        A hex digest.
    """
    return hashlib.sha256(section.encode("utf-8")).hexdigest()[:16]
