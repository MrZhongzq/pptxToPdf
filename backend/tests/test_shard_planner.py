from app.services.shard_planner import needs_sharding, plan_ranges

MIB = 1024 * 1024


def _covers(ranges, total):
    """页范围必须无缝、无重叠地覆盖 1..total。"""
    assert ranges[0][0] == 1
    assert ranges[-1][1] == total
    for (_, prev_end), (nxt_start, _) in zip(ranges, ranges[1:]):
        assert nxt_start == prev_end + 1
    return True


def test_no_sharding_when_within_limits():
    assert needs_sharding(80, 40 * MIB) is False
    assert needs_sharding(1, 1) is False


def test_sharding_when_pages_exceed():
    assert needs_sharding(81, 1 * MIB) is True


def test_sharding_when_bytes_exceed():
    assert needs_sharding(10, 41 * MIB) is True


def test_single_range_when_within_limits():
    assert plan_ranges(50, 10 * MIB, 80, 40 * MIB) == [(1, 50)]


def test_splits_by_page_limit():
    ranges = plan_ranges(200, 10 * MIB, 80, 40 * MIB)
    assert _covers(ranges, 200)
    assert all(end - start + 1 <= 80 for start, end in ranges)
    assert len(ranges) == 3


def test_splits_by_size_limit():
    # 40 页 400MB -> 每页均 10MB -> 每片最多 4 页
    ranges = plan_ranges(40, 400 * MIB, 80, 40 * MIB)
    assert _covers(ranges, 40)
    assert all(end - start + 1 <= 4 for start, end in ranges)


def test_size_limit_wins_over_page_limit():
    ranges = plan_ranges(100, 500 * MIB, 80, 40 * MIB)
    assert _covers(ranges, 100)
    assert all(end - start + 1 <= 8 for start, end in ranges)


def test_boundary_exactly_at_limits():
    assert plan_ranges(80, 40 * MIB, 80, 40 * MIB) == [(1, 80)]
    ranges = plan_ranges(81, 40 * MIB, 80, 40 * MIB)
    assert _covers(ranges, 81)
    assert len(ranges) == 2


def test_single_page_deck():
    assert plan_ranges(1, 100 * MIB, 80, 40 * MIB) == [(1, 1)]
