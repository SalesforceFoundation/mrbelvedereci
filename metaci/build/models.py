import json
import os
import shutil
import sys
import tempfile
import traceback
import zipfile
from glob import iglob
from io import BytesIO

from cumulusci import __version__ as cumulusci_version
from cumulusci.core.config import FAILED_TO_CREATE_SCRATCH_ORG
from cumulusci.core.exceptions import (
    ApexTestException,
    BrowserTestFailure,
    RobotTestFailure,
    ScratchOrgException,
)
from cumulusci.core.flowrunner import FlowCoordinator
from cumulusci.salesforce_api.exceptions import MetadataComponentFailure
from cumulusci.utils import elementtree_parse_file
from django.apps import apps
from django.conf import settings
from django.contrib.postgres.fields import JSONField
from django.core.exceptions import ObjectDoesNotExist
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from jinja2.sandbox import ImmutableSandboxedEnvironment

from metaci.build.tasks import set_github_status
from metaci.build.utils import format_log, set_build_info
from metaci.cumulusci.config import MetaCIUniversalConfig
from metaci.cumulusci.keychain import MetaCIProjectKeychain
from metaci.cumulusci.logger import init_logger
from metaci.release.utils import (
    send_release_webhook,
    send_start_webhook,
    send_stop_webhook,
)
from metaci.testresults.importer import import_test_results
from metaci.utils import generate_hash

BUILD_STATUSES = (
    ("queued", "Queued"),
    ("waiting", "Waiting"),
    ("running", "Running"),
    ("success", "Success"),
    ("error", "Error"),
    ("fail", "Failed"),
    ("qa", "QA Testing"),
)
BUILD_FLOW_STATUSES = (
    ("queued", "Queued"),
    ("running", "Running"),
    ("success", "Success"),
    ("error", "Error"),
    ("fail", "Failed"),
)
FLOW_TASK_STATUSES = (
    ("initializing", "Initializing"),
    ("running", "Running"),
    ("complete", "Completed"),
    ("error", "Error"),
)
BUILD_TYPES = (
    ("manual", "Manual"),
    ("auto", "Auto"),
    ("scheduled", "Scheduled"),
    ("legacy", "Legacy - Probably Automatic"),
    ("manual-command", "Created from command line"),
)
RELEASE_REL_TYPES = (
    ("test", "Release Test"),
    ("automation", "Release Automation"),
    ("manual", "Manual Release Activity"),
)
FAIL_EXCEPTIONS = (
    ApexTestException,
    BrowserTestFailure,
    MetadataComponentFailure,
    RobotTestFailure,
)

jinja2_env = ImmutableSandboxedEnvironment()


class GnarlyEncoder(DjangoJSONEncoder):
    """ A Very Gnarly Encoder that serializes a repr() if it can't get anything else.... """

    def default(self, obj):  # pylint: disable=W0221, E0202
        try:
            return super().default(obj)
        except TypeError:
            return repr(obj)


class BuildQuerySet(models.QuerySet):
    def for_user(self, user, perms=None):
        if user.is_superuser:
            return self
        if perms is None:
            perms = "plan.view_builds"
        PlanRepository = apps.get_model("plan.PlanRepository")
        return self.filter(planrepo__in=PlanRepository.objects.for_user(user, perms))

    def get_for_user_or_404(self, user, query, perms=None):
        try:
            return self.for_user(user, perms).get(**query)
        except Build.DoesNotExist:
            raise Http404


