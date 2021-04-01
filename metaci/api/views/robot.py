import datetime

import dateutil.parser
from dateutil.relativedelta import MO, relativedelta
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import BrowsableAPIRenderer, JSONRenderer

from metaci.api.renderers.csv_renderer import SimpleCSVRenderer
from metaci.api.serializers.robot import RobotTestResultSerializer
from metaci.build.models import BuildFlow
from metaci.plan.models import PlanRepository
from metaci.testresults.filters import RobotResultFilter
from metaci.testresults.models import TestResult


class RobotTestResultViewSet(viewsets.ReadOnlyModelViewSet):
    """
    A viewset for viewing robot framework test results
    """

    serializer_class = RobotTestResultSerializer
    renderer_classes = [BrowsableAPIRenderer, JSONRenderer, SimpleCSVRenderer]
    filterset_class = RobotResultFilter
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Return a query set for robot results

        'from' and 'to' query parameters can be used to constrain
        the results. If not provided, and the 'range' parameter hasn't
        been provided, results for the current day will be returned.

        'range' can be used to more easily define a range. 'range'
        can be 'today', 'thisweek', 'lastweek', 'thismonth', or
        'lastmonth'.  If you use both from/to and range, only the
        range will have any effect.

        In the following example, results will be returnd for the entire
        month of April, 2020:

            /api/robot?from=2020-04-01&to=2020-04-30

        """
        date_range = self.request.query_params.get("range", None)
        if date_range:
            start_date, end_date = self._get_date_range(date_range)
        else:
            # no range? Look for from/to with "from" defaulting to
            # the start of today. If "to" is not provided, the results
            # for a single day will be returned.
            from_ = self.request.query_params.get("from", "00:00:00")
            start_date = dateutil.parser.parse(from_).date()

            to_ = self.request.query_params.get("to", from_)
            end_date = dateutil.parser.parse(to_)
            end_date = end_date.date() + relativedelta(days=1)

        assert start_date <= end_date

        buildflows = BuildFlow.objects.filter(
            time_end__date__gte=start_date,
            time_end__date__lt=end_date,
            build__planrepo__in=PlanRepository.objects.for_user(self.request.user),
        )

        repo_name = self.request.query_params.get("repo_name", None)
        if repo_name is not None:
            buildflows = buildflows.filter(build__repo__name=repo_name)

        branch_name = self.request.query_params.get("branch_name", None)
        if branch_name is not None:
            buildflows = buildflows.filter(build__branch__name=branch_name)

        queryset = (
            TestResult.objects.filter(
                method__testclass__test_type="Robot",
                build_flow_id__in=buildflows,
            )
            .prefetch_related(
                "build_flow__build__branch",
                "build_flow__build__repo",
                "method__testclass",
            )
            .order_by("build_flow__time_end")
        )

        return queryset

    def _get_today(self):
        """Return today's date as a datetime.date object

        This is done in a method so that it can be easily mocked out
        for tests.

        """
        today = datetime.date.today()
        return today

    def _get_date_range(self, date_range):
        """Returns a start and end date for a given period.

        These dates are used to in a query to pick rows on or
        after the start date but before the end date.

        """
        today = self._get_today()
        if date_range == "today":
            start_date = today
            end_date = start_date + relativedelta(days=1)
        elif date_range == "yesterday":
            start_date = today + relativedelta(days=-1)
            end_date = today
        elif date_range == "thisweek":
            start_date = today + relativedelta(weekday=MO(-1))
            end_date = today + relativedelta(days=1)
        elif date_range == "lastweek":
            start_date = today + relativedelta(weekday=MO(-2))
            end_date = start_date + relativedelta(weeks=1)
        elif date_range == "thismonth":
            start_date = today + relativedelta(day=1)
            end_date = today + relativedelta(months=1, day=1)
        elif date_range == "lastmonth":
            start_date = today + relativedelta(months=-1, day=1)
            end_date = start_date + relativedelta(months=1, day=1)
        else:
            raise Exception(f"invalid value '{date_range}' for date_range parameter.")

        return start_date, end_date
