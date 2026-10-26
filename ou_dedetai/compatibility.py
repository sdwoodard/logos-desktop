"""Wine version facts and known eligibility constraints, without runtime effects."""

from dataclasses import dataclass
from enum import StrEnum
import re

from packaging.version import Version


class WineChannel(StrEnum):
    STABLE = "stable"
    DEVELOPMENT = "devel"
    RELEASE_CANDIDATE = "rc"


class WineFlavor(StrEnum):
    UPSTREAM = "upstream"
    STAGING = "staging"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WineVersion:
    # Keep the first three fields compatible with wine.WineRelease callers.
    major: int
    minor: int
    release: str | None
    patch: tuple[int, ...] = ()
    rc: int | None = None

    @property
    def version(self) -> Version:
        number = ".".join(str(part) for part in (self.major, self.minor, *self.patch))
        if self.rc is not None:
            number += f"rc{self.rc}"
        return Version(number)

    @property
    def channel(self) -> WineChannel:
        if self.rc is not None:
            return WineChannel.RELEASE_CANDIDATE
        return WineChannel.STABLE if self.minor == 0 else WineChannel.DEVELOPMENT

    @property
    def flavor(self) -> WineFlavor:
        if self.release == "staging":
            return WineFlavor.STAGING
        if self.release in ("stable", "devel"):
            return WineFlavor.UPSTREAM
        return WineFlavor.UNKNOWN


def _parse_version(output: str, prefix: str) -> WineVersion:
    match = re.fullmatch(
        re.escape(prefix) + r"(\d+\.\d+(?:\.\d+)*)(?:-rc(\d+))?(?: \((stable|devel|staging)\))?",
        output.strip(), re.IGNORECASE,
    )
    if match is None:
        raise ValueError("Unrecognized Wine version output")

    major, minor, *patch = (int(part) for part in match[1].split("."))
    rc = int(match[2]) if match[2] is not None else None
    if major == 0:
        raise ValueError("Wine major version must be positive")

    branch = "stable" if minor == 0 and rc is None else "devel"
    label = match[3].lower() if match[3] else branch
    if label != "staging" and label != branch:
        raise ValueError("Wine version label disagrees with its release channel")
    return WineVersion(major, minor, label, tuple(patch), rc)


def parse_wine_version(output: str) -> WineVersion:
    return _parse_version(output, "wine-")


def parse_wineserver_version(output: str) -> Version:
    """Servers can omit Staging labels, but must identify the full base version."""
    return _parse_version(output, "Wine ").version


def wine_eligibility(
    wine: WineVersion | None, target: Version | None, product_version: str,
) -> tuple[bool, str]:
    """Apply known constraints. Eligibility does not certify native application support."""
    if product_version == "10":
        minimum = (7, 18) if target is not None and target < Version("30.0.0.0") else (9, 10)
    elif product_version == "9":
        minimum = (7, 0)
    else:
        raise ValueError(
            "Invalid target version, expecting 9 or 10 but got: "
            f"{product_version} ({type(product_version)})"
        )

    if wine is None:
        return False, "Could not identify the selected Wine version"

    major, minor, branch = wine.major, wine.minor, wine.release
    if (major, minor) < minimum:
        return False, f"Wine {major}.{minor} is below minimum required ({minimum[0]}.{minimum[1]})"
    if wine.flavor == WineFlavor.UNKNOWN:
        return False, f"Unrecognized Wine branch: {branch}"
    if major == 8 and minor == 0:
        return False, "Wine 8.0 will not work"
    if major == 7 and branch != "staging":
        return False, "Wine 7 requires staging"
    if major == 8 and branch != "staging" and not (minor >= 16 and branch == "devel"):
        return False, "Wine 8 requires staging or devel 8.16 or later"
    if major == 9 and branch not in ("devel", "staging"):
        return False, "Wine 9 requires devel or staging"
    return True, "Eligible under known Wine version rules, not native-validated support"