class Build(models.Model):
    repo = models.ForeignKey(
        "repository.Repository", related_name="builds", on_delete=models.CASCADE
    )
    branch = models.ForeignKey(
        "repository.Branch",
        related_name="builds",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    commit = models.CharField(max_length=64)
    commit_message = models.TextField(null=True, blank=True)
    commit_status = models.CharField(
        max_length=140,
        null=True,
        blank=True,
        help_text="Optional success message to be reported as a github commit status",
    )
    tag = models.CharField(max_length=255, null=True, blank=True)
    pr = models.IntegerField(null=True, blank=True)
    plan = models.ForeignKey(
        "plan.Plan", related_name="builds", on_delete=models.PROTECT
    )
    planrepo = models.ForeignKey(
        "plan.PlanRepository",
        related_name="builds",
        on_delete=models.PROTECT,
        null=True,
    )
    org = models.ForeignKey(
        "cumulusci.Org",
        related_name="builds",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    org_instance = models.ForeignKey(
        "cumulusci.ScratchOrgInstance",
        related_name="builds",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    schedule = models.ForeignKey(
        "plan.PlanSchedule",
        related_name="builds",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    log = models.TextField(null=True, blank=True)
    exception = models.TextField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    traceback = models.TextField(null=True, blank=True)
    qa_comment = models.TextField(null=True, blank=True)
    qa_user = models.ForeignKey(
        "users.User",
        related_name="builds_qa",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    status = models.CharField(max_length=16, choices=BUILD_STATUSES, default="queued")
    keep_org = models.BooleanField(default=False)
    task_id_status_start = models.CharField(max_length=64, null=True, blank=True)
    task_id_check = models.CharField(max_length=64, null=True, blank=True)
    task_id_run = models.CharField(max_length=64, null=True, blank=True)
    task_id_status_end = models.CharField(max_length=64, null=True, blank=True)
    current_rebuild = models.ForeignKey(
        "build.Rebuild",
        related_name="current_builds",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    time_queue = models.DateTimeField(auto_now_add=True)
    time_start = models.DateTimeField(null=True, blank=True)
    time_end = models.DateTimeField(null=True, blank=True)
    time_qa_start = models.DateTimeField(null=True, blank=True)
    time_qa_end = models.DateTimeField(null=True, blank=True)

    build_type = models.CharField(max_length=16, choices=BUILD_TYPES, default="legacy")
    user = models.ForeignKey(
        "users.User", related_name="builds", null=True, on_delete=models.PROTECT
    )

    release_relationship_type = models.CharField(
        max_length=50, choices=RELEASE_REL_TYPES, null=True, blank=True
    )
    release = models.ForeignKey(
        "release.Release", on_delete=models.PROTECT, null=True, blank=True
    )
    org_note = models.CharField(max_length=255, default="", blank=True, null=True)
    org_api_version = models.CharField(max_length=5, blank=True, null=True)

    objects = BuildQuerySet.as_manager()

    class Meta:
        ordering = ["-time_queue"]
        permissions = (("search_builds", "Search Builds"),)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._try_populate_planrepo()

    def save(self, *args, **kwargs):
        self._try_populate_planrepo()
        super().save(*args, **kwargs)

    def _try_populate_planrepo(self):
        if self.plan_id and self.repo_id and not self.planrepo:
            PlanRepository = apps.get_model("plan.PlanRepository")
            matching_repo = PlanRepository.objects.filter(
                plan=self.plan, repo=self.repo
            )
            if matching_repo.exists():
                self.planrepo = matching_repo[0]

    def __str__(self):
        return f"{self.id}: {self.repo} - {self.commit}"

    def get_log_html(self):
        if self.log:
            return format_log(self.log)

    def get_absolute_url(self):
        return reverse("build_detail", kwargs={"build_id": str(self.id)})

    def get_external_url(self):
        url = f"{settings.SITE_URL}{self.get_absolute_url()}"
        return url

    def get_build(self):
        return self.current_rebuild if self.current_rebuild else self

    def get_build_attr(self, attr):
        # get an attribute from the most recent build/rebuild
        build = self.get_build()
        return getattr(build, attr)

    def get_status(self):
        return self.get_build_attr("status")

    def get_exception(self):
        return self.get_build_attr("exception")

    def get_error_message(self):
        return self.get_build_attr("error_message")

    def get_qa_comment(self):
        return self.get_build_attr("qa_comment")

    def get_qa_user(self):
        return self.get_build_attr("qa_user")

    def get_time_queue(self):
        return self.get_build_attr("time_queue")

    def get_time_start(self):
        return self.get_build_attr("time_start")

    def get_time_end(self):
        return self.get_build_attr("time_end")

    def get_time_qa_start(self):
        return self.get_build_attr("time_qa_start")

    def get_time_qa_end(self):
        return self.get_build_attr("time_qa_end")

    def get_commit(self):
        return f"{self.commit[:8]}"

    def set_status(self, status):
        build = self.get_build()
        build.status = status
        build.save()

    def flush_log(self):
        for handler in self.logger.handlers:
            handler.stream.flush()

    @property
    def worker_id(self):
        return os.environ.get("DYNO")

    def run(self):
        self.logger = init_logger(self)
        worker_str = f"in {self.worker_id}" if self.worker_id else ""
        self.logger.info(
            f"-- Building commit {self.commit} {worker_str} with CumulusCI version {cumulusci_version}"
        )
        self.flush_log()
        build = self.current_rebuild if self.current_rebuild else self
        set_build_info(build, status="running", time_start=timezone.now())

        if self.schedule:
            self.logger.info(
                f"Build triggered by {self.schedule.schedule} schedule #{self.schedule.id}"
            )

        try:

            # Extract the repo to a temp build dir
            self.build_dir = self.checkout()
            self.root_dir = os.getcwd()

            # Change directory to the build_dir
            os.chdir(self.build_dir)

            # Initialize the project config
            project_config = self.get_project_config()

            # Set the sentry context for build errors
            sentry_environment = "metaci"
            project_config.config["sentry_environment"] = sentry_environment

            # Look up or spin up the org
            org_config = self.get_org(project_config)
            if (
                self.org and self.org.name and self.org.name.lower() == "packaging"
            ):  # Calling for any actions taken against packaging org
                send_start_webhook(
                    project_config,
                    self.release,
                    self.plan.role,
                    self.org.configuration_item,
                )

        except Exception as e:
            self.logger.error(str(e))
            set_build_info(
                build,
                status="error",
                time_end=timezone.now(),
                error_message=str(e),
                exception=e.__class__.__name__,
                traceback="".join(traceback.format_tb(e.__traceback__)),
            )
            self.delete_build_dir()
            self.flush_log()
            return

        try:
            self.org_api_version = org_config.latest_api_version
        except Exception as e:
            self.logger.warning(f"Could not retrieve salesforce API version: {e}")

        # Run flows
        try:
            flows = [flow.strip() for flow in self.plan.flows.split(",")]
            for flow in flows:
                self.logger = init_logger(self)
                self.logger.info(f"Running flow: {flow}")
                self.save()

                build_flow = BuildFlow(
                    build=self, rebuild=self.current_rebuild, flow=flow
                )
                build_flow.save()
                build_flow.run(project_config, org_config, self.root_dir)

                if build_flow.status != "success":
                    self.logger = init_logger(self)
                    self.logger.error(
                        f"Build flow {flow} completed with status {build_flow.status}"
                    )
                    self.logger.error(
                        f"    {build_flow.exception}: {build_flow.error_message}"
                    )
                    set_build_info(
                        build,
                        status=build_flow.status,
                        exception=build_flow.exception,
                        traceback=build_flow.traceback,
                        error_message=build_flow.error_message,
                        time_end=timezone.now(),
                    )
                    self.flush_log()
                    if org_config.created:
                        self.delete_org(org_config)
                    return
                else:
                    self.logger = init_logger(self)
                    self.logger.info(f"Build flow {flow} completed successfully")
                    self.flush_log()
                    self.save()

        except Exception as e:
            set_build_info(
                build,
                exception=str(e),
                traceback="".join(traceback.format_tb(e.__traceback__)),
                status="error",
                time_end=timezone.now(),
            )
            if org_config.created:
                self.delete_org(org_config)
            self.logger = init_logger(self)
            self.logger.error(str(e))
            self.delete_build_dir()
            self.flush_log()
            return

        if self.plan.role == "qa":
            self.logger.info("Build complete, org is now ready for QA testing")
        elif org_config.created:
            self.delete_org(org_config)

        self.delete_build_dir()
        self.flush_log()

        if self.plan.role == "release":
            try:
                send_release_webhook(self.release, self.org.configuration_item)
            except Exception as err:
                message = f"Error while sending release webhook: {err}"
                self.logger.error(message)
                set_build_info(
                    build, status="error", exception=message, time_end=timezone.now()
                )
                return
        if (
            self.org and self.org.name and self.org.name.lower() == "packaging"
        ):  # Calling for any actions taken against packaging org
            try:
                send_stop_webhook(
                    project_config,
                    self.release,
                    self.plan.role,
                    self.org.configuration_item,
                )
            except Exception as err:
                message = f"Error while sending implementation stop step webhook: {err}"
                self.logger.error(message)
                set_build_info(
                    build, status="error", exception=message, time_end=timezone.now()
                )
                return

        if self.plan.role == "qa":
            set_build_info(
                build,
                status="qa",
                time_end=timezone.now(),
                time_qa_start=timezone.now(),
            )
        else:
            set_build_info(build, status="success", time_end=timezone.now())

    def checkout(self):
        # get the ref
        zip_content = BytesIO()
        gh = self.repo.get_github_api()
        gh.archive("zipball", zip_content, ref=self.commit)
        build_dir = tempfile.mkdtemp()
        self.logger.info(f"-- Extracting zip to temp dir {build_dir}")
        self.save()
        zip_file = zipfile.ZipFile(zip_content)
        zip_file.extractall(build_dir)
        # assume the zipfile has a single child dir with the repo
        build_dir = os.path.join(build_dir, os.listdir(build_dir)[0])
        self.logger.info(f"-- Commit extracted to build dir: {build_dir}")
        self.save()

        if self.plan.sfdx_config:
            self.logger.info("-- Injecting custom sfdx-workspace.json from plan")
            filename = os.path.join(build_dir, "sfdx-workspace.json")
            with open(filename, "w") as f:
                f.write(self.plan.sfdx_config)

        return build_dir

    def get_project_config(self):
        universal_config = MetaCIUniversalConfig()
        project_config = universal_config.get_project_config(self)
        keychain = MetaCIProjectKeychain(project_config, None, self)
        project_config.set_keychain(keychain)
        return project_config

    def get_org(self, project_config, retries=3):
        self.logger = init_logger(self)
        attempt = 1
        if self.org:
            # If the build's org was already set, keep using it
            org_name = self.org.name
        else:
            org_name = self.plan.org
        while True:
            try:
                org_config = project_config.keychain.get_org(org_name)
                break
            except ScratchOrgException as e:
                if (
                    str(e).startswith(FAILED_TO_CREATE_SCRATCH_ORG)
                    and attempt <= retries
                ):
                    self.logger.warning(str(e))
                    self.logger.info(
                        "Retrying create scratch org "
                        + f"(retry {attempt} of {retries})"
                    )
                    attempt += 1
                    continue
                else:
                    raise e
        self.org = org_config.org
        if self.current_rebuild:
            self.current_rebuild.org_instance = org_config.org_instance
            self.current_rebuild.save()
        else:
            self.org_instance = org_config.org_instance
        self.save()
        return org_config

    def get_org_instance(self):
        if self.current_rebuild and self.current_rebuild.org_instance:
            return self.current_rebuild.org_instance
        else:
            return self.org_instance

    def get_org_attr(self, attr):
        org_instance = self.get_org_instance()
        obj = getattr(org_instance, attr, "")
        return obj() if callable(obj) else obj

    def get_org_deleted(self):
        return self.get_org_attr("deleted")

    def get_org_sf_org_id(self):
        return self.get_org_attr("sf_org_id")

    def get_org_name(self):
        return self.get_org_attr("__str__")

    def get_org_time_deleted(self):
        return self.get_org_attr("time_deleted")

    def get_org_url(self):
        return self.get_org_attr("get_absolute_url")

    def get_org_username(self):
        return self.get_org_attr("username")

    def delete_org(self, org_config):
        self.logger = init_logger(self)
        if not org_config.scratch:
            return
        if self.keep_org:
            self.logger.info(
                "Skipping scratch org deletion since keep_org was requested"
            )
            return
        if self.status == "error" and self.plan.keep_org_on_error:
            self.logger.info(
                "Skipping scratch org deletion since keep_org_on_error is enabled"
            )
            return
        if self.status == "fail" and self.plan.keep_org_on_fail:
            self.logger.info(
                "Skipping scratch org deletion since keep_org_on_fail is enabled"
            )
            return

        try:
            org_instance = self.get_org_instance()
            org_instance.delete_org(org_config)
        except Exception as e:
            self.logger.error(str(e))
            self.save()

    def delete_build_dir(self):
        if hasattr(self, "build_dir"):
            self.logger.info(f"Deleting build dir {self.build_dir}")
            shutil.rmtree(self.build_dir)
            self.save()


class BuildFlow(models.Model):
    build = models.ForeignKey(
        "build.Build", related_name="flows", on_delete=models.CASCADE
    )
    rebuild = models.ForeignKey(
        "build.Rebuild",
        related_name="flows",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    status = models.CharField(
        max_length=16, choices=BUILD_FLOW_STATUSES, default="queued"
    )
    flow = models.CharField(max_length=255, null=True, blank=True)
    log = models.TextField(null=True, blank=True)
    exception = models.TextField(null=True, blank=True)
    traceback = models.TextField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    time_queue = models.DateTimeField(auto_now_add=True)
    time_start = models.DateTimeField(null=True, blank=True)
    time_end = models.DateTimeField(null=True, blank=True)
    tests_total = models.IntegerField(null=True, blank=True)
    tests_pass = models.IntegerField(null=True, blank=True)
    tests_fail = models.IntegerField(null=True, blank=True)
    asset_hash = models.CharField(max_length=64, unique=True, default=generate_hash)

    def __str__(self):
        return f"{self.build.id}: {self.build.repo} - {self.build.commit} - {self.flow}"

    def get_absolute_url(self):
        return (
            reverse("build_detail", kwargs={"build_id": str(self.build.id)})
            + f"#flow-{self.flow}"
        )

    def get_log_html(self):
        if self.log:
            return format_log(self.log)

    def run(self, project_config, org_config, root_dir):
        self.root_dir = root_dir
        # Record the start
        set_build_info(self, status="running", time_start=timezone.now())

        # Update github status
        if settings.GITHUB_STATUS_UPDATES_ENABLED:
            set_github_status.delay(self.build_id)

        # Set up logger
        self.logger = init_logger(self)

        try:
            # Run the flow
            self.run_flow(project_config, org_config)

            # Determine build commit status
            self.set_commit_status()

            # Load test results
            self.load_test_results()

            # Record result
            exception = None
            status = "success"

        except FAIL_EXCEPTIONS as e:
            self.logger.error(traceback.format_exc())
            exception = e
            self.load_test_results()
            status = "fail"

        except Exception as e:
            self.logger.error(traceback.format_exc())
            exception = e
            status = "error"

        kwargs = {"status": status, "time_end": timezone.now()}
        if exception:
            kwargs["error_message"] = str(exception)
            kwargs["exception"] = exception.__class__.__name__
            kwargs["traceback"] = "".join(traceback.format_tb(exception.__traceback__))
        set_build_info(self, **kwargs)

    def run_flow(self, project_config, org_config):
        # Add the repo root to syspath to allow for custom tasks and flows in
        # the repo
        sys.path.append(project_config.repo_root)

        flow_config = project_config.get_flow(self.flow)

        # If it's a release build, pass the dates in
        options = self._get_flow_options()

        callbacks = None
        if settings.METACI_FLOW_CALLBACK_ENABLED:
            from metaci.build.flows import MetaCIFlowCallback

            callbacks = MetaCIFlowCallback(buildflow_id=self.pk)

        # Create the flow and handle initialization exceptions
        self.flow_instance = FlowCoordinator(
            project_config,
            flow_config,
            name=self.flow,
            options=options,
            callbacks=callbacks,
        )

        # Run the flow
        return self.flow_instance.run(org_config)

    def _get_flow_options(self) -> dict:
        options = {}
        if self.build.plan.role == "release" and self.build.release:
            options["github_release_notes"] = {
                "sandbox_date": self.build.release.sandbox_push_date,
                "production_date": self.build.release.production_push_date,
            }
        if (
            self.build.plan.role == "push_sandbox" and self.build.release
        ):  # override lives in MetaCI
            options["push_sandbox"] = {
                "version": f"{self.build.release.version_number}",
            }
        if (
            self.build.plan.role == "push_production" and self.build.release
        ):  # override lives in MetaCI
            options["push_all"] = {
                "version": f"{self.build.release.version_number}",
            }

        return options

    def set_commit_status(self):
        if self.build.plan.commit_status_template:
            template = jinja2_env.from_string(self.build.plan.commit_status_template)
            message = template.render(results=self.flow_instance.results)
            self.build.commit_status = message
            self.build.save()

    def record_result(self):
        self.status = "success"
        self.time_end = timezone.now()
        self.save()

    def load_test_results(self):
        """Import results from JUnit or test_results.json.

        Robot Framework results are imported in MetaCIFlowCallback.post_task
        """
        # Load JUnit
        results = []
        if self.build.plan.junit_path:
            for filename in iglob(self.build.plan.junit_path):
                results.extend(self.load_junit(filename))
            if not results:
                self.logger.warning(
                    f"No results found at JUnit path {self.build.plan.junit_path}"
                )
        if results:
            import_test_results(self, results, "JUnit")

        # Load from test_results.json
        results = []
        try:
            results_filename = "test_results.json"
            with open(results_filename, "r") as f:
                results.extend(json.load(f))
            for result in results:
                result["SourceFile"] = results_filename
        except IOError:
            try:
                results_filename = "test_results.xml"
                results.extend(self.load_junit(results_filename))
            except IOError:
                pass

        if results:
            import_test_results(self, results, "Apex")

        self.tests_total = self.test_results.count()
        self.tests_pass = self.test_results.filter(outcome="Pass").count()
        self.tests_fail = self.test_results.filter(
            outcome__in=["Fail", "CompileFail"]
        ).count()
        self.save()

    def load_junit(self, filename):
        results = []
        tree = elementtree_parse_file(filename)
        testsuite = tree.getroot()
        for testcase in testsuite.iter("testcase"):
            result = {
                "ClassName": testcase.attrib["classname"],
                "Method": testcase.attrib["name"],
                "Outcome": "Pass",
                "StackTrace": "",
                "Message": "",
                "Stats": {"duration": testcase.get("time")},
                "SourceFile": filename,
            }
            for element in testcase.iter():
                if element.tag not in ["failure", "error"]:
                    continue
                result["Outcome"] = "Fail"
                if element.text:
                    result["StackTrace"] += element.text + "\n"
                message = element.get("type", "")
                if element.get("message"):
                    message += ": " + element.get("message", "")
                    result["Message"] += message + "\n"
            results.append(result)
        return results


def asset_upload_to(instance, filename):
    folder = instance.build_flow.asset_hash
    return os.path.join(folder, filename)


class BuildFlowAsset(models.Model):
    build_flow = models.ForeignKey(
        BuildFlow, related_name="assets", on_delete=models.CASCADE
    )
    asset = models.FileField(upload_to=asset_upload_to)
    category = models.CharField(max_length=1024)


class Rebuild(models.Model):
    build = models.ForeignKey(
        "build.Build", related_name="rebuilds", on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        "users.User", related_name="rebuilds", on_delete=models.PROTECT
    )
    org_instance = models.ForeignKey(
        "cumulusci.ScratchOrgInstance",
        related_name="rebuilds",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    status = models.CharField(max_length=16, choices=BUILD_STATUSES, default="queued")
    exception = models.TextField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    qa_comment = models.TextField(null=True, blank=True)
    qa_user = models.ForeignKey(
        "users.User", related_name="rebuilds_qa", null=True, on_delete=models.PROTECT
    )
    time_queue = models.DateTimeField(auto_now_add=True)
    time_start = models.DateTimeField(null=True, blank=True)
    time_end = models.DateTimeField(null=True, blank=True)
    time_qa_start = models.DateTimeField(null=True, blank=True)
    time_qa_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]

    def get_absolute_url(self):
        return reverse(
            "build_detail",
            kwargs={"build_id": str(self.build.id), "rebuild_id": str(self.id)},
        )


class FlowTaskManager(models.Manager):

    # TODO: refactor to use step strings?
    def find_task(self, build_flow_id, path, step_num):
        try:
            return self.get(build_flow_id=build_flow_id, path=path, stepnum=step_num)
        except ObjectDoesNotExist:
            return FlowTask(build_flow_id=build_flow_id, path=path, stepnum=step_num)


class FlowTask(models.Model):
    """ A FlowTask holds the result of a task execution during a BuildFlow. """

    time_start = models.DateTimeField(null=True, blank=True)
    time_end = models.DateTimeField(null=True, blank=True)
    # time_initialize = models.DateTimeField(null=True, blank=True)

    description = models.TextField(null=True, blank=True)
    stepnum = models.CharField(
        max_length=64, help_text="dotted step number for CCI task"
    )
    path = models.CharField(
        max_length=2048, help_text="dotted path e.g. flow1.flow2.task_name"
    )
    class_path = models.TextField(null=True, blank=True)
    options = JSONField(null=True, blank=True, encoder=GnarlyEncoder)
    result = JSONField(null=True, blank=True, encoder=GnarlyEncoder)
    return_values = JSONField(null=True, blank=True, encoder=GnarlyEncoder)
    exception = models.CharField(max_length=255, null=True, blank=True)
    exception_value = JSONField(null=True, blank=True, encoder=GnarlyEncoder)

    status = models.CharField(
        max_length=16, choices=FLOW_TASK_STATUSES, default="queued"
    )

    build_flow = models.ForeignKey(
        "build.BuildFlow", related_name="tasks", on_delete=models.CASCADE
    )

    objects = FlowTaskManager()

    def __str__(self):
        return f"{self.build_flow_id}: {self.stepnum} - {self.path}"

    class Meta:
        ordering = ["-build_flow", "stepnum"]
        verbose_name = "Flow Task"
        verbose_name_plural = "Flow Tasks"
