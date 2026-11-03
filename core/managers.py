from django.db import models


def scope_to_company(queryset, request, field="company"):
    """Fail-closed company scoping shared by parent and child viewsets.

    SOL-16 must never leak cross-company rows, and must never crash:

    - Unauthenticated request -> no rows. (request.user is AnonymousUser,
      which has no `userprofile`; returning .none() yields a safe 404, not
      an AttributeError 500.)
    - Superuser -> unfiltered. The shared API service account authenticates
      as a superuser and is cross-company by definition; row-level company
      scoping does not apply to it. (It also has no `userprofile`, so direct
      attribute access would crash it too.)
    - Authenticated user -> filter `field` to the user's UserProfile.company.
      If no profile/company can be resolved (e.g. the JWT->UserProfile link
      is not yet wired up), fail closed to no rows rather than leaking.

    `field` lets child/line-item viewsets scope via an FK traversal
    (e.g. "order__company") with the exact same semantics as a model whose
    own `company` FK is scoped directly.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return queryset.none()
    if user.is_superuser:
        return queryset
    company = getattr(getattr(user, "userprofile", None), "company", None)
    if company is None:
        return queryset.none()
    return queryset.filter(**{field: company})


class CompanyScopedQuerySet(models.QuerySet):
    def for_company(self, company):
        return self.filter(company=company)

    def for_request(self, request):
        """Scope rows to the requesting user's company (see scope_to_company)."""
        return scope_to_company(self, request)


class CompanyScopedManager(models.Manager):
    def get_queryset(self):
        return CompanyScopedQuerySet(self.model, using=self._db)

    def for_company(self, company):
        return self.get_queryset().for_company(company)

    def for_request(self, request):
        return self.get_queryset().for_request(request)
