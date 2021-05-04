import os
import re
import subprocess
import sys

import cumulusci
import django
from django.conf import settings
from django.http import HttpResponseForbidden
from django.shortcuts import render
from django.views.generic.base import TemplateView


class AboutView(TemplateView):
    def get_context_data(self, **kwargs):
        context = super(AboutView, self).get_context_data(**kwargs)

        # django (major.minor.micro)
        context[
            "DJANGO_VERSION"
        ] = f"{django.VERSION[0]}.{django.VERSION[1]}.{django.VERSION[2]}"

        # python
        context[
            "PYTHON_VERSION"
        ] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

        # Salesforce DX
        out = subprocess.check_output(["sfdx", "--version"])
        match = re.match(r"sfdx-cli/(\d+.\d+.\d+)-.+", out.decode("utf-8"))
        if match:
            context["SFDX_CLI_VERSION"] = match.group(1)

        # cumulusci
        context["CUMULUSCI_VERSION"] = cumulusci.__version__

        # heroku
        heroku_env_vars = [
            "HEROKU_APP_ID",
            "HEROKU_APP_NAME",
            "HEROKU_DYNO_ID",
            "HEROKU_RELEASE_CREATED_AT",
            "HEROKU_RELEASE_VERSION",
            "HEROKU_SLUG_COMMIT",
            "HEROKU_SLUG_DESCRIPTION",
        ]
        for var in heroku_env_vars:
            context[var] = os.environ.get(var, "Heroku dyno metadata not found")

        context["METACI_FLOW_CALLBACK_ENABLED"] = settings.METACI_FLOW_CALLBACK_ENABLED

        return context


def custom_403(request, exception):
    """Always redirect users to login via GitHub"""
    return HttpResponseForbidden(render(request, "account/login.html"))


def gus_bus_enabled(request):
    context = {
        "METACI_START_STOP_WEBHOOK": os.environ.get("METACI_START_STOP_WEBHOOK", False)
    }
    return render(request, "admin/release/release/change_form.html", context=context)
