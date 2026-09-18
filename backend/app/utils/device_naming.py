"""One rule for what a device is called, shared by every consumer.

The name a device carries is assembled from several fields of decreasing authority,
and the order matters enough to have one home: a screen, an export and an API
response disagreeing about which of two identical units a sample came from is worse
than any of them being terse.

Mirrored in frontend/src/lib/utils/device.ts, which applies the same order to the
same fields for the cases where the client has the device but not this response.
"""

# LabelSource.AUTO, spelled out rather than imported: this module is pure display and
# is imported by schemas that the enums package must not have to reach back into.
_AUTO = "auto"

# What a device type is called when nothing else names the device. Deliberately a
# small map rather than a call into the enum: this is a display string, and the
# fallback it feeds ("Muse EEG") reads as a description, not as a model name.
_TYPE_LABELS: dict[str, str] = {
    "chest_strap": "chest strap",
    "eeg": "EEG",
    "headband": "headband",
    "watch": "watch",
    "band": "band",
    "ring": "ring",
    "phone": "phone",
    "scale": "scale",
    "other": "device",
    "unknown": "device",
}


def device_display_name(
    label: str | None,
    model_display: str | None,
    model_raw: str | None,
    brand_display: str | None = None,
    brand: str | None = None,
    device_type: str | None = None,
    label_source: str | None = None,
) -> str:
    """What to call a device on screen.

    A label a person set wins over everything, because it is the only name that can say
    which of two identical units this is. Then the hand-set model, then a label
    detection guessed, then ``model_raw``, the provider's verbatim string - a raw code
    beats a friendlier guess that may name the wrong hardware. A device none of those
    name falls back to a description of what it is, which is the normal state of a
    relayed stream nobody has edited yet.

    An auto label ranks *below* the hand-set model on purpose. Detection names a relayed
    stream after the app that wrote it, which is a placeholder; a person who then types
    the model has said something the guess did not, and would not expect the guess to
    keep winning.
    """
    hand_set_label = bool(label) and label_source != _AUTO
    if hand_set_label:
        return label or ""
    if model_display:
        return model_display
    if label:
        return label
    if model_raw:
        return model_raw
    maker = brand_display or brand
    kind = _TYPE_LABELS.get((device_type or "").casefold(), "device")
    return f"{maker} {kind}" if maker else f"Unidentified {kind}"
