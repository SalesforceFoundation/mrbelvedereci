import os


def get_release_values(request):
    return {
        "METACI_START_STOP_WEBHOOK": os.environ.get("METACI_START_STOP_WEBHOOK", False),
        "METACI_RELEASE_WEBHOOK_URL": os.environ.get(
            "METACI_RELEASE_WEBHOOK_URL", None
        ),
    }
