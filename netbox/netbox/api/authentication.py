import logging

from django.conf import settings
from django.utils import timezone
from rest_framework import authentication, exceptions
from rest_framework.permissions import BasePermission, DjangoObjectPermissions, SAFE_METHODS

from netbox.config import get_config
from users.models import Token


class TokenAuthentication(authentication.TokenAuthentication):
    """
    A custom authentication scheme which enforces Token expiration times.
    """
    model = Token

    def authenticate_credentials(self, key):
        model = self.get_model()
        try:
            token = model.objects.prefetch_related('user').get(key=key)
        except model.DoesNotExist:
            raise exceptions.AuthenticationFailed("Invalid token")

        # Update last used, but only once a minute. This reduces the write load on the db
        if not token.last_used or (timezone.now() - token.last_used).total_seconds() > 6:
            # If maintenance mode is enabled, assume the database is read-only, and disable updating the token's
            # last_used time upon authentication.
            if get_config().MAINTENANCE_MODE:
                logger = logging.getLogger('netbox.auth.login')
                logger.warning("Maintenance mode enabled: disabling update of token's last used timestamp")
            else:
                token.last_used = timezone.now()
                token.save()

        # Enforce the Token's expiration time, if one has been set.
        if token.is_expired:
            raise exceptions.AuthenticationFailed("Token expired")

        if not token.user.is_active:
            raise exceptions.AuthenticationFailed("User inactive")

        # When LDAP authentication is active try to load user data from LDAP directory
        if settings.REMOTE_AUTH_BACKEND == 'netbox.authentication.LDAPBackend':
            from netbox.authentication import LDAPBackend
            ldap_backend = LDAPBackend()

            # Load from LDAP if FIND_GROUP_PERMS is active
            if ldap_backend.settings.FIND_GROUP_PERMS:
                user = ldap_backend.populate_user(token.user.username)
                # If the user is found in the LDAP directory use it, if not fallback to the local user
                if user:
                    return user, token

        return token.user, token


class TokenPermissions(DjangoObjectPermissions):
    """
    Custom permissions handler which extends the built-in DjangoModelPermissions to validate a Token's write ability
    for unsafe requests (POST/PUT/PATCH/DELETE).
    """
    # Override the stock perm_map to enforce view permissions
    perms_map = {
        'GET': ['%(app_label)s.view_%(model_name)s'],
        'OPTIONS': [],
        'HEAD': ['%(app_label)s.view_%(model_name)s'],
        'POST': ['%(app_label)s.add_%(model_name)s'],
        'PUT': ['%(app_label)s.change_%(model_name)s'],
        'PATCH': ['%(app_label)s.change_%(model_name)s'],
        'DELETE': ['%(app_label)s.delete_%(model_name)s'],
    }

    def __init__(self):

        # LOGIN_REQUIRED determines whether read-only access is provided to anonymous users.
        self.authenticated_users_only = settings.LOGIN_REQUIRED

        super().__init__()

    def _verify_write_permission(self, request):

        # If token authentication is in use, verify that the token allows write operations (for unsafe methods).
        if request.method in SAFE_METHODS or request.auth.write_enabled:
            return True

    def has_permission(self, request, view):

        # Enforce Token write ability
        if isinstance(request.auth, Token) and not self._verify_write_permission(request):
            return False

        return super().has_permission(request, view)

    def has_object_permission(self, request, view, obj):

        # Enforce Token write ability
        if isinstance(request.auth, Token) and not self._verify_write_permission(request):
            return False

        return super().has_object_permission(request, view, obj)


class IsAuthenticatedOrLoginNotRequired(BasePermission):
    """
    Returns True if the user is authenticated or LOGIN_REQUIRED is False.
    """
    def has_permission(self, request, view):
        if not settings.LOGIN_REQUIRED:
            return True
        return request.user.is_authenticated
