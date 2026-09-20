"""What a connected provider account is for."""

from enum import StrEnum


class AccountType(StrEnum):
    """Why this provider account is linked to this participant.

    A user may hold several accounts with one provider, and the provider name no
    longer says anything about which is which. The e-mail identifies an account;
    this says what it is *for*, which is what a study needs to filter on - the
    validation arm separated from the participant's own everyday data, the
    device-checkout account kept out of the analysis entirely.

    Stored as a plain string rather than a database enum, matching DeviceType and
    ProviderName, so a value can be added here without a migration.
    """

    # The participant's own everyday account - their normal wear, their data.
    PERSONAL = "personal"
    # Criterion or device-comparison work: this unit is being measured against
    # another, and the two arms must never pool.
    VALIDATION = "validation"
    # Test-retest or inter-device consistency, where the same brand is worn
    # twice over and agreement between the two is the measurement.
    RELIABILITY = "reliability"
    # Ongoing readiness or load monitoring in an athletic or tactical cohort,
    # as opposed to a bounded study.
    MONITORING = "monitoring"
    # Device or integration checkout. Not participant data, and usually the
    # first thing to exclude from an analysis.
    TESTING = "testing"
    # Deliberately kept: forcing a wrong pick is worse than recording "other"
    # and letting the account name or e-mail carry the detail.
    OTHER = "other"


# Order used wherever the set is presented, most common first rather than
# alphabetical - "other" stays last because it is the fallback, not a peer.
ACCOUNT_TYPE_ORDER: tuple[AccountType, ...] = (
    AccountType.PERSONAL,
    AccountType.VALIDATION,
    AccountType.RELIABILITY,
    AccountType.MONITORING,
    AccountType.TESTING,
    AccountType.OTHER,
)

# One line each, for the API's own documentation and for any client that would
# otherwise invent its own wording for these.
ACCOUNT_TYPE_DESCRIPTIONS: dict[AccountType, str] = {
    AccountType.PERSONAL: "The participant's own everyday account",
    AccountType.VALIDATION: "Criterion or device-comparison study",
    AccountType.RELIABILITY: "Test-retest or inter-device consistency",
    AccountType.MONITORING: "Ongoing athlete or tactical readiness monitoring",
    AccountType.TESTING: "Device or integration checkout, not participant data",
    AccountType.OTHER: "Anything else",
}
