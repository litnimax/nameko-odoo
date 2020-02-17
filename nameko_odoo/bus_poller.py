from functools import partial
import eventlet
import json
import logging
import requests
import traceback
from nameko.extensions import SharedExtension, ProviderCollector
from nameko.extensions import Entrypoint
from .command import Command
from .connection import OdooConnection

logger = logging.getLogger(__name__)


class BusPoller(SharedExtension, ProviderCollector):
    db_selected = False
    http_session = None  # Requests session for bus polling
    channels = []  # Channels to poll bus on.
    connection = OdooConnection()

    def add_channel(self, channel):
        self.channels.append(channel)

    def setup(self):
        self.channels.append('remote_agent/{}'.format(
            self.container.config['SYSTEM_NAME']))
        self.host = self.container.config['ODOO_HOST']
        self.port = self.container.config.get('ODOO_BUS_PORT', 8072)
        self.user = self.container.config['ODOO_USER']
        self.password = self.container.config['ODOO_PASS']
        self.db = self.container.config['ODOO_DB']
        self.use_ssl = self.container.config.get('ODOO_USE_SSL')
        self.verify_certificate = self.container.config.get(
            'ODOO_VERIFY_CERTIFICATE')
        self.bus_enabled = self.container.config.get('ODOO_BUS_ENABLED')
        self.bus_polling_port = self.container.config.get(
            'ODOO_BUS_POLLING_PORT', 8072)
        self.bus_timeout = self.container.config.get(
            'ODOO_BUS_TIMEOUT', 55)
        self.bus_trace = self.container.config.get(
            'ODOO_BUS_TRACE', False)
        self.single_db = self.container.config.get('ODOO_SINGLE_DB')
        self.http_session = requests.Session()
        logger.debug('Odoo connection setup done.')

    def start(self):
        self.container.spawn_managed_thread(self.poll_bus,
                                            identifier='odoo_bus_poller')
        logger.debug('Odoo connection has been started.')

    def select_db(self):
        """
        For multi database Odoo setup it is required to first select a database
        to work with. But if you have single db setup or use db_filters
        so that always one db is selected set ODOO_SINGLE_DB=yes.
        """
        if self.db_selected:
            return
        logger.debug('Selecting Odoo database (session refresh)')
        scheme = 'https' if self.use_ssl else 'http'
        auth_url = '{}://{}:{}/web/session/authenticate'.format(
            scheme, self.host, self.bus_polling_port)
        data = {
            'jsonrpc': '2.0',
            'params': {
                'context': {},
                'db': self.db,
                'login': self.user,
                'password': self.password,
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
        scheme = 'https' if self.use_ssl else 'http'
        while True:
            try:
                bus_url = '{}://{}:{}/longpolling/poll'.format(
                    scheme, self.host, self.bus_polling_port)
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

    def handle_bus_message(self, channel, raw_message):
        # Check message type
        try:
            message = json.loads(raw_message)
        except TypeError:
            logger.error('Cannot load json from message: %s', raw_message)
            return
        # Check for security token
        if not self.connection.check_security_token(message.get('token')):
            return
        # Check if we need to confirm receive.
        if message.get('reply_channel'):
            self.connection.odoo.env['remote_agent.agent'].bus_sendone(
                message.get('reply_channel'), {'status': 'ok'})
        # Check if this is internal commands
        if message.get('command') in ['nameko_rpc', 'ping', 'restart']:
            command = Command(self)
            method = getattr(command, message['command'])
            self.container.spawn_managed_thread(
                partial(method, channel, message))
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


class BusEventHandler(Entrypoint):
    poller = BusPoller()
    check_entrypoint_channels = True

    def __init__(self, channels=[]):
        self.channels = list(channels)
        super(BusEventHandler, self).__init__()

    def add_channel(self, channel):
        if channel not in self.channels:
            logger.debug('Adding %s to polling channels.', channel)
            self.channels.append(channel)
            self.poller.channels.append(channel)

    def setup(self):
        # Check for additional channels supplied in configuration file.
        config_channels = self.container.config.get('ODOO_BUS_POLL_CHANNELS')
        if config_channels:
            logger.info('Adding poll channels from config file.')
            self.channels.extend(config_channels)
        # Check for additional channels from decorator.
        for channel in self.channels:
            self.poller.channels.append(channel)
        self.poller.register_provider(self)

    def stop(self):
        self.poller.unregister_provider(self)

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
