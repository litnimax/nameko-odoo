from functools import partial
import json
import logging
from nameko.web.handlers import HttpRequestHandler
from nameko.web.server import WebServer
from werkzeug.wrappers import Response
from .command import Command
from .connection import OdooConnection

logger = logging.getLogger(__name__)


# Override to allow disabling HTTP server from config file.
class AgentWebServer(WebServer):

    def start(self):
        if self.container.config.get('WEB_SERVER_ENABLED'):
            logger.info('Starting HTTP server at %s.',
                        self.container.config.get('WEB_SERVER_ADDRESS'))
            super(AgentWebServer, self).start()


class OdooRequestHandler(HttpRequestHandler):
    server = AgentWebServer()
    connection = OdooConnection()

    def handle_request(self, request):
        request.shallow = False
        # Check for security token
        token = request.headers.get('X-Token')
        if not self.connection.check_security_token(token):
            return Response(
                json.dumps({'error': 'Bad token'}), status=400)
        try:
            message = json.loads(request.get_data(as_text=True))
            if message.get('command') in ['nameko_rpc', 'ping', 'restart']:
                command = Command(self)
                method = getattr(command, message['command'])
                self.container.spawn_managed_thread(
                    partial(method, 'http', message))
                return Response(json.dumps({'status': 'ok'}), status=200)
            else:
                return super(OdooRequestHandler, self).handle_request(request)
        except ValueError:
            return Response(
                json.dumps({'error': 'Not a JSON data'}), status=400)


http = OdooRequestHandler.decorator
