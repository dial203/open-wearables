"""Whether a relayed source is shown when its maker is also connected directly."""

from enum import StrEnum


class RelayVisibility(StrEnum):
    """Per-source override of the redundant-relay rule.

    The rule itself is derived, not stored: whether an Apple Health copy of a Garmin
    is redundant depends on whether the Garmin connection still delivers, which
    changes when a token expires or an account is revoked. Persisting "redundant" on
    the row would freeze an answer that has to be recomputed. What *is* worth storing
    is a person's decision to override it, which nothing should recompute away.

    ``AUTO`` is the resting state and the only value the system ever sets itself.
    """

    # Follow the rule: hidden where a direct connection of the same brand covers the
    # same span, visible everywhere else.
    AUTO = "auto"
    # Never hide this source, whatever the rule says. For the relay that carries
    # something the maker's own API does not - a metric the direct integration
    # never exposes, or a unit only ever seen through the aggregator.
    ALWAYS = "always"
    # Always hide it, even where no direct source covers the span. For a relay
    # judged useless outright, without deleting data that has already been ingested.
    NEVER = "never"
