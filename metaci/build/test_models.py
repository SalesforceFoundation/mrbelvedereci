from django.test import TestCase
from metaci.build.models import Build
from metaci.conftest import (
    RepositoryFactory,
    BranchFactory,
    PlanFactory,
    PlanRepositoryFactory,
    PlanScheduleFactory,
)


# Create your tests here.
class BuildTestCase(TestCase):
    def test_empty_build_init(self):
        build = Build()
        self.assertTrue(build)

    def test_scheduled_build_init(self):
        repo = RepositoryFactory()
        branch = BranchFactory(name="branch", repo=repo)
        schedule = PlanScheduleFactory(branch=branch)
        plan = PlanFactory()
        build = Build(
            repo=branch.repo,
            plan=plan,
            branch=branch,
            commit="shashasha",
            schedule=schedule,
            build_type="scheduled",
        )
        self.assertTrue(build.planrepo)
        build.save()

    def test_planrepo_create_on_build_init(self):
        repo = RepositoryFactory()
        plan = PlanFactory()

        build = Build(repo=repo, plan=plan)
        self.assertTrue(build.planrepo)

    def test_planrepo_find_on_build_init(self):
        repo = RepositoryFactory()
        plan = PlanFactory()
        planrepo = PlanRepositoryFactory(plan=plan, repo=repo)

        build = Build(repo=repo, plan=plan)
        self.assertEqual(build.planrepo, planrepo)
