import eventlet
from eventlet.event import Event
import json
import logging
from nameko.extensions import SharedExtension
import odoorpc
import time
import uuid
from urllib.error import URLError

logger = logging.getLogger(__name__)


class OdooConnection(SharedExtension):
    odoo = None
    connected = Event()
    # Init token
    token = old_token = None
    token_update_time = time.time()

    def setup(self):
        # Must be defined settings
        self.host = self.container.config['ODOO_HOST']
        self.port = int(self.container.config['ODOO_PORT'])
        self.user = str(self.container.config['ODOO_USER'])
        self.password = str(self.container.config['ODOO_PASS'])
        self.db = str(self.container.config['ODOO_DB'])
        self.protocol = (
            'jsonrpc+ssl' if self.container.config.get(
                'ODOO_USE_SSL') else 'jsonrpc')
        self.verify_certificate = self.container.config.get(
            'ODOO_VERIFY_CERTIFICATE', False)
        while not self.setup_rpc_session():
            eventlet.sleep(1)

    def setup_rpc_session(self):
        try:
            logger.info('Connecting to Odoo at %s://%s:%s',
                        self.protocol, self.host, self.port)
            odoo = odoorpc.ODOO(self.host, port=self.port,
                                protocol=self.protocol)
            odoo.login(self.db, self.user, self.password)
            logger.info('Connected to Odoo as %s', self.user)
            self.odoo = odoo
            self.container.spawn_managed_thread(self.update_token)
            self.container.spawn_managed_thread(self.ping_on_start)
            if not self.connected.ready():
                self.connected.send()
            return True
        except odoorpc.error.RPCError as e:
            if 'res.users()' in str(e):
                logger.error('Odoo login %s not found or bad password %s.',
                             self.user, self.password)
            elif 'FATAL' in repr(e):
                logger.error('Odoo fatal error: %s', e)
            else:
                logger.exception('RPC error:')
        except URLError as e:
            logger.error(e)
        except Exception as e:
            if 'Connection refused' in repr(e):
                logger.error('Odoo refusing connection.')
            else:
                logger.exception(e)

    def ping_on_start(self):
        try:
            # Ping the bus to initialize message counter is bus is enabled.
            if self.container.config.get('ODOO_BUS_ENABLED'):
                self.odoo.env['bus.bus'].sendone('remote_agent/{}'.format(
                    self.container.config['SYSTEM_NAME']),
                    json.dumps({'command': 'ping'}))
        except Exception:
            logger.exception('Ping on start error:')

    def check_security_token(self, token):
        if self.token == token:
            return True
        elif self.token != token:
            # Check for race condition when token has been just updated
            if self.old_token == token:
                if abs(time.time() - self.token_update_time) > 3:
                    logger.error('Outdated token, ignoring message: %s', token)
                    return False
                else:
                    logger.debug('Accepting old token message: %s', token)
                    return True
            else:
                logger.error('Bad message token: %s', token)
                return False

    def update_token(self):
        self.connected.wait()
        while True:
            try:
                new_token = uuid.uuid4().hex
                self.odoo.env['remote_agent.agent'].update_token(new_token)
                # Keep previous token
                self.old_token = self.token
                # Generate new token
                self.token = new_token
                # Keep time of token update
                self.token_update_time = time.time()
                logger.debug('Agent token updated.')
            except Exception:
                logger.exception('Update token error:')
            finally:
                eventlet.sleep(float(self.container.config.get(
                    'ODOO_REFRESH_TOKEN_SECONDS', 60)))
