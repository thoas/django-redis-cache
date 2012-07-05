from collections import defaultdict
from django.core.cache.backends.base import BaseCache, InvalidCacheBackendError
from django.core.exceptions import ImproperlyConfigured
from django.utils import importlib
from django.utils.datastructures import SortedDict
from .compat import smart_text, smart_bytes, bytes_type, python_2_unicode_compatible

try:
    import cPickle as pickle
except ImportError:
    import pickle

try:
    import redis
except ImportError:
    raise InvalidCacheBackendError(
        "Redis cache backend requires the 'redis-py' library")
from redis.connection import UnixDomainSocketConnection, Connection
from redis.connection import DefaultParser
from redis_cache.sharder import CacheSharder


@python_2_unicode_compatible
class CacheKey(object):
    """
    A stub string class that we can use to check if a key was created already.
    """
    def __init__(self, key):
        self._key = key

    def __eq__(self, other):
        return self._key == other

    def __str__(self):
        return smart_text(self._key)

    def __repr__(self):
        return repr(self._key)

    def __hash__(self):
        return hash(self._key)

    __repr__ = __str__ = __unicode__


class CacheConnectionPool(object):

    def __init__(self):
        self._connection_pools = {}

    def get_connection_pool(self, host='127.0.0.1', port=6379, db=1,
                            password=None, parser_class=None,
                            unix_socket_path=None):
        connection_identifier = (host, port, db, parser_class, unix_socket_path)
        if not self._connection_pools.get(connection_identifier):
            connection_class = (
                unix_socket_path and UnixDomainSocketConnection or Connection
            )
            kwargs = {
                'db': db,
                'password': password,
                'connection_class': connection_class,
                'parser_class': parser_class,
            }
            if unix_socket_path is None:
                kwargs.update({
                    'host': host,
                    'port': port,
                })
            else:
                kwargs['path'] = unix_socket_path
            self._connection_pools[connection_identifier] = redis.ConnectionPool(**kwargs)
        return self._connection_pools[connection_identifier]
pool = CacheConnectionPool()


