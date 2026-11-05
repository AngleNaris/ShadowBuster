"""ShadowBuster mastering. Safe to import from the lightweight desktop shell."""

ENGINE_VERSION = "shadowbuster-mastering-v7"
# Compatibility with the former default Pop-based targets, rounded to 0.01 LU.
# These are application settings, not reference measurements or genre profiles.
LOUDNESS_TARGETS = {"soft": -12.30, "dynamic": -11.14, "normal": -9.20, "loud": -6.70}
# -1.0 dBTP leaves headroom for lossy (MP3/AAC) encoding overshoot.
TRUE_PEAK_CEILING_DB = -1.0
