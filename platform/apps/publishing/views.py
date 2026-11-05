from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services


class PublishingStatusView(APIView):
    """Audit status and quota, so a client can explain private uploads and delays."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.status_summary())
