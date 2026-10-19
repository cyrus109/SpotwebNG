"""Release-title normalisation: the key that groups the same title posted in several
qualities, formats or by several posters ("duplicate grouping").

"Dune Part Two 2024 2160p UHD BluRay x265" and "Dune.Part.Two.2024.1080p.WEB-DL.DDP5.1.x264-GRP"
share the key "dune part two 2024". Series keep their SxxExx marker so episodes stay apart.
"""

from __future__ import annotations

import re

# Words that describe the release, not the work.
_NOISE = {
    "2160p",
    "1080p",
    "1080i",
    "720p",
    "576p",
    "480p",
    "4k",
    "uhd",
    "hd",
    "sd",
    "hdr",
    "hdr10",
    "hdr10plus",
    "dv",
    "dolby",
    "vision",
    "bluray",
    "blu",
    "ray",
    "bdrip",
    "brrip",
    "bdremux",
    "remux",
    "webrip",
    "web",
    "dl",
    "webdl",
    "hdtv",
    "pdtv",
    "dvdrip",
    "dvd",
    "dvdr",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "avc",
    "xvid",
    "divx",
    "aac",
    "ac3",
    "eac3",
    "dts",
    "ddp",
    "dd",
    "dd5",
    "ddp5",
    "truehd",
    "atmos",
    "flac",
    "mp3",
    "proper",
    "repack",
    "internal",
    "extended",
    "unrated",
    "directors",
    "cut",
    "complete",
    "multi",
    "dubbed",
    "subbed",
    "nl",
    "nlsubs",
    "dutch",
    "eng",
    "english",
    "ned",
    "subs",
    "sub",
    "hardsub",
    "retail",
    "ntsc",
    "pal",
    "iso",
    "10bit",
    "8bit",
    "amzn",
    "nf",
    "dsnp",
    "atvp",
    "hmax",
    "pcok",
    "pack",
    "season",
    "seizoen",
}
_BRACKETS = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")
_SEPARATORS = re.compile(r"[^0-9a-zà-ÿ]+")
_YEAR = re.compile(r"^(19|20)\d{2}$")
_EPISODE = re.compile(r"^s\d{1,2}(e\d{1,3})?$")
_CHANNELS = re.compile(r"^\d\.\d$")


def title_key(title: str, limit: int = 255) -> str:
    """The grouping key for a title ('' is never returned: falls back to the lowered title)."""
    text = re.sub(
        r"[\[\(]((?:19|20)\d{2})[\]\)]", r" \1 ", title.lower()
    )  # "(2023)" keeps its year
    text = _BRACKETS.sub(" ", text)
    # drop a trailing "-GROUP" release tag (dash directly followed by letters at the very end)
    text = re.sub(r"-[a-z0-9]{2,12}$", "", text.strip())
    words = []
    for token in _SEPARATORS.split(text):
        if not token or token in _NOISE or _CHANNELS.match(token):
            continue
        if re.fullmatch(r"\d{3,4}[pi]", token) or re.fullmatch(r"x?26[45]", token):
            continue
        words.append(token)
    # everything after the first year / episode marker is release detail
    cut = next(
        (i + 1 for i, w in enumerate(words) if i > 0 and (_YEAR.match(w) or _EPISODE.match(w))),
        len(words),
    )
    key = " ".join(words[:cut]).strip()
    return (key or " ".join(_SEPARATORS.split(title.lower())).strip() or title.lower())[:limit]
