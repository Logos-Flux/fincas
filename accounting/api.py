"""Accounting DRF API (Step 1b): fiscal periods + close/reopen actions."""

from django.db.models import Count, F, Q
from rest_framework import status as drf_status
from rest_framework.decorators import action
from rest_framework.response import Response

from core.views import CompanyScopedViewSet

from .models import FiscalPeriod, JournalEntry
from .periods import PeriodCloseError, close_period, reopen_period
from .serializers import FiscalPeriodSerializer


class FiscalPeriodViewSet(CompanyScopedViewSet):
    queryset = FiscalPeriod.objects.order_by("-start_date")
    serializer_class = FiscalPeriodSerializer
    filterset_fields = ["status", "company"]
    search_fields = ["name"]
    ordering_fields = ["start_date", "end_date"]

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.annotate(
            draft_entry_count=Count(
                "company__journal_entries",
                filter=Q(
                    company__journal_entries__status=JournalEntry.Status.DRAFT,
                    company__journal_entries__entry_date__gte=F("start_date"),
                    company__journal_entries__entry_date__lte=F("end_date"),
                ),
            )
        )

    def _refuse(self, exc: PeriodCloseError):
        return Response(
            {"detail": str(exc)}, status=drf_status.HTTP_409_CONFLICT
        )

    @action(detail=True, methods=["post"], url_path="close")
    def close(self, request, pk=None):
        if not request.user.is_staff:
            return Response(
                {"detail": "Staff only."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        period = self.get_object()
        try:
            close_period(period)
        except PeriodCloseError as exc:
            return self._refuse(exc)
        return Response(self.get_serializer(period).data)

    @action(detail=True, methods=["post"], url_path="reopen")
    def reopen(self, request, pk=None):
        if not request.user.is_staff:
            return Response(
                {"detail": "Staff only."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        period = self.get_object()
        try:
            reopen_period(period)
        except PeriodCloseError as exc:
            return self._refuse(exc)
        return Response(self.get_serializer(period).data)
