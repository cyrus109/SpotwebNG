import pytest

from app.titles import title_key


@pytest.mark.parametrize(
    "a, b",
    [
        (
            "Dune Part Two 2024 2160p UHD BluRay x265",
            "Dune.Part.Two.2024.1080p.WEB-DL.DDP5.1.x264-GRP",
        ),
        ("Oppenheimer 2023 1080p BluRay x264", "Oppenheimer (2023) 720p WEBRip [NL subs]"),
        ("The Bear S03 1080p WEB-DL DDP5.1 x264", "The.Bear.S03.2160p.HDR.WEB-DL.x265"),
    ],
)
def test_the_same_work_in_different_qualities_shares_a_key(a, b):
    assert title_key(a) == title_key(b)


def test_different_works_and_episodes_stay_apart():
    assert title_key("Dune 1984 1080p BluRay") != title_key("Dune Part Two 2024 1080p BluRay")
    assert title_key("Silo S03E05 1080p WEB-DL") != title_key("Silo S03E06 1080p WEB-DL")
    assert title_key("Silo S03E05 1080p") == "silo s03e05"


def test_a_key_is_never_empty_and_is_bounded():
    assert title_key("1080p") != "" and title_key("!!!") != ""
    assert len(title_key("x" * 1000)) <= 255