class RedisCache(BaseCache):
    def __init__(self, server, params):
        """
        Connect to Redis, and set up cache backend.
        """
        self._init(server, params)

    def _init(self, server, params):
        super(RedisCache, self).__init__(params)
        self._params = params
        self._server = server
        self.clients = []
        self.sharder = CacheSharder()

        if not isinstance(server, (list, tuple)):
            servers = [server]
        else:
            servers = server

        for server in servers:
            unix_socket_path = None
            if ':' in server:
                host, port = server.rsplit(':', 1)
                try:
                    port = int(port)
                except (ValueError, TypeError):
                    raise ImproperlyConfigured("port value must be an integer")
            else:
                host, port = None, None
                unix_socket_path = server

            kwargs = {
                'db': self.db,
                'password': self.password,
                'host': host,
                'port': port,
                'unix_socket_path': unix_socket_path,
            }
            connection_pool = pool.get_connection_pool(
                parser_class=self.parser_class,
                **kwargs
            )
            client = redis.Redis(
                connection_pool=connection_pool,
                **kwargs
            )
            self.clients.append(client)
            self.sharder.add(client, "%s:%s" % (host, port))

    @property
    def params(self):
        return self._params or {}

    @property
    def options(self):
        return self.params.get('OPTIONS', {})

    @property
    def db(self):
        _db = self.params.get('db', self.options.get('DB', 1))
        try:
            _db = int(_db)
        except (ValueError, TypeError):
            raise ImproperlyConfigured("db value must be an integer")
        return _db

    @property
    def password(self):
        return self.params.get('password', self.options.get('PASSWORD', None))

    @property
    def parser_class(self):
        cls = self.options.get('PARSER_CLASS', None)
        if cls is None:
            return DefaultParser
        mod_path, cls_name = cls.rsplit('.', 1)
        try:
            mod = importlib.import_module(mod_path)
            parser_class = getattr(mod, cls_name)
        except (AttributeError, ImportError):
            raise ImproperlyConfigured("Could not find parser class '%s'" % parser_class)
        return parser_class

    def __getstate__(self):
        return {'params': self._params, 'server': self._server}

    def __setstate__(self, state):
        self._init(**state)

    def get_value(self, original):
        try:
            value = int(original)
        except (ValueError, TypeError):
            value = self.deserialize(original)
        return value

    def get_client(self, key):
        return self.sharder.get_client(key)

    def shard(self, keys, version=None):
        """
        Returns a dict of keys that belong to a cache's keyspace.
        """
        clients = defaultdict(list)
        for key in keys:
            clients[self.get_client(key)].append(key)
        return clients

    def make_key(self, key, version=None):
        if not isinstance(key, CacheKey):
            key = super(RedisCache, self).make_key(key, version)
            key = CacheKey(key)
        return key

    def add(self, key, value, timeout=None, version=None):
        """
        Add a value to the cache, failing if the key already exists.

        Returns ``True`` if the object was added, ``False`` if not.
        """
        client = self.get_client(key)
        if client.exists(self.make_key(key, version=version)):
            return False
        return self.set(key, value, timeout, _add_only=True)

    def get(self, key, default=None, version=None):
        """
        Retrieve a value from the cache.

        Returns unpickled value if key is found, the default if not.
        """
        client = self.get_client(key)
        key = self.make_key(key, version=version)
        value = client.get(key)
        if value is None:
            return default
        value = self.get_value(value)
        return value

    def _set(self, key, value, timeout, client, _add_only=False):
        if timeout == 0:
            if _add_only:
                return client.setnx(key, value)
            return client.set(key, value)
        elif timeout > 0:
            if _add_only:
                added = client.setnx(key, value)
                if added:
                    client.expire(key, timeout)
                return added
            return client.setex(key, value, timeout)
        else:
            return False

    def set(self, key, value, timeout=None, version=None, client=None, _add_only=False):
        """
        Persist a value to the cache, and set an optional expiration time.
        """
        if client is None:
            client = self.get_client(key)
        key = self.make_key(key, version=version)
        if timeout is None:
            timeout = self.default_timeout
        # If ``value`` is not an int, then pickle it
        if not isinstance(value, int) or isinstance(value, bool):
            result = self._set(key, self.serialize(value), int(timeout), client, _add_only)
        else:
            result = self._set(key, value, int(timeout), client, _add_only)
        # result is a boolean
        return result

    def delete(self, key, version=None):
        """
        Remove a key from the cache.
        """
        client = self.get_client(key)
        key = self.make_key(key, version=version)
        client.delete(key)

    def delete_many(self, keys, version=None):
        """
        Remove multiple keys at once.
        """
        clients = self.shard(keys)
        for client, keys in clients.items():
            keys = [self.make_key(key, version=version) for key in keys]
            client.delete(*keys)

    def delete_pattern(self, pattern, version=None):
        pattern = self.make_key(pattern, version=version)
        for client in self.clients:
            keys = client.keys(pattern)
            if len(keys):
                client.delete(*keys)

    def clear(self):
        """
        Flush all cache keys.
        """
        # TODO : potential data loss here, should we only delete keys based on the correct version ?
        for client in self.clients:
            client.flushdb()

    def serialize(self, value):
        return pickle.dumps(value)

    def deserialize(self, value):
        """
        Unpickles the given value.
        """
        value = smart_bytes(value)
        return pickle.loads(value)

    def _get_many(self, client, keys, version=None):
        """
        Retrieve many keys.
        """
        if not keys:
            return {}
        recovered_data = SortedDict()
        new_keys = list(map(lambda key: self.make_key(key, version=version), keys))
        map_keys = dict(zip(new_keys, keys))
        results = client.mget(new_keys)
        for key, value in zip(new_keys, results):
            if value is None:
                continue
            value = self.get_value(value)
            recovered_data[map_keys[key]] = value
        return recovered_data

    def get_many(self, keys, version=None):
        data = {}
        clients = self.shard(keys)
        for client, keys in clients.items():
            data.update(self._get_many(client, keys))
        return data

    def set_many(self, data, timeout=None, version=None):
        """
        Set a bunch of values in the cache at once from a dict of key/value
        pairs. This is much more efficient than calling set() multiple times.

        If timeout is given, that timeout will be used for the key; otherwise
        the default cache timeout will be used.
        """
        clients = self.shard(data.keys())
        for client, keys in clients.iteritems():
            pipeline = client.pipeline()
            for key in keys:
                self.set(key, data[key], timeout, version=version, client=pipeline)
            pipeline.execute()

    def incr(self, key, delta=1, version=None):
        """
        Add delta to value in the cache. If the key does not exist, raise a
        ValueError exception.
        """
        client = self.get_client(key)
        key = self.make_key(key, version=version)
        exists = client.exists(key)
        if not exists:
            raise ValueError("Key '%s' not found" % key)
        try:
            value = client.incr(key, delta)
        except redis.ResponseError:
            value = self.get(key) + 1
            self.set(key, value)
        return value

    def incr_version(self, key, delta=1, version=None):
        """
        Adds delta to the cache version for the supplied key. Returns the
        new version.

        """
        if version is None:
            version = self.version
        client = self.get_client(key)
        old = self.make_key(key, version)
        new = self.make_key(key, version=version + delta)
        try:
            client.rename(old, new)
        except redis.ResponseError:
            raise ValueError("Key '%s' not found" % key)

        return version + delta
