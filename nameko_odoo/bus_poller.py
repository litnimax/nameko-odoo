from functools import partial
import eventlet
import json
import http.client
import logging
from nameko.extensions import Entrypoint, DependencyProvider
from nameko.extensions import SharedExtension, ProviderCollector
from nameko.standalone.rpc import ClusterRpcProxy
from nameko.exceptions import RpcTimeout
import odoorpc
from functools import partial
import requests
import time
import traceback
import uuid
from urllib.error import URLError

logger = logging.getLogger(__name__)


class OdooConnection(SharedExtension, ProviderCollector):
    odoo = None
    odoo_connected = eventlet.Event()
    db_selected = False
    http_session = None  # Requests session for bus polling
    channels = []  # Channels to poll bus on.

    def add_channel(self, channel):
        self.channels.append(channel)

    def setup(self):
        # Must be defined settings
        self.odoo_host = self.container.config['ODOO_HOST']
        self.odoo_port = self.container.config['ODOO_PORT']
        self.odoo_user = self.container.config['ODOO_USER']
        self.odoo_pass = self.container.config['ODOO_PASS']
        self.odoo_db = self.container.config['ODOO_DB']
        # Optional settings, use defaults.
        self.odoo_scheme = self.container.config.get(
            'ODOO_SCHEME', 'http')
        self.odoo_protocol = ('jsonrpc' if self.odoo_scheme == 'http' else
                              'jsonrpc+ssl')
        self.bus_enabled = self.container.config.get('ODOO_BUS_ENABLED', True)
        self.bus_polling_port = self.container.config.get(
            'ODOO_BUS_POLLING_PORT', 8072)
        self.bus_timeout = self.container.config.get(
            'ODOO_BUS_TIMEOUT', 55)
        self.bus_trace = self.container.config.get(
            'ODOO_BUS_TRACE', False)
        self.single_db = self.container.config.get('ODOO_SINGLE_DB', False)
        self.verify_certificate = self.container.config.get(
            'ODOO_VERIFY_CERTIFICATE', False)
        logger.debug('Odoo connection setup done.')

    def update_token(self):
        # Generate and send a secure token used to communicate with Agent
        agent_model = self.container.config.get('ODOO_AGENT_MODEL')
        if not agent_model:
            logger.info('Agent model not set, disabling token update routine.')
            return
        while True:
            try:
                # Keep previous token
                if hasattr(self, 'token'):
                    self.token_old = self.token
                else:
                    self.token_old = None
                # Generate new token
                self.token = uuid.uuid4().hex
                self.odoo.env[agent_model].update_token(
                    self.container.config['ODOO_AGENT_UID'], self.token)
                # Keep time of token update
                self.token_update_time = time.time()
                logger.debug('Agent token updated.')
            except Exception:
                logger.exception('Update token error:')
            finally:
                eventlet.sleep(
                    float(self.container.config['ODOO_REFRESH_TOKEN_SECONDS']))

    def start(self):
        effort = 11
        while True:
            self.setup_rpc_session()
            if self.odoo:
                break
            logger.info('Odoo RPC not setup, waiting...')
            eventlet.sleep(effort % 10)
            effort += 1
        self.http_session = requests.Session()
        self.container.spawn_managed_thread(self.update_token)
        self.container.spawn_managed_thread(self.poll_bus,
                                            identifier='odoo_bus_poller')
        logger.debug('Odoo connection has been started.')

    def setup_rpc_session(self):
        try:
            logger.info(
                'Connecting to Odoo at %s://%s:%s',
                self.odoo_protocol, self.odoo_host, self.odoo_port)
            odoo = odoorpc.ODOO(self.odoo_host, port=self.odoo_port,
                                protocol=self.odoo_protocol)
            odoo.login(self.odoo_db, self.odoo_user, self.odoo_pass)
            logger.info('Connected to Odoo as %s', self.odoo_user)
            self.odoo = odoo
            if not self.odoo_connected.ready():
                self.odoo_connected.send(odoo)
            # Ping the bus to initialize message counter
            self.bus_sendone(self.channels[0], {'command': 'bus-ping'})

        except odoorpc.error.RPCError as e:
            if 'res.users()' in str(e):
                logger.error('Odoo login %s not found or bad password %s, '
                             'check in Odoo!', self.odoo_user, self.odoo_pass)
            elif 'FATAL' in repr(e):
                logger.error('Odoo fatal error: %s', e)
            else:
                logger.exception('RPC error:')
        except URLError as e:
            logger.error(e)
        except http.client.RemoteDisconnected:
            logger.error('Odoo remote disconnection.')
        except Exception as e:
            if 'Connection refused' in repr(e):
                logger.error('Odoo refusing connection.')
            else:
                logger.exception(e)

    def select_db(self):
        """
        For multi database Odoo setup it is required to first select a database
        to work with.
        But if you have single db setup or use db_filters so that always one db
        is selected set ODOO_SINGLE_DB=yes.
        """
        if self.db_selected:
            return
        logger.debug('Selecting Odoo database (session refresh)')
        auth_url = '{}://{}:{}/web/session/authenticate'.format(
            self.odoo_scheme, self.odoo_host, self.bus_polling_port)
        data = {
            'jsonrpc': '2.0',
            'params': {
                'context': {},
                'db': self.odoo_db,
                'login': self.odoo_user,
                'password': self.odoo_pass,
            },
        }
        headers = {
            'Content-type': 'application/json'
        }
        rep = self.http_session.post(
            auth_url,
            verify=self.verify_certificate,
            data=json.dumps(data),
            headers=headers)
        result = rep.json()
        if rep.status_code != 200 or result.get('error'):
            logger.error(u'Odoo authenticate error {}: {}'.format(
                rep.status_code,
                json.dumps(result['error'], indent=2)))
        else:
            logger.info('Odoo authenticated for long polling')
        self.db_selected = True

    def poll_bus(self):
        """
        Odoo bus poller to get massages from Odoo
        and route the to corresponding services.
        """
        if not self.bus_enabled:
            logger.info(
                'Odoo bus poll is not enabled, not using /longpolling/poll.')
            return
        last = 0
        while True:
            try:
                bus_url = '{}://{}:{}/longpolling/poll'.format(
                    self.odoo_scheme, self.odoo_host, self.bus_polling_port)
                # Select DB first
                if not self.single_db:
                    self.select_db()
                # Now let try to poll
                logger.debug('Polling %s at %s', self.channels, bus_url)
                r = self.http_session.post(
                    bus_url,
                    timeout=self.bus_timeout,
                    verify=self.verify_certificate,
                    headers={'Content-Type': 'application/json'},
                    json={
                        'params': {
                            'last': last,
                            'channels': self.channels}})
                if self.bus_trace:
                    logger.debug('Bus trace: %s', r.text)
                try:
                    r.json()
                except ValueError:
                    logger.error('JSON parse bus reply error: %s', r.text)
                result = r.json().get('result')
                if not result:
                    error = r.json().get('error')
                    if error:
                        logger.error(json.dumps(error, indent=2))
                        eventlet.sleep(1)
                        continue
                if last == 0:
                    # Ommit queued data
                    for msg in result:
                        logger.debug('Ommit bus message %s', msg)
                        last = msg['id']
                    continue
                # TODO: Check that tis is really
                # my channel as Odoo can send a match
                for msg in result:
                    last = msg['id']
                    logger.debug('Received bus message %s', msg)
                    try:
                        self.handle_bus_message(msg['channel'], msg['message'])
                    except Exception:
                        logger.exception('Handle bus message error:')

            except Exception as e:
                no_wait = False
                if isinstance(e, requests.exceptions.ConnectionError):
                    if 'Connection aborted' in str(e):
                        logger.warning('Odoo Connection aborted')
                    elif 'Failed to establish' in str(e):
                        logger.warning('Odoo Connection refused')
                    else:
                        logger.warning(e.strerror)
                elif isinstance(e, requests.exceptions.HTTPError):
                    logger.warning(r.reason)
                elif isinstance(e, requests.exceptions.ReadTimeout):
                    no_wait = True
                    logger.warning('Bus poll timeout, re-polling')
                else:
                    logger.exception('Bus error:')
                if not no_wait:
                    eventlet.sleep(1)

    def check_security_token(self, message):
        # If no agent model is given then check is disabled.
        if not self.container.config.get('ODOO_AGENT_MODEL'):
            return
        if message['token'] != self.odoo.token:
            # Check for race condition when token has been just updated
            if self.odoo.token_old == message['token']:
                if abs(time.time() - self.odoo.token_update_time) > 3:
                    logger.error(
                        'Outdated token, ignoring message: %s', message)
                    return
                else:
                    logger.debug('Accepting old token message: %s', message)
            else:
                logger.error('Bad message token: %s', message)
                return

    def handle_bus_message(self, channel, raw_message):
        # Check message type
        try:
            message = json.loads(raw_message)
        except TypeError:
            logger.error('Cannot load json from message: %s', raw_message)
            return
        # Check if this is nameko RPC message
        if message.get('command') == 'nameko-rpc':
            handle_nameko_rpc = partial(
                self.handle_nameko_rpc, channel, message)
            self.container.spawn_managed_thread(handle_nameko_rpc)
            return
        elif message.get('command') == 'bus-ping':
            logger.debug('Bus ping received.')
            return
        # Get proviers and pass the message
        for provider in self._providers:
                provider.handle_message(channel, message)
        if not self._providers:
            logger.warning('Ignoring message on channel %s, no providers',
                           channel)

    def handle_nameko_rpc(self, channel, message):
        logger.debug('Channel %s nameko RPC message: %s', channel, message)
        # Handle Nameko RPC request sent from Odoo and return the result.
        result = {'error': {}}
        # Return back data sent by caller.
        if message.get('pass_back'):
            result['pass_back'] = message['pass_back']
        try:
            service_name = message['service']
            method = message['method']
            args = message.get('args', ())
            kwargs = message.get('kwargs', {})
            callback_model = message.get('callback_model')
            callback_method = message.get('callback_method')
            timeout = float(message.get('timeout', '3'))
            with ClusterRpcProxy(
                    self.container.config, timeout=timeout) as cluster_rpc:
                service = getattr(cluster_rpc, service_name)
                ret = getattr(service, method)(*args, **kwargs)
                logger.debug('Nameko RPC result: %s', ret)
                result['result'] = ret
        except RpcTimeout:
            logger.warning('[NAMEKO_RPC_TIMEOUT] %s.%s timeout: %s',
                           service_name, method, timeout)
            result['error']['message'] = 'Service {} {} timeout.'.format(
                service_name, method)
        except Exception as e:
            logger.exception('[NAMEKO_RPC_ERROR]')
            result['error']['message'] = str(e)
        finally:
            if callback_model and callback_method:
                try:
                    logger.debug('Execute %s.%s.',
                                 callback_model, callback_method)
                    self.odoo.execute(callback_model, callback_method, result)
                except Exception:
                    logger.exception('[ODOO_RPC_ERROR]')
            else:
                logger.debug('No callback model / method specified.')

    def bus_sendone(self, channel, message):
        self.odoo.env['bus.bus'].sendone(channel, message)


