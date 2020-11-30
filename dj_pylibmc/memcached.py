"""
Memcached cache backend for Django using pylibmc.

If you want to use the binary protocol, specify `'BINARY': True` in your CACHES
settings.  The default is `False`, using the text protocol.

pylibmc behaviors can be declared as a dict in `CACHES` backend `OPTIONS`
setting.

Unlike the default Django caching backends, this backend lets you pass 0 as a
timeout, which translates to an infinite timeout in memcached.
"""
import logging
import warnings
from threading import local

from django.conf import settings
from django.core.cache.backends.base import InvalidCacheBackendError
from django.core.cache.backends.memcached import BaseMemcachedCache, DEFAULT_TIMEOUT

try:
    import pylibmc
    from pylibmc import Error as MemcachedError
except ImportError:
    raise InvalidCacheBackendError('Could not import pylibmc.')


log = logging.getLogger('django.pylibmc')


MIN_COMPRESS_LEN = getattr(settings, 'PYLIBMC_MIN_COMPRESS_LEN', 0)  # Disabled
if MIN_COMPRESS_LEN > 0 and not pylibmc.support_compression:
    MIN_COMPRESS_LEN = 0
    warnings.warn('A minimum compression length was provided but pylibmc was '
                  'not compiled with support for it.')


COMPRESS_LEVEL = getattr(settings, 'PYLIBMC_COMPRESS_LEVEL', -1)  # zlib.Z_DEFAULT_COMPRESSION
if not COMPRESS_LEVEL == -1:
    if not pylibmc.support_compression:
        warnings.warn('A compression level was provided but pylibmc was '
                      'not compiled with support for it.')

# Keyword arguments to configure compression options
COMPRESS_KWARGS = {
    'min_compress_len': MIN_COMPRESS_LEN,
    'compress_level': COMPRESS_LEVEL,
}


RETRY_ON_BROKEN_CONNECTION = getattr(settings, 'DJPYLIBMC_RETRY_ON_BROKEN_CONNECTION', True)  # disabled


def retry_on_broken_connection(func):
    """
    The only goal of this decorator is to add retry functionality to pylibmc.Client's methods

    It's necessary because sometimes long-living connection can be broken
    which leads to a `miss`, though memcache server can be available again at the moment.
    """
    def wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
        except MemcachedError as e:
            # libmemcached error code is stored in retcode attribute
            # 3 - ConnectionError
            if getattr(e, 'retcode', None) == 3:
                result = func(*args, **kwargs)
            else:
                raise
        return result
    return wrapper


class PyLibMCCache(BaseMemcachedCache):

    def __init__(self, server, params, username=None, password=None):
        import os
        self._local = local()
        self.binary = int(params.get('BINARY', False))
        self._username = os.environ.get('MEMCACHE_USERNAME', username or params.get('USERNAME'))
        self._password = os.environ.get('MEMCACHE_PASSWORD', password or params.get('PASSWORD'))
        self._server = os.environ.get('MEMCACHE_SERVERS', server)
        super(PyLibMCCache, self).__init__(self._server, params, library=pylibmc,
                                           value_not_found_exception=pylibmc.NotFound)

    @property
    def _cache(self):
        # PylibMC uses cache options as the 'behaviors' attribute.
        # It also needs to use threadlocals, because some versions of
        # PylibMC don't play well with the GIL.
        client = getattr(self._local, 'client', None)
        if client:
            return client

        client_kwargs = {'binary': self.binary}
        if self._username is not None and self._password is not None:
            client_kwargs.update({
                'username': self._username,
                'password': self._password
            })
        client = self._lib.Client(self._servers, **client_kwargs)
        if self._options:
            client.behaviors = self._options

        self._local.client = client

        if RETRY_ON_BROKEN_CONNECTION:
            methods_with_retry = [
                'add',
                'add_multi',
                'get',
                'get_multi',
                'set',
                'set_multi',
                'delete',
                'delete_multi',
                'incr',
                'incr_multi',
                'decr',
            ]
            for method_name in methods_with_retry:
                setattr(client, method_name, retry_on_broken_connection(getattr(client, method_name)))

        return client

    def get_backend_timeout(self, timeout=DEFAULT_TIMEOUT):
        """
        Special case timeout=0 to allow for infinite timeouts.
        """
        if timeout == 0:
            return timeout

        return super(PyLibMCCache, self).get_backend_timeout(timeout)

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        key = self.make_key(key, version=version)
        try:
            return self._cache.add(key, value,
                                   self.get_backend_timeout(timeout),
                                   **COMPRESS_KWARGS)
        except pylibmc.ServerError:
            log.error('ServerError saving %s (%d bytes)', key, len(str(value)),
                      exc_info=True)
            return False
        except MemcachedError as e:
            log.error('MemcachedError: %s', e, exc_info=True)
            return False

    def get(self, key, default=None, version=None):
        try:
            return super(PyLibMCCache, self).get(key, default, version)
        except MemcachedError as e:
            log.error('MemcachedError: %s', e, exc_info=True)
            return default

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        key = self.make_key(key, version=version)
        try:
            return self._cache.set(key, value,
                                   self.get_backend_timeout(timeout),
                                   **COMPRESS_KWARGS)
        except pylibmc.ServerError:
            log.error('ServerError saving %s (%d bytes)', key, len(str(value)),
                      exc_info=True)
            return False
        except MemcachedError as e:
            log.error('MemcachedError: %s', e, exc_info=True)
            return False

    def delete(self, *args, **kwargs):
        try:
            return super(PyLibMCCache, self).delete(*args, **kwargs)
        except MemcachedError as e:
            log.error('MemcachedError: %s', e, exc_info=True)
            return False

    def get_many(self, *args, **kwargs):
        try:
            return super(PyLibMCCache, self).get_many(*args, **kwargs)
        except MemcachedError as e:
            log.error('MemcachedError: %s', e, exc_info=True)
            return {}

    def set_many(self, *args, **kwargs):
        try:
            return super(PyLibMCCache, self).set_many(*args, **kwargs)
        except MemcachedError as e:
            log.error('MemcachedError: %s', e, exc_info=True)
            return False

    def delete_many(self, *args, **kwargs):
        try:
            return super(PyLibMCCache, self).delete_many(*args, **kwargs)
        except MemcachedError as e:
            log.error('MemcachedError: %s', e, exc_info=True)
            return False

    def close(self, **kwargs):
        # Override BaseMemcachedCache since libmemcached manages its own connections,
        # and calling disconnect_all() resets the failover state and causes unnecessary
        # reconnects. Copied from Django's PyLibMCCache backend:
        # https://github.com/django/django/blob/1.11.9/django/core/cache/backends/memcached.py#L207-L210
        pass
