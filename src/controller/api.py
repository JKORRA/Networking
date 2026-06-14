"""
api.py

This module contains the WSGI REST API endpoints for the Ryu controller.
It securely exposes network topology, active traffic metrics, and flow tables
to the dashboard and the digital twin instances using a token-based authentication.
"""

from ryu.app.wsgi import ControllerBase, route
from webob import Response
import json
import os

api_instance_name = 'api_app'

class NetworkAPI(ControllerBase):
    """REST API exposing SDN controller state."""
    def __init__(self, req, link, data, **config):
        super(NetworkAPI, self).__init__(req, link, data, **config)
        self.controller = data[api_instance_name]
        self.secret_token = os.environ.get('SDN_TWIN_AUTH_TOKEN', 'Bearer SDN-Twin-Secret-Token-2026')

    def _check_auth(self, req):
        """Validates the authentication token."""
        if req.headers.get('Authorization') != self.secret_token:
            return Response(status=401, body='{"error": "Unauthorized"}', content_type='application/json')
        return None
    
    @route('topology', '/api/topology', methods=['GET'])
    def get_topology(self, req, **kwargs):
        """Returns the full network topology, implementing ETag for caching."""
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        version = str(self.controller.topology['version'])
        if req.headers.get('If-None-Match') == version:
            return Response(status=304)

        body = json.dumps(self.controller.topology, separators=(',', ':'))
        resp = Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )
        resp.headers['ETag'] = version
        return resp
    
    @route('switches', '/api/switches', methods=['GET'])
    def get_switches(self, req, **kwargs):
        """Returns the list of discovered switches."""
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        body = json.dumps(self.controller.topology['switches'], separators=(',', ':'))
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )
    
    @route('links', '/api/links', methods=['GET'])
    def get_links(self, req, **kwargs):
        """Returns the list of active links."""
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        body = json.dumps(self.controller.topology['links'], separators=(',', ':'))
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )
    
    @route('hosts', '/api/hosts', methods=['GET'])
    def get_hosts(self, req, **kwargs):
        """Returns the list of discovered hosts and their MAC/IP assignments."""
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        body = json.dumps(self.controller.topology['hosts'], separators=(',', ':'))
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )

    @route('version', '/api/version', methods=['GET'])
    def get_version(self, req, **kwargs):
        """Returns the current topology version identifier."""
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        version_info = {
            'version': self.controller.topology['version']
        }
        body = json.dumps(version_info, separators=(',', ':'))
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )

    @route('traffic', '/api/traffic', methods=['GET'])
    def get_traffic(self, req, **kwargs):
        """Returns the active traffic rates (Rx/Tx Mbps) per port."""
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        body = json.dumps(self.controller.traffic, separators=(',', ':'))
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )
        
    @route('flows', '/api/flows', methods=['GET'])
    def get_flows(self, req, **kwargs):
        """Returns the IP-to-IP traffic flow matrix."""
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        body = json.dumps(self.controller.flow_matrix, separators=(',', ':'))
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )
