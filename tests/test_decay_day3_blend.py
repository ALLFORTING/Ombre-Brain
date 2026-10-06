# ============================================================
# Test: short-term → long-term weight blend around day 3
# 测试：第 3 天附近短期/长期权重的平滑过渡
#
# calculate_score() used to switch weight mixes at exactly 3.0 days,
# which made the score jump. It now blends over [2, 4] days.
# Outside that window the score must match the previous formula.
# ============================================================

import math

import pytest

import decay_engine
from decay_engine import DecayEngine


@pytest.fixture
def engine():
    return DecayEngine({}, None)


@pytest.fixture
def score_at(engine, monkeypatch):
    """Score a bucket at an exact age, bypassing datetime.now() drift."""

    def _score(days, **meta):
        monkeypatch.setattr(decay_engine, "_parse_decay_age", lambda md, *keys: days)
        base = {"importance": 5, "activation_count": 1, "arousal": 0.3}
        base.update(meta)
        return engine.calculate_score(base)

    return _score


def _old_score(days, importance=5, activation_count=1, arousal=0.3,
               resolved=False, digested=False):
    """Reference copy of the pre-blend formula (hard switch at 3.0 days)."""
    time_weight = 1.0 + math.exp(-(days * 24.0) / 36.0)
    emotion_weight = 1.0 + arousal * 0.8
    if days <= 3.0:
        combined = time_weight * 0.7 + emotion_weight * 0.3
    else:
        combined = emotion_weight * 0.7 + time_weight * 0.3
    base = importance * (activation_count ** 0.3) * math.exp(-0.05 * days) * combined
    if resolved and digested:
        factor = 0.02
    elif resolved:
        factor = 0.05
    else:
        factor = 1.0
    urgency = 1.5 if (arousal > 0.7 and not resolved) else 1.0
    return round(base * factor * urgency, 4)


class TestBlendFactor:
    def test_endpoints_and_midpoint(self, engine):
        assert engine._calc_long_term_blend(0.0) == 0.0
        assert engine._calc_long_term_blend(2.0) == 0.0
        assert engine._calc_long_term_blend(3.0) == pytest.approx(0.5)
        assert engine._calc_long_term_blend(4.0) == 1.0
        assert engine._calc_long_term_blend(30.0) == 1.0

    def test_monotonic_non_decreasing(self, engine):
        values = [engine._calc_long_term_blend(i / 100) for i in range(0, 601)]
        assert all(b >= a for a, b in zip(values, values[1:]))


class TestDay3Continuity:
    @pytest.mark.parametrize("arousal", [0.0, 0.3, 0.8, 1.0])
    def test_no_jump_at_day3(self, score_at, arousal):
        eps = 1e-6
        left = score_at(3.0 - eps, arousal=arousal)
        right = score_at(3.0 + eps, arousal=arousal)
        assert abs(right - left) <= 1e-3

    @pytest.mark.parametrize("edge", [2.0, 4.0])
    @pytest.mark.parametrize("arousal", [0.3, 0.8])
    def test_no_jump_at_window_edges(self, score_at, edge, arousal):
        eps = 1e-6
        left = score_at(edge - eps, arousal=arousal)
        right = score_at(edge + eps, arousal=arousal)
        assert abs(right - left) <= 1e-3

    @pytest.mark.parametrize("arousal", [0.3, 0.8])
    def test_small_steps_across_window(self, score_at, arousal):
        """Sweep 1.5→4.5 days in 0.01-day steps: no step larger than a smooth slope allows."""
        days = [1.5 + i * 0.01 for i in range(301)]
        scores = [score_at(d, arousal=arousal) for d in days]
        max_step = max(abs(b - a) for a, b in zip(scores, scores[1:]))
        # The old hard switch moved these scores by ~0.14 (arousal 0.3)
        # and ~1.24 (arousal 0.8) in a single step at day 3.
        assert max_step < 0.02

    def test_day3_is_midpoint_of_both_mixes(self, score_at):
        short = _old_score(3.0, arousal=0.3)
        long_ = _old_score(3.0 + 1e-9, arousal=0.3)
        assert score_at(3.0, arousal=0.3) == pytest.approx((short + long_) / 2, abs=2e-4)


class TestOutsideWindowUnchanged:
    # Values produced by the pre-blend formula at commit fdb16f4.
    PINNED = {
        (0.3, 1.0): 6.8079,
        (0.3, 2.0): 5.6847,
        (0.3, 4.0): 4.8667,
        (0.3, 5.0): 4.5899,
        (0.3, 7.0): 4.1253,
        (0.3, 14.0): 2.9001,
        (0.3, 30.0): 1.3031,
        (0.8, 1.0): 11.0680,
        (0.8, 2.0): 9.3414,
        (0.8, 4.0): 9.0194,
        (0.8, 5.0): 8.5203,
        (0.8, 7.0): 7.6678,
        (0.8, 14.0): 5.3930,
        (0.8, 30.0): 2.4232,
    }

    @pytest.mark.parametrize("key", sorted(PINNED))
    def test_pinned_values(self, score_at, key):
        arousal, days = key
        assert score_at(days, arousal=arousal) == self.PINNED[key]

    @pytest.mark.parametrize("days", [0.0, 0.5, 1.0, 1.99, 2.0, 4.0, 4.01, 6.0, 10.0, 60.0])
    @pytest.mark.parametrize("importance,activation_count,arousal,resolved,digested", [
        (5, 1, 0.3, False, False),
        (9, 4, 0.9, False, False),
        (2, 1, 0.0, True, False),
        (7, 2, 0.75, True, True),
    ])
    def test_matches_old_formula(self, score_at, days, importance, activation_count,
                                 arousal, resolved, digested):
        meta = dict(importance=importance, activation_count=activation_count,
                    arousal=arousal, resolved=resolved, digested=digested)
        assert score_at(days, **meta) == _old_score(days, **meta)
