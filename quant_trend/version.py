import os


APP_VERSION = os.getenv("APP_VERSION", "0.5.4")
APP_RELEASE_DATE = "2026-06-19"
APP_BUILD = (
    os.getenv("RAILWAY_GIT_COMMIT_SHA")
    or os.getenv("GIT_COMMIT_SHA")
    or os.getenv("SOURCE_VERSION")
    or ""
)


def app_version_payload() -> dict:
    return {
        "version": APP_VERSION,
        "release_date": APP_RELEASE_DATE,
        "build": APP_BUILD[:7] if APP_BUILD else None,
    }
