from django.shortcuts import render
from metaci.api.serializers.cumulusci import OrgSerializer
from metaci.api.serializers.cumulusci import ScratchOrgInstanceSerializer
from metaci.api.serializers.cumulusci import ServiceSerializer
from metaci.cumulusci.filters import OrgFilter
from metaci.cumulusci.filters import ScratchOrgInstanceFilter
from metaci.cumulusci.models import Org
from metaci.cumulusci.models import ScratchOrgInstance
from metaci.cumulusci.models import Service
from rest_framework import viewsets


class OrgViewSet(viewsets.ModelViewSet):
    """
    A viewset for viewing and editing Orgs
    """

    serializer_class = OrgSerializer
    queryset = Org.objects.all()
    filterset_class = OrgFilter


class ScratchOrgInstanceViewSet(viewsets.ModelViewSet):
    """
    A viewset for viewing and editing ScratchOrgInstances
    """

    serializer_class = ScratchOrgInstanceSerializer
    queryset = ScratchOrgInstance.objects.all()
    filterset_class = ScratchOrgInstanceFilter


class ServiceViewSet(viewsets.ModelViewSet):
    """
    A viewset for viewing and editing Services
    """

    serializer_class = ServiceSerializer
    queryset = Service.objects.all()
