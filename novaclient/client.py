# Copyright 2010 Jacob Kaplan-Moss
# Copyright 2011 Piston Cloud Computing, Inc.

"""
OpenStack Client interface. Handles the REST calls and responses.
"""

import httplib2
import logging
import os
import time
import urllib
import urlparse

from novaclient import service_catalog

try:
    import json
except ImportError:
    import simplejson as json

# Python 2.5 compat fix
if not hasattr(urlparse, 'parse_qsl'):
    import cgi
    urlparse.parse_qsl = cgi.parse_qsl


from novaclient import exceptions


_logger = logging.getLogger(__name__)


class HTTPClient(httplib2.Http):

    USER_AGENT = 'python-novaclient'

    def __init__(self, user, apikey, projectid, auth_url, timeout=None):
        super(HTTPClient, self).__init__(timeout=timeout)
        self.user = user
        self.apikey = apikey
        self.projectid = projectid
        self.auth_url = auth_url
        self.version = 'v1.0'

        self.management_url = None
        self.auth_token = None

        # httplib2 overrides
        self.force_exception_to_status_code = True

    def http_log(self, args, kwargs, resp, body):
        if 'NOVACLIENT_DEBUG' in os.environ and os.environ['NOVACLIENT_DEBUG']:
            ch = logging.StreamHandler()
            _logger.setLevel(logging.DEBUG)
            _logger.addHandler(ch)
        elif not _logger.isEnabledFor(logging.DEBUG):
            return

        string_parts = ['curl -i']
        for element in args:
            if element in ('GET', 'POST'):
                string_parts.append(' -X %s' % element)
            else:
                string_parts.append(' %s' % element)

        for element in kwargs['headers']:
            header = ' -H "%s: %s"' % (element, kwargs['headers'][element])
            string_parts.append(header)

        _logger.debug("REQ: %s\n" % "".join(string_parts))
        if 'body' in kwargs:
            _logger.debug("REQ BODY: %s\n" % (kwargs['body']))
        _logger.debug("RESP:%s %s\n", resp, body)

    def request(self, *args, **kwargs):
        kwargs.setdefault('headers', {})
        kwargs['headers']['User-Agent'] = self.USER_AGENT
        if 'body' in kwargs:
            kwargs['headers']['Content-Type'] = 'application/json'
            kwargs['body'] = json.dumps(kwargs['body'])

        resp, body = super(HTTPClient, self).request(*args, **kwargs)

        self.http_log(args, kwargs, resp, body)

        if body:
            try:
                body = json.loads(body)
            except ValueError, e:
                pass
        else:
            body = None

        if resp.status in (400, 401, 403, 404, 408, 413, 500, 501):
            raise exceptions.from_response(resp, body)

        return resp, body

    def _cs_request(self, url, method, **kwargs):
        if not self.management_url:
            self.authenticate()

        # Perform the request once. If we get a 401 back then it
        # might be because the auth token expired, so try to
        # re-authenticate and try again. If it still fails, bail.
        try:
            kwargs.setdefault('headers', {})['X-Auth-Token'] = self.auth_token
            if self.projectid:
                kwargs['headers']['X-Auth-Project-Id'] = self.projectid

            resp, body = self.request(self.management_url + url, method,
                                      **kwargs)
            return resp, body
        except exceptions.Unauthorized, ex:
            try:
                self.authenticate()
                resp, body = self.request(self.management_url + url, method,
                                          **kwargs)
                return resp, body
            except exceptions.Unauthorized:
                raise ex

    def get(self, url, **kwargs):
        url = self._munge_get_url(url)
        return self._cs_request(url, 'GET', **kwargs)

    def post(self, url, **kwargs):
        return self._cs_request(url, 'POST', **kwargs)

    def put(self, url, **kwargs):
        return self._cs_request(url, 'PUT', **kwargs)

    def delete(self, url, **kwargs):
        return self._cs_request(url, 'DELETE', **kwargs)

    def authenticate(self):
        scheme, netloc, path, query, frag = urlparse.urlsplit(
                                                    self.auth_url)
        path_parts = path.split('/')
        for part in path_parts:
            if len(part) > 0 and part[0] == 'v':
                self.version = part
                break

        auth_url = self.auth_url
        if self.version == "v2.0":  # FIXME(chris): This should be better.
            while auth_url:
                auth_url = self._v2_auth(auth_url)
        else:
            try:
                while auth_url:
                    auth_url = self._v1_auth(auth_url)
            # In some configurations nova makes redirection to
            # v2.0 keystone endpoint. Also, new location does not contain
            # real endpoint, only hostname and port.
            except exceptions.AuthorizationFailure:
                if auth_url.find('v2.0') < 0:
                    auth_url = urlparse.urljoin(auth_url, 'v2.0/')
                self._v2_auth(auth_url)

    def _v1_auth(self, url):
        headers = {'X-Auth-User': self.user,
                   'X-Auth-Key': self.apikey}
        if self.projectid:
            headers['X-Auth-Project-Id'] = self.projectid

        resp, body = self.request(url, 'GET', headers=headers)
        if resp.status in (200, 204):  # in some cases we get No Content
            try:
                self.management_url = resp['x-server-management-url']
                self.auth_token = resp['x-auth-token']
                self.auth_url = url
            except KeyError:
                raise exceptions.AuthorizationFailure()
        elif resp.status == 305:
            return resp['location']
        else:
            raise exceptions.from_response(resp, body)

    def _v2_auth(self, url):
        body = {"passwordCredentials": {"username": self.user,
                                        "password": self.apikey}}

        if self.projectid:
            body['passwordCredentials']['tenantId'] = self.projectid

        token_url = urlparse.urljoin(url, "tokens")
        resp, body = self.request(token_url, "POST", body=body)

        if resp.status == 200:  # content must always present
            try:
                self.auth_url = url
                self.service_catalog = \
                    service_catalog.ServiceCatalog(body)
                self.auth_token = self.service_catalog.token.id
                self.management_url = self.service_catalog.url_for('nova',
                                                                   'public')
            except KeyError:
                raise exceptions.AuthorizationFailure()
        elif resp.status == 305:
            return resp['location']
        else:
            raise exceptions.from_response(resp, body)

    def _munge_get_url(self, url):
        """
        Munge GET URLs to always return uncached content.

        The OpenStack Compute API caches data *very* agressively and doesn't
        respect cache headers. To avoid stale data, then, we append a little
        bit of nonsense onto GET parameters; this appears to force the data not
        to be cached.
        """
        scheme, netloc, path, query, frag = urlparse.urlsplit(url)
        query = urlparse.parse_qsl(query)
        query.append(('fresh', str(time.time())))
        query = urllib.urlencode(query)
        return urlparse.urlunsplit((scheme, netloc, path, query, frag))
