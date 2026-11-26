"""ShadowBuster mastering. Safe to import from the lightweight desktop shell."""

ENGINE_VERSION = "shadowbuster-mastering-v10"
ALL_DENSITY_BANDS = "low,mid,high"


def normalize_density_scope(value):
    """Canonical order keeps equivalent selections identical in reports and caches."""
    if value in ("global", "none"):
        return value
    selected = {band.strip() for band in value.split(",")}
    ordered = ALL_DENSITY_BANDS.split(",")
    if not selected.issubset(ordered):
        raise ValueError(f"Unknown density scope: {value}")
    return ",".join(band for band in ordered if band in selected)

# Compatibility with the former default Pop-based targets, rounded to 0.01 LU.
# These are application settings, not reference measurements or genre profiles.
LOUDNESS_TARGETS = {"soft": -12.30, "dynamic": -11.14, "normal": -9.20, "loud": -8.0}
LOUDNESS_TARGET_RANGE = (-8.0, -6.0)
LOUD_TRUE_PEAK_CEILING_DB = -0.5
# -1.0 dBTP leaves headroom for lossy (MP3/AAC) encoding overshoot.
TRUE_PEAK_CEILING_DB = -1.0
