from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.contrib.auth.models import User
from django.contrib.auth.views import PasswordChangeView
from django.core import signing
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import FormView, ListView, TemplateView, UpdateView

from .. import user_roles
from ..forms import AccountEditForm, CreateUserForm
from ..models import ERPUserSettings
from ..role_registry import STAFF_ROLE
from ..roles import grant_role
from .mixins import ERPViewMixin


class AccountView(ERPViewMixin, TemplateView):
    template_name = "erp/account.html"
    current_section = "account"
    page_title = "My Account"


class AccountEditView(ERPViewMixin, UpdateView):
    template_name = "erp/account_edit.html"
    form_class = AccountEditForm
    current_section = "account"
    page_title = "Edit Profile"
    success_url = reverse_lazy("erp-account")

    def get_object(self):
        return self.request.user

    def form_valid(self, form):
        messages.success(self.request, "Profile updated successfully.")
        return super().form_valid(form)


class AccountPasswordChangeView(ERPViewMixin, PasswordChangeView):
    template_name = "erp/password_change.html"
    current_section = "account"
    page_title = "Change Password"
    success_url = reverse_lazy("erp-account")

    def form_valid(self, form):
        messages.success(self.request, "Your password has been changed successfully.")
        response = super().form_valid(form)
        # Clear forced password change flag if set
        erp_settings = getattr(self.request.user, "erp_settings", None)
        if erp_settings and erp_settings.must_change_password:
            erp_settings.must_change_password = False
            erp_settings.save(update_fields=["must_change_password"])
        return response


class StaffRequiredMixin(UserPassesTestMixin):
    login_url = "/erp/login/"

    def test_func(self):
        from ..permissions import is_staff_principal

        return is_staff_principal(self.request.user)


class UserListView(ERPViewMixin, StaffRequiredMixin, ListView):
    template_name = "erp/user_list.html"
    current_section = "users"
    page_title = "Users"
    context_object_name = "users"

    def get_queryset(self):
        return User.objects.order_by("-date_joined").prefetch_related("groups")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        for user in context["users"]:
            user.role_labels = user_roles.role_labels(user)
        context["can_manage_roles"] = user_roles.can_manage(self.request.user)
        return context


class UserRolesView(ERPViewMixin, StaffRequiredMixin, TemplateView):
    """Grant and revoke a user's roles (docs/roles.md; harness R10.02).

    Reading the page and changing roles both need ``auth.change_user``:
    the page is the change form. Service roles are not offered; a
    superuser's own flag is never touched; a user cannot edit their own
    roles here (the same self-lockout guard as activate/deactivate).
    """

    template_name = "erp/user_roles.html"
    current_section = "users"
    page_title = "Roles"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not user_roles.can_manage(request.user):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("Changing roles needs the auth.change_user permission.")
        return super().dispatch(request, *args, **kwargs)

    def _user(self):
        return get_object_or_404(User, pk=self.kwargs["pk"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        target = self._user()
        context["target"] = target
        context["rows"] = user_roles.role_rows(target)
        context["is_self"] = target == self.request.user
        context["page_title"] = f"Roles for {target.username}"
        return context

    def post(self, request, pk):
        target = self._user()
        if target == request.user:
            messages.error(request, "You cannot change your own roles.")
            return redirect("erp-user-list")
        wanted = request.POST.getlist("roles")
        try:
            granted, revoked = user_roles.set_roles(target, wanted)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("erp-user-roles", pk=target.pk)
        target.refresh_from_db()
        parts = []
        if granted:
            parts.append("granted " + ", ".join(granted))
        if revoked:
            parts.append("revoked " + ", ".join(revoked))
        summary = "; ".join(parts) if parts else "no change"
        access = "has back-office access" if target.is_staff else "has no back-office access"
        messages.success(request, f'Roles for "{target.username}": {summary}. The user now {access}.')
        return redirect("erp-user-list")


class UserToggleActiveView(StaffRequiredMixin, View):
    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        if user == request.user:
            messages.error(request, "You cannot disable your own account.")
            return redirect("erp-user-list")
        user.is_active = not user.is_active
        user.save(update_fields=["is_active"])
        state = "enabled" if user.is_active else "disabled"
        messages.success(request, f'User "{user.username}" has been {state}.')
        return redirect("erp-user-list")


class UserCreateView(ERPViewMixin, StaffRequiredMixin, FormView):
    template_name = "erp/user_create.html"
    current_section = "users"
    page_title = "Create User"
    form_class = CreateUserForm
    success_url = reverse_lazy("erp-user-list")

    def form_valid(self, form):
        user = User.objects.create_user(
            username=form.cleaned_data["username"],
            email=form.cleaned_data["email"],
            password=form.cleaned_data["password1"],
            first_name=form.cleaned_data.get("first_name", ""),
            last_name=form.cleaned_data.get("last_name", ""),
        )
        # Back-office access comes from the staff role; core.roles derives
        # is_staff from it (identity-unification D3). The form may pick
        # another human role to start with (R10.02); staff is the default.
        grant_role(user, form.cleaned_data.get("role") or STAFF_ROLE)
        ERPUserSettings.objects.create(
            user=user, must_change_password=True, email_verified=False
        )
        # Send verification email
        self._send_verification_email(user)
        messages.success(
            self.request,
            f'User "{user.username}" created. They will be required to change '
            f"their password on first login. A verification email has been sent.",
        )
        return super().form_valid(form)

    def _send_verification_email(self, user):
        token = signing.dumps(user.pk, salt="email-verify")
        verify_url = self.request.build_absolute_uri(
            reverse("erp-verify-email", kwargs={"token": token})
        )
        body = render_to_string(
            "erp/email_verify_email.html",
            {"user": user, "verify_url": verify_url},
        )
        send_mail(
            subject="Verify your email address",
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
        )


class EmailVerifyView(TemplateView):
    template_name = "erp/email_verify.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        token = self.kwargs["token"]
        try:
            user_pk = signing.loads(token, salt="email-verify", max_age=86400 * 7)
            user = User.objects.get(pk=user_pk)
            settings_obj, _ = ERPUserSettings.objects.get_or_create(user=user)
            settings_obj.email_verified = True
            settings_obj.save(update_fields=["email_verified"])
            context["success"] = True
            context["verified_user"] = user
        except (signing.BadSignature, User.DoesNotExist):
            context["success"] = False
        return context
