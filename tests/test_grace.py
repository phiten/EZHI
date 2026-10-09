"""How long a failing poll may keep showing the last good data."""
from __future__ import annotations

from ezhi_component.grace import Grace


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_without_a_success_nothing_is_covered():
    clock = Clock()
    grace = Grace(45, clock)
    assert grace.holds() is False
    grace.extend(180)
    assert grace.holds() is False        # there is no good data to show


def test_a_failure_is_covered_for_the_grace_period_after_the_last_success():
    clock = Clock()
    grace = Grace(45, clock)
    grace.ok()
    clock.now += 45
    assert grace.holds() is True
    clock.now += 1
    assert grace.holds() is False


def test_a_new_success_starts_the_period_over():
    clock = Clock()
    grace = Grace(45, clock)
    grace.ok()
    clock.now += 40
    grace.ok()
    clock.now += 40
    assert grace.holds() is True


def test_an_extension_covers_a_long_silence_and_then_ends():
    """After a command that makes the inverter reconnect it can be silent for
    a minute or more; the extension counts from the command."""
    clock = Clock()
    grace = Grace(45, clock)
    grace.ok()
    grace.extend(180)
    clock.now += 179
    assert grace.holds() is True
    clock.now += 2
    assert grace.holds() is False


def test_a_shorter_extension_does_not_cut_a_longer_one_short():
    clock = Clock()
    grace = Grace(45, clock)
    grace.ok()
    grace.extend(180)
    grace.extend(10)
    clock.now += 100
    assert grace.holds() is True
