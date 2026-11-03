from rest_framework import status, viewsets
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from core.managers import scope_to_company
from core.models import AuditLog, Company, Team, UserProfile
from core.permissions import PrincipalPermission
from core.serializers import (
    AuditLogSerializer,
    CompanySerializer,
    TeamSerializer,
    UserProfileSerializer,
)
from core.sidecar_api import WARNING_ATTR as SIDECAR_IGNORED_ATTR
from core.sidecar_api import warning_header as sidecar_warning_header


def request_company(request):
    """Return the requester's company, or None when scoping cannot apply.

    None covers anonymous users, superusers (cross-company service
    account by definition), and authenticated users with no resolvable
    UserProfile.company link. Callers must fail closed on None.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    if user.is_superuser:
        return None
    return getattr(getattr(user, "userprofile", None), "company", None)


NO_TENANT_API = (
    "Several companies exist and this account has none: name the tenant with "
    "company=<uuid>."
)


def sole_company():
    """The only Company on a single-company instance, else None.

    Deliberately not ``GOLDEN_PORTAL_COMPANY``: that tenant belongs to the
    portal and storefront channels. A staff or API create with no company of
    its own lands in the only tenant there is, or is refused
    (phase4-fields.md 6, items 2 and 3).
    """
    only = list(Company.objects.all()[:2])
    return only[0] if len(only) == 1 else None


def staff_create_company(user):
    """The tenant a back-office create by ``user`` lands in, or None (refuse).

    The user's profile company (a superuser's too: the back office acts as
    the person), else the only Company on a single-company instance.
    """
    company = getattr(getattr(user, "userprofile", None), "company", None)
    return company if company is not None else sole_company()


def _resolve_path(obj, path):
    """Traverse a ``__``-separated FK path, returning None if any hop is None."""
    for bit in path.split("__"):
        obj = getattr(obj, bit, None)
        if obj is None:
            return None
    return obj


class CompanyScopedViewSet(viewsets.ModelViewSet):
    """Mixin-style base: overrides get_queryset() to filter by the
    authenticated user's company via CompanyScopedManager.for_request().

    Viewsets whose model has a `company` FK should inherit from this
    instead of ModelViewSet directly. The queryset class attribute still
    defines the base (unscoped) queryset for DRF's schema introspection;
    get_queryset() narrows it at runtime.
    """

    # Explicit auth: never rely on the settings default (test settings
    # inherit dev's AllowAny and DRF binds permission_classes at import —
    # see testing-plan §4 gotcha / F-003). PrincipalPermission (D6) admits
    # staff humans and superusers, holds the low-privilege storefront
    # identity to its guest contract (F-037: catalog reads and guest order
    # creation, nothing else), and 403s every other principal, including a
    # customer-only account with a session.
    permission_classes = [PrincipalPermission]

    # Direct company FK name on the model. Child viewsets with no direct
    # FK set this to None and declare parent_links instead.
    company_field = "company"
    # Parent-link checks for child viewsets: list of
    # (parent_field, company_path_relative_to_parent). The parent's
    # company must equal the requester's company on create/update.
    # A null parent, or a parent whose company cannot be resolved
    # (global record), skips the check — reads still fail closed.
    parent_links: list[tuple[str, str]] = []
    # Refuse a create that would leave ``company_field`` NULL: a superuser or
    # the legacy API key naming no company on a multi-company instance gets a
    # 400 ``no_tenant``; on a single-company instance the row gets the only
    # Company (phase4-fields.md 6, item 3).
    require_company = False

    def get_queryset(self):
        # Child viewsets (company_field = None) apply their own
        # parent-traversal scoping in their override; the base must not
        # filter on a `company` field they do not have.
        if self.company_field is None:
            return super().get_queryset()
        # Scope the viewset's OWN base queryset (which already carries any
        # select_related/prefetch_related declared on the subclass) so the
        # company filter preserves those optimizations instead of rebuilding
        # an unoptimized queryset from the model manager (N+1 on detail/custom
        # actions). scope_to_company() applies the same fail-closed logic as
        # CompanyScopedQuerySet.for_request(), but works on ANY queryset —
        # including models with a `company` field whose manager is the plain
        # default Manager (no for_request), which the old getattr-based path
        # silently returned UNSCOPED. Every viewset inheriting this base has a
        # direct `company` field; models without one (e.g. global TaxRate) must
        # not subclass CompanyScopedViewSet.
        return scope_to_company(super().get_queryset(), self.request)

    def _enforce_company(self, serializer, instance=None):
        """Block cross-company writes (S6 mass-assignment guard).

        - Direct company FK: on create, an explicitly different company
          is a 400; an absent one is filled with the requester's. On
          update, pinning the record to another company is a 400.
        - Parent links: the linked parent must belong to the requester's
          company (400 otherwise).
        Superusers / unresolved companies skip enforcement (reads still
        fail closed via get_queryset).
        """
        company = request_company(self.request)
        if company is None:
            return {}
        extra = {}
        data = serializer.validated_data
        if self.company_field:
            requested = data.get(self.company_field, None)
            if instance is None:
                if requested is not None and requested.pk != company.pk:
                    raise ValidationError(
                        {self.company_field: "Cannot create records for another company."}
                    )
                extra[self.company_field] = company
            elif requested is not None and requested.pk != company.pk:
                raise ValidationError(
                    {self.company_field: "Cannot move records to another company."}
                )
            elif requested is None and self.company_field in data:
                # An explicit null would hide the row from its own tenant.
                raise ValidationError(
                    {self.company_field: "Cannot remove the company from a record."}
                )
        for parent_field, company_path in self.parent_links:
            parent = data.get(parent_field, None)
            if parent is None and instance is not None:
                parent = getattr(instance, parent_field, None)
            if parent is None:
                continue
            parent_company = _resolve_path(parent, company_path)
            if parent_company is not None and parent_company.pk != company.pk:
                raise ValidationError(
                    {parent_field: "Linked record belongs to another company."}
                )
        return extra

    def create(self, request, *args, **kwargs):
        """Pre-fill the company FK before serializer validation.

        ``company`` is a required field on most models, so validation
        would 400 before perform_create could fill it. A company user
        omitting it (or repeating their own) gets it stamped with their
        company; naming another company is a 400 here, not a silent
        override. Superusers pass through untouched.
        """
        company = request_company(request)
        if company is not None and self.company_field:
            data = request.data
            requested = data.get(self.company_field, None) if hasattr(data, "get") else None
            if requested is None or str(requested) == str(company.pk):
                mutable = data.copy() if hasattr(data, "copy") else dict(data)
                mutable[self.company_field] = str(company.pk)
                serializer = self.get_serializer(data=mutable)
                serializer.is_valid(raise_exception=True)
                self.perform_create(serializer)
                headers = self.get_success_headers(serializer.data)
                return Response(
                    serializer.data, status=201, headers=headers
                )
            raise ValidationError(
                {self.company_field: "Cannot create records for another company."}
            )
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        extra = self._enforce_company(serializer)
        field = self.company_field
        if (
            self.require_company and field
            and extra.get(field) is None
            and serializer.validated_data.get(field) is None
        ):
            company = sole_company()
            if company is None:
                raise ValidationError({field: [ErrorDetail(NO_TENANT_API, code="no_tenant")]})
            extra[field] = company
        serializer.save(**extra)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        # Keys a ``metadata`` write ignored (core.sidecar_api): say so.
        ignored = getattr(request, SIDECAR_IGNORED_ATTR, None)
        if ignored and 200 <= response.status_code < 300:
            response["Warning"] = sidecar_warning_header(ignored)
        return response

    def perform_update(self, serializer):
        self._enforce_company(serializer, instance=serializer.instance)
        field = self.company_field
        if (
            self.require_company and field
            and field in serializer.validated_data
            and serializer.validated_data[field] is None
        ):
            # A superuser clearing the company: _enforce_company lets it through.
            raise ValidationError({field: [ErrorDetail(NO_TENANT_API, code="no_tenant")]})
        serializer.save()


class CompanyViewSet(viewsets.ModelViewSet):
    # Tenant registry + identity links + audit trail: staff-admin only.
    # Any authenticated user could otherwise enumerate every tenant,
    # harvest profile links, and read the audit log.
    permission_classes = [IsAdminUser]
    queryset = Company.objects.all()
    serializer_class = CompanySerializer
    search_fields = ["name"]
    ordering_fields = ["name", "created_at"]

    def create(self, request, *args, **kwargs):
        """One instance per company (core.E070): a second Company is refused
        with ``409`` and the stable code ``one_company_per_instance``."""
        from core.checks import ONE_COMPANY_CODE, one_company_message

        existing = list(Company.objects.order_by("name", "pk").values_list("pk", "name"))
        if existing:
            return Response(
                {"detail": one_company_message(existing), "code": ONE_COMPANY_CODE},
                status=status.HTTP_409_CONFLICT,
            )
        return super().create(request, *args, **kwargs)


class UserProfileViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdminUser]
    queryset = UserProfile.objects.all()
    serializer_class = UserProfileSerializer
    filterset_fields = ["company"]
    search_fields = ["display_name"]


class TeamViewSet(CompanyScopedViewSet):
    """``teams/`` (phase4-fields.md 2.1): every staff user reads the tenant's
    teams; creating or editing one needs ``core.manage_teams``. No DELETE: an
    archived team (``is_active=false``) stays on the records stamped with it.
    """

    permission_classes = [PrincipalPermission]
    queryset = Team.objects.all()
    serializer_class = TeamSerializer
    filterset_fields = ["is_active"]
    ordering_fields = ["sort_order", "name", "key"]
    ordering = ["sort_order", "name"]
    http_method_names = ["get", "post", "patch", "head", "options"]
    require_company = True

    def check_permissions(self, request):
        super().check_permissions(request)
        # Before validation, so a caller without the permission learns nothing.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not request.user.has_perm("core.manage_teams"):
            self.permission_denied(request, message="Editing teams needs core.manage_teams.")


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAdminUser]
    queryset = AuditLog.objects.all()
    serializer_class = AuditLogSerializer
    filterset_fields = ["action", "model_name"]
    search_fields = ["model_name"]
    ordering_fields = ["created_at"]