class OdooClient(DependencyProvider):
    connection = OdooConnection()

    def get_dependency(self, worker_ctx):
        effort = 10
        while True:
            if not self.connection.odoo:
                if effort % 10 == 0:
                    # Log first and every 10-n effort
                    logger.debug('Odoo not ready, waiting...')
                effort += 1
                eventlet.sleep(0.1)
            else:
                break
        return self.connection.odoo

    def worker_setup(self, worker_ctx):
        worker_ctx.service.odoo_connected = self.connection.odoo_connected


class BusEventHandler(Entrypoint):
    connection = OdooConnection()
    # Sometimes you inherit OdooConnection and put custom channels there.
    # So in this case Entrypoint class does not have those channels.
    check_entrypoint_channels = True

    def __init__(self, channels=[]):
        self.channels = list(channels)
        super(BusEventHandler, self).__init__()

    def add_channel(self, channel):
        if channel not in self.channels:
            logger.debug('Adding %s to polling channels.', channel)
            self.channels.append(channel)
            self.connection.channels.append(channel)

    def setup(self):
        # Check for channels supplied in configuration file.
        config_channels = self.container.config.get('ODOO_BUS_POLL_CHANNELS')
        if config_channels:
            logger.info('Adding poll channels from config file.')
            self.channels.extend(config_channels)
        for channel in self.channels:
            self.connection.channels.append(channel)
        self.connection.register_provider(self)

    def stop(self):
        self.connection.unregister_provider(self)

    def handle_message(self, channel, message):
        handle_result = partial(self.handle_result, message)
        if self.check_entrypoint_channels and channel not in self.channels:
            logger.debug('Ignoring message on uknown channel %s', channel)
        else:
            self.container.spawn_worker(
                self, (channel, message), {}, handle_result=handle_result)

    def handle_result(self, message, worker_ctx, result, exc_info):
        if exc_info:
            tb = ''.join(traceback.format_tb(exc_info[2]))
            logger.error('Handle message error: %s\n%s', repr(exc_info[1]), tb)
        # TODO: reply channel
        return result, exc_info


bus = BusEventHandler.decorator
