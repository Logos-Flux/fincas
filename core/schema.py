"""OpenAPI 3.0 schema extensions and security definitions for Fincas."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class BearerTokenScheme(OpenApiAuthenticationExtension):
    target_class = "core.authentication.BearerTokenAuthentication"
    name = "BearerAuth"

    def get_security_requirement(self, auto_schema):
        return {self.name: []}

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Session or service account JWT token.",
        }


class APIKeyScheme(OpenApiAuthenticationExtension):
    target_class = "core.authentication.APIKeyAuthentication"
    name = "ApiKeyAuth"

    def get_security_requirement(self, auto_schema):
        return {self.name: []}

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Server-to-server API Key authentication.",
        }


class SupabaseJWTScheme(OpenApiAuthenticationExtension):
    target_class = "core.portal_auth.PortalTokenAuthentication"
    name = "SupabaseJWT"

    def get_security_requirement(self, auto_schema):
        return {self.name: []}

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Customer portal provider JWT (Supabase or another configured portal provider).",
        }
