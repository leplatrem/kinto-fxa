import logging

import requests
from fxa.oauth import Client as OAuthClient
from fxa import errors as fxa_errors
from pyramid import authentication as base_auth
from pyramid import httpexceptions
from pyramid.interfaces import IAuthenticationPolicy
from pyramid.settings import aslist
from six.moves.urllib.parse import urljoin
from zope.interface import implementer

from kinto_fxa.utils import fxa_conf

logger = logging.getLogger(__name__)


class TokenVerificationCache(object):
    """Verification cache class as expected by PyFxa library.

    This basically wraps the cache backend instance to specify a constant ttl.
    """
    def __init__(self, cache, ttl):
        self.cache = cache
        self.ttl = ttl

    def get(self, key):
        return self.cache.get(key)

    def set(self, key, value):
        self.cache.set(key, value, self.ttl)

    def delete(self, key):
        self.cache.delete(key)


@implementer(IAuthenticationPolicy)
class FxAOAuthAuthenticationPolicy(base_auth.CallbackAuthenticationPolicy):
    def __init__(self, realm='Realm'):
        self.realm = realm
        self._cache = None

    def unauthenticated_userid(self, request):
        """Return the FxA userid or ``None`` if token could not be verified.
        """
        authorization = request.headers.get('Authorization', '')
        try:
            authmeth, token = authorization.split(' ', 1)
        except ValueError:
            return None
        if authmeth.lower() != 'bearer':
            return None
        return self._verify_token(token, request)

    def forget(self, request):
        """A no-op. Credentials are sent on every request.
        Return WWW-Authenticate Realm header for Bearer token.
        """
        return [('WWW-Authenticate', 'Bearer realm="%s"' % self.realm)]

    def _verify_token(self, token, request):
        """Verify the token extracted from the Authorization header.

        This method stores the result in two locations to avoid hitting the
        auth remote server as much as possible:

        - on the request object, in case the Pyramid authentication methods
          like `effective_principals()` or `authenticated_userid()` are called
          several times during the request cycle;

        - in the cache backend, to reuse validated token from one request to
          another (during ``cache_ttl_seconds`` seconds.)
        """
        # First check if this request was already verified.
        # `request.bound_data` is an attribute provided by Kinto to store
        # some data that is shared among sub-requests (e.g. default bucket
        # or batch requests)
        key = 'fxa_verified_token'
        if key in request.bound_data:
            return request.bound_data[key]

        # Use PyFxa defaults if not specified
        server_url = fxa_conf(request, 'oauth_uri')
        scope = aslist(fxa_conf(request, 'required_scope'))
        auth_cache = self._get_cache(request)
        auth_client = OAuthClient(server_url=server_url, cache=auth_cache)
        try:
            profile = auth_client.verify_token(token=token, scope=scope)
            user_id = profile['user']
        except fxa_errors.OutOfProtocolError as e:
            logger.exception("Protocol error")
            raise httpexceptions.HTTPServiceUnavailable()
        except (fxa_errors.InProtocolError, fxa_errors.TrustError) as e:
            logger.debug("Invalid FxA token: %s" % e)
            user_id = None

        # Save for next call.
        request.bound_data[key] = user_id

        return user_id

    def _get_cache(self, request):
        """Instantiate cache when first request comes in.
        This way, the policy instantiation is decoupled from registry object.
        """
        if self._cache is None:
            if hasattr(request.registry, 'cache'):
                cache_ttl = float(fxa_conf(request, 'cache_ttl_seconds'))
                oauth_cache = TokenVerificationCache(request.registry.cache,
                                                     ttl=cache_ttl)
                self._cache = oauth_cache

        return self._cache


def fxa_ping(request):
    """Verify if the OAuth server is ready."""
    server_url = fxa_conf(request, 'oauth_uri')

    oauth = None
    if server_url is not None:
        auth_client = OAuthClient(server_url=server_url)
        server_url = auth_client.server_url
        oauth = False

        try:
            heartbeat_url = urljoin(server_url, '/__heartbeat__')
            timeout = float(fxa_conf(request, 'heartbeat_timeout_seconds'))
            r = requests.get(heartbeat_url, timeout=timeout)
            r.raise_for_status()
            oauth = True
        except requests.exceptions.HTTPError:
            pass

    return oauth
