"""Page rendering helpers."""

from bgrules.render import merge_regions


def test_overlapping_crop_regions_merge_but_columns_stay_separate():
    # Two overlapping boxes in one text block merge; a box in the next column stays its own crop.
    regions = [(10, 10, 100, 40), (50, 30, 120, 60), (300, 10, 400, 40)]
    assert merge_regions(regions) == [(10, 10, 120, 60), (300, 10, 400, 40)]
